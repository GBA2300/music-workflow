# -*- coding: utf-8 -*-
"""番茄音乐 **爆款创作配方** 生成器 —— 把「榜单 + 歌词拆解」压成一张能照着写的配方卡（只读）

它解决什么问题
──────────────
研究做完之后，还差一层：**把研究结论变成「下一步怎么写」的具体参数**。
`fanqie_rank.py` 给榜单（哪首火 / 平台给的受众与曲风标签），
`fanqie_lyric.py` 给五层拆解（那些歌的写法指标），
但**指标 ≠ 能落笔的配方** —— 中间缺一道「交叉压缩」。

本脚本干的就是这道压缩，输出一张**配方卡**：

1. **本期受众画像** —— 从榜单官方标签算「谁在听」（70/80/90/00后 + KTV/广场舞/车载场景 + 年代）
2. **题材主盘** —— 本期官方曲风标签里哪几类是真大盘（别凭印象）
3. **歌名配方** —— 字数区间 + **版本后缀占比**（多版本铺量是跑通的打法）+ 人称词/首尾字
4. **结构配方** —— 目标行数、副歌块大小与整块重复次数、hook 落点与重复次数、单行字数
5. **韵脚候选池** —— 从拆解样本汇总出的行尾字，直接可挑
6. **情绪词 / 意象词库** —— 本期听众在要的情绪 + 可用的具象抓手
7. **情绪需求四问** —— 留空模板，**必须逐条回答后才能动笔**
8. **一句话配方** —— 可直接抄进写作 prompt 的浓缩参数
9. **撞名校验清单** —— 本期榜单全部歌名，写歌名前逐条比对
10. **写完自检清单** —— 拿配方反向验收成稿

> **纪律**：本脚本**只读本地文件**（`charts/*.json` + `study/*.json`），
> 不联网、不登录、不写平台数据。纯本地纯计算，零风险。

用法
────
    # 1) 先采榜（APP 官方榜）
    python fanqie_rank.py --rank 热歌榜 --limit 50
    python fanqie_rank.py --rank 80后热歌,90后热歌   # 想更贴受众就多采几个

    # 2) 再拆前十首歌词
    python fanqie_lyric.py --top 10

    # 3) 出配方
    python fanqie_recipe.py                          # 默认只用「热歌榜」
    python fanqie_recipe.py --ranks 热歌榜,80后热歌,90后热歌
    python fanqie_recipe.py --ranks all              # 合并当天全部榜
    python fanqie_recipe.py --no-save                # 只在屏幕上打

产物
────
- `<workdir>/study/番茄创作配方-YYYY-MM-DD.md`  —— 人读：配方卡
- `<workdir>/study/番茄创作配方-YYYY-MM-DD.json` —— 机读：参数对象（可直接喂给写作环节）
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCRIPT_DIR = Path(__file__).resolve().parent

try:
    from paths import default_workdir
except Exception:                                     # pragma: no cover

    def default_workdir():
        return None


# ── 官方标签分桶（2026-09-21 从 APP 榜单实测标签集归纳） ──────────────────────
AGE_LABELS = ["70后最爱", "80后最爱", "90后最爱", "00后最爱"]
SCENE_LABELS = ["KTV必点歌", "广场舞", "车载", "运动", "助眠", "合唱"]
ERA_LABELS = ["80年代", "90年代", "00年代", "10年代", "20年代"]
GENRE_ORDER = ["流行", "情歌", "伤感", "浪漫", "轻柔", "DJ", "舞曲",
               "国风", "古风", "红歌", "民歌", "摇滚", "电子", "民谣",
               "怀旧", "快乐", "活泼", "经典", "金曲"]

# 歌名里的版本后缀：歌名（DJ版）/ 歌名(深情女版) / 歌名 (少年版) …
VER_SUFFIX = re.compile(r"[（(]([^）)]{0,12}版)[）)]")
# 结尾的任意括号组（不只「…版」，还有 (DJ空灵鼓) / （Remix）这类没写「版」的）
TAIL_PAREN = re.compile(r"[（(][^（）()]{0,20}[）)]\s*$")
# 人称代词：番茄爆款歌名高频用「你 / 我」
PERSON_WORDS = ["你", "我", "他", "她", "咱", "自己"]


def log(msg: str) -> None:
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# ── 小工具 ────────────────────────────────────────────────────────────────────
def pct(sorted_vals: list, q: float):
    """简单分位数（q 取 0~1）。列表为空返回 None。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    i = int(round((len(sorted_vals) - 1) * q))
    return sorted_vals[max(0, min(i, len(sorted_vals) - 1))]


