"""诊断：量出番茄上传页真实视口 + 右下角所有按钮的位置，判断是「超出视口」还是「被浮层盖住」。

复用 fanqie_upload.py 的启动方式（profile_fanqie + --start-maximized + viewport=None）。
只观察、不改任何东西、不点生成。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright
from fanqie_upload import PROFILE, UPLOAD_URL

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE), headless=False,
        args=["--start-maximized"], viewport=None)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(UPLOAD_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    info = page.evaluate("""() => {
        const out = {};
        out.innerW = window.innerWidth;
        out.innerH = window.innerHeight;
        out.scrollH = document.documentElement.scrollHeight;
        out.clientH = document.documentElement.clientHeight;
        out.bodyScrollH = document.body.scrollHeight;
        // 右下角附近的按钮：所有 button，挑出 y 在视口下 40% 区域或 x 在右 40% 区域
        const vh = window.innerHeight, vw = window.innerWidth;
        const btns = [...document.querySelectorAll('button, a[role=button], [class*=btn]')];
        out.btns = btns.map(b => {
            const r = b.getBoundingClientRect();
            const cx = r.x + r.width/2, cy = r.y + r.height/2;
            let covered = null;
            try {
                const top = document.elementFromPoint(cx, cy);
                covered = (top === b || b.contains(top)) ? null : (top ? (top.tagName + '.' + (top.className||'').toString().slice(0,40)) : 'null');
            } catch(e) { covered = 'err'; }
            return {
                txt: (b.textContent||'').trim().slice(0,24),
                x: Math.round(r.x), y: Math.round(r.y),
                w: Math.round(r.width), h: Math.round(r.height),
                belowFold: r.bottom > vh,
                rightOut: r.right > vw,
                covered
            };
        }).filter(b => b.txt && (b.belowFold || b.rightOut || b.covered));
        // 也列出页面上所有含「签署/授权/发布/下一步」文字的按钮（可能就是要点的）
        out.signBtns = [...document.querySelectorAll('button')]
            .filter(b => /签署|授权|发布|下一步|确认|跳转/.test(b.textContent||''))
            .map(b => { const r=b.getBoundingClientRect();
                return {txt:(b.textContent||'').trim().slice(0,24), x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height), belowFold:r.bottom>window.innerHeight}; });
        return out;
    }""")

    print("=== 视口 / 页面高度 ===")
    print(f"  innerWidth×innerHeight = {info['innerW']} × {info['innerH']}")
    print(f"  documentElement.scrollHeight = {info['scrollH']}  clientHeight = {info['clientH']}")
    print(f"  body.scrollHeight = {info['bodyScrollH']}")
    print(f"  页面是否比视口高（需滚动）: {'是' if info['scrollH'] > info['innerH'] else '否'}")
    print("\n=== 右下角/被遮挡的按钮（belowFold 或 rightOut 或 covered）===")
    if info['btns']:
        for b in info['btns']:
            print(f"  「{b['txt']}」 x={b['x']} y={b['y']} w={b['w']} h={b['h']} "
                  f"belowFold={b['belowFold']} rightOut={b['rightOut']} covered={b['covered']}")
    else:
        print("  （无超出视口/被遮挡的按钮）")
    print("\n=== 含「签署/授权/发布/下一步/确认/跳转」的按钮 ===")
    for b in info['signBtns']:
        print(f"  「{b['txt']}」 x={b['x']} y={b['y']} w={b['w']} h={b['h']} belowFold={b['belowFold']}")

    page.screenshot(path=str(Path(__file__).resolve().parent / "_fanqie_diag.png"), full_page=False)
    print("\n>>> 已存截图 _fanqie_diag.png")
    ctx.close()
