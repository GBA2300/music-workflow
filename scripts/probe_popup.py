# -*- coding: utf-8 -*-
"""
弹窗探针 probe_popup.py —— 专门查「弹窗关不掉」这个问题
==========================================================

什么时候用它：
    脚本卡在弹窗上不动了（比如 MiniMax 弹了个 ❌ 要关的窗，守卫没认出来）。
    跑一下它，它会打开真实页面，把弹窗的**真实 DOM 结构**抓出来并告诉你：

      · 挡路的浮层是哪个元素（tag / class / id / role / z-index）
      · ★ 它有没有命中现有白名单 popup_roots（没命中 = 守卫压根没往里找）
      · 浮层里有哪些可点的东西（× / 确认按钮 / 文字按钮），各自的选择器怎么写
      · 顺带存一张截图，肉眼对照

用法（在脚本所在目录）：
    python probe_popup.py              # 打开 config.json 里的 base_url
    python probe_popup.py --wait 60    # 最多等 60 秒等弹窗出现（默认 45）
    python probe_popup.py --url https://...   # 指定别的页面

跑完看终端输出，把「建议加进 popup_roots 的容器」那一段照抄进 config.json 即可。
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from popup_guard import load_guard_config, DEFAULT_POPUP_ROOTS   # noqa: E402

REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized", "--no-first-run", "--no-default-browser-check",
    "--disable-infobars", "--lang=zh-CN",
]
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
"""

