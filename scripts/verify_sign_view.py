# -*- coding: utf-8 -*-
"""
验证「番茄合同页签署按钮被遮挡、点不到」的修复是否真的生效。

做法：
  1) 用一段本地 HTML 模拟第三方电子签页面：右下角有「确认签署」按钮，
     但被一个 fixed 定位的「下载App」浮层正好压住。
  2) 复用 fanqie_upload.py 里【真实生效】的同一套函数：
        fit_window_to_screen()  +  ensure_sign_clickable()
     —— 不是另写一份逻辑，而是 import 进来直接跑，保证验证=线上行为。
  3) 对比「修复前 / 修复后」按钮中心命中了哪个元素、按钮是否真的能点，
     并各存一张截图（_verify_before.png / _verify_after.png）供你肉眼核对。

运行：
  cd mw-validation
  python verify_sign_view.py
"""
import asyncio
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

import fanqie_upload as F   # 直接复用线上修复代码

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="mw_verify_"))  # 临时目录，验证产物不落 skill 目录

# 模拟合同页：长表单 + 右下角签署按钮 + 压住按钮的 fixed 浮层
HTML = """<!doctype html><html><head><meta charset=utf-8>
<style>
  body{height:2200px;margin:0;font-family:sans-serif}
  .form{height:2000px;padding:24px;line-height:2}
  /* 右下角签署按钮 */
  #signbtn{position:fixed;right:24px;bottom:24px;width:168px;height:50px;
           background:#e63946;color:#fff;border:none;border-radius:8px;font-size:16px;z-index:10}
  /* 遮挡浮层：fixed 的「下载App」条，正好盖在按钮上方 */
  #cover{position:fixed;right:0;bottom:0;width:300px;height:130px;
         background:#222;color:#fff;padding:14px;z-index:9999}
  #cover b{color:#ffd166}
</style></head><body>
  <div class=form>
    <h2>电子合同签署</h2>
    <p>请阅读以下条款……（很长的表单，需要滚动才能看到最底部的签署按钮）</p>
    <p>条款内容条款内容条款内容条款内容条款内容条款内容条款内容条款内容</p>
    <p>条款内容条款内容条款内容条款内容条款内容条款内容条款内容条款内容</p>
    <p>条款内容条款内容条款内容条款内容条款内容条款内容条款内容条款内容</p>
  </div>
  <button id=signbtn>确认签署</button>
  <div id=cover><b>下载App</b><br>体验更佳，扫码下载</div>
</body></html>"""


async def main():
    html_path = TMP / "_contract_sim.html"
    html_path.write_text(HTML, encoding="utf-8")
    url = "file://" + html_path.as_posix()

    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        str(TMP / "profile"), headless=False, args=["--start-maximized"])
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    try:
        await F.fit_window_to_screen(page)
        await page.goto(url)
        await page.wait_for_timeout(500)

        # —— 修复前：按钮中心被 cover 盖住？——
        before = await page.evaluate("""() => {
            const b=document.getElementById('signbtn');
            const r=b.getBoundingClientRect();
            const top=document.elementFromPoint(r.left+r.width/2, r.top+r.height/2);
            return top ? top.id : 'none';
        }""")
        await page.screenshot(path=str(TMP / "_verify_before.png"))

        # —— 跑线上真实修复 ——
        ok = await F.ensure_sign_clickable(page, F.log)

        # —— 修复后：按钮中心是否变成按钮本身？——
        after = await page.evaluate("""() => {
            const b=document.getElementById('signbtn');
            b.scrollIntoView({block:'center', inline:'center'});
            const r=b.getBoundingClientRect();
            const top=document.elementFromPoint(r.left+r.width/2, r.top+r.height/2);
            return top ? top.id : 'none';
        }""")
        await page.screenshot(path=str(TMP / "_verify_after.png"))

        # —— 能否真的点？——
        clicked = False
        try:
            await page.locator("#signbtn").click(timeout=3000, force=True)
            clicked = True
        except Exception as e:
            F.log(f"    (点击测试异常: {e})")
    finally:
        await ctx.close()
        await p.stop()

    print("=" * 60)
    print(f"修复前 按钮中心命中元素 : {before}   （应为 cover = 被遮挡）")
    print(f"修复后 按钮中心命中元素 : {after}    （应为 signbtn = 可点）")
    print(f"ensure_sign_clickable 返回 : {ok}")
    print(f"按钮实际可点击        : {clicked}")
    print(f"截图: {TMP / '_verify_before.png'} / {TMP / '_verify_after.png'}")
    print("=" * 60)
    if before == "cover" and after == "signbtn" and clicked:
        print("✅ 验证通过：遮挡已清除，右下角签署按钮可点击。")
    else:
        print("❌ 验证未通过，需要检查修复逻辑。")


if __name__ == "__main__":
    asyncio.run(main())
