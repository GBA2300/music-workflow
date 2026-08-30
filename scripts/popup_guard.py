# -*- coding: utf-8 -*-
"""
弹窗守卫 popup_guard.py —— 让自动化"看见弹窗就自己关掉"

为什么需要它
------------
MiniMax 音乐创作页、番茄音频上传页会不定时弹出浮层（活动公告、新功能引导、
会员推广、额度提醒、Cookie 提示、版本更新……）。这类浮层有两种，后果都很致命：

1. **DOM 浮层**（div 做的弹窗）：盖在按钮上方，Playwright 点击时直接报
   `element intercepts pointer events`，流程断在这一步，后面的歌全都不生成了。
   **这是真正会卡住流水线的一种**，本模块主要就是治它。
   最麻烦的是那种"整屏半透明遮罩、还没有关闭按钮"的——它连关闭按钮自己都挡住，
   所以本模块的策略是：点不动按钮时，先把这类纯遮罩铲掉，再回去点关闭按钮。

2. **原生 JS 弹窗**（alert / confirm / prompt）：严格说 Playwright 在**没有**
   注册 dialog 监听器时会自动关掉它们，所以它们通常不会让页面卡死（这一点已用
   test_popup_guard.py 的对照实验验证过）。但仍然值得挂处理器，因为：
     - 能在日志里看到弹窗说了什么（比如"额度用尽""需要验证"），排查问题时很有用；
     - `beforeunload`（离开页面确认）不会被自动关闭，它会真的把流程卡住；
     - 一旦别处注册了 dialog 监听器却又没处理，页面就会永久冻结——
       我们自己注册一个明确关闭的处理器，等于把这个雷排掉。

设计要点
--------
- **两种弹窗都管**：`guard_page()` 管原生弹窗，`dismiss_popups()` 管 DOM 浮层。
- **选择器可配置**：关不掉的弹窗往 `config.json → popup_guard.close_selectors`
  最前面加新选择器即可，不用改代码（页面改版时尤其省事）。
- **只关"关闭按钮"，不乱点**：优先点 × 图标（aria-label / antd 的 close 类），
  文字类按钮（"我知道了"/"取消"）排在后面，且可用配置关掉。
- **点击失败自动重试**：`click_with_guard()` 遇到"被挡住"的错误会先清弹窗再点一次。
- **最后兜底**：真遇到"整屏透明遮罩、还没有关闭按钮"的极端情况，
  会移除纯遮罩层（只删内部没有任何可交互元素的那种，不会误删真弹窗）。

用法
----
    from popup_guard import guard_page, dismiss_popups, click_with_guard

    guard_page(page, log=log)              # 每个 page 挂一次
    dismiss_popups(page, cfg, log=log)     # 每次 goto 之后、每次关键点击之前
    click_with_guard(page, sel, log=log)   # 点不动就自动清弹窗再重试

同步版给 generate.py 用，异步版（a* 开头）给 fanqie_upload.py 用。
"""
import time

# ---------------------------------------------------------------- 默认选择器

# 图标类关闭按钮（纯 CSS 选择器，可用 document.querySelectorAll 直接跑，速度快）
DEFAULT_CLOSE_SELECTORS = [
    # Ant Design —— MiniMax 和番茄平台都用这套组件库
    ".ant-modal-close",
    ".ant-drawer-close",
    ".ant-notification-notice-close",
    ".ant-message-notice-content .ant-message-custom-content + .anticon-close",
    ".ant-modal-root .ant-modal-close",
    ".ant-image-preview-close",
    ".ant-popover .ant-popover-close",
    # aria-label 标注的关闭按钮（最通用、最可靠）
    "button[aria-label='关闭']",
    "button[aria-label='Close']",
    "button[aria-label='close']",
    "[aria-label='关闭']",
    "[aria-label='Close']",
    "[aria-label='close']",
    "[aria-label='关闭弹窗']",
    "[aria-label='关闭窗口']",
    "[title='关闭']",
    "[title='Close']",
    # 常见 class 命名
    ".modal-close",
    ".dialog-close",
    ".popup-close",
    ".drawer-close",
    ".close-btn",
    ".closeBtn",
    ".btn-close",
    "[class*='close-icon']",
    "[class*='closeIcon']",
    "[class*='close-btn']",
    "[class*='closeBtn']",
    "[class*='-close']",
    "[class*='Close']",
    "[class*='modal__close']",
    "[class*='popup__close']",
]

