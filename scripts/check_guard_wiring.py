# -*- coding: utf-8 -*-
"""弹窗守卫「接线自检」—— 防止「以为接了，其实没接」。

为什么需要它
────────────
2026-09-21 端到端验收时踩的坑：`fanqie_upload.py` 早就接了 `popup_guard`，
但 `miaoxiang.py` **一次都没调用过** —— 没人发现，直到平台新加一个浮层
（「写歌升级为Agent模式」）把「专业模式」盖住，脚本才全线卡死，而且报错只说
「XX intercepts pointer events / selector 点不动」，**完全看不出是弹窗问题**。

这类漏接靠"看代码"很难发现（两个文件都长得挺正常），靠"跑一遍"又太贵
（真跑一次要耗妙响额度）。所以用一个**零成本、不开浏览器**的静态检查。

它做什么
────────
扫描 `<skill>/scripts/*.py`，对每个文件统计四项：

    popup_guard 引用 / dismiss_popups() / goto_with_guard() / guard_context()

再按**是否在生产链路上**分级判定，并给裸 `page.goto(` 标出**所在函数名**
（因为辅助函数里的裸 goto 是可接受的，生产函数里的不是）。

分级口径（★ 关键：不分级就会满屏报警，等于没报）
────────────────────────────────────────────
- **生产链路**（`PRODUCTION`）：硬性要求接守卫。裸 goto 若出现在
  **辅助函数**（`AUX_FUNCS`）里可放行；出现在生产函数里 = 失败。
- **辅助/一次性脚本**（`probe_*` `test_*` `diag_*` `inspect_*` …）：只汇总一行，不逐条报错。
- **白名单**：不驱动页面的、或守卫本体。

退出码：无「失败」= 0；有 = 1（可挂进提交前检查）。

它绝不做什么
────────────
- 不开浏览器、不连网络、不改任何文件。纯静态阅读 + 打印。

用法
────
    <python> scripts/check_guard_wiring.py            # 只看生产链路 + 汇总
    <python> scripts/check_guard_wiring.py --all      # 连辅助脚本也逐条列
"""
from __future__ import annotations

import os
import re
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 生产链路：驱动浏览器、且是流水线必经环节 ──────────────────────────
#    （改这里要慎重：名单漏了 -> 漏检；名单多了 -> 噪声）
PRODUCTION = {
    "miaoxiang.py",        # 妙响生成（六步铁律的落地端）
    "fanqie_upload.py",    # 番茄上传 + 授权 + 签合同
    "verify_published.py", # 纪律 3：后台只读核对
    "fanqie_chart.py",     # 六步铁律第 1 步：榜单采集
    "fanqie_lyric.py",     # 六步铁律第 2 步：歌词拆解
    "fanqie_learn.py",     # 歌词学习
    "login_check.py",      # 登录诊断
}

# ── 白名单：四项全 0 属正常 ───────────────────────────────────────────
WHITELIST = {
    "popup_guard.py": "守卫本体",
    "test_popup_guard.py": "守卫自检脚本",
    "check_guard_wiring.py": "本文件",
    "init_workdir.py": "纯文件复制工具，不驱动页面",
}

# ── 辅助函数名（正则）：这些函数里出现裸 goto 不算问题 ────────────────
#    理由：它们要么是给**人**看的页面（登录页，用户自己在旁边操作），
#    要么是一次性诊断（抓 DOM / 走查 / 录制）。弹窗不挡人眼。
AUX_FUNCS = re.compile(
    r"^(do_login|do_probe|do_walk|do_learn|learn_\w*|probe_\w*|diag_\w*|"
    r"dump_\w*|inspect_\w*|main)$"
)

# ── 辅助脚本文件名前缀：只汇总，不逐条报 ──────────────────────────────
AUX_PREFIXES = ("probe_", "test_", "diag_", "inspect_", "validate_",
                "verify_", "meta_", "check_")

PAGE_DRIVER_HINT = re.compile(r"^\s*(from|import)\s+playwright", re.M)
NAKED_GOTO = re.compile(r"page\.goto\s*\(")
DEF_LINE = re.compile(r"^(\s*)def\s+(\w+)\s*\(")


def enclosing_func(lines: list[str], lineno: int) -> str:
    """从 lineno 往上找最近的 def，返回函数名。"""
    for i in range(lineno - 2, -1, -1):
        m = DEF_LINE.match(lines[i])
        if m:
            return m.group(2)
    return "<模块级>"


