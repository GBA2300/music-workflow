# -*- coding: utf-8 -*-
"""
弹窗守卫自测 test_popup_guard.py

干嘛用的：
    用本地假页面模拟真实网站会遇到的几种弹窗，验证 popup_guard 确实能关掉它们，
    并且**不会误伤页面上的正常内容**。改过 popup_guard.py 或 config.json 里的选择器
    之后，跑一次就能确认没改坏：

        python test_popup_guard.py

覆盖 8 组：
    1. 原生 alert —— 不挂处理器会怎样（对照实验）
    2. 原生 alert —— 挂了 guard_page 会怎样
    3. DOM 浮层 —— antd 弹窗 + 文字弹窗，一次清干净
    4. 被遮挡的按钮 —— click_with_guard 能不能救回来
    5. 异步版 —— a_dismiss_popups 是否同样有效（上传脚本用的就是异步版）
    6. ★ 表单安全 —— 绝不能把已填的标签删掉（曾真实发生过的事故）
    7. ★ 确认优先 —— 同时有「我知道了」和「以后再说」时，必须点前者
    8. ★ 遮罩默认不删 —— 删遮罩是兜底手段，默认关闭；显式开启才生效

思路：不 mock、不假装，真起一个 chromium 真点一遍。
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright            # noqa: E402
from playwright.async_api import async_playwright          # noqa: E402

from popup_guard import (                                  # noqa: E402
    guard_page, dismiss_popups, click_with_guard, goto_with_guard,
    a_guard_page, a_dismiss_popups, a_click_with_guard, a_goto_with_guard,
)

# ---------------------------------------------------------------- 假页面

_STYLE = """
body{margin:0;height:100vh;font-family:sans-serif}
#genBtn{width:220px;height:64px;font-size:20px;margin:40px}
.ant-modal-root,.ant-modal-mask,.ant-modal-wrap{position:fixed;inset:0;z-index:1000}
.ant-modal-mask{background:rgba(0,0,0,.45)}
.ant-modal-wrap{display:flex;align-items:center;justify-content:center}
.ant-modal{background:#fff;padding:36px;width:420px;position:relative;border-radius:8px}
.ant-modal-close{position:absolute;right:10px;top:10px;width:26px;height:26px}
#pureMask{position:fixed;inset:0;z-index:2000;background:rgba(0,0,0,.25)}
.tag{display:inline-block;background:#eef;padding:6px 10px;margin:4px;border:1px solid #99c}
.tag-close-icon{width:16px;height:16px;margin-left:6px}
.pop{position:fixed;inset:0;z-index:1200;background:rgba(0,0,0,.4);
     display:flex;align-items:center;justify-content:center}
.pop .box{background:#fff;padding:28px;border-radius:8px}
.pop button{padding:8px 20px;font-size:15px;margin:0 6px}
"""

_SCRIPT = "window.__clicks=0;window.__alertFired=0;window.__c1=0;window.__c2=0;"


def _page(body, script=_SCRIPT, extra_css=""):
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{_STYLE}{extra_css}</style>" \
           f"</head><body>{body}<script>{script}</script></body></html>"


# ① 两种常规弹窗（都能被识别为弹窗容器）
HTML_MAIN = _page("""
<button id="genBtn" onclick="window.__clicks=(window.__clicks||0)+1">生成音乐</button>

<div id="modal1" class="ant-modal-root">
  <div class="ant-modal-mask"></div>
  <div class="ant-modal-wrap"><div class="ant-modal">
    <button class="ant-modal-close" onclick="document.getElementById('modal1').style.display='none';window.__c1=1">×</button>
    <h3>限时活动</h3><p>新版本上线啦</p>
  </div></div>
</div>

<div id="textPop" class="pop" role="dialog" aria-modal="true">
  <div class="box"><p>新功能引导</p>
    <button onclick="document.getElementById('textPop').style.display='none';window.__c2=1">我知道了</button>
  </div>
</div>
""")

# ② 只有一层整屏遮罩、没有关闭按钮（最恶心的一种）
HTML_MASK = _page("""
<button id="genBtn" onclick="window.__clicks=(window.__clicks||0)+1">生成音乐</button>
<div id="pureMask"></div>
""")

# ③ ★ 表单 + 真实弹窗：模拟番茄上传页「歌手名/词作者/曲作者/制作人」标签
HTML_FORM = _page("""
<form id="songForm">
  <div class="tag"><span>歌手名</span><button class="tag-close-icon" onclick="this.parentElement.remove()">×</button></div>
  <div class="tag"><span>词作者</span><button class="tag-close-icon" onclick="this.parentElement.remove()">×</button></div>
  <div class="tag"><span>曲作者</span><button class="tag-close-icon" onclick="this.parentElement.remove()">×</button></div>
  <div class="tag"><span>制作人</span><button class="tag-close-icon" onclick="this.parentElement.remove()">×</button></div>
</form>

<div id="realPop" class="ant-modal-root" role="dialog" aria-modal="true">
  <div class="ant-modal-mask"></div>
  <div class="ant-modal-wrap"><div class="ant-modal">
    <button class="ant-modal-close" onclick="document.getElementById('realPop').style.display='none';window.__c1=1">×</button>
    <h3>公告</h3><p>系统维护通知</p>
  </div></div>
</div>
""")

# ④ ★ 确认按钮 vs 关闭按钮：必须点「我知道了」，不能点「以后再说」
HTML_CONFIRM = _page("""
<button id="genBtn" onclick="window.__clicks=(window.__clicks||0)+1">生成音乐</button>
<div id="confirmPop" class="pop" role="dialog" aria-modal="true">
  <div class="box"><p>额度提醒</p>
    <button onclick="document.getElementById('confirmPop').style.display='none';window.__c1=1">我知道了</button>
    <button onclick="document.getElementById('confirmPop').style.display='none';window.__c2=1">以后再说</button>
  </div>
</div>
""")

# ⑤ 番茄/Arco 风格弹窗：确认按钮是 footer 里的主按钮，文案是「保存」
HTML_ARCO = _page("""
<button id="genBtn" onclick="window.__clicks=(window.__clicks||0)+1">生成音乐</button>
<div id="cropPop" class="arco-modal-wrapper" role="dialog" aria-modal="true">
  <div class="arco-modal">
    <div class="arco-modal-footer">
      <button class="arco-btn arco-btn-primary"
        onclick="document.getElementById('cropPop').style.display='none';window.__c1=1">保存</button>
    </div>
  </div>
</div>
""")

# ⑥ ★ 危险弹窗：footer 主按钮是「确认删除」—— 这种情况宁可不管，也不能点
HTML_DANGER = _page("""
<button id="genBtn" onclick="window.__clicks=(window.__clicks||0)+1">生成音乐</button>
<div id="delPop" class="arco-modal-wrapper" role="dialog" aria-modal="true">
  <div class="arco-modal">
    <div class="arco-modal-footer">
      <button class="arco-btn arco-btn-primary"
        onclick="document.getElementById('cropPop');window.__c1=1">确认删除</button>
    </div>
  </div>
</div>
""")

# ⑦ ★ 模拟 MiniMax：容器 class 完全不是标准 antd/arco，也没有 role=dialog。
#    守卫靠"它挡住了屏幕"来认出这是弹窗，而不是靠猜 class 名字。
HTML_MINIMAX = _page("""
<button id="genBtn" onclick="window.__clicks=(window.__clicks||0)+1">限时免费</button>
<div class="music-modal-container">
  <div class="music-modal-mask"></div>
  <div class="music-modal-content">
    <span class="icon-close" onclick="document.querySelector('.music-modal-container').style.display='none';window.__c1=1">&#10060;</span>
    <h3>新功能上线</h3><p>来看看有什么新东西</p>
  </div>
</div>
""", extra_css="""
.music-modal-container{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center}
.music-modal-mask{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000}
.music-modal-content{background:#fff;padding:36px;width:420px;position:relative;border-radius:8px;z-index:1001}
.icon-close{position:absolute;right:12px;top:10px;width:24px;height:24px;cursor:pointer}
""")

# ⑧ ★★ 最刁钻的一种：容器 class 不标准，❌ 的 class 里也完全没有 close 字样，
#     而且它是个裸 span（不是 button）。只能靠"在对话框右上角、又小、还是 ❌ 符号"认出来。
HTML_CORNER = _page("""
<button id="genBtn" onclick="window.__clicks=(window.__clicks||0)+1">限时免费</button>
<div class="xyz-layer">
  <div class="xyz-box">
    <span class="abc-shut" onclick="document.querySelector('.xyz-layer').style.display='none';window.__c1=1">&#10060;</span>
    <h3>活动公告</h3><p>这条弹窗什么标准类名都没有</p>
  </div>
</div>
""", extra_css="""
.xyz-layer{position:fixed;inset:0;z-index:1500;display:flex;align-items:center;justify-content:center}
.xyz-box{background:#fff;padding:36px;width:420px;position:relative;border-radius:8px}
.abc-shut{position:absolute;right:12px;top:10px;width:24px;height:24px;cursor:pointer;font-size:18px}
""")

# ⑨ ★ MiniMax 真实结构（用户实测抓到的）
#    容器是 section.responsive-modal z-[1050]，❌ 的 class 是 icon-btn（没有 close 字样）
HTML_REAL = _page("""
<button id="genBtn" onclick="window.__clicks=(window.__clicks||0)+1">限时免费</button>
<section class="responsive-modal z-[1050] flex items-center justify-center">
  <div class="modal-panel">
    <button class="icon-btn" onclick="document.querySelector('section.responsive-modal').style.display='none';window.__c1=1">&#10060;</button>
    <h3>公告</h3><p>MiniMax 真实结构</p>
  </div>
</section>
""", extra_css="""
.responsive-modal{position:fixed;inset:0;z-index:1050;display:flex;align-items:center;justify-content:center}
.modal-panel{background:#fff;padding:36px;width:460px;position:relative;border-radius:12px}
.icon-btn{position:absolute;right:12px;top:12px;width:28px;height:28px;border:none;background:transparent;cursor:pointer}
""")

# ⑩ ★ MiniMax 活动弹窗（用户实测记录：关闭按钮带 aria-label='close'）
#    这条走的是「选择器直接命中」路径，不需要靠角落兜底，验证主路径可用。
HTML_REAL2 = _page("""
<button id="genBtn" onclick="window.__clicks=(window.__clicks||0)+1">限时免费</button>
<section class="responsive-modal z-[1050]">
  <div class="modal-panel">
    <button aria-label="close" class="close-x"
      onclick="document.querySelector('section.responsive-modal').style.display='none';window.__c1=1"></button>
    <h3>Music 3.0 创作者内测</h3><p>欢迎参加内测活动</p>
  </div>
</section>
""", extra_css="""
.responsive-modal{position:fixed;inset:0;z-index:1050;display:flex;align-items:center;justify-content:center}
.modal-panel{background:#fff;padding:36px;width:460px;position:relative;border-radius:12px}
.close-x{position:absolute;right:14px;top:14px;width:24px;height:24px;border:none;background:#eee;cursor:pointer}
""")

# 带 alert 的版本
HTML_ALERT = HTML_MAIN.replace(
    "window.__alertFired=0;",
    "window.__alertFired=0;\nsetTimeout(function(){window.__alertFired=1;alert('系统公告：服务升级');},200);",
)


def _write(tmp, name, body):
    p = Path(tmp) / name
    p.write_text(body, encoding="utf-8")
    return p.as_uri()


# ---------------------------------------------------------------- 测试框架

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))
    return ok


def run_tests():
    # 真实配置（config.json），测试 14 用它跑，确保真实设置下确实有效
    try:
        real_cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception:
        real_cfg = None

    tmp = tempfile.mkdtemp(prefix="pgtest_")
    url_main = _write(tmp, "main.html", HTML_MAIN)
    url_mask = _write(tmp, "mask.html", HTML_MASK)
    url_form = _write(tmp, "form.html", HTML_FORM)
    url_confirm = _write(tmp, "confirm.html", HTML_CONFIRM)
    url_arco = _write(tmp, "arco.html", HTML_ARCO)
    url_danger = _write(tmp, "danger.html", HTML_DANGER)
    url_minimax = _write(tmp, "minimax.html", HTML_MINIMAX)
    url_corner = _write(tmp, "corner.html", HTML_CORNER)
    url_real = _write(tmp, "real.html", HTML_REAL)
    url_real2 = _write(tmp, "real2.html", HTML_REAL2)
    url_alert = _write(tmp, "alert.html", HTML_ALERT)

    with sync_playwright() as p:
        browser = p.chromium.launch()

        # ── 1. 原生 alert，不挂处理器（对照实验）
        print("\n【测试 1】原生 alert —— 不挂处理器会怎样（对照实验）")
        pg = browser.new_page()
        pg.set_default_timeout(4000)
        pg.goto(url_alert)
        pg.wait_for_timeout(800)
        try:
            fired = pg.evaluate("window.__alertFired")
            val = pg.evaluate("1+1")
            check("无处理器时页面仍能响应", val == 2,
                  f"alert 已触发={fired} → Playwright 默认自动关闭原生弹窗")
        except Exception as e:
            check("无处理器时页面仍能响应", False, f"被卡住：{type(e).__name__}")
        pg.close()

        # ── 2. 原生 alert，挂上 guard_page
        print("\n【测试 2】原生 alert —— 挂上 guard_page 处理器")
        pg2 = browser.new_page()
        pg2.set_default_timeout(4000)
        logs = []
        guard_page(pg2, log=logs.append)
        pg2.goto(url_alert)
        pg2.wait_for_timeout(800)
        try:
            val = pg2.evaluate("1+1")
            check("挂处理器后页面正常响应", val == 2)
            hit = any("原生弹窗" in x for x in logs)
            check("处理器捕获并记录原生弹窗", hit, logs[0].strip() if logs else "没捕获到")
        except Exception as e:
            check("挂处理器后页面正常响应", False, f"{type(e).__name__}: {e}")
        pg2.close()

        # ── 3. 常规 DOM 浮层
        print("\n【测试 3】DOM 浮层 —— antd 弹窗 + 文字弹窗")
        pg3 = browser.new_page()
        pg3.set_default_timeout(6000)
        logs3 = []
        guard_page(pg3, log=logs3.append)
        pg3.goto(url_main)
        pg3.wait_for_timeout(400)
        n = dismiss_popups(pg3, log=logs3.append)
        pg3.wait_for_timeout(500)
        c1 = pg3.evaluate("window.__c1 || 0")
        c2 = pg3.evaluate("window.__c2 || 0")
        check("antd 弹窗被关掉（× 按钮）", c1 == 1)
        check('文字弹窗被关掉（「我知道了」）', c2 == 1)
        check("dismiss_popups 报告了处理结果", n > 0, f"共处理 {n} 处")
        pg3.close()

        # ── 4. 被遮挡的按钮
        print("\n【测试 4】被遮挡的按钮 —— click_with_guard")
        pg4 = browser.new_page()
        pg4.set_default_timeout(6000)
        logs4 = []
        guard_page(pg4, log=logs4.append)
        pg4.goto(url_mask)
        pg4.wait_for_timeout(400)
        plain_ok = True
        try:
            pg4.locator("#genBtn").click(timeout=2500)
        except Exception:
            plain_ok = False
        check("复现问题：不清理时普通点击确实失败", not plain_ok)
        got = click_with_guard(pg4, "#genBtn", log=logs4.append, timeout=2500)
        clicks = pg4.evaluate("window.__clicks")
        check("click_with_guard 清掉障碍后点中按钮", got and clicks == 1,
              f"返回={got}，计数={clicks}")
        pg4.close()

        # ── 6. ★ 表单安全（核心回归）
        print("\n【测试 6】★ 表单安全 —— 绝不能删掉已填的内容")
        pg6 = browser.new_page()
        pg6.set_default_timeout(6000)
        logs6 = []
        guard_page(pg6, log=logs6.append)
        pg6.goto(url_form)
        pg6.wait_for_timeout(400)
        before = pg6.evaluate("document.querySelectorAll('.tag').length")
        dismiss_popups(pg6, log=logs6.append)
        pg6.wait_for_timeout(500)
        after = pg6.evaluate("document.querySelectorAll('.tag').length")
        pop_closed = pg6.evaluate("window.__c1 || 0")
        check("已填的 4 个标签一个没少", before == 4 and after == 4,
              f"调用前 {before} 个 → 调用后 {after} 个")
        check("同时真弹窗仍然被关掉", pop_closed == 1)
        pg6.close()

        # ── 7. ★ 确认按钮优先
        print("\n【测试 7】★ 确认优先 —— 「我知道了」必须压过「以后再说」")
        pg7 = browser.new_page()
        pg7.set_default_timeout(6000)
        logs7 = []
        guard_page(pg7, log=logs7.append)
        pg7.goto(url_confirm)
        pg7.wait_for_timeout(400)
        dismiss_popups(pg7, log=logs7.append)
        pg7.wait_for_timeout(500)
        c1 = pg7.evaluate("window.__c1 || 0")
        c2 = pg7.evaluate("window.__c2 || 0")
        check('点的是「我知道了」（确认类）', c1 == 1)
        check('没有误点「以后再说」（关闭类）', c2 == 0)
        pg7.close()

        # ── 8. ★ 遮罩默认不删
        print("\n【测试 8】★ 遮罩默认不删 —— 删遮罩只是兜底手段")
        pg8 = browser.new_page()
        pg8.set_default_timeout(6000)
        logs8 = []
        guard_page(pg8, log=logs8.append)
        pg8.goto(url_mask)
        pg8.wait_for_timeout(400)
        dismiss_popups(pg8, log=logs8.append)
        pg8.wait_for_timeout(400)
        left_default = pg8.evaluate("document.querySelectorAll('#pureMask').length")
        check("默认配置下遮罩被保留（不动它）", left_default == 1,
              f"剩余 {left_default} 层")
        # 显式开启才删
        dismiss_popups(pg8, log=logs8.append, force_mask=True)
        pg8.wait_for_timeout(400)
        left_forced = pg8.evaluate("document.querySelectorAll('#pureMask').length")
        check("显式 force_mask=True 时才移除", left_forced == 0,
              f"剩余 {left_forced} 层")
        pg8.close()

        # ── 11. ★ 登录前不清弹窗
        print("\n【测试 11】★ 未登录时 —— 弹窗一个都不能关（登录框会被关掉）")
        pg11 = browser.new_page()
        pg11.set_default_timeout(6000)
        logs11 = []
        guard_page(pg11, log=logs11.append)
        # dismiss=False：模拟「刚进页面、还没确认登录」
        goto_with_guard(pg11, url_main, log=logs11.append, dismiss=False)
        pg11.wait_for_timeout(600)
        c1 = pg11.evaluate("window.__c1 || 0")
        c2 = pg11.evaluate("window.__c2 || 0")
        still = pg11.evaluate("document.querySelectorAll('#modal1,#textPop').length")
        check("dismiss=False 时弹窗全部保留", c1 == 0 and c2 == 0 and still == 2,
              f"modal1关闭={c1} textPop关闭={c2} 剩余浮层={still}")
        # 确认登录后（dismiss=True）就要能关掉
        goto_with_guard(pg11, url_main, log=logs11.append, dismiss=True)
        pg11.wait_for_timeout(600)
        c1b = pg11.evaluate("window.__c1 || 0")
        c2b = pg11.evaluate("window.__c2 || 0")
        check("登录后再跳（dismiss=True）就能正常清掉", c1b == 1 and c2b == 1,
              f"modal1={c1b} textPop={c2b}")
        pg11.close()

        # ── 12. ★ 模拟 MiniMax：容器 class 非标准，靠"挡住屏幕"认出来
        print("\n【测试 12】★ 非标准容器弹窗（模拟 MiniMax）—— 靠行为识别而非 class 名")
        pg12 = browser.new_page()
        pg12.set_default_timeout(6000)
        logs12 = []
        guard_page(pg12, log=logs12.append)
        pg12.goto(url_minimax)
        pg12.wait_for_timeout(400)
        n = dismiss_popups(pg12, log=logs12.append)
        pg12.wait_for_timeout(500)
        c1 = pg12.evaluate("window.__c1 || 0")
        left = pg12.evaluate("document.querySelectorAll('.music-modal-container').length")
        check("容器不在白名单也能关掉（靠遮挡识别）", c1 == 1, f"关闭={c1}，处理={n} 处")
        # 关掉之后，被挡住的按钮必须能点了
        got = click_with_guard(pg12, "#genBtn", log=logs12.append, timeout=2500)
        clicks = pg12.evaluate("window.__clicks")
        check("关掉之后生成按钮能正常点中", got and clicks == 1,
              f"返回={got}，计数={clicks}")
        pg12.close()

        # ── 13. ★★ 角落兜底：❌ 没有任何 close 类名
        print("\n【测试 13】★★ 角落兜底 —— ❌ 连 close 类名都没有也能关掉")
        pg13 = browser.new_page()
        pg13.set_default_timeout(6000)
        logs13 = []
        guard_page(pg13, log=logs13.append)
        pg13.goto(url_corner)
        pg13.wait_for_timeout(400)
        n = dismiss_popups(pg13, log=logs13.append)
        pg13.wait_for_timeout(500)
        c1 = pg13.evaluate("window.__c1 || 0")
        check("靠「右上角+小尺寸+❌符号」认出关闭按钮", c1 == 1, f"关闭={c1}，处理={n} 处")
        got = click_with_guard(pg13, "#genBtn", log=logs13.append, timeout=2500)
        clicks = pg13.evaluate("window.__clicks")
        check("关掉之后生成按钮能点中", got and clicks == 1, f"计数={clicks}")
        pg13.close()

        # ── 14. ★ MiniMax 真实结构（用 config.json 里的真实配置跑）
        print("\n【测试 14】★ MiniMax 真实结构 —— section.responsive-modal + icon-btn")
        pg14 = browser.new_page()
        pg14.set_default_timeout(6000)
        logs14 = []
        guard_page(pg14, log=logs14.append)
        pg14.goto(url_real)
        pg14.wait_for_timeout(400)
        n = dismiss_popups(pg14, cfg=real_cfg, log=logs14.append)
        pg14.wait_for_timeout(500)
        c1 = pg14.evaluate("window.__c1 || 0")
        check("用真实配置能关掉 MiniMax 弹窗", c1 == 1, f"关闭={c1}，处理={n} 处")
        got = click_with_guard(pg14, "#genBtn", cfg=real_cfg, log=logs14.append, timeout=2500)
        clicks = pg14.evaluate("window.__clicks")
        check("关掉之后生成按钮能点中", got and clicks == 1, f"计数={clicks}")
        pg14.close()

        # ── 15. ★ MiniMax 活动弹窗（aria-label='close'，走选择器直接命中路径）
        print("\n【测试 15】★ MiniMax 活动弹窗 —— button[aria-label='close']")
        pg15 = browser.new_page()
        pg15.set_default_timeout(6000)
        logs15 = []
        guard_page(pg15, log=logs15.append)
        pg15.goto(url_real2)
        pg15.wait_for_timeout(400)
        n = dismiss_popups(pg15, cfg=real_cfg, log=logs15.append)
        pg15.wait_for_timeout(500)
        c1 = pg15.evaluate("window.__c1 || 0")
        check("aria-label='close' 能被选择器直接命中", c1 == 1,
              f"关闭={c1}，处理={n} 处，日志={logs15[0].strip() if logs15 else '无'}")
        pg15.close()

        # ── 9. 番茄/Arco 风格弹窗（footer 主按钮）
        print("\n【测试 9】番茄/Arco 风格 —— footer 主按钮就是确认")
        pg9 = browser.new_page()
        pg9.set_default_timeout(6000)
        logs9 = []
        guard_page(pg9, log=logs9.append)
        pg9.goto(url_arco)
        pg9.wait_for_timeout(400)
        dismiss_popups(pg9, log=logs9.append)
        pg9.wait_for_timeout(500)
        c1 = pg9.evaluate("window.__c1 || 0")
        left = pg9.evaluate("document.querySelectorAll('#cropPop').length")
        check('footer 主按钮（文案「保存」）被点掉', c1 == 1, f"计数={c1}")
        pg9.close()

        # ── 10. ★ 危险弹窗绝不点
        print("\n【测试 10】★ 危险弹窗 —— 「确认删除」宁可不管也不能点")
        pg10 = browser.new_page()
        pg10.set_default_timeout(6000)
        logs10 = []
        guard_page(pg10, log=logs10.append)
        pg10.goto(url_danger)
        pg10.wait_for_timeout(400)
        dismiss_popups(pg10, log=logs10.append)
        pg10.wait_for_timeout(500)
        c1 = pg10.evaluate("window.__c1 || 0")
        check('没有点「确认删除」', c1 == 0, f"计数={c1}（应为 0）")
        pg10.close()

        browser.close()

    # ── 5. 异步版
    print("\n【测试 5】异步版 —— 上传脚本用的是这套")
    asyncio.run(_async_tests(url_main, url_form))


async def _async_tests(url_main, url_form):
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        pg = await browser.new_page()
        pg.set_default_timeout(6000)
        logs = []
        await a_guard_page(pg, log=logs.append)
        await pg.goto(url_main)
        await pg.wait_for_timeout(400)
        n = await a_dismiss_popups(pg, log=logs.append)
        await pg.wait_for_timeout(500)
        c1 = await pg.evaluate("window.__c1 || 0")
        c2 = await pg.evaluate("window.__c2 || 0")
        check("异步版：antd 弹窗被关掉", c1 == 1)
        check('异步版：文字弹窗（「我知道了」）被关掉', c2 == 1)
        check("异步版：报告了处理结果", n > 0, f"共处理 {n} 处")
        await pg.close()

        # 异步版同样必须保护表单（上传脚本走的就是这条路径）
        pg2 = await browser.new_page()
        pg2.set_default_timeout(6000)
        logs2 = []
        await a_guard_page(pg2, log=logs2.append)
        await pg2.goto(url_form)
        await pg2.wait_for_timeout(400)
        before = await pg2.evaluate("document.querySelectorAll('.tag').length")
        await a_dismiss_popups(pg2, log=logs2.append)
        await pg2.wait_for_timeout(500)
        after = await pg2.evaluate("document.querySelectorAll('.tag').length")
        pop_closed = await pg2.evaluate("window.__c1 || 0")
        check("异步版：已填的 4 个标签一个没少", before == 4 and after == 4,
              f"调用前 {before} 个 → 调用后 {after} 个")
        check("异步版：真弹窗仍被关掉", pop_closed == 1)
        await pg2.close()

        await browser.close()


def main():
    print("=" * 68)
    print("弹窗守卫自测（真起 chromium 真点一遍）")
    print("=" * 68)
    try:
        run_tests()
    except Exception as e:
        print(f"\n❌ 测试过程异常：{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 68)
    print(f"结果：{passed}/{total} 项通过")
    if passed == total:
        print("✅ 弹窗守卫工作正常，且不会误伤页面内容")
    else:
        print("❌ 有失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"   - {name}  {detail}")
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