# 文字类关闭按钮（找不到图标按钮时才考虑，按配置开关控制）
DEFAULT_TEXT_SELECTORS = [
    "我知道了",
    "我知道啦",
    "我已知晓",
    "好的",
    "明白",
    "不再提示",
    "以后再说",
    "稍后再说",
    "下次再说",
    "暂不需要",
    "暂不",
    "跳过",
    "关闭",
    "取消",
    "×",
    "✕",
]

# ---------------------------------------------------------------- 内部 JS

# 第一遍：找图标类关闭按钮，给它们打标记，一次网络往返搞定（比逐个 locator 快得多）
_JS_FIND_ICON = """
(sels) => {
  const found = [];
  const vw = window.innerWidth, vh = window.innerHeight;
  for (const sel of sels) {
    let nodes;
    try { nodes = document.querySelectorAll(sel); } catch (e) { continue; }
    for (const el of nodes) {
      if (el.hasAttribute('data-pg-close')) continue;
      let r, st;
      try {
        r = el.getBoundingClientRect();
        st = getComputedStyle(el);
      } catch (e) { continue; }
      if (!r || r.width < 4 || r.height < 4) continue;
      // 尺寸上限：关闭按钮都很小。像 [class*='-close'] 这类宽泛选择器
      // 有可能命中整屏容器，点下去等于乱点，必须挡掉。
      if (r.width > vw * 0.5 || r.height > vh * 0.5) continue;
      if (!st || st.display === 'none' || st.visibility === 'hidden') continue;
      if (parseFloat(st.opacity || '1') < 0.05) continue;
      // 命中的可能是 svg 图标本身，往上找到真正能点的按钮
      let target = el;
      if (el.tagName !== 'BUTTON' && el.tagName !== 'A') {
        const up = el.closest('button, a, [role="button"]');
        if (up) target = up;
      }
      if (target.hasAttribute('data-pg-close')) continue;
      target.setAttribute('data-pg-close', '1');
      found.push(sel);
    }
  }
  return found;
}
"""

# 第二遍：找文字类按钮。先收精确匹配，没有再用"包含匹配"（且限短文本，避免误伤大按钮）
_JS_FIND_TEXT = """
(keys) => {
  const exact = [], fuzzy = [];
  const vw = window.innerWidth, vh = window.innerHeight;
  const nodes = document.querySelectorAll('button, a[role="button"], [role="button"], .ant-btn');
  for (const el of nodes) {
    if (el.hasAttribute('data-pg-text')) continue;
    let r, st;
    try {
      r = el.getBoundingClientRect();
      st = getComputedStyle(el);
    } catch (e) { continue; }
    if (!r || r.width < 8 || r.height < 8) continue;
    // 同样加尺寸上限，避免"取消"这类常见词命中整屏容器或页面主按钮
    if (r.width > vw * 0.5 || r.height > vh * 0.5) continue;
    if (!st || st.display === 'none' || st.visibility === 'hidden') continue;
    const t = ((el.innerText || el.textContent) || '').trim();
    if (!t) continue;
    for (const k of keys) {
      if (t === k) { exact.push([el, k]); break; }
      if (t.length <= 10 && t.indexOf(k) >= 0) { fuzzy.push([el, k]); break; }
    }
  }
  const chosen = exact.length ? exact : fuzzy;
  const out = [];
  for (const [el, k] of chosen) {
    el.setAttribute('data-pg-text', k);
    out.push(k);
  }
  return out;
}
"""

# 兜底：删掉"盖满整屏、且内部没有任何可交互元素"的纯遮罩。
# 真弹窗（里面有按钮/输入框）会被跳过，不会被误删。
_JS_DROP_MASKS = """
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const removed = [];
  let els;
  try { els = document.querySelectorAll('body *'); } catch (e) { return removed; }
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
    if (!r || r.width < vw * 0.6 || r.height < vh * 0.6) continue;
    // 里面还有能点的东西 → 说明是真正的对话框，不动它
    if (el.querySelector('button, input, textarea, select, a, [role="button"]')) continue;
    removed.push((el.className || '').toString().slice(0, 50));
    try { el.remove(); } catch (e) {}
  }
  return removed;
}
"""

