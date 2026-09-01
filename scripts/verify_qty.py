"""真页面验证：在 MiniMax/Hailuo 真实页面上把生成数量 2→1（只改控件，不点生成）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright

from generate import (load_config, ROOT, window_args, viewport_for,
                      goto_with_guard, guard_context, set_generate_quantity)

cfg = load_config()
profile = ROOT / cfg["profile_dir"]


def read_qty(page):
    return page.evaluate("""() => {
        const labels = [...document.querySelectorAll('span,div,label')]
            .filter(e => /数量/.test(e.textContent||'') && e.children.length <= 4);
        for (const lab of labels) {
            let el = lab;
            for (let k=0;k<4;k++){
                if(!el) break;
                const bs = el.querySelectorAll('button');
                const ns = [...el.querySelectorAll('span')].filter(s=>/^\\d+$/.test((s.textContent||'').trim()));
                if (bs.length>=2 && ns.length>=1) return ns[0].textContent.trim();
                el = el.parentElement;
            }
        }
        return '(未找到)';
    }""")


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(profile), headless=False,
        args=window_args(False), viewport=viewport_for(False),
        accept_downloads=True, slow_mo=150)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    guard_context(ctx, log=print)
    goto_with_guard(page, cfg["base_url"], cfg=cfg, log=print, settle_sec=5)
    page.wait_for_timeout(3000)

    before = read_qty(page)
    print(f"\n[修改前] 页面生成数量 = {before}")

    print("\n>>> 调用 set_generate_quantity(..., want=1)")
    ok = set_generate_quantity(page, cfg, 1, log=print)
    after1 = read_qty(page)
    print(f"[修改后] 页面生成数量 = {after1}  | 函数返回 {ok}")

    # 反向测试：设成 3（走加号路径），再设回 1
    print("\n>>> 反向测试：set_generate_quantity(..., want=3)")
    set_generate_quantity(page, cfg, 3, log=print)
    after3 = read_qty(page)
    print(f"[设成3后] 页面生成数量 = {after3}")
    print("\n>>> 设回 1")
    set_generate_quantity(page, cfg, 1, log=print)
    after_back = read_qty(page)
    print(f"[设回1后] 页面生成数量 = {after_back}")

    print("\n" + "=" * 60)
    ok_main = (after1 == "1")
    ok_plus = (after3 == "3")
    ok_back = (after_back == "1")
    print(f"主路径 2→1 : {'✅' if ok_main else '❌'}  (读到 {after1})")
    print(f"加号路径 →3 : {'✅' if ok_plus else '❌'}  (读到 {after3})")
    print(f"设回路径 3→1: {'✅' if ok_back else '❌'}  (读到 {after_back})")
    print("结论:", "全部通过 ✅" if (ok_main and ok_plus and ok_back) else "仍有问题 ❌")
    ctx.close()
