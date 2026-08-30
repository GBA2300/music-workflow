# -*- coding: utf-8 -*-
"""
弹窗守卫自测 test_popup_guard.py

干嘛用的：
    用一个本地假页面模拟真实网站会遇到的几种弹窗，验证 popup_guard 确实能关掉它们。
    改过 popup_guard.py 或 config.json 里的选择器之后，跑一次就能确认没改坏：

        python test_popup_guard.py

覆盖 5 项：
    1. 原生 JS 弹窗 alert —— 不挂处理器会怎样（对照实验）
    2. 原生 JS 弹窗 alert —— 挂了 guard_page 会怎样
    3. DOM 浮层 —— antd 弹窗 + "我知道了"文字弹窗 + 无关闭按钮的纯遮罩，三种一次清干净
    4. 被遮挡的按钮 —— click_with_guard 能不能救回来
    5. 异步版 —— a_dismiss_popups 是否同样有效（上传脚本用的就是异步版）

思路：不 mock、不假装，真起一个 chromium 真点一遍。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright            # noqa: E402
from playwright.async_api import async_playwright          # noqa: E402

from popup_guard import (                                  # noqa: E402
    guard_page, dismiss_popups, click_with_guard,
    a_guard_page, a_dismiss_popups, a_click_with_guard,
)

# ---------------------------------------------------------------- 假页面

HTML = """
<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;height:100vh;font-family:sans-serif}
#genBtn{width:220px;height:64px;font-size:20px;margin:40px}
.ant-modal-root,.ant-modal-mask,.ant-modal-wrap{position:fixed;inset:0;z-index:1000}
.ant-modal-mask{background:rgba(0,0,0,.45)}
.ant-modal-wrap{display:flex;align-items:center;justify-content:center}
.ant-modal{background:#fff;padding:36px;width:420px;position:relative;border-radius:8px}
.ant-modal-close{position:absolute;right:10px;top:10px;width:26px;height:26px}
#textPop{position:fixed;inset:0;z-index:1500;background:rgba(0,0,0,.4);
         display:flex;align-items:center;justify-content:center}
#textPop .box{background:#fff;padding:28px;border-radius:8px}
#textPop button{padding:8px 20px;font-size:15px}
#pureMask{position:fixed;inset:0;z-index:2000;background:rgba(0,0,0,.25)}
</style></head><body>

<button id="genBtn" onclick="window.__clicks=(window.__clicks||0)+1">生成音乐</button>

<!-- ① antd 风格弹窗：有关闭图标 -->
<div id="modal1" class="ant-modal-root">
  <div class="ant-modal-mask"></div>
  <div class="ant-modal-wrap"><div class="ant-modal">
    <button class="ant-modal-close" onclick="document.getElementById('modal1').style.display='none';window.__c1=1">×</button>
    <h3>限时活动</h3><p>新版本上线啦</p>
  </div></div>
</div>

<!-- ② 只有文字按钮的弹窗 -->
<div id="textPop">
  <div class="box"><p>新功能引导</p>
    <button onclick="document.getElementById('textPop').style.display='none';window.__c2=1">我知道了</button>
  </div>
</div>

<!-- ③ 既没有关闭按钮、也点不掉的纯遮罩（最恶心的一种） -->
<div id="pureMask"></div>