_JS_CLEANUP = """
() => {
  document.querySelectorAll('[data-pg-close],[data-pg-text]').forEach(e => {
    e.removeAttribute('data-pg-close');
    e.removeAttribute('data-pg-text');
  });
}
"""


# ---------------------------------------------------------------- 配置

def load_guard_config(cfg):
    """从 config.json 里取弹窗守卫的配置，缺省值兜底"""
    g = (cfg or {}).get("popup_guard") or {}
    return {
        "enabled": bool(g.get("enabled", True)),
        "close_selectors": list(g.get("close_selectors") or DEFAULT_CLOSE_SELECTORS),
        "dismiss_text_selectors": list(g.get("dismiss_text_selectors") or DEFAULT_TEXT_SELECTORS),
        "allow_text_buttons": bool(g.get("allow_text_buttons", True)),
        "max_rounds": max(1, int(g.get("max_rounds", 3))),
        "press_escape": bool(g.get("press_escape", True)),
        "force_remove_blocking_mask": bool(g.get("force_remove_blocking_mask", True)),
        "dismiss_after_goto_ms": int(g.get("dismiss_after_goto_ms", 1500)),
    }


# ---------------------------------------------------------------- 原生弹窗

def guard_page(page, log=None):
    """给页面挂上原生 JS 弹窗（alert / confirm / prompt）的自动处理器。

    这一步是**必须的**：Playwright 默认不处理原生弹窗，页面会一直挂起。
    每个新开的 page 都要挂一次。
    """
    def _on_dialog(dialog):
        try:
            msg = (dialog.message or "").replace("\n", " ")[:80]
        except Exception:
            msg = ""
        if log:
            log(f"  ⚑ 自动关闭原生弹窗（{dialog.type}）：{msg}")
        try:
            dialog.dismiss()
        except Exception:
            pass

    try:
        page.on("dialog", _on_dialog)
    except Exception:
        pass
    return page


def guard_context(ctx, log=None):
    """给 context 里已有的、以及将来新开的 page 都挂上弹窗处理器。

    网站经常用 window.open 弹出新窗口（比如授权页、支付页），
    不监听新 page 的话，那些窗口里的弹窗照样会把流程卡死。
    """
    try:
        for pg in ctx.pages:
            guard_page(pg, log=log)
    except Exception:
        pass

    def _on_new_page(pg):
        guard_page(pg, log=log)

    try:
        ctx.on("page", _on_new_page)
    except Exception:
        pass
    return ctx


# ---------------------------------------------------------------- DOM 浮层

def _click_tagged(page, attr, limit, log, kind):
    """点击被打过标记的元素；点不动就摘掉标记，避免下一轮死循环"""
    closed = 0
    try:
        els = page.query_selector_all(f"[{attr}]")
    except Exception:
        return 0
    for el in els[:limit]:
        try:
            el.click(timeout=1500)
            closed += 1
            if log:
                log(f"  ✓ 已关闭一个弹窗（{kind}）")
            time.sleep(0.35)
        except Exception:
            pass
        try:
            el.evaluate("e => { e.removeAttribute('data-pg-close'); e.removeAttribute('data-pg-text'); }")
        except Exception:
            pass
    return closed


def _drop_masks(page, log):
    try:
        removed = page.evaluate(_JS_DROP_MASKS)
    except Exception:
        return 0
    if removed and log:
        names = [r for r in removed if r][:2]
        log(f"  ✓ 已移除 {len(removed)} 层整屏遮罩{f'（{names}）' if names else ''}")
    return len(removed or [])