def med(vals: list):
    return round(statistics.median(vals), 1) if vals else None


def ri(v):
    """数字显示用：整数就别带 .0（'11.0 行' 读着别扭）。"""
    if v is None:
        return "-"
    try:
        f = float(v)
    except Exception:
        return str(v)
    return str(int(f)) if abs(f - round(f)) < 1e-9 else ("%.1f" % f)


def fmt_num(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "-"
    if n >= 100000000:
        return "%.1f亿" % (n / 100000000)
    if n >= 10000:
        return "%.1f万" % (n / 10000)
    return str(int(n))


# ── 读输入 ────────────────────────────────────────────────────────────────────
def load_charts(workdir: Path, ranks: str) -> tuple[list[dict], list[str], str]:
    """读 charts/番茄榜单-*.json。ranks='' 用热歌榜；'all' 用当天全部；否则逗号分隔。

    返回 (rows, 实际的榜名列表, 说明文字)
    """
    charts = workdir / "charts"
    if not charts.is_dir():
        return [], [], "charts/ 目录不存在"
    files = sorted(charts.glob("番茄榜单-*.json"))
    if not files:
        return [], [], ("charts/ 里没有 APP 榜单 json —— 先跑 "
                        "`python fanqie_rank.py --rank 热歌榜`")

    def name_of(p: Path) -> str:
        # 番茄榜单-<榜名>-YYYY-MM-DD.json
        m = re.match(r"番茄榜单-(.+?)-(\d{4}-\d{2}-\d{2})\.json$", p.name)
        return m.group(1) if m else p.stem

    want = (ranks or "热歌榜").strip()
    missing: list[str] = []
    if want.lower() == "all":
        picked = files
    else:
        names = [s.strip() for s in want.split(",") if s.strip()]
        # 每个名字取最新一天的那份
        picked = []
        for nm in names:
            cand = [p for p in files if name_of(p) == nm]
            if cand:
                picked.append(sorted(cand)[-1])
            else:
                missing.append(nm)

    if not picked:
        have = "、".join(sorted({name_of(p) for p in files}))
        return [], [], ("没找到榜单「%s」。当前 charts/ 里有的榜：%s\n"
                        "→ 先采：`python fanqie_rank.py --rank %s`；"
                        "或用 `--ranks all` 合并全部已有榜"
                        % ("、".join(missing), have, (missing or ["热歌榜"])[0]))

    if missing:
        log("  ⚠️ 没找到榜单「%s」，已跳过（用 --ranks all 可合并全部已有榜）" % "、".join(missing))

    # ★ 让「热歌榜」排在最前，这样 TOP10 明细就是**热歌榜的前十首**（与纪律 0 的口径一致）
    picked.sort(key=lambda p: (0 if name_of(p) == "热歌榜" else 1, str(p)))

    rows: list[dict] = []
    got: list[str] = []
    for p in picked:
        d = json.loads(p.read_text(encoding="utf-8"))
        for r in d.get("rows") or []:
            r = dict(r)
            r["_rank_name"] = d.get("rank_name") or name_of(p)
            rows.append(r)
        got.append(d.get("rank_name") or name_of(p))
    note = "charts/ 里共 %d 份 APP 榜单" % len(files)
    return rows, got, note


def dedupe_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """同一个作品会同时出现在多个榜里 —— 按作品 id 去重（保留先出现的，即前排各榜优先）。

    不去重的话，一首歌会被算好几次：曲风标签被灌水、歌名字数统计被放大、
    还会把「同一首歌跨榜出现」误报成「撞名」。
    """
    seen, out = set(), []
    for r in rows:
        k = str(r.get("id") or r.get("name") or "")
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out, len(rows) - len(out)


def load_study(workdir: Path) -> tuple[list[dict], str]:
    """读 study/ 里最新的歌词拆解 json。"""
    study = workdir / "study"
    if not study.is_dir():
        return [], "study/ 目录不存在"
    js = sorted(study.glob("番茄歌词拆解-*.json"))
    if not js:
        return [], ("study/ 里没有歌词拆解 json —— 先跑 `python fanqie_lyric.py --top 10`")
    d = json.loads(js[-1].read_text(encoding="utf-8"))
    songs = [s for s in (d.get("songs") or []) if s.get("analysis")]
    return songs, "%s（采集于 %s，%d 首）" % (js[-1].name, d.get("collected_at"), len(songs))


# ── 分析：榜单侧 ──────────────────────────────────────────────────────────────
def tag_counter(rows: list[dict], pool: list[str]) -> Counter:
    c = Counter()
    for r in rows:
        for t in r.get("tags") or []:
            if t in pool:
                c[t] += 1
    return c


def audience_profile(rows: list[dict]) -> dict:
    """从官方标签算受众画像。

    ⚠️ 2026-09-21 修正的方法论坑：**年龄榜不能用来自证受众**。
    `80后热歌` 本身就是按 80 后筛过的，把它和热歌榜混在一起统计「哪个年龄档最多」，
    结论必然偏向你采进来的那个年龄榜 —— **这是自我实现，不是洞察**。
    → 受众画像**只用大盘榜（热歌榜）**算；年龄榜只用来补充歌名形态与题材。
    """
    all_rows = rows
    base = [r for r in rows if r.get("_rank_name") == "热歌榜"]
    audience_basis = "热歌榜（大盘榜）"
    if not base:
        base = all_rows
        audience_basis = "全部所选榜（⚠️ 未含热歌榜，若含年龄榜则受众推断会自我实现）"

    age = tag_counter(base, AGE_LABELS)
    scene = tag_counter(base, SCENE_LABELS)
    era = tag_counter(base, ERA_LABELS)
    n = max(1, len(base))
    main_age = age.most_common(1)[0][0] if age else "—"
    main_scene = scene.most_common(1)[0][0] if scene else "—"
    return {
        "songs": len(base),
        "basis": audience_basis,
        "age": age.most_common(),
        "age_pct": {k: round(v * 100.0 / n, 1) for k, v in age.most_common()},
        "scene": scene.most_common(),
        "era": era.most_common(),
        "main_age": main_age,
        "main_scene": main_scene,
    }


def genre_profile(rows: list[dict]) -> list[tuple]:
    return tag_counter(rows, GENRE_ORDER).most_common()


def title_recipe(rows: list[dict], top: int = 10) -> dict:
    """歌名配方：字数、版本后缀、人称词、首尾字。

    ⚠️ 两个必须防的坑（2026-09-21 实测踩到）：
    1. **同一首歌会出现在多个榜里** → 传进来前必须先按作品 id 去重，否则同一首被算好几次。
    2. **括号后缀不只「…版」** —— 还有 `(DJ空灵鼓)` / `（Remix）` / `（DJ)`（括号都不配对）这类。
       只剥「含版字」的会漏，导致「收尾字」里冒出 `）` `(` 这种噪声字符。
    """
    names = [r.get("name") or "" for r in rows]
    names = [n for n in names if n]
    if not names:
        return {}

    base, suffixes = [], []
    for nm in names:
        # 先剥结尾的任意括号组（拿干净的基础歌名）
        m = TAIL_PAREN.search(nm)
        if m:
            removed = m.group(0)
            clean = nm[:m.start()].strip()
            base.append(clean or nm.strip())
            # 只有「…版」才算版本后缀（DJ版/深情女版/少年版…）
            if "版" in removed:
                suffixes.append(removed.strip("（）() \t"))
        else:
            base.append(nm.strip())
    lens = sorted(len(b) for b in base if b)

    person = Counter()
    for nm in base:
        for w in PERSON_WORDS:
            if w in nm:
                person[w] += 1

    head = Counter(b[0] for b in base if b)
    tail = Counter(b[-1] for b in base if b)

    with_ver = len(suffixes)
    # 撞名 = **不同作品**（id 不同）用了同一个基础歌名
    ids_by_base: dict[str, set] = {}
    for nm, r in zip(names, rows):
        m = TAIL_PAREN.search(nm)
        b = (nm[:m.start()].strip() if m else nm.strip()) or nm
        ids_by_base.setdefault(b, set()).add(str(r.get("id") or nm))
    dupes = sorted(b for b, ids in ids_by_base.items() if len(ids) > 1)

    return {
        "sample": len(base),
        "len_min": lens[0] if lens else None,
        "len_p25": pct(lens, 0.25),
        "len_median": med(lens),
        "len_p75": pct(lens, 0.75),
        "len_max": lens[-1] if lens else None,
        "with_version": with_ver,
        "with_version_pct": round(with_ver * 100.0 / max(1, len(base)), 1),
        "suffix_top": Counter(suffixes).most_common(10),
        "person_top": person.most_common(),
        "head_top": head.most_common(8),
        "tail_top": tail.most_common(8),
        "top10_titles": [
            {"rank": r.get("rank"), "rank_change": r.get("rank_change"),
             "name": r.get("name"), "author": r.get("author"),
             "play": r.get("play_num"), "tags": (r.get("tags") or [])[:5],
             "rank_name": r.get("_rank_name")}
            for r in rows[:top]
        ],
        "dupes": dupes,
    }


# ── 分析：歌词侧 ──────────────────────────────────────────────────────────────
def structure_recipe(songs: list[dict]) -> dict:
    """把多首拆解结果汇总成一份「怎么写」的结构参数。"""
    A = [s["analysis"] for s in songs if s.get("analysis")]
    if not A:
        return {}
    lines = sorted(a["lines"] for a in A)
    avg_len = sorted(a["avg_line_len"] for a in A)
    # ★ 2026-09-21：主歌/副歌**分开**统计句长 —— 一把尺子量不了两种结构（见 fanqie_lyric.analyze 的说明）
    v_len = sorted(a["verse_avg_len"] for a in A if a.get("verse_avg_len"))
    v_max = sorted(a["verse_max_len"] for a in A if a.get("verse_max_len"))
    c_len = sorted(a["chorus_avg_len"] for a in A if a.get("chorus_avg_len"))
    c_max = sorted(a["chorus_max_len"] for a in A if a.get("chorus_max_len"))
    twin = sorted(a["chorus_two_clause_ratio"] for a in A
                  if a.get("chorus_two_clause_ratio") is not None)
    csize = sorted(a["chorus_size"] for a in A if a.get("chorus_size"))
    crep = sorted(a["chorus_repeat"] for a in A if a.get("chorus_repeat"))
    hrep = sorted(a["hook_repeat"] for a in A)

    tails = Counter()
    feels = Counter()
    imgs = Counter()
    for a in A:
        for t, c in a.get("tail_top") or []:
            tails[t] += c
        feels.update(a.get("feelings") or {})
        imgs.update(a.get("images") or {})

    block_share = round(
        100.0 * sum(1 for a in A if a.get("chorus_size", 0) >= 2) / len(A), 1)

    return {
        "songs": len(A),
        "lines_min": lines[0], "lines_p25": pct(lines, 0.25),
        "lines_median": med(lines), "lines_p75": pct(lines, 0.75),
        "lines_max": lines[-1],
        "avg_line_len_median": med(avg_len),
        "avg_line_len_min": avg_len[0], "avg_line_len_max": avg_len[-1],
        "avg_line_len_p75": pct(avg_len, 0.75),
        # ★ 分层句长（2026-09-21）：主歌短句 / 副歌长句，落笔时分开用
        "verse_len_median": med(v_len) if v_len else None,
        "verse_len_p75": pct(v_len, 0.75) if v_len else None,
        "verse_len_max": v_max[-1] if v_max else None,
        "chorus_len_median": med(c_len) if c_len else None,
        "chorus_len_p75": pct(c_len, 0.75) if c_len else None,
        "chorus_len_max": c_max[-1] if c_max else None,
        "chorus_two_clause_median": med(twin) if twin else None,
        "chorus_size_median": med(csize),
        "chorus_size_p25": pct(csize, 0.25), "chorus_size_p75": pct(csize, 0.75),
        "chorus_size_range": [csize[0], csize[-1]] if csize else [],
        "chorus_repeat_median": med(crep),
        "chorus_repeat_min": crep[0] if crep else None,
        "block_repeat_pct": block_share,
        "hook_repeat_median": med(hrep), "hook_repeat_min": hrep[0] if hrep else None,
        "tail_pool": tails.most_common(14),
        "feeling_pool": feels.most_common(14),
        "image_pool": imgs.most_common(14),
    }


# ── 输出 ──────────────────────────────────────────────────────────────────────
def render(st: dict) -> str:
    """把配方参数渲染成人读的 markdown。数据不足时返回一句错误说明。"""
    rows = st["rows"]
    if not rows:
        return st["chart_note"]

    L = []
    stamp = datetime.now().strftime("%Y-%m-%d")
    L.append("# 番茄音乐爆款创作配方（%s）" % stamp)
    L.append("")
    L.append("- 生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    L.append("- 数据源：%s" % st["chart_note"])
    L.append("- 采用的榜：**%s**（共 %d 首）" % ("、".join(st["ranks"]) or "—", len(rows)))
    L.append("- 歌词样本：%s" % st["study_note"])
    L.append("")
    L.append("> 本卡片只做**参数压缩**：榜单负责「谁在听、平台推什么」，"
             "拆解负责「怎么写」，这里把它们压成**能照着落笔的数字**。")
    L.append("")

    # ── 一、受众画像 ──
    ap = st["audience"]
    L.append("## 一、本期受众画像（谁在听）")
    L.append("")
    L.append("> 统计口径：**%s**，共 %d 首。" % (ap["basis"], ap["songs"]))
    L.append("> ⚠️ **年龄榜（80后/90后热歌）不能用来自证受众** —— 它们本来就是按年龄筛过的，"
             "混进来统计「哪个年龄档最多」是**自我实现**。所以受众只用大盘榜算。")
    L.append("")
    if ap["age"]:
        L.append("| 年龄档 | 命中曲数 | 占比 |")
        L.append("|---|---|---|")
        for k, v in ap["age"]:
            L.append("| %s | %d | %s%% |" % (k, v, ap["age_pct"].get(k, "-")))
        L.append("")
        L.append("- **主受众：%s**" % ap["main_age"])
    if ap["scene"]:
        L.append("- 场景标签：%s" % "、".join("%s(%d)" % (k, v) for k, v in ap["scene"]))
        L.append("- **主场景：%s**" % ap["main_scene"])
    if ap["era"]:
        L.append("- 年代标签：%s" % "、".join("%s(%d)" % (k, v) for k, v in ap["era"]))
    if not (ap["age"] or ap["scene"]):
        L.append("（本期榜单没有年龄/场景标签，无法判断受众 —— 建议加采 `80后热歌` / `90后热歌`）")
    L.append("")
    L.append("> **怎么用**：主受众是 80 后 → 写「上有老下有小、旧情与故乡」；"
             "70 后 → 写「半生已过、身体与老伴」；90 后 → 写「错过与当年」。"
             "主场景是 KTV → **副歌要好唱、整块重复**；广场舞 → **节奏要跳得起来**。")
    L.append("")

    # ── 二、题材主盘 ──
    L.append("## 二、题材主盘（平台在推什么）")
    L.append("")
    gp = st["genres"]
    if gp:
        L.append("| 曲风/题材标签 | 命中曲数 |")
        L.append("|---|---|")
        for k, v in gp[:12]:
            L.append("| %s | %d |" % (k, v))
        L.append("")
        top3 = "、".join(k for k, _ in gp[:3])
        L.append("- **本期主盘：%s**" % top3)
        L.append("> **怎么用**：主盘 = 安全区（跟得上大盘，但同质化重）；"
                 "**主盘之外的次盘 = 差异化机会**（榜上有位、竞争更少）。")
    else:
        L.append("（本期榜单无曲风标签）")
    L.append("")

    # ── 三、歌名配方 ──
    tr = st["titles"]
    L.append("## 三、歌名配方（榜单实测）")
    L.append("")
    if tr:
        L.append("- **字数**：中位 **%s 字**（区间 %s~%s 字，P25~P75 = %s~%s）"
                 % (tr["len_median"], tr["len_min"], tr["len_max"], tr["len_p25"], tr["len_p75"]))
        L.append("- **带版本后缀**：%d/%d 首（**%s%%**）"
                 % (tr["with_version"], tr["sample"], tr["with_version_pct"]))
        if tr["suffix_top"]:
            L.append("- 常见后缀：%s"
                     % "、".join("%s(%d)" % (k, v) for k, v in tr["suffix_top"][:8]))
        if tr["person_top"]:
            L.append("- 人称词：%s" % "、".join("%s(%d)" % (k, v) for k, v in tr["person_top"]))
        if tr["head_top"]:
            L.append("- 起首字：%s" % "、".join("%s(%d)" % (k, v) for k, v in tr["head_top"][:6]))
        if tr["tail_top"]:
            L.append("- 收尾字：%s" % "、".join("%s(%d)" % (k, v) for k, v in tr["tail_top"][:6]))
        if tr["dupes"]:
            L.append("- ⚠️ 同榜重名：%s" % "、".join(tr["dupes"]))
        L.append("")
        L.append("> **怎么用**：歌名按上面字数区间写（**别超 P75**）；"
                 "**多版本铺量是跑通打法** —— 同一首词出「（DJ版）/（深情版）/（女版）」多个版本，"
                 "同名多版本可以同时挂榜（第十一轮《琵琶曲》就占了两个位次）。")
        L.append("")
        first_rank = tr["top10_titles"][0].get("rank_name") or (st["ranks"] or ["榜单"])[0]
        L.append("**%s TOP%d 明细（官方名次）**：" % (first_rank, len(tr["top10_titles"])))
        L.append("")
        L.append("| # | 涨跌 | 歌名 | 歌手 | 播放量 | 标签 |")
        L.append("|---|---|---|---|---|---|")
        for t in tr["top10_titles"]:
            L.append("| %s | %s | %s | %s | %s | %s |"
                     % (t["rank"], t.get("rank_change") or "", t["name"], t["author"],
                        fmt_num(t["play"]), "/".join(t["tags"] or [])))
        L.append("")

    # ── 四、结构配方 ──
    sr = st["structure"]
    L.append("## 四、结构配方（歌词五层拆解的汇总）")
    L.append("")
    if sr:
        L.append("| 参数 | 建议值 | 来源 |")
        L.append("|---|---|---|")
        L.append("| 总行数 | **%s~%s 行**（中位 %s） | %d 首样本 P25~P75 |"
                 % (sr["lines_p25"], sr["lines_p75"], ri(sr["lines_median"]), sr["songs"]))
        # ★ 2026-09-21：原「单行字数 约 X 字」把全文混合均值当上限发出去，
        #    副歌永远超标（副歌本来就是长句）。改为**主歌/副歌两把尺子**。
        if sr.get("verse_len_median") and sr.get("chorus_len_median"):
            L.append("| 单行字数（**主歌**） | **≤ %s 字**（中位 %s，多数不到 %s） | 主歌行 P75 |"
                     % (ri(sr["verse_len_p75"]), ri(sr["verse_len_median"]),
                        ri(sr["verse_len_p75"])))
            L.append("| 单行字数（**副歌**） | **%s~%s 字**（中位 %s，最长 %s） | 副歌行 P75 |"
                     % (ri(sr["chorus_len_median"]), ri(sr["chorus_len_p75"]),
                        ri(sr["chorus_len_median"]), ri(sr["chorus_len_max"])))
            L.append("| 全文单行字数（参考） | 约 %s 字（混合口径，**别当上限用**） | 样本均值 |"
                     % ri(sr["avg_line_len_median"]))
        else:
            L.append("| 单行字数 | **约 %s 字**（多数 %s~%s） | 样本均值 |"
                     % (ri(sr["avg_line_len_median"]), ri(sr["avg_line_len_min"]),
                        ri(sr["avg_line_len_p75"])))
        L.append("| 副歌块大小 | **%s 行**（多数 %s~%s） | 样本中位 |"
                 % (ri(sr["chorus_size_median"]), ri(sr["chorus_size_p25"]),
                    ri(sr["chorus_size_p75"])))
        L.append("| 副歌整块重复 | **≥ %s 次**（中位 %s） | 样本中位 |"
                 % (ri(sr["chorus_repeat_min"]), ri(sr["chorus_repeat_median"])))
        L.append("| hook 重复 | **≥ %s 次**（中位 %s） | 样本中位 |"
                 % (ri(sr["hook_repeat_min"]), ri(sr["hook_repeat_median"])))
        L.append("| 有「整块重复副歌」的歌 | **%s%%** | 样本占比 |" % sr["block_repeat_pct"])
        L.append("")
        if sr.get("chorus_two_clause_median") is not None:
            L.append("> ⚠️ **两条线别用错**：主歌是**短句**（≤%s 字），副歌是**长句**"
                     "（%s~%s 字，形态是「**两个分句 + 空格**」，如「山风山风等等我 带我去山那头」）。"
                     "榜上样本里 %s%% 的副歌行是这种双分句结构。"
                     "**别拿主歌的字数上限去卡副歌** —— 副歌写成短句会失去「一整句压上来」的气势。"
                     % (ri(sr["verse_len_p75"]), ri(sr["chorus_len_median"]),
                        ri(sr["chorus_len_p75"]),
                        round(100 * sr["chorus_two_clause_median"])))
            L.append("")
        L.append("> **怎么用**：**结构先定死再写词** —— 先排 %s 行骨架、留出 %s 行的副歌块，"
                 "把整块原样重复 ≥%s 次，hook（副歌第一句）重复 ≥%s 次。"
                 "**记住：番茄爆款靠「整块重复」，不是靠提炼两句口号。**"
                 % (ri(sr["lines_median"]), ri(sr["chorus_size_median"]),
                    ri(sr["chorus_repeat_min"]), ri(sr["hook_repeat_min"])))
    else:
        L.append("（没有歌词拆解数据 —— 先跑 `python fanqie_lyric.py --top 10`）")
    L.append("")

    # ── 五、韵脚候选池 ──
    if sr and sr["tail_pool"]:
        L.append("## 五、韵脚候选池（拆解样本的行尾字）")
        L.append("")
        L.append("、".join("%s(%d)" % (k, v) for k, v in sr["tail_pool"]))
        L.append("")
        L.append("> **怎么用**：从这里挑 1~2 个韵脚**一韵到底**，别挑生僻字 —— "
                 "听众 45~70 岁，第一遍听不清就划走了。")
        L.append("")

    # ── 六、情绪词 / 意象词库 ──
    if sr and (sr["feeling_pool"] or sr["image_pool"]):
        L.append("## 六、情绪词 / 意象词库")
        L.append("")
        if sr["feeling_pool"]:
            L.append("- **情绪词**：%s" % "、".join("%s(%d)" % (k, v) for k, v in sr["feeling_pool"]))
        if sr["image_pool"]:
            L.append("- **意象词**：%s" % "、".join("%s(%d)" % (k, v) for k, v in sr["image_pool"]))
        L.append("")
        L.append("> **怎么用**：情绪词决定「哪一类痛」，意象词决定「用什么具体东西承载它」。"
                 "**别写抽象情绪，写能看见的东西。**")
        L.append("")

    # ── 七、情绪需求四问 ──
    L.append("## 七、★ 听众情绪需求四问（必须逐条回答后才能动笔）")
    L.append("")
    L.append("> 研究成果必须落到**情绪层**，不许只罗列歌名或数字。**四问不答完，不许开写。**")
    L.append("")
    L.append("**(1) 这期最热的几首，听众在它们身上要的是什么情绪？**")
    L.append("")
    L.append("→ ")
    L.append("")
    L.append("**(2) 这个情绪是「宣泄」还是「出口」？**")
    L.append("")
    L.append("→ （番茄爆款共同点是「**痛，但克制**」：不骂人、不撒泼、不认命，"
             "而是「我记得，我也放下了」。副歌要给一句**体面的出口**，不是一句**控诉**。）")
    L.append("")
    L.append("**(3) 听众的现实处境是什么？如何共鸣？**")
    L.append("")
    L.append("→ （三四线 / 45~70 岁 / 有子女在外 / 有旧情未了 / 有未说出口的遗憾。"
             "**时间感极强** —— 用「时间节点」而不是「抽象情绪」："
             "那年 / 到现在 / 这一回 / 最后一次 / 又一年。）")
    L.append("")
    L.append("**(4) 我要写的这首，歌名与副歌第一句怎么让 TA 觉得「这说的就是我」？**")
    L.append("")
    L.append("→ （歌名按第三节的字数区间，且必须是 **TA 自己会说的白话**；"
             "副歌第一句按「歌名级句子」打磨。）")
    L.append("")

    # ── 八、一句话配方 ──
    L.append("## 八、★ 一句话配方（可直接抄进写作 prompt）")
    L.append("")
    if sr and tr:
        L.append("> 写一首面向 **%s**、适配 **%s** 场景的爆款歌："
                 % (ap["main_age"], ap["main_scene"]))
        L.append("> 歌名 **%s~%s 字白话短词**（可配版本后缀：%s）；"
                 % (tr["len_p25"], tr["len_p75"],
                    "、".join(k for k, _ in (tr["suffix_top"] or [])[:3]) or "DJ版 / 深情版"))
        if sr.get("verse_len_median") and sr.get("chorus_len_median"):
            L.append("> 全篇 **%s~%s 行**；**主歌**单行 ≤%s 字（短句），"
                     "**副歌**单行 %s~%s 字（长句，两个分句 + 空格）；"
                     % (sr["lines_p25"], sr["lines_p75"], ri(sr["verse_len_p75"]),
                        ri(sr["chorus_len_median"]), ri(sr["chorus_len_p75"])))
        else:
            L.append("> 全篇 **%s~%s 行**，单行 **约 %s 字**；"
                     % (sr["lines_p25"], sr["lines_p75"], ri(sr["avg_line_len_median"])))
        L.append("> 副歌写成 **%s 行一整块**，**原样重复 ≥%s 次**，"
                 % (ri(sr["chorus_size_median"]), ri(sr["chorus_repeat_min"])))
        L.append("> **副歌第一句 = hook**，全篇重复 **≥%s 次**；"
                 % ri(sr["hook_repeat_min"]))
        L.append("> 韵脚一韵到底（候选：%s）；"
                 % "、".join(k for k, _ in sr["tail_pool"][:5]))
        L.append("> 情绪走「**痛但克制 + 体面的出口**」，用时间节点起共鸣，"
                 "意象落在：%s。" % "、".join(k for k, _ in sr["image_pool"][:6]))
    else:
        L.append("（缺少歌名或结构参数，先把榜单与歌词拆解跑齐）")
    L.append("")

    # ── 九、撞名校验清单 ──
    L.append("## 九、撞名校验清单（写歌名前逐条比对）")
    L.append("")
    L.append("> 本清单 = 本期所采榜单的全部歌名 + 各轮研究档案里的历史黑名单。")
    L.append("")
    allnames = sorted({(r.get("name") or "") for r in rows if r.get("name")})
    for i in range(0, len(allnames), 6):
        L.append("- " + " ｜ ".join(allnames[i:i + 6]))
    L.append("")

    # ── 十、自检清单 ──
    L.append("## 十、写完自检清单（拿配方反向验收）")
    L.append("")
    if sr and tr:
        L.append("| 检查项 | 达标线 |")
        L.append("|---|---|")
        L.append("| 总行数 | %s~%s 行 |" % (sr["lines_p25"], sr["lines_p75"]))
        if sr.get("verse_len_median") and sr.get("chorus_len_median"):
            L.append("| 单行字数（**主歌**）| ≤ %s 字 |" % ri(sr["verse_len_p75"]))
            L.append("| 单行字数（**副歌**）| %s~%s 字，且多数写成「两个分句 + 空格」 |"
                     % (ri(sr["chorus_len_median"]), ri(sr["chorus_len_p75"])))
        else:
            L.append("| 单行字数 | 不超过 %s 字 |" % ri(sr["avg_line_len_p75"]))
        L.append("| 副歌是「一整块」 | 块 ≥%s 行 |" % ri(sr["chorus_size_median"]))
        L.append("| 副歌整块原样重复 | ≥%s 次 |" % ri(sr["chorus_repeat_min"]))
        L.append("| 副歌第一句 = hook 且重复 | ≥%s 次 |" % ri(sr["hook_repeat_min"]))
        L.append("| 歌名字数 | %s~%s 字 |" % (tr["len_p25"], tr["len_p75"]))
        L.append("| 歌名与榜上歌名撞名 | 0 处 |")
        L.append("| 情绪 | 有出口、不撒泼 |")
        L.append("")

    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="番茄音乐爆款创作配方生成器（只读）")
    ap.add_argument("--ranks", default="热歌榜",
                    help="用哪些榜，逗号分隔；all = 当天全部（默认 热歌榜）")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    import os
    wd = Path(args.workdir).expanduser().resolve() if args.workdir else (
        Path(os.environ["MW_WORKDIR"]).expanduser().resolve() if os.environ.get("MW_WORKDIR")
        else (Path(default_workdir()).expanduser().resolve() if default_workdir()
              else (Path.cwd() if (Path.cwd() / "tasks.csv").exists() else SCRIPT_DIR.parent)))

    log("工作目录：%s" % wd)

    rows, ranks, chart_note = load_charts(wd, args.ranks)
    if not rows:
        log("✗ %s" % chart_note)
        return 2
    rows, dropped = dedupe_rows(rows)
    log("榜单：%s（去重后 %d 首%s）"
        % ("、".join(ranks), len(rows),
           "，跨榜重复 %d 条已合并" % dropped if dropped else ""))

    songs, study_note = load_study(wd)
    if songs:
        log("歌词样本：%d 首" % len(songs))
    else:
        log("⚠️ %s" % study_note)
        log("  → 配方会缺「结构/韵脚/情绪词」三节，其余仍可用。")

    st = {
        "rows": rows, "ranks": ranks, "chart_note": chart_note,
        "songs": songs, "study_note": study_note,
        "audience": audience_profile(rows),
        "genres": genre_profile(rows),
        "titles": title_recipe(rows, top=10),
        "structure": structure_recipe(songs),
    }

    md = render(st)
    if isinstance(md, str) and md.startswith("charts/"):
        log("✗ %s" % md)
        return 2

    # 屏幕摘要
    ap_ = st["audience"]
    tr, sr = st["titles"], st["structure"]
    log("")
    log("── 配方摘要 ──")
    log("  主受众：%s ｜ 主场景：%s" % (ap_["main_age"], ap_["main_scene"]))
    log("  题材主盘：%s" % "、".join(k for k, _ in st["genres"][:3]))
    if tr:
        log("  歌名：%s~%s 字（带版本后缀占 %s%%）"
            % (tr["len_p25"], tr["len_p75"], tr["with_version_pct"]))
    if sr:
        log("  结构：%s~%s 行 / 单行约 %s 字 / 副歌 %s 行整块重复 ≥%s 次 / hook ≥%s 次"
            % (sr["lines_p25"], sr["lines_p75"], ri(sr["avg_line_len_median"]),
               ri(sr["chorus_size_median"]), ri(sr["chorus_repeat_min"]),
               ri(sr["hook_repeat_min"])))

    if not args.no_save:
        study = wd / "study"
        study.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d")
        md_p = study / ("番茄创作配方-%s.md" % stamp)
        js_p = study / ("番茄创作配方-%s.json" % stamp)
        md_p.write_text(md, encoding="utf-8")
        js_p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        log("")
        log("配方卡：%s" % md_p)
        log("参数：  %s" % js_p)
        log("下一步：回答第七节「情绪需求四问」，再按第八节「一句话配方」动笔。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
