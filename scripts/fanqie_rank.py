# -*- coding: utf-8 -*-
"""番茄音乐 **APP 官方榜单**采集（真·热歌榜，**只读**）

为什么有它（2026-09-21 用户纠正）
────────────────────────────────
用户原话：**「网页版的跟 APP 里音乐版的差距太大了」** —— 这一条是对的，实测确认：
- 网页版 `novelfm.com` 实际是个**「扫码下载」落地页**，HTML 里的「榜单/热歌」只出现在
  **meta SEO 关键词**里，页面上没有任何榜单入口（18 个候选路径全回落首页）。
- 所以 `fanqie_chart.py` 只能采首页的「热门歌曲 + 在听人数」，**那是网页版的推荐位，
  不是 APP 的榜单**，两者内容确实不一样。

**本脚本走 APP 自己的官方接口**，拿到的是**和 APP「排行榜」页一模一样的数据**：
10 个榜单 × 每榜 TOP50，**带官方名次、真实播放量、收藏量、曲风标签**。

接口是怎么找到的（留档，便于将来接口变动时重查）
──────────────────────────────────────────────
1. 网页版 JS 包里挖到后端主机：`api5-lite-sinfonlineb.novelfm.com`（aid=8661 = FmMusic）。
2. 下载 APK（`com.xs.fm.lite`）→ 解开 dex → 提取 `/novelfm/...` 接口路径，
   拿到 `/novelfm/bookmall/tab_v2/v1/`、`/novelfm/bookmall/recommend/book/list/v1/` 等。
3. `tab_v2` 的「金刚位」里写着榜单入口：
   `king_kong_type=music_rank` +
   `surl=.../pages-music-rank-list/template.js`（榜单页是 Lynx 页面）。
4. 下载该 `template.js` → 里面就是完整的 API 客户端：
   - `GetTopTabs` → `POST /novelfm/bookmall/top/tabs/v1/`     取榜单分类
   - `GetRecommendBookList` → `POST /novelfm/bookmall/recommend/book/list/v1/`  取榜单歌曲
   - scene 枚举：`MusicRankWithGenre=13`（取分类用）、`MUSIC_RANK=119`（取歌用）

> ⚠️ **接口是 APP 的公开只读内容接口，不需要签名、不需要登录、不需要 Cookie。**
> 关键必需参数是 `update_version_code`（早期漏了它会报 `update_version_code is 0`）。
> 本脚本**不登录任何账号**，所以**不存在封号风险**，也不会写任何平台侧数据。

用法
────
    python fanqie_rank.py --list-ranks              # 先看有哪些榜
    python fanqie_rank.py                           # 默认采「热歌榜」TOP50
    python fanqie_rank.py --rank all                # 10 个榜全采
    python fanqie_rank.py --rank 热歌榜 --limit 30
    python fanqie_rank.py --rank 70后热歌,80后热歌   # 逗号分隔多个榜
    python fanqie_rank.py --no-save                 # 只打印，不写文件
    python fanqie_rank.py --workdir "D:\\music-workflow"

产物
────
- `<workdir>/charts/番茄榜单-<榜名>-YYYY-MM-DD.md`  —— 人读：名次/歌名/歌手/播放量/标签 + 标签分布
- `<workdir>/charts/番茄榜单-<榜名>-YYYY-MM-DD.json` —— 机读：全量原始字段（供下轮对比涨跌）

JSON 里的 `rows[].id` 就是**作品 ID（book_id）**，可直接喂给：
    python fanqie_lyric.py --top 10     # 拆解歌词五层（★ 优先自动读本脚本产出的 APP 榜 json）
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from paths import default_workdir
except Exception:                                     # pragma: no cover

    def default_workdir():
        return None


# ── 接口常量（2026-09-21 实测可用） ────────────────────────────────────────────
API_HOST = "https://api5-lite-sinfonlineb.novelfm.com"
AID = "8661"                     # FmMusic
UPDATE_VERSION_CODE = "66732"    # 6.6.7.32；APP 升级后若报错优先改这里
DEVICE_PLATFORM = "android"
CHANNEL = "novelfm8704"

UA = ("com.xs.fm.lite/6.6.7.32 (Linux; U; Android 13; zh_CN; SM-S918B; "
      "Build/TP1A; Cronet/119)")

SCENE_TOP_TABS = 13              # De.MusicRankWithGenre —— 取榜单分类
SCENE_RANK_LIST = 119            # sa.MUSIC_RANK        —— 取榜单歌曲

TIMEOUT = 25


def log(msg: str) -> None:
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def _requests():
    try:
        import requests  # noqa: F401
        return requests
    except ImportError:
        print("缺少依赖 requests。装一下：pip install requests", file=sys.stderr)
        raise SystemExit(2)


def _query() -> str:
    return ("?aid=%s&update_version_code=%s&device_platform=%s&channel=%s"
            % (AID, UPDATE_VERSION_CODE, DEVICE_PLATFORM, CHANNEL))


def _headers() -> dict:
    return {"User-Agent": UA, "Accept": "application/json",
            "Content-Type": "application/json"}


def fetch_ranks() -> list[dict]:
    """取榜单分类列表（APP 排行榜页顶部的那排 tab）。"""
    requests = _requests()
    url = API_HOST + "/novelfm/bookmall/top/tabs/v1/" + _query()
    r = requests.post(url, headers=_headers(), json={"scene": SCENE_TOP_TABS},
                      timeout=TIMEOUT, verify=False)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError("取榜单分类失败：code=%s message=%s"
                           % (j.get("code"), j.get("message")))
    return (j.get("data") or {}).get("cell_labels") or []


def fetch_rank_songs(label_id: str, limit: int = 50) -> list[dict]:
    """取某个榜的歌曲列表（带官方名次与播放量）。"""
    requests = _requests()
    url = API_HOST + "/novelfm/bookmall/recommend/book/list/v1/" + _query()
    body = {"scene": SCENE_RANK_LIST, "limit": limit, "offset": 0,
            "label_id": str(label_id)}
    r = requests.post(url, headers=_headers(), json=body,
                      timeout=TIMEOUT, verify=False)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError("取榜单歌曲失败：code=%s message=%s"
                           % (j.get("code"), j.get("message")))
    return (j.get("data") or {}).get("books") or []


def _int(v):
    try:
        return int(v)
    except Exception:
        return None


def parse_rank_change(v) -> str:
    """把 stat_rank_desc 解析成可读的涨跌文案。

    语义来自榜单页 Lynx 模板源码（2026-09-21 实测确认）：
        "hidden" → 不显示 ; "null" → 新上榜
        >0 → 上升 N 位   ; <0 → 下降 N 位 ; 0 → 持平
    ⚠️ **它不是名次**，名次就是接口返回的列表顺序（见 normalize 的 1-based 序号）。
    """
    s = str(v or "").strip()
    if s == "" or s == "hidden":
        return "-"
    if s == "null":
        return "新上榜"
    n = _int(s)
    if n is None:
        return "-"
    if n > 0:
        return "▲%d" % n
    if n < 0:
        return "▼%d" % -n
    return "—"


def normalize(raw: list[dict], rank_name: str) -> list[dict]:
    """把接口原始字段整理成稳定的 rows 结构（供下轮对比与歌词拆解复用）。

    ⚠️ **名次来自列表顺序**（1-based）：接口不返回绝对名次，榜单页也是按下标渲染的。
       `stat_rank_desc` 是「名次变化」，映射到 `rank_change`。
    """
    rows = []
    for i, b in enumerate(raw, 1):
        b = b or {}
        rows.append({
            "rank": i,                                    # ← 列表顺序即名次
            "rank_change": parse_rank_change(b.get("stat_rank_desc")),
            "rank_change_raw": str(b.get("stat_rank_desc") or ""),
            "id": str(b.get("book_id") or ""),             # ← fanqie_lyric.py 用这个
            "name": b.get("book_name") or b.get("abstract") or "",
            "author": b.get("author") or b.get("anchor") or "",
            "album": (b.get("common_book_info") or {}).get("album_title") or "",
            "play_num": _int(b.get("play_num")),
            "collect_num": _int(b.get("collect_num")),
            "subscribe_num": _int(b.get("subscribe_num")),
            "search_num": _int(b.get("search_num")),
            "duration": b.get("audio_duration"),
            "tags": [t for t in str(b.get("tags") or "").split(",") if t],
            "album_id": str(b.get("album_id") or ""),
            "source": b.get("source") or "",
        })
    return rows


def fmt_num(n) -> str:
    if not isinstance(n, int):
        return "-"
    if n >= 100000000:
        return "%.2f亿" % (n / 100000000)
    if n >= 10000:
        return "%.1f万" % (n / 10000)
    return str(n)


def fmt_dur(sec) -> str:
    try:
        s = int(float(sec))
        return "%02d:%02d" % (s // 60, s % 60)
    except Exception:
        return "-"


def tag_stats(rows: list[dict], top: int = 25) -> list[tuple]:
    """跨曲曲风/情绪标签分布 —— 这是本工具的研究价值所在。"""
    c = Counter()
    for r in rows:
        for t in r.get("tags") or []:
            c[t] += 1
    return c.most_common(top)


def write_report(rank_name: str, rows: list[dict], workdir: Path) -> tuple[Path, Path]:
    charts = workdir / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    safe = rank_name.replace("/", "_").replace("\\", "_")
    md_p = charts / ("番茄榜单-%s-%s.md" % (safe, stamp))
    js_p = charts / ("番茄榜单-%s-%s.json" % (safe, stamp))

    L = []
    L.append("# 番茄音乐 APP 榜单 · %s（%s）" % (rank_name, stamp))
    L.append("")
    L.append("- 采集时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    L.append("- 数据源：**APP 官方接口**（`api5-lite-sinfonlineb.novelfm.com`，aid=8661）")
    L.append("- 与 APP「排行榜 → %s」页同源；**带官方名次、真实播放量、曲风标签**" % rank_name)
    L.append("- 采集量：%d 首" % len(rows))
    L.append("")
    L.append("> 本工具**只读**、**不登录**，不写任何平台侧数据，不存在账号风险。")
    L.append("")

    L.append("## 一、榜单")
    L.append("")
    L.append("> 名次 = 接口返回顺序（平台不返回绝对名次字段）。「涨跌」来自 `stat_rank_desc`：")
    L.append("> ▲N 上升 / ▼N 下降 / — 持平 / 新上榜。")
    L.append("")
    L.append("| # | 涨跌 | 歌名 | 歌手 | 播放量 | 收藏 | 时长 | 曲风标签 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["rank"], r.get("rank_change", "-"), r["name"] or "-", r["author"] or "-",
            fmt_num(r["play_num"]), fmt_num(r["collect_num"]),
            fmt_dur(r["duration"]), "/".join(r["tags"]) or "-"))
    L.append("")

    st = tag_stats(rows)
    if st:
        L.append("## 二、曲风/情绪标签分布（跨曲计数，本工具独有）")
        L.append("")
        L.append("> 网页版详情页**拿不到曲风标签**，只有 APP 接口的 `tags` 字段有 ——")
        L.append("> 所以「什么曲风在榜」这个判断，必须走本接口。")
        L.append("")
        L.append("| 标签 | 出现次数 | 占比 |")
        L.append("|---|---|---|")
        for t, n in st:
            L.append("| %s | %d | %.0f%% |" % (t, n, 100.0 * n / max(1, len(rows))))
        L.append("")

    L.append("## 三、怎么用")
    L.append("")
    L.append("```bat")
    L.append(":: 拿本榜单的 TOP10 去拆解「怎么写」")
    L.append("python fanqie_lyric.py --top 10")
    L.append("```")
    L.append("")

    md_p.write_text("\n".join(L), encoding="utf-8")
    js_p.write_text(json.dumps({
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "rank_name": rank_name,
        "source": "番茄音乐 APP 官方榜单接口 (aid=8661)",
        "note": "rows[].id = 作品 ID(book_id)，可直接喂给 fanqie_lyric.py",
        "count": len(rows),
        "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_p, js_p


def resolve_workdir(arg) -> Path:
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
    if (cwd / "tasks.csv").exists():
        return cwd
    return Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description="番茄音乐 APP 官方榜单采集（只读）")
    ap.add_argument("--rank", default="热歌榜",
                    help="榜单名（如 热歌榜 / 抖音榜 / 飙升榜 / 70后热歌）；all=全部；逗号分隔可多选")
    ap.add_argument("--limit", type=int, default=50, help="每榜取几首（默认 50，接口上限 50）")
    ap.add_argument("--list-ranks", action="store_true", help="只列出可选榜单")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--no-save", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    try:
        ranks = fetch_ranks()
    except Exception as e:
        log("✗ 取榜单分类失败：%r" % (e,))
        log("  常见原因：网络不通 / 接口参数变了（先改脚本顶部的 UPDATE_VERSION_CODE）")
        return 1

    if not ranks:
        log("✗ 接口没返回任何榜单分类")
        return 1

    if args.list_ranks:
        log("可用榜单（%d 个）：" % len(ranks))
        for r in ranks:
            print("    %-8s id=%-4s %s" % (r.get("name"), r.get("id"),
                                           r.get("rank_rule_desc") or ""))
        return 0

    want = args.rank.strip()
    if want.lower() == "all":
        targets = ranks
    else:
        names = [x.strip() for x in want.split(",") if x.strip()]
        targets = [r for r in ranks if r.get("name") in names]
        missing = [n for n in names if n not in {r.get("name") for r in ranks}]
        if missing:
            log("⚠️ 不认识的榜单：%s" % "、".join(missing))
            log("   可用：%s" % "、".join(r.get("name") for r in ranks))
        if not targets:
            return 2

    wd = resolve_workdir(args.workdir)
    log("工作目录：%s" % wd)
    log("榜单来源：番茄音乐 APP 官方接口（aid=%s）" % AID)

    total = 0
    for t in targets:
        nm, lid = t.get("name"), str(t.get("id"))
        log("")
        log("── %s（id=%s）" % (nm, lid))
        if t.get("rank_rule_desc"):
            log("   上榜规则：%s" % t["rank_rule_desc"])
        try:
            raw = fetch_rank_songs(lid, args.limit)
        except Exception as e:
            log("   ✗ 取歌失败：%r" % (e,))
            continue
        rows = normalize(raw, nm)
        if not rows:
            log("   ✗ 这个榜没返回歌曲")
            continue
        total += len(rows)
        log("   ✓ %d 首" % len(rows))
        for r in rows[:10]:
            print("      %2d. %-28s | %-14s | %-8s | %s" % (
                r["rank"], r["name"][:28], r["author"][:14],
                fmt_num(r["play_num"]), "/".join(r["tags"][:4])))
        st = tag_stats(rows, 12)
        if st:
            log("   标签 TOP：%s" % "、".join("%s(%d)" % (a, b) for a, b in st))
        if not args.no_save:
            md_p, js_p = write_report(nm, rows, wd)
            log("   报告：%s" % md_p)
            log("   数据：%s" % js_p)

    log("")
    log("共采集 %d 首。" % total)
    if not args.no_save:
        log("下一步：python fanqie_lyric.py --top 10   （拆解「怎么写」）")
    return 0


if __name__ == "__main__":
    try:
        import urllib3
        urllib3.disable_warnings()
    except Exception:
        pass
    sys.exit(main())
