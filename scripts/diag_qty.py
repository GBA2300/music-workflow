"""诊断脚本 v2：抓 MiniMax/Hailuo 真实页面的「生成数量」控件结构。

这个页面是 Next.js + Tailwind 自定义 UI（非 antd），数量控件是自定义组件。
本脚本：
  1) 列出所有含「生成」文字的按钮（区分「参考音乐上传」与真正的生成按钮）
  2) 找叶子元素里含「数量」的标签，并 dump 其祖先容器
  3) 找文字是单个 +/-/− 的点击元素（自定义 stepper 的加减按钮）
  4) 存整页截图 scripts/_qty_diag.png，用眼睛看最稳
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright

from generate import (load_config, ROOT, window_args, viewport_for,
                      goto_with_guard, guard_context)

cfg = load_config()
profile = ROOT / cfg["profile_dir"]
profile.mkdir(exist_ok=True)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        headless=False,
        args=window_args(False),
        viewport=viewport_for(False),
        accept_downloads=True,
        slow_mo=150,
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    guard_context(ctx, log=print)

    print(">>> 打开 MiniMax 音乐页 ...")
    goto_with_guard(page, cfg["base_url"], cfg=cfg, log=print, settle_sec=5)
    page.wait_for_timeout(3000)

    has_gen = page.evaluate("""() => [...document.querySelectorAll('button')]
        .some(b => /生成|一键|立即/.test(b.textContent||''))""")
    if not has_gen:
        print("\n!!! 没找到「生成」按钮 —— 可能没登录或停在登录页。")
        print("    请先运行：  python generate.py --login   登录后重跑本诊断。")
        page.screenshot(path=str(ROOT / "_qty_diag.png"), full_page=True)
        print(">>> 已存截图 _qty_diag.png")
        ctx.close()
        sys.exit(2)

    print(">>> 已登录，抓取数量控件结构 ...")
    report = page.evaluate("""() => {
        const cut = (s, n=600) => (s||'').slice(0, n);
        const out = {};

        // 1) 所有含「生成」的按钮（区分参考上传 vs 真生成）
        out.genButtons = [...document.querySelectorAll('button')]
            .filter(b => /生成|一键|立即/.test(b.textContent||''))
            .map(b => ({txt:(b.textContent||'').trim().slice(0,30), cls:cut(b.className,90)}));

        // 2) 叶子元素里含「数量」的标签
        out.qtyLabels = [...document.querySelectorAll('*')]
            .filter(e => e.children.length === 0 && /数量/.test(e.textContent||'')
                        && (e.textContent||'').trim().length <= 8)
            .map(e => ({txt:(e.textContent||'').trim(),
                        cls:cut(e.className,80),
                        parent: e.parentElement ? cut(e.parentElement.outerHTML, 600) : null}));

        // 3) 文字是单个 +/-/− 的点击元素（自定义 stepper 加减键）
        out.pmChars = [];
        document.querySelectorAll('*').forEach(e => {
            const t = (e.textContent||'').trim();
            if (/^[+＋]$/.test(t) || /^[\\-\\−\\–]$/.test(t)) {
                out.pmChars.push({txt:t, tag:e.tagName, cls:cut(e.className,90),
                    parent: e.parentElement ? cut(e.parentElement.outerHTML, 600) : null});
            }
        });

        // 4) 显示数字 1~9 且独占文本的叶子元素（可能是 stepper 的当前值）
        out.numLeaves = [...document.querySelectorAll('*')]
            .filter(e => e.children.length === 0 && /^[1-9]$/.test((e.textContent||'').trim()))
            .map(e => ({txt:(e.textContent||'').trim(), cls:cut(e.className,80),
                        parent: e.parentElement ? cut(e.parentElement.outerHTML, 600) : null}));

        // 5) 真正的生成按钮祖先链（取「生成音乐/立即生成/一键」中不含「参考」的那个）
        const real = [...document.querySelectorAll('button')].find(b => {
            const t = b.textContent||'';
            return /生成音乐|立即生成|一键音乐|开始生成|生成歌曲/.test(t) && !/参考/.test(t);
        }) || [...document.querySelectorAll('button')].find(b => /生成|一键|立即/.test(b.textContent||'') && !/参考/.test(b.textContent||''));
        if (real) {
            out.realGenBtn = {txt:real.textContent.trim().slice(0,30), cls:cut(real.className,90)};
            let el = real, chain=[];
            for (let k=0;k<7;k++){ if(!el) break; chain.push(cut(el.outerHTML, 600)); el=el.parentElement; }
            out.realGenAncestors = chain;
        }
        return out;
    }""")

    print("\n" + "=" * 70)
    print("【含「生成」的按钮】")
    for b in report.get("genButtons", []):
        print("  ", b)
    print("\n【含「数量」的标签】")
    for x in report.get("qtyLabels", []):
        print("  label:", x["txt"], "| cls:", x["cls"])
        print("    parent:", x["parent"])
    print("\n【单个 +/- 点击元素】")
    for x in report.get("pmChars", []):
        print("  char:", x["txt"], "| tag:", x["tag"], "| cls:", x["cls"])
        print("    parent:", x["parent"])
    print("\n【显示 1~9 的叶子元素】")
    for x in report.get("numLeaves", []):
        print("  num:", x["txt"], "| cls:", x["cls"])
        print("    parent:", x["parent"])
    print("\n【真正的生成按钮】", report.get("realGenBtn"))
    print("\n【真正生成按钮祖先链】")
    for i, h in enumerate(report.get("realGenAncestors", [])):
        print(f"  [{i}] {h}")

    # 先存截图（最稳），再存 json
    try:
        page.screenshot(path=str(ROOT / "_qty_diag.png"), full_page=True)
        print("\n>>> 已存截图 _qty_diag.png")
    except Exception as e:
        print("\n!!! 截图失败:", e)
    (ROOT / "_qty_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(">>> 已存 _qty_report.json")
    ctx.close()