<script>
window.__clicks = 0;
window.__alertFired = 0;
</script>
</body></html>
"""

# 带 alert 的版本：页面加载后 200ms 弹原生对话框
HTML_ALERT = HTML.replace(
    "window.__alertFired = 0;",
    "window.__alertFired = 0;\nsetTimeout(function(){ window.__alertFired = 1; alert('系统公告：服务升级'); }, 200);",
)


def _write(tmp, name, body):
    p = Path(tmp) / name
    p.write_text(body, encoding="utf-8")
    return p.as_uri()


# ---------------------------------------------------------------- 测试

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))
    return ok


def run_tests():
    tmp = tempfile.mkdtemp(prefix="pgtest_")
    url_plain = _write(tmp, "page.html", HTML)
    url_alert = _write(tmp, "alert.html", HTML_ALERT)

    with sync_playwright() as p:
        browser = p.chromium.launch()

        # ── 测试 1：原生 alert，不挂处理器（对照实验）
        print("\n【测试 1】原生 alert —— 不挂处理器会怎样（对照实验）")
        pg = browser.new_page()
        pg.set_default_timeout(4000)
        pg.goto(url_alert)
        pg.wait_for_timeout(800)          # 等 alert 弹出来
        try:
            fired = pg.evaluate("window.__alertFired")
            val = pg.evaluate("1+1")
            check("无处理器时页面仍能响应", val == 2,
                  f"alert 已触发={fired}，evaluate 返回 {val} → Playwright 默认自动关闭原生弹窗")
        except Exception as e:
            check("无处理器时页面仍能响应", False,
                  f"被卡住了：{type(e).__name__} → 说明必须挂处理器")
        pg.close()

        # ── 测试 2：原生 alert，挂上 guard_page
        print("\n【测试 2】原生 alert —— 挂上 guard_page 处理器")
        pg2 = browser.new_page()
        pg2.set_default_timeout(4000)
        logs = []
        guard_page(pg2, log=logs.append)
        pg2.goto(url_alert)
        pg2.wait_for_timeout(800)
        try:
            val = pg2.evaluate("1+1")
            hit = any("原生弹窗" in x for x in logs)
            check("挂处理器后页面正常响应", val == 2)
            check("处理器捕获到原生弹窗并记录下来", hit,
                  logs[0].strip() if logs else "没捕获到")
        except Exception as e:
            check("挂处理器后页面正常响应", False, f"{type(e).__name__}: {e}")
        pg2.close()

        # ── 测试 3：三种 DOM 浮层一起清掉
        print("\n【测试 3】DOM 浮层 —— antd 弹窗 + 文字弹窗 + 无关闭按钮的纯遮罩")
        pg3 = browser.new_page()
        pg3.set_default_timeout(6000)
        logs3 = []
        guard_page(pg3, log=logs3.append)
        pg3.goto(url_plain)
        pg3.wait_for_timeout(400)

        check("初始状态：纯遮罩确实挡住了按钮",
              pg3.locator("#pureMask").count() == 1)

        n = dismiss_popups(pg3, log=logs3.append)
        pg3.wait_for_timeout(500)
        c1 = pg3.evaluate("window.__c1 || 0")
        c2 = pg3.evaluate("window.__c2 || 0")
        mask_left = pg3.locator("#pureMask").count()

        check("antd 弹窗被关掉（× 按钮）", c1 == 1)
        check('文字弹窗被关掉（"我知道了"）', c2 == 1)
        check("无关闭按钮的纯遮罩被移除", mask_left == 0,
              f"剩余遮罩 {mask_left} 层")
        check("dismiss_popups 报告了处理结果", n > 0, f"共处理 {n} 处")

        # ── 测试 4：被遮挡的按钮，click_with_guard 能否救回来
        print("\n【测试 4】被遮挡的按钮 —— click_with_guard")
        pg4 = browser.new_page()
        pg4.set_default_timeout(6000)
        logs4 = []
        guard_page(pg4, log=logs4.append)
        pg4.goto(url_plain)
        pg4.wait_for_timeout(400)

        # 先确认：不清弹窗时，普通点击确实点不动（复现用户遇到的症状）
        plain_ok = False
        try:
            pg4.locator("#genBtn").click(timeout=2500)
            plain_ok = True
        except Exception as e:
            plain_ok = False
        check("复现问题：不清理弹窗时，普通点击确实失败", not plain_ok)

        got = click_with_guard(pg4, "#genBtn", log=logs4.append, timeout=2500)
        clicks = pg4.evaluate("window.__clicks")
        check("click_with_guard 清掉弹窗后点中了按钮", got and clicks == 1,
              f"返回={got}，按钮点击计数={clicks}")

        browser.close()

    # ── 测试 5：异步版
    print("\n【测试 5】异步版 —— 上传脚本用的是这套")
    ok5 = asyncio.run(_async_tests(url_plain))
    return ok5


async def _async_tests(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page()
        pg.set_default_timeout(6000)
        logs = []
        await a_guard_page(pg, log=logs.append)
        await pg.goto(url)
        await pg.wait_for_timeout(400)

        n = await a_dismiss_popups(pg, log=logs.append)
        await pg.wait_for_timeout(500)
        c1 = await pg.evaluate("window.__c1 || 0")
        c2 = await pg.evaluate("window.__c2 || 0")
        mask_left = await pg.locator("#pureMask").count()

        check("异步版：antd 弹窗被关掉", c1 == 1)
        check('异步版：文字弹窗（"我知道了"）被关掉', c2 == 1)
        check("异步版：纯遮罩被移除", mask_left == 0, f"剩余 {mask_left} 层")

        got = await a_click_with_guard(pg, "#genBtn", log=logs.append, timeout=2500)
        clicks = await pg.evaluate("window.__clicks")
        check("异步版：被遮挡的按钮能点中", got and clicks == 1,
              f"返回={got}，计数={clicks}")

        await browser.close()
    return True


def main():
    print("=" * 66)
    print("弹窗守卫自测（真起 chromium 真点一遍）")
    print("=" * 66)
    try:
        run_tests()
    except Exception as e:
        print(f"\n❌ 测试过程异常：{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 66)
    print(f"结果：{passed}/{total} 项通过")
    if passed == total:
        print("✅ 弹窗守卫工作正常")
    else:
        print("❌ 有失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"   - {name}  {detail}")
    print("=" * 66)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
