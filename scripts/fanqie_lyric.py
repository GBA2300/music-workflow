# -*- coding: utf-8 -*-
"""番茄音乐**歌词拆解**工具 —— 研究爆款的「创作思维模式」与「创作风格」（只读）

它解决什么问题
──────────────
「研究热歌」不是研究数字（在听人数只是用来**挑研究哪几首**），
真正要研究的是**这些歌怎么写出来的**：结构怎么排、副歌怎么起、
记忆点靠什么撑、韵脚怎么走、意象怎么选、句子多长。

所以本工具干两件事：
1. **采歌词原文**：番茄音乐歌曲详情页 `https://www.novelfm.com/music/<作品ID>`
   正文里就含**完整歌词**（2026-09-21 实测确认），连段落重复都能看出来。
2. **自动拆解**：把歌词拆成结构/记忆点/句式/韵脚/意象五层，输出可复用的规律。

> 为什么不去第三方歌词站抄：那些站点的版本常与平台在播版本不一致
> （多段/少段/改词），**研究样本必须与平台实际在听的一致**。
> 另外本工具只用平台自己的页面，不引入额外数据源，口径统一。

⚠️ 已知边界（2026-09-21 实测）
──────────────────────────────
- 详情页**不含作词/作曲/编曲署名**（搜「作词/作曲/编曲/制作人」全 0 命中），
  所以**无法研究「谁写的」**，只能研究「怎么写」。
- 详情页**不含曲风标签**。曲风需另判：可优先用 `fanqie_rank.py` 榜单自带的
  官方曲风标签（`tags` 字段，最准），否则用「专辑名 + 时长 + 歌名形态」推断。
  **本工具不猜曲风**，只输出可验证的文本结构。

样本从哪来（重要）
──────────────────
样本 ID 从 `charts/` 里取，优先级：
1. **`番茄榜单-热歌榜-*.json`（首选）** —— `fanqie_rank.py` 采的**番茄音乐 APP 官方热歌榜**
   （名次与数据都是平台自己的，与用户在 APP 里看到的**一致**）。用 `--rank` 可换成别的榜。
2. `番茄榜单-<其它榜>-*.json` —— 若没有热歌榜，才用别的 APP 官方榜。
3. `番茄热门歌曲-*.json`（兜底）—— `fanqie_chart.py` 采的**网页版**热门位。
   网页版与 APP 榜单**差距很大**（网页版没有榜单页），仅在拿不到 APP 榜时才用。

> ⚠️ 同一天 `charts/` 里会有**多份** `番茄榜单-*.json`（热歌/飙升/80后…）。
> 早期版本用 `sorted(...)[-1]` 取「最后一份」，那是**按文件名排序的巧合**
> —— 实测静默拿到了「飙升榜」而不是热歌榜。**现在按榜名显式挑，默认热歌榜。**

用法
────
    # 采热歌榜 TOP10 的歌词并拆解（默认按榜名挑热歌榜）
    python fanqie_lyric.py --top 10

    # 换别的官方榜当样本源
    python fanqie_lyric.py --top 10 --rank 飙升榜
    python fanqie_lyric.py --top 10 --rank 80后热歌

    # 指定作品 ID（逗号分隔）
    python fanqie_lyric.py --ids 7646108934238899225,7621396821633403929

    # 直接指定榜单 json
    python fanqie_lyric.py --chart-json "D:\\music-workflow\\charts\\番茄榜单-热歌榜-2026-09-21.json" --top 5

产物
────
- `<workdir>/study/番茄歌词拆解-YYYY-MM-DD.md`  —— 人读：每首全文 + 拆解 + 跨曲规律
- `<workdir>/study/番茄歌词拆解-YYYY-MM-DD.json` —— 机读：每首的歌词与拆解指标

**只读**：只打开详情页读 DOM，不播放、不登录（首页与详情页游客可见）、不写平台数据。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright  # noqa: E402
from browser_utils import clear_profile_locks, window_args  # noqa: E402
from popup_guard import a_guard_context, a_goto_with_guard  # noqa: E402

try:
    from paths import default_workdir, user_profile
except Exception:                                     # pragma: no cover
    import os

    def user_profile(name):
        return Path(os.environ.get("LOCALAPPDATA", "")) / "music-workflow" / "profiles" / name

    def default_workdir():
        return None

SCRIPT_DIR = Path(__file__).resolve().parent
SONG_URL = "https://www.novelfm.com/music/{sid}"
PROFILE = "profile_fanqie"
# 少于这个行数就不算有效样本：1~2 行通常是「新歌还没上歌词」或页面结构不同，
# 收进来会把结构统计带偏（实测踩到《等不到的老伴》只取到 1 行）。
MIN_LYRIC_LINES = 8

# 详情页正文里，歌词区的上界是播放器时间「00:00」下一行的时长；下界是这两句之一
LYRIC_STOP = ("打开番茄音乐", "猜你喜欢", "扫码下载")
# 明显不是歌词的行（播放器/UI 残留）
NOISE = re.compile(r"^\d+$|^\d{1,2}:\d{2}$|^[\d.]+万?$")

# 意象词表：只统计「出现在歌里的实体/意象」，用来对比不同歌的取材偏好
IMAGE_WORDS = ["月", "风", "雨", "雪", "花", "夜", "梦", "酒", "烟", "灯", "船", "桥",
               "山", "海", "江", "河", "云", "星", "窗", "门", "路", "车", "票", "信",
               "琴", "琵", "笛", "剑", "茶", "船", "码头", "站台", "城", "巷", "院",
               "衣", "伞", "钟", "泪", "眼", "手", "心", "头", "发", "老屋", "灶"]
# 情绪/关系词
FEEL_WORDS = ["想", "念", "等", "忘", "恨", "疼", "痛", "遗憾", "后悔", "放下",
              "原谅", "牵挂", "孤独", "沉默", "错过", "回不", "再也", "如初", "从前",
              "以后", "一生", "余生", "岁月", "时间", "那年", "当初", "如今", "最后"]


def log(m):
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def extract_lyrics(body: str) -> tuple[list[str], str]:
    """从详情页正文里切出歌词；返回 (歌词行, 时长)。"""
    lines = [l.strip() for l in (body or "").splitlines() if l.strip()]
    # 时长：紧跟在 '00:00' 之后的那一行
    dur = ""
    start = 0
    for i, l in enumerate(lines):
        if l == "00:00":
            if i + 1 < len(lines) and re.fullmatch(r"\d{1,2}:\d{2}", lines[i + 1]):
                dur = lines[i + 1]
                start = i + 2
            else:
                start = i + 1
            break
    end = len(lines)
    for j in range(start, len(lines)):
        if any(k in lines[j] for k in LYRIC_STOP):
            end = j
            break
    out = [l for l in lines[start:end] if not NOISE.match(l)]
    return out, dur


def analyze(lyrics: list[str]) -> dict:
    """把一首歌拆成 结构 / 记忆点 / 句式 / 韵脚 / 意象 五层。"""
    if not lyrics:
        return {}
    # 1) 记忆点：重复次数 ≥2 的句子
    cnt = Counter(lyrics)
    repeated = [(t, n) for t, n in cnt.most_common() if n >= 2]
    hook = repeated[0][0] if repeated else lyrics[0]

    # 2) 副歌块还原：找「连续若干行整体重复出现」的最长块
    #    ⚠️ 2026-09-21 修：原来 best_n 存的是**块的行数**，却被当成「重复次数」去打印，
    #       于是报告里出现「整体重复 10 次，10 行」这种自相矛盾的话。
    #       现在 size 和重复次数分开存。
    best_block, best_size, best_rep = [], 0, 0
    n = len(lyrics)
    for size in range(2, 13):
        seen = {}
        for i in range(0, n - size + 1):
            chunk = tuple(lyrics[i:i + size])
            seen.setdefault(chunk, []).append(i)
        for chunk, pos in seen.items():
            if len(pos) >= 2 and size > best_size:
                best_block, best_size, best_rep = list(chunk), size, len(pos)

    # 3) 句长
    lens = [len(l) for l in lyrics]

    # 3b) ★ 2026-09-21 新增：句长**按主歌/副歌分层**统计。
    #     ⚠️ 为什么必须分层：原来只有一个全文 `avg_line_len`，配方卡把它当成
    #        「单行字数上限」发给落笔环节，结果**副歌永远超标** —— 因为全文均值
    #        混合了主歌（短句）与副歌（长句），拿混合值去卡副歌等于要求副歌跟主歌一样短。
    #        实测 342 行：全文均值 9.44 / P75 11 / P90 13 / 最大 16；
    #        14~16 字仅占 7.0%，而且**全是副歌**，形态是「两个分句 + 空格」。
    #        → 主歌线 ≈ ≤11 字；副歌线 ≈ 13~16 字（双分句）。两把尺子必须分开给。
    CJK_V = re.compile(r"[\u4e00-\u9fff]")
    chorus_line_len = [len(l) for l in best_block] if best_block else []
    # 主歌 = 全文里不属于副歌块的汉字行（用块内容做集合剔除）
    block_set = set(best_block)
    verse_line_len = [len(l) for l in lyrics if l not in block_set]
    # 副歌块里「双分句」占比：行内有空格且空格两侧各有汉字
    two_clause = sum(1 for l in best_block
                     if " " in l.strip() and all(CJK_V.search(p) for p in l.strip().split(" ") if p))

    # 4) 韵脚：行尾字频次
    #    ⚠️ 只统计**汉字**行尾 —— 榜单里混有梵语/韩语歌（如《祈神怜》），
    #       不过滤的话 `요(12)`、`व(7)` 会挤进「高频行尾字」，把中文韵脚规律冲淡。
    CJK = re.compile(r"[\u4e00-\u9fff]")
    tails = Counter(l[-1] for l in lyrics if l and CJK.match(l[-1]))
    tail_groups = Counter({t: c for t, c in tails.items() if c >= 2})

    # 5) 意象/情绪词
    joined = "".join(lyrics)
    imgs = {w: joined.count(w) for w in IMAGE_WORDS if joined.count(w)}
    feels = {w: joined.count(w) for w in FEEL_WORDS if joined.count(w)}

    return {
        "lines": len(lyrics),
        "chars": sum(lens),
        "avg_line_len": round(sum(lens) / max(1, len(lens)), 1),
        # ★ 2026-09-21：分层句长（主歌 vs 副歌），落笔时**分开**用两把尺子
        "verse_avg_len": round(sum(verse_line_len) / max(1, len(verse_line_len)), 1),
        "verse_max_len": max(verse_line_len) if verse_line_len else 0,
        "chorus_avg_len": round(sum(chorus_line_len) / max(1, len(chorus_line_len)), 1),
        "chorus_max_len": max(chorus_line_len) if chorus_line_len else 0,
        "chorus_two_clause_ratio": round(two_clause / max(1, len(chorus_line_len)), 2),
        "min_line_len": min(lens), "max_line_len": max(lens),
        "hook": hook,
        "hook_repeat": cnt.get(hook, 1),
        "repeated_lines": repeated[:8],
        "chorus_block": best_block,
        "chorus_size": best_size,
        "chorus_repeat": best_rep,
        "tail_top": tail_groups.most_common(8),
        "images": dict(sorted(imgs.items(), key=lambda x: -x[1])[:12]),
        "feelings": dict(sorted(feels.items(), key=lambda x: -x[1])[:12]),
    }


async def fetch_one(page, sid: str, title_hint: str = "") -> dict:
    url = SONG_URL.format(sid=sid)
    await a_goto_with_guard(page, url, log=log)
    await page.wait_for_timeout(5500)
    body = await page.evaluate("document.body.innerText") or ""
    lyrics, dur = extract_lyrics(body)
    page_title = (await page.title() or "").strip()
    return {
        "id": sid, "url": url,
        "title": page_title or title_hint,
        "duration": dur,
        "lyrics": lyrics,
        "analysis": analyze(lyrics),
    }


def pick_chart_file(charts: Path, want_rank: str) -> Path | None:
    """在 charts/ 里挑一份榜单 json。

    ⚠️ 2026-09-21 修的真 bug：同一天会有**多份** `番茄榜单-*.json`（热歌/飙升/80后…），
    原来直接取 `sorted(...)[-1]` —— 那是**按文件名排序的最后一份**（实测拿到「飙升榜」），
    纪律 0 明明要求热歌榜。**默认必须明确挑热歌榜**，挑不到才退而求其次。

    优先级：`番茄榜单-热歌榜-最新日期` > `番茄榜单-<want_rank>-最新日期`
          > 任意 `番茄榜单-*`（最新日期）> `番茄热门歌曲-*`（网页版兜底）
    """
    def newest(paths: list[Path]) -> Path | None:
        return sorted(paths)[-1] if paths else None

    app = sorted(charts.glob("番茄榜单-*.json"))

    def by_rank(name: str) -> list[Path]:
        return [p for p in app if p.name.startswith(f"番茄榜单-{name}-")]

    # 1) 用户点名的那份（默认热歌榜）
    if want_rank:
        hit = newest(by_rank(want_rank))
        if hit:
            return hit
    # 2) 兜底：明确再试一次热歌榜（want_rank 传了别的榜时也保证不会误取）
    if want_rank != "热歌榜":
        hit = newest(by_rank("热歌榜"))
        if hit:
            return hit
    # 3) 任意 APP 榜
    if app:
        return newest(app)
    # 4) 网页版（已降级为兜底）
    return newest(sorted(charts.glob("番茄热门歌曲-*.json")))


def pick_ids_from_chart(workdir: Path, top: int, explicit: str = "",
                        want_rank: str = "热歌榜") -> list[dict]:
    """从 charts/ 取样本 ID。

    优先级：APP 官方榜单（`番茄榜单-<want_rank>-*.json`，默认热歌榜）
          > 网页版热门（`番茄热门歌曲-*.json`，已降级为兜底）。
    两者字段里都有 `id`（作品 ID）与 `name`，可直接复用。
    """
    charts = workdir / "charts"
    if explicit:
        cand = Path(explicit)
    elif charts.is_dir():
        cand = pick_chart_file(charts, want_rank)
    else:
        return []
    if not cand or not cand.exists():
        return []
    data = json.loads(cand.read_text(encoding="utf-8"))
    src = cand.name
    kind = "番茄音乐 APP 官方榜" if src.startswith("番茄榜单-") else "网页版热门（非榜单）"
    log(f"榜单来源：{src}（{kind}，采集于 {data.get('collected_at')}）")
    if not src.startswith("番茄榜单-"):
        log("  ⚠️ 当前用的是网页版热门位，与 APP 音乐榜差距大；"
            "建议先跑 `python fanqie_rank.py --rank 热歌榜` 拿 APP 官方榜。")
    elif want_rank and not src.startswith(f"番茄榜单-{want_rank}-"):
        log(f"  ℹ️ 没找到「{want_rank}」，改用这份榜。")
    return (data.get("rows") or [])[:top]


def write_report(songs: list[dict], workdir: Path) -> tuple[Path, Path]:
    study = workdir / "study"
    study.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    md_p = study / f"番茄歌词拆解-{stamp}.md"
    js_p = study / f"番茄歌词拆解-{stamp}.json"

    L = []
    L.append(f"# 番茄音乐爆款歌词拆解（{stamp}）")
    L.append("")
    L.append(f"- 采集时间：{datetime.now():%Y-%m-%d %H:%M}")
    L.append("- 歌词来源：番茄音乐歌曲详情页 `novelfm.com/music/<作品ID>`（**平台在播版本**，非第三方歌词站）")
    L.append(f"- 样本：{len(songs)} 首（取自 `charts/` 榜单的 TOP{len(songs)}，APP 官方榜优先）")
    L.append("- 用途：研究**创作思维模式与创作风格**（结构 / 记忆点 / 句式 / 韵脚 / 意象），"
             "不研究播放量")
    L.append("")
    L.append("> ⚠️ 详情页**不含**作词作曲署名与曲风标签，所以本报告只覆盖「怎么写」，"
             "不覆盖「谁写的」与「用什么曲风」——曲风另判。")
    L.append("")

    # ── 跨曲规律（先给结论）──
    L.append("## 一、跨曲规律（这份样本共同点）")
    L.append("")
    A = [s["analysis"] for s in songs if s.get("analysis")]
    if A:
        avg_lines = round(sum(a["lines"] for a in A) / len(A), 1)
        avg_len = round(sum(a["avg_line_len"] for a in A) / len(A), 1)
        with_chorus = sum(1 for a in A if a["chorus_repeat"] >= 2)
        avg_rep = round(sum(a["hook_repeat"] for a in A) / len(A), 1)
        avg_block = round(sum(a["chorus_size"] for a in A) / len(A), 1)
        avg_block_rep = round(sum(a["chorus_repeat"] for a in A) / len(A), 1)
        L.append(f"- **篇幅**：平均 {avg_lines} 行 / 平均单行 {avg_len} 字")
        L.append(f"- **有「整块重复副歌」的**：{with_chorus}/{len(A)} 首"
                 f"（副歌块平均 {avg_block} 行，整块平均重复 {avg_block_rep} 次）")
        L.append(f"- **记忆点（hook）平均重复 {avg_rep} 次**")
        # 共性 hook 句式
        hooks = [a["hook"] for a in A]
        L.append("- **各首 hook 原句**：")
        for h in hooks:
            L.append(f"    - {h}")
        # 高频韵脚
        all_tails = Counter()
        for a in A:
            for t, c in a["tail_top"]:
                all_tails[t] += c
        L.append(f"- **高频行尾字（跨曲）**：{'、'.join(f'{t}({c})' for t, c in all_tails.most_common(10))}")
        # 高频意象
        all_img = Counter()
        for a in A:
            for w, c in a["images"].items():
                all_img[w] += c
        L.append(f"- **高频意象（跨曲）**：{'、'.join(f'{w}({c})' for w, c in all_img.most_common(12))}")
        all_feel = Counter()
        for a in A:
            for w, c in a["feelings"].items():
                all_feel[w] += c
        L.append(f"- **高频情绪词（跨曲）**：{'、'.join(f'{w}({c})' for w, c in all_feel.most_common(12))}")
    L.append("")

    # ── 逐首 ──
    L.append("## 二、逐首全文 + 拆解")
    L.append("")
    for i, s in enumerate(songs, 1):
        a = s.get("analysis") or {}
        L.append(f"### {i}. 《{s['title']}》　时长 {s.get('duration') or '?'}　`/music/{s['id']}`")
        L.append("")
        if a:
            L.append(f"- 行数 {a['lines']} / 总字 {a['chars']} / 平均行长 {a['avg_line_len']} 字"
                     f"（{a['min_line_len']}~{a['max_line_len']}）")
            L.append(f"- **hook（重复最多的那句）**：`{a['hook']}`（出现 {a['hook_repeat']} 次）")
            if a["chorus_block"]:
                L.append(f"- **副歌块（{a['chorus_size']} 行，整块重复 {a['chorus_repeat']} 次）**：")
                for ln in a["chorus_block"]:
                    L.append(f"    - {ln}")
            if a["tail_top"]:
                L.append(f"- 高频行尾字：{'、'.join(f'{t}×{c}' for t, c in a['tail_top'])}")
            if a["images"]:
                L.append(f"- 意象：{'、'.join(f'{w}×{c}' for w, c in a['images'].items())}")
            if a["feelings"]:
                L.append(f"- 情绪词：{'、'.join(f'{w}×{c}' for w, c in a['feelings'].items())}")
        L.append("")
        L.append("**歌词全文**")
        L.append("")
        L.append("```")
        L.extend(s["lyrics"] or ["（未取到歌词）"])
        L.append("```")
        L.append("")

    md_p.write_text("\n".join(L), encoding="utf-8")
    js_p.write_text(json.dumps(
        {"collected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
         "note": "歌词取自番茄音乐歌曲详情页（平台在播版本）",
         "songs": songs},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return md_p, js_p


async def main():
    ap = argparse.ArgumentParser(description="番茄音乐爆款歌词拆解（只读）")
    ap.add_argument("--top", type=int, default=10, help="从榜单取前几首（默认 10）")
    ap.add_argument("--ids", default="", help="直接指定作品 ID，逗号分隔")
    ap.add_argument("--rank", default="热歌榜",
                    help="用哪个 APP 官方榜当样本源（默认 热歌榜；如 飙升榜/80后热歌）")
    ap.add_argument("--chart-json", default="",
                    help="直接指定榜单 json（优先级最高，默认按 --rank 自动挑）")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    import os
    wd = Path(args.workdir).expanduser().resolve() if args.workdir else (
        Path(os.environ["MW_WORKDIR"]).expanduser().resolve() if os.environ.get("MW_WORKDIR")
        else (Path(default_workdir()).expanduser().resolve() if default_workdir()
              else (Path.cwd() if (Path.cwd() / "tasks.csv").exists() else SCRIPT_DIR.parent)))

    if args.ids:
        targets = [{"id": s.strip(), "name": ""} for s in args.ids.split(",") if s.strip()]
    else:
        targets = pick_ids_from_chart(wd, args.top, args.chart_json, args.rank)
    if not targets:
        log("没有样本。先跑 fanqie_rank.py（APP 官方榜）或 fanqie_chart.py（网页版）生成榜单，"
            "或用 --ids / --chart-json 指定。")
        return 2

    log(f"工作目录：{wd}")
    log(f"待采歌词：{len(targets)} 首")

    pd = user_profile(PROFILE)
    clear_profile_locks(pd)

    songs = []
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            pd, headless=False, args=window_args(), viewport=None)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(30000)
        # ★ 2026-09-21：挂弹窗守卫。番茄详情页会弹活动/会员/下载引导浮层，
        #   裸 goto 进来后直接读 body.innerText，会把弹窗文案混进歌词正文 ——
        #   这比「点不动」更隐蔽：不报错，只是统计被悄悄带偏。
        await a_guard_context(ctx, log=log)
        for k, t in enumerate(targets, 1):
            sid = str(t.get("id") or "")
            if not sid:
                continue
            try:
                s = await fetch_one(page, sid, t.get("name", ""))
                got = len(s["lyrics"])
                log(f"  [{k}/{len(targets)}] 《{s['title']}》 取到 {got} 行歌词  （{s['duration'] or '?'}）")
                if got >= MIN_LYRIC_LINES:
                    songs.append(s)
                elif got:
                    # 太少 = 大概率是「新歌还没上歌词」或页面结构不同 ——
                    # 1~2 行的「样本」会把结构统计彻底带偏（比如「平均 1 行」），必须挡掉
                    log(f"      ⚠️ 只有 {got} 行，不足以当结构样本，已跳过"
                        f"（《{s['title']}》可能还没上歌词）")
                else:
                    log("      ⚠️ 没解析到歌词 —— 该页结构可能不同，已跳过")
            except Exception as e:
                log(f"  [{k}/{len(targets)}] 失败 {type(e).__name__}: {str(e)[:80]}")
        await ctx.close()

    if not songs:
        log("一首歌词都没取到，放弃写报告。")
        return 3

    if not args.no_save:
        md_p, js_p = write_report(songs, wd)
        log(f"拆解报告：{md_p}")
        log(f"原始数据：{js_p}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