# 扫描页面上的浮层：fixed/absolute + z-index>=100 + 覆盖面积够大
_JS_SCAN_OVERLAYS = """
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  const els = document.querySelectorAll('body *');
  for (const el of els) {
    let st, r;
    try {
      st = getComputedStyle(el);
      r = el.getBoundingClientRect();
    } catch (e) { continue; }
    if (!st || st.display === 'none' || st.visibility === 'hidden') continue;
    if (st.position !== 'fixed' && st.position !== 'absolute') continue;
    const z = parseInt(st.zIndex, 10);
    if (isNaN(z) || z < 100) continue;
    const area = (r.width * r.height) / (vw * vh);
    if (area < 0.15) continue;                 // 只关心够大的浮层
    if (parseFloat(st.opacity || '1') < 0.05) continue;

    // 浮层里所有能点的东西
    const clickable = [];
    let nodes;
    try {
      nodes = el.querySelectorAll(
        'button, a, [role="button"], [role="img"], svg, [class*="close"], [class*="Close"], [aria-label]'
      );
    } catch (e) { nodes = []; }
    for (const c of nodes) {
      let cr, cst;
      try {
        cr = c.getBoundingClientRect();
        cst = getComputedStyle(c);
      } catch (e) { continue; }
      if (!cr || cr.width < 4 || cr.height < 4) continue;
      if (!cst || cst.display === 'none' || cst.visibility === 'hidden') continue;
      clickable.push({
        tag: c.tagName,
        cls: (c.className || '').toString().slice(0, 100),
        text: ((c.innerText || c.textContent) || '').trim().slice(0, 30),
        aria: c.getAttribute('aria-label') || '',
        id: c.id || '',
        x: Math.round(cr.x), y: Math.round(cr.y),
        w: Math.round(cr.width), h: Math.round(cr.height),
        // 小元素才可能是 × 图标
        looksLikeClose: (cr.width <= 60 && cr.height <= 60),
      });
    }

    out.push({
      tag: el.tagName,
      cls: (el.className || '').toString().slice(0, 140),
      id: el.id || '',
      role: el.getAttribute('role') || '',
      ariaModal: el.getAttribute('aria-modal') || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      z: z,
      pos: st.position,
      area: Math.round(area * 100),
      rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
      clickable: clickable.slice(0, 25),
      clickableCount: clickable.length,
    });
  }
  out.sort((a, b) => b.z - a.z);
  return out;
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="", help="要打开的页面（默认用 config.json 的 base_url）")
    ap.add_argument("--wait", type=int, default=45, help="最多等多少秒等弹窗出现（默认 45）")
    ap.add_argument("--profile", default="", help="登录态目录（默认用 config.json 的 profile_dir）")
    args = ap.parse_args()

    cfg = {}
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception as e:
        print(f"读 config.json 失败：{e}")
        return 1

    url = args.url or cfg.get("base_url", "")
    if not url:
        print("没给 URL，config.json 里也没有 base_url")
        return 1

    from paths import user_profile  # 登录态存每用户私有目录，绝不在 skill 内
    profile_dir = user_profile(args.profile or cfg.get("profile_dir", "profile"))

    g = load_guard_config(cfg)
    roots = g["popup_roots"]

    print("=" * 70)
    print(f"弹窗探针  {datetime.now():%H:%M:%S}")
    print(f"页面：{url}")
    print(f"登录态：{profile_dir}")
    print("=" * 70)

    # 跨平台清理残留浏览器
    try:
        from browser_utils import cleanup
        cleanup(profile_dir)
    except Exception:
        try:
            subprocess.run(["taskkill", "/f", "/im", "chrome.exe"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        for n in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                fp = profile_dir / n
                if fp.exists():
                    fp.unlink()
            except Exception:
                pass

    found = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir), headless=False, args=STEALTH_ARGS,
            user_agent=REAL_UA, locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport=None, ignore_default_args=["--enable-automation"],
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(20000)

        print(f"\n打开页面…")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  （打开页面时报了个错，忽略继续：{type(e).__name__}）")

        print(f"等页面稳定并轮询弹窗（最多 {args.wait} 秒）…")
        deadline = time.time() + args.wait
        last_report = 0
        while time.time() < deadline:
            time.sleep(2)
            try:
                overlays = page.evaluate(_JS_SCAN_OVERLAYS)
            except Exception:
                continue
            # 只关心"里面能点东西"的浮层（纯遮罩不算）
            real = [o for o in (overlays or []) if o.get("clickableCount", 0) > 0]
            if real:
                found = overlays
                print(f"\n✓ 第 {int(args.wait - (deadline - time.time()))} 秒发现 {len(real)} 个带按钮的浮层")
                break
            if time.time() - last_report > 10:
                last_report = time.time()
                left = int(deadline - time.time())
                print(f"  …还没看到弹窗，再等 {left} 秒（期间可以手动操作页面）")

        if not found:
            print("\n⚠️ 在这段时间内没检测到带按钮的浮层。可能：")
            print("   1) 弹窗需要你手动触发（现在就可以在浏览器里点一下让它弹出来）")
            print("   2) 弹窗已经关掉了")
            print("   3) 弹窗不是 fixed/absolute 定位，或 z-index 小于 100")
            try:
                input("\n如果现在弹窗已经出来了，按回车再扫一次；否则直接回车结束：")
                found = page.evaluate(_JS_SCAN_OVERLAYS) or []
            except Exception:
                pass

        # 截图留证
        shot = ROOT / "_probe_popup.png"
        try:
            page.screenshot(path=str(shot), full_page=False)
            print(f"\n📷 截图已存：{shot}")
        except Exception as e:
            print(f"\n（截图失败：{e}）")

        # 存 JSON
        out_json = ROOT / "_probe_popup.json"
        try:
            out_json.write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"📄 原始数据已存：{out_json}")
        except Exception:
            pass

        ctx.close()

    # ---------------------------------------------------------------- 分析报告
    print("\n" + "=" * 70)
    print("分析报告")
    print("=" * 70)

    if not found:
        print("没抓到浮层，无法分析。把终端输出和 _probe_popup.png 发出来即可。")
        return 0

    real = [o for o in found if o.get("clickableCount", 0) > 0]
    print(f"\n共 {len(found)} 个浮层，其中 {len(real)} 个里面有可点元素\n")

    for i, o in enumerate(real[:5]):
        print(f"--- 浮层 [{i}] {o['tag']}  z-index={o['z']}  覆盖={o['area']}%  {o['w'] if 'w' in o else o['rect']['w']}x{o['rect']['h']}")
        print(f"    class = {o['cls']!r}")
        if o["id"]:
            print(f"    id    = {o['id']!r}")
        if o["role"]:
            print(f"    role  = {o['role']!r}")
        if o["ariaModal"]:
            print(f"    aria-modal = {o['ariaModal']!r}")

        # ★ 关键：有没有命中白名单
        hits = []
        for r in roots:
            # 用选择器匹配这个元素（在同一个页面里没法直接 matches，交给浏览器做）
            hits.append(r)
        print(f"    可点元素 {o['clickableCount']} 个：")
        for c in o["clickable"][:12]:
            mark = "  ← 像 × 关闭按钮" if c["looksLikeClose"] else ""
            print(f"       <{c['tag']}> class={c['cls'][:50]!r} text={c['text']!r} "
                  f"aria={c['aria']!r} pos=({c['x']},{c['y']}) {c['w']}x{c['h']}{mark}")
        print()

    # 给出可抄进 config.json 的建议
    print("=" * 70)
    print("★ 建议：把下面这些容器选择器加进 config.json 的 popup_guard.extra_popup_roots")
    print("=" * 70)
    sugg = []
    for o in real[:5]:
        if o["id"]:
            sugg.append(f"#{o['id']}")
        cls = (o["cls"] or "").split()
        if cls:
            sugg.append("." + cls[0])
    seen = set()
    for s in sugg:
        if s in seen or s in (".", "#"):
            continue
        seen.add(s)
        print(f'    "{s}",')
    print("\n（加完重跑一次，看浮层是否被识别）")

    print("\n★ 现有 popup_roots 白名单（共 %d 项）：" % len(roots))
    for r in roots:
        print(f"    {r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