def scan(path: str) -> dict:
    src = open(path, encoding="utf-8", errors="replace").read()
    lines = src.split("\n")
    naked = []
    for i, l in enumerate(lines):
        if NAKED_GOTO.search(l):
            naked.append({"line": i + 1, "func": enclosing_func(lines, i + 1),
                          "code": l.strip()})
    return {
        "name": os.path.basename(path),
        "drives_page": bool(PAGE_DRIVER_HINT.search(src)),
        "ref_guard": src.count("popup_guard"),
        # 只认真正的调用（带左括号），避免把「字符串里提到 popup_guard」当成接线
        "wired": (src.count("dismiss_popups(") + src.count("goto_with_guard(")
                  + src.count("guard_context(") + src.count("a_guard_")),
        "naked": naked,
    }


def main() -> int:
    show_all = "--all" in sys.argv
    files = sorted(os.path.join(SCRIPTS_DIR, f)
                   for f in os.listdir(SCRIPTS_DIR) if f.endswith(".py"))
    rows = [scan(f) for f in files if scan(f)["drives_page"]]

    print("=" * 74)
    print("弹窗守卫接线自检（静态，不开浏览器）")
    print("=" * 74)

    fails: list[str] = []
    warns: list[str] = []
    aux_rows: list[dict] = []

    # ── ① 生产链路 ──────────────────────────────────────────────
    print("\n【生产链路】硬性要求接守卫：\n")
    print(f"  {'脚本':<22}{'guard调用':<11}{'裸goto':<9}判定")
    print("  " + "-" * 68)
    for r in rows:
        if r["name"] not in PRODUCTION:
            continue
        prod_naked = [n for n in r["naked"] if not AUX_FUNCS.match(n["func"])]
        aux_naked = [n for n in r["naked"] if AUX_FUNCS.match(n["func"])]

        if r["wired"] == 0:
            verdict = "✗ 完全没接"
            fails.append(r["name"])
        elif prod_naked:
            verdict = f"✗ 生产函数里有 {len(prod_naked)} 处裸 goto"
            fails.append(r["name"])
        elif aux_naked:
            verdict = f"✓ OK（另有 {len(aux_naked)} 处辅助路径裸 goto，可接受）"
        else:
            verdict = "✓ OK"
        print(f"  {r['name']:<22}{r['wired']:<11}{len(r['naked']):<9}{verdict}")
        if prod_naked:
            for n in prod_naked:
                print(f"      第 {n['line']} 行 [{n['func']}]：{n['code'][:96]}")
                print(f"       → 换成 goto_with_guard(page, URL, cfg=cfg, log=log)")

    # ── ② 辅助脚本（只汇总） ────────────────────────────────────
    for r in rows:
        if r["name"] not in PRODUCTION and r["name"] not in WHITELIST:
            aux_rows.append(r)
    if aux_rows:
        unwired = [r["name"] for r in aux_rows if r["wired"] == 0]
        print(f"\n【辅助/一次性脚本】{len(aux_rows)} 个（不作为硬性要求）")
        if unwired:
            shown = unwired if show_all else unwired[:6]
            more = "" if show_all else (f" …等 {len(unwired)} 个" if len(unwired) > 6 else "")
            print(f"  ℹ 未接守卫 {len(unwired)} 个：{', '.join(shown)}{more}")
            print(f"    （诊断脚本崩了不影响发布，但**要真靠它跑流程就顺手接上**）")
            # 其中"接了但又出现裸 goto"的更值得看一眼
            partial = [r["name"] for r in aux_rows if r["wired"] > 0 and r["naked"]]
            if partial:
                print(f"  ℹ 已接但仍有裸 goto：{', '.join(partial)}")

    # ── ③ 结论 ──────────────────────────────────────────────────
    print("\n" + "=" * 74)
    if fails:
        print(f"✗ 生产链路上有 {len(fails)} 个脚本未达标：{', '.join(fails)}")
        for name in fails:
            r = next(x for x in rows if x["name"] == name)
            if r["wired"] == 0:
                print(f"\n  【{name}】接法（照抄 SKILL.md「弹窗挡路」章）：")
                print(f"    from popup_guard import dismiss_popups, goto_with_guard, guard_context")
                print(f"    · 建好 context 后：guard_context(ctx, log=log)")
                print(f"    · 页面跳转：page.goto(URL, ...) → goto_with_guard(page, URL, cfg=cfg, log=log)")
                print(f"    · 关键点击前：dismiss_popups(page, cfg=cfg, log=log)")
                print(f"    ⚠ 未登录时的首次 goto 用 goto_with_guard(..., dismiss=False)，"
                      f"否则会把登录框本身关掉")
        print("=" * 74)
        return 1

    print("✓ 生产链路全部达标：都接了守卫，且生产函数里没有裸 page.goto()")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
