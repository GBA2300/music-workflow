# -*- coding: utf-8 -*-
"""番茄音乐「热门歌曲」榜单采集（网页版首页，**只读**）

为什么需要它
────────────
SKILL.md 纪律 0 要求：代创作前**必须**先研究当期热歌榜。
但第五～第十轮用的是**第三方榜**（搜狐全网榜 / 酷狗内地榜 / 歌曲吧短视频榜 / eemp3…），
**锚没打在番茄自己身上** —— 而我们的听众就在番茄。
本脚本把锚拉回来：直接采番茄音乐网页版首页的「热门歌曲」+ 真实「在听人数」。

⚠️ 重要事实（2026-09-21 实测确认，别再重复试了）
──────────────────────────────────────────────
**番茄音乐网页版没有「热歌榜」页面。**
- 探测了 18 个候选路径（`/rank` `/chart` `/hot` `/toplist` `/music/rank`
  `/billboard` `/ranking` …）→ **全部回落首页**（HTTP 200，内容仍是首页那 199 条），
  说明是 SPA 的 catch-all 路由，**并不存在真正的榜单页**。
- 扫首页所有含「榜 / 排行 / 热歌 / 热门」的可点元素 → **0 个**（没有任何榜单导航入口）。
- **也没有榜单 JSON 接口**：首页歌曲是**服务端直出（SSR）到 HTML** 的；
  网络请求里只有字节的埋点 SDK（`mon.zijieapi.com` / `mcs.zijieapi.com` / `security.zijieapi.com`），
  没有任何歌曲列表接口可供翻页/切榜。

**所以本脚本采的是：首页「热门歌曲」列表 + 每首的「在听人数」，按在听人数降序自排 TOP N。**
优点：数据是番茄用户**真实在听量**，比第三方榜更贴我们的听众。
局限：它没有平台官方的「第几名」编号，排名由我们自己按在听人数排。

> APP 端真正的「音乐榜 / 热歌榜」需要手机抓包（Appium / mitmproxy）或
> 模拟器 + 代理，本脚本**不覆盖**，别指望在这里改改就能拿到。

解析依据（DOM 契约，2026-09-21 实测）
────────────────────────────────────
    a.pc-music-card-grid-item[href="/music/<作品ID>"]
      div.pc-music-card-grid-listeners
        span.pc-music-card-grid-listeners-num     ← 「10.4万」
      div.pc-music-card-grid-info
        div.pc-music-card-grid-name               ← 歌名
        div.pc-music-card-grid-subtitle
          span.pc-music-card-grid-tag             ← 「原唱」（可选）
          span.pc-music-card-grid-subtitle-text   ← 「歌手 · 专辑」

用法
────
    python fanqie_chart.py                    # 采 TOP 10 → 屏幕 + 写 <workdir>/charts/
    python fanqie_chart.py --top 30
    python fanqie_chart.py --no-save          # 只在屏幕上打，不写文件
    python fanqie_chart.py --workdir "D:\\music-workflow"

产物
────
- `<workdir>/charts/番茄热门歌曲-YYYY-MM-DD.md`  —— 人读的榜单 + 结构观察
- `<workdir>/charts/番茄热门歌曲-YYYY-MM-DD.json` —— 机读的全量原始数据（供下轮对比涨跌）

**只读**：只打开首页、只读 DOM，不点播放、不登录、不写任何平台侧数据。
登录态用 `profile_fanqie`（若已登录就直接用；没登录也能看首页，因为首页游客可见）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright  # noqa: E402
from browser_utils import clear_profile_locks, window_args  # noqa: E402

try:
    from paths import default_workdir, user_profile
except Exception:                                     # pragma: no cover
    import os

    def user_profile(name):
        return Path(os.environ.get("LOCALAPPDATA", "")) / "music-workflow" / "profiles" / name

    def default_workdir():
        return None

SCRIPT_DIR = Path(__file__).resolve().parent
HOME_URL = "https://www.novelfm.com/"
PROFILE = "profile_fanqie"

CARDS_JS = """
() => Array.from(document.querySelectorAll('a.pc-music-card-grid-item')).map(a => {
  const q = s => a.querySelector(s);
  const num  = q('.pc-music-card-grid-listeners-num');
  const name = q('.pc-music-card-grid-name');
  const sub  = q('.pc-music-card-grid-subtitle-text');
  const tag  = q('.pc-music-card-grid-tag');
  return {
    href: a.getAttribute('href') || '',
    num:  num  ? num.innerText.trim()  : '',
    name: name ? name.innerText.trim() : '',
    sub:  sub  ? sub.innerText.trim()  : '',
    tag:  tag  ? tag.innerText.trim()  : '',
  };
})
"""

# 「10.4万」→ 104000；「1.2亿」→ 120000000；「9800」→ 9800；解析不了返回 0
_UNIT = {"万": 10_000, "亿": 100_000_000, "w": 10_000, "W": 10_000}


def parse_count(s: str) -> int:
    m = re.match(r"^\s*([\d.]+)\s*([万亿wW]?)", s or "")
    if not m:
        return 0
    try:
        v = float(m.group(1))
    except ValueError:
        return 0
    return int(v * _UNIT.get(m.group(2), 1))


def log(m):
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


# ─────────────────────── 题材关键词粗筛（客观计数，不做主观判断） ───────────────────────
# 说明：只是「歌名里出现过这些字」的计数，用来快速看出当期题材分布；
# 同一首歌可能落进多个桶（有意如此，因为「泪」和「爱」常常同现）。
THEME_HINTS = {
    "苦情/遗憾/放手": ["泪", "疼", "伤", "遗憾", "错", "忘", "放下", "负", "恨", "痛", "欠", "后悔", "两清"],
    "思念/等待": ["想", "念", "等", "望", "盼", "思念", "想你"],
    "亲情/故乡/归途": ["妈", "娘", "爸", "故乡", "家", "归", "回来", "港湾", "老"],
    "国风/古风": ["江湖", "红尘", "月", "花", "剑", "仙", "佛", "人间", "相思", "情长",
                "琵琶", "西厢", "钗", "千年", "神", "宫"],
    "人生/释怀": ["岁月", "余生", "顺其自然", "自己", "活", "命", "慢慢", "路", "这辈子", "一生"],
    "爱情/甜": ["爱", "恋", "心动", "喜欢你", "缘"],
    "夜晚/孤单/酒": ["夜", "孤单", "孤独", "醉", "酒", "烟", "杯", "风"],
    "经典老歌迹象": ["原唱", "合辑", "精选", "典藏", "经典"],
}


def theme_stats(rows: list[dict]) -> list[tuple[str, int, list[str]]]:
    out = []
    for theme, kws in THEME_HINTS.items():
        hits = []
        for r in rows:
            if any(k in r["name"] for k in kws):
                hits.append(r["name"])
        out.append((theme, len(hits), hits[:5]))
    return sorted(out, key=lambda x: -x[1])


def resolve_workdir(arg) -> Path:
    """工作目录：--workdir > MW_WORKDIR > settings.json > 当前目录(有 tasks.csv) > 脚本目录。"""
    import os
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("MW_WORKDIR")
    if env:
        return Path(env).expanduser().resolve()
    d = default_workdir()
    if d:
        return Path(d).expanduser().resolve()
    cwd = Path.cwd()
    if (cwd / "tasks.csv").exists() or (cwd / "library").is_dir():
        return cwd
    return SCRIPT_DIR.parent


async def collect(scroll_rounds: int = 10) -> list[dict]:
    """打开番茄音乐首页，滚动加载全部卡片，返回解析后的列表。"""
    pd = user_profile(PROFILE)
    clear_profile_locks(pd)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            pd, headless=False, args=window_args(), viewport=None)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(30000)

        log(f"打开 {HOME_URL}")
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(7000)

        # 滚动到底逼出懒加载，直到卡片数不再增长
        prev = 0
        for i in range(scroll_rounds):
            n = await page.evaluate(
                "() => document.querySelectorAll('a.pc-music-card-grid-item').length")
            if n and n == prev:
                break
            prev = n
            await page.mouse.wheel(0, 6000)
            await page.wait_for_timeout(1000)

        raw = await page.evaluate(CARDS_JS)
        title = await page.title()
        await ctx.close()

    rows = []
    for r in raw:
        cnt = parse_count(r["num"])
        if not r["name"] or cnt <= 0:
            continue
        sub = r.get("sub") or ""
        artist, _, album = sub.partition(" · ")
        rows.append({
            "name": r["name"],
            "artist": artist.strip(),
            "album": album.strip(),
            "listeners": cnt,
            "listeners_text": r["num"],
            "tag": r.get("tag") or "",
            "id": (r["href"] or "").rstrip("/").rsplit("/", 1)[-1],
            "href": r["href"],
        })

    # 同名不同版本（如同一首歌的多个版本）保留，但按 (歌名, 歌手, 专辑) 去重
    seen, uniq = set(), []
    for r in rows:
        k = (r["name"], r["artist"], r["album"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    uniq.sort(key=lambda x: -x["listeners"])
    log(f"页面标题：{title}")
    log(f"解析到 {len(uniq)} 首（原始卡片 {len(raw)} 张）")
    return uniq


def write_report(rows: list[dict], top: int, workdir: Path) -> tuple[Path, Path]:
    charts = workdir / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    md_p = charts / f"番茄热门歌曲-{stamp}.md"
    js_p = charts / f"番茄热门歌曲-{stamp}.json"

    t = rows[:top]
    L = []
    L.append(f"# 番茄音乐·热门歌曲 TOP{top}（网页版首页实时）")
    L.append("")
    L.append(f"- 采集时间：{datetime.now():%Y-%m-%d %H:%M}")
    L.append(f"- 数据源：<{HOME_URL}> 首页「热门歌曲」，带**真实在听人数**")
    L.append(f"- 采集样本：{len(rows)} 首（滚动加载后全量，按在听人数降序）")
    L.append("- ⚠️ 番茄音乐**网页版没有「热歌榜」页面**（18 个候选路径全回落首页、"
             "无榜单导航入口、无列表接口）；此处排名是按「在听人数」自排。"
             "真正的 APP 榜单需手机抓包，本工具不覆盖。")
    L.append("")
    L.append(f"## TOP{top}")
    L.append("")
    L.append("| # | 歌名 | 歌手 | 在听人数 | 标签 |")
    L.append("|---|---|---|---|---|")
    for i, r in enumerate(t, 1):
        L.append(f"| {i} | {r['name']} | {r['artist']} | {r['listeners_text']}({r['listeners']:,}) | {r['tag']} |")
    L.append("")
    L.append("## 题材分布（歌名关键词粗筛，一首可落多桶）")
    L.append("")
    L.append("> ⚠️ 这只是「歌名里出现过这些字」的计数，用来快速看题材分布，**不能代替读歌词**。")
    L.append("> 只看 TOP10 样本太小（常常一片 0），所以同时给全样本和 TOP30 两个口径。")
    L.append("")
    L.append(f"### 全样本（{len(rows)} 首）")
    L.append("")
    L.append("| 题材 | 命中数 | 例 |")
    L.append("|---|---|---|")
    for theme, n, eg in theme_stats(rows):
        L.append(f"| {theme} | {n}/{len(rows)} | {'、'.join(eg) if eg else '—'} |")
    L.append("")
    L.append("### TOP30（当期最热的那批）")
    L.append("")
    L.append("| 题材 | 命中数 | 例 |")
    L.append("|---|---|---|")
    t30 = rows[:30]
    for theme, n, eg in theme_stats(t30):
        L.append(f"| {theme} | {n}/{len(t30)} | {'、'.join(eg) if eg else '—'} |")
    L.append("")
    L.append("## 歌名形态观察")
    L.append("")
    lens = [len(r["name"]) for r in t]
    L.append(f"- 字数：最短 {min(lens)} / 最长 {max(lens)} / 平均 {sum(lens)/len(lens):.1f}")
    paren = [r["name"] for r in t if "（" in r["name"] or "(" in r["name"]]
    L.append(f"- 带括号副题的：{len(paren)}/{len(t)}"
             + (f"（{'、'.join(paren[:4])}…）" if paren else ""))
    L.append(f"- 带「原唱」标签的：{sum(1 for r in t if r['tag'])}/{len(t)}")
    L.append("")
    L.append("## 全量（按在听人数）")
    L.append("")
    L.append("| # | 歌名 | 歌手 | 在听人数 |")
    L.append("|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        L.append(f"| {i} | {r['name']} | {r['artist']} | {r['listeners_text']} |")
    L.append("")

    md_p.write_text("\n".join(L), encoding="utf-8")
    js_p.write_text(json.dumps(
        {"collected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
         "source": HOME_URL, "total": len(rows), "rows": rows},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return md_p, js_p


async def main():
    ap = argparse.ArgumentParser(description="番茄音乐热门歌曲榜单采集（只读）")
    ap.add_argument("--top", type=int, default=10, help="报前几名（默认 10）")
    ap.add_argument("--workdir", default=None, help="工作目录（决定 charts/ 落哪儿）")
    ap.add_argument("--no-save", action="store_true", help="只在屏幕打印，不写文件")
    ap.add_argument("--scroll-rounds", type=int, default=10, help="最多滚几轮加载")
    args = ap.parse_args()

    rows = await collect(args.scroll_rounds)
    if not rows:
        log("⚠️ 一首都没解析到 —— 页面结构可能改版了，"
            "请用 probe 方式重新确认 a.pc-music-card-grid-item 这套 class。")
        return 2

    t = rows[:args.top]
    print()
    print("=" * 78)
    print(f"番茄音乐·热门歌曲 TOP{args.top}（按在听人数）")
    print("=" * 78)
    for i, r in enumerate(t, 1):
        tag = f" [{r['tag']}]" if r["tag"] else ""
        print(f"  {i:>2}. {r['name']}")
        print(f"      {r['artist']}  ·  {r['listeners_text']} 人在听{tag}  ·  /music/{r['id']}")
    print("=" * 78)

    if not args.no_save:
        wd = resolve_workdir(args.workdir)
        md_p, js_p = write_report(rows, args.top, wd)
        log(f"报告已写入：{md_p}")
        log(f"原始数据：{js_p}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