def dismiss_popups(page, cfg=None, log=None, force_mask=False):
    """关掉页面上所有能关的浮层弹窗，返回关掉的数量。

    调用时机：每次 goto 之后、每次关键点击之前。
    也可以在任何"卡住"的地方随手调一下，它是幂等的（没弹窗时几乎零开销）。
    """
    g = load_guard_config(cfg)
    if not g["enabled"]:
        return 0

    total = 0
    try:
        for rnd in range(g["max_rounds"]):
            round_hit = 0

            # ① 图标类关闭按钮（× 等）—— 最直接，优先试
            try:
                page.evaluate(_JS_FIND_ICON, g["close_selectors"])
            except Exception:
                pass
            round_hit += _click_tagged(page, "data-pg-close", 6, log, "关闭按钮")

            # ② 文字类按钮（"我知道了"/"取消"等），只在没点到图标按钮时才试
            if round_hit == 0 and g["allow_text_buttons"]:
                try:
                    page.evaluate(_JS_FIND_TEXT, g["dismiss_text_selectors"])
                except Exception:
                    pass
                round_hit += _click_tagged(page, "data-pg-text", 2, log, "文字按钮")

            # ③ Esc 键：有些引导层吃这一套，成本为零
            if round_hit == 0 and g["press_escape"] and rnd == 0:
                try:
                    page.keyboard.press("Escape")
                    time.sleep(0.3)
                except Exception:
                    pass

            # ④ 关键：整屏遮罩会挡住【所有】点击 —— 包括关闭按钮自己。
            #    所以点不动时，必须先把这种"纯遮罩"铲掉，再回到下一轮重新点按钮。
            #    这一条放在每一轮的末尾（而不是某一次性的兜底），否则第一轮
            #    就会因为"按钮被遮罩挡住点不动"而空手而归，后面全部作废。
            #    只删内部没有任何可交互元素的遮罩，真正的对话框会被跳过、不会误删。
            if round_hit == 0 and g["force_remove_blocking_mask"]:
                round_hit += _drop_masks(page, log)

            try:
                page.evaluate(_JS_CLEANUP)
            except Exception:
                pass

            # 一整轮下来什么都做不了，才算真的没弹窗
            if round_hit == 0:
                break
            total += round_hit
            time.sleep(0.4)
    except Exception:
        # 守卫本身绝不能把主流程搞崩
        try:
            page.evaluate(_JS_CLEANUP)
        except Exception:
            pass
    return total


def goto_with_guard(page, url, cfg=None, log=None, wait_until="domcontentloaded",
                    timeout=60000, settle_sec=0.0):
    """goto 之后自动清弹窗。替换掉脚本里所有的裸 page.goto。"""
    try:
        page.goto(url, wait_until=wait_until, timeout=timeout)
    except Exception:
        pass
    if settle_sec:
        time.sleep(settle_sec)
    time.sleep(load_guard_config(cfg)["dismiss_after_goto_ms"] / 1000.0)
    dismiss_popups(page, cfg=cfg, log=log)
    return page


# 判断 Playwright 报错是不是"被挡住了"
_BLOCKED_HINTS = (
    "intercepts pointer events",
    "element is not visible",
    "not clickable",
    "timeout",
    "detached from the dom",
    "element is outside of the viewport",
)


def _is_blocked_err(msg):
    m = (msg or "").lower()
    return any(h in m for h in _BLOCKED_HINTS)


def click_with_guard(page, selector, cfg=None, log=None, timeout=3000, retries=2):
    """点一个元素；如果被弹窗挡住，先清弹窗再重试。

    返回 True/False。generate.py / fanqie_upload.py 里凡是"点了就没下文"的
    关键按钮（尤其是生成按钮、上传按钮），都应该换成这个。
    """
    for attempt in range(retries + 1):
        try:
            loc = page.locator(selector).first
            loc.wait_for(state="visible", timeout=timeout)
            loc.click(timeout=timeout)
            return True
        except Exception as e:
            if attempt >= retries or not _is_blocked_err(str(e)):
                return False
            if log:
                log(f"  ⚠ 点击被挡住（{selector}），先清弹窗再重试…")
            dismiss_popups(page, cfg=cfg, log=log, force_mask=True)
            time.sleep(0.5)
    return False


# ---------------------------------------------------------------- 异步版
# fanqie_upload.py 用的是 playwright 的 async API，这里给一套同样逻辑的异步实现。

async def a_guard_page(page, log=None):
    def _on_dialog(dialog):
        try:
            msg = (dialog.message or "").replace("\n", " ")[:80]
        except Exception:
            msg = ""
        if log:
            log(f"  ⚑ 自动关闭原生弹窗（{dialog.type}）：{msg}")
        try:
            dialog.dismiss()
        except Exception:
            pass

    try:
        page.on("dialog", _on_dialog)
    except Exception:
        pass
    return page


async def a_guard_context(ctx, log=None):
    try:
        for pg in ctx.pages:
            await a_guard_page(pg, log=log)
    except Exception:
        pass

    def _on_new_page(pg):
        try:
            import asyncio
            asyncio.ensure_future(a_guard_page(pg, log=log))
        except Exception:
            pass

    try:
        ctx.on("page", _on_new_page)
    except Exception:
        pass
    return ctx


async def _a_click_tagged(page, attr, limit, log, kind):
    closed = 0
    try:
        els = await page.query_selector_all(f"[{attr}]")
    except Exception:
        return 0
    import asyncio
    for el in els[:limit]:
        try:
            await el.click(timeout=1500)
            closed += 1
            if log:
                log(f"  ✓ 已关闭一个弹窗（{kind}）")
            await asyncio.sleep(0.35)
        except Exception:
            pass
        try:
            await el.evaluate("e => { e.removeAttribute('data-pg-close'); e.removeAttribute('data-pg-text'); }")
        except Exception:
            pass
    return closed


async def a_dismiss_popups(page, cfg=None, log=None, force_mask=False):
    """dismiss_popups 的异步版，逻辑完全一致"""
    import asyncio
    g = load_guard_config(cfg)
    if not g["enabled"]:
        return 0

    total = 0
    try:
        for rnd in range(g["max_rounds"]):
            round_hit = 0

            try:
                await page.evaluate(_JS_FIND_ICON, g["close_selectors"])
            except Exception:
                pass
            round_hit += await _a_click_tagged(page, "data-pg-close", 6, log, "关闭按钮")

            if round_hit == 0 and g["allow_text_buttons"]:
                try:
                    await page.evaluate(_JS_FIND_TEXT, g["dismiss_text_selectors"])
                except Exception:
                    pass
                round_hit += await _a_click_tagged(page, "data-pg-text", 2, log, "文字按钮")

            if round_hit == 0 and g["press_escape"] and rnd == 0:
                try:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

            # 与同步版一致：遮罩会挡住关闭按钮自己，必须纳入每轮的升级链
            if round_hit == 0 and g["force_remove_blocking_mask"]:
                try:
                    removed = await page.evaluate(_JS_DROP_MASKS)
                    if removed and log:
                        names = [r for r in removed if r][:2]
                        log(f"  ✓ 已移除 {len(removed)} 层整屏遮罩{f'（{names}）' if names else ''}")
                    round_hit += len(removed or [])
                except Exception:
                    pass

            try:
                await page.evaluate(_JS_CLEANUP)
            except Exception:
                pass

            if round_hit == 0:
                break
            total += round_hit
            await asyncio.sleep(0.4)
    except Exception:
        try:
            await page.evaluate(_JS_CLEANUP)
        except Exception:
            pass
    return total


async def a_goto_with_guard(page, url, cfg=None, log=None, wait_until="domcontentloaded",
                            timeout=60000, settle_sec=0.0):
    """goto_with_guard 的异步版"""
    import asyncio
    try:
        await page.goto(url, wait_until=wait_until, timeout=timeout)
    except Exception:
        pass
    if settle_sec:
        await asyncio.sleep(settle_sec)
    await asyncio.sleep(load_guard_config(cfg)["dismiss_after_goto_ms"] / 1000.0)
    await a_dismiss_popups(page, cfg=cfg, log=log)
    return page


async def a_click_with_guard(page, selector, cfg=None, log=None, timeout=3000, retries=2):
    """click_with_guard 的异步版"""
    import asyncio
    for attempt in range(retries + 1):
        try:
            loc = page.locator(selector).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click(timeout=timeout)
            return True
        except Exception as e:
            if attempt >= retries or not _is_blocked_err(str(e)):
                return False
            if log:
                log(f"  ⚠ 点击被挡住（{selector}），先清弹窗再重试…")
            await a_dismiss_popups(page, cfg=cfg, log=log, force_mask=True)
            await asyncio.sleep(0.5)
    return False
