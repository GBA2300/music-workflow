# -*- coding: utf-8 -*-
"""
登录诊断 / 登录辅助工具 —— 专治「点了登录没反应」
==========================================================
用法（在你的工作目录里运行）：
    python login_check.py minimax    # 登录 MiniMax（生成音乐用）
    python login_check.py fanqie     # 登录 番茄音频创作平台（发布用）

比普通 `--login` 多做了 4 件事，这 4 件正是「点登录没反应」的常见原因：

 1. **反自动化检测**：去掉 Chromium 的 `--enable-automation` 标记、
    把 `navigator.webdriver` 改成 undefined、补上真实 UA 和语言。
    国内平台的风控一旦识别出是自动化浏览器，常见表现就是
    「登录按钮点了没反应 / 弹窗一闪就消失」。
 2. **弹窗捕获并置前**：微信扫码、第三方授权常开新标签页或新窗口，
    脚本会把它抓住并自动切到前台，不会藏到后面去。
 3. **自动截图**：每 5 秒把当前画面存成 `_login_shot.png`
    （另存带时间戳的历史图到 `_login_shots/`），
    出问题时可以直接把图给 AI 看，不用你描述。
 4. **自动判定登录成功**：不需要回到黑窗口按任何键。

登录态保存在工作目录的 profile 文件夹里（MiniMax→`profile/`，
番茄→`profile_fanqie/`），只属于你本机，不会外泄。
"""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
SHOT = ROOT / "_login_shot.png"
SHOT_DIR = ROOT / "_login_shots"

# 真实 Chrome 的 UA（避免暴露 HeadlessChrome / 自动化特征）
REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# 关掉自动化特征的启动参数
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--lang=zh-CN",
]

# 注入到每个页面最前面执行，抹掉 webdriver 等指纹
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
window.chrome = window.chrome || { runtime: {} };
const _q = navigator.permissions && navigator.permissions.query;
if (_q) {
  navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : _q(p)
  );
}
"""

def _cfg():
    """读取同目录 config.json（读不到就返回空字典）。"""
    try:
        with open(ROOT / "config.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_CFG = _cfg()

TARGETS = {
    "minimax": {
        "name": "MiniMax 音乐",
        # 以 config.json 的 base_url 为准，保证和 generate.py 打开的是同一页
        "url": _CFG.get("base_url") or "https://www.minimaxi.com/audio/music",
        "profile": _CFG.get("profile_dir") or "profile",
        # 这些文字还看得见 => 说明还没登录
        "login_marks": [
            "text=登录", "text=立即登录", "text=Log in", "text=Sign in",
        ],
        # 出现这些 => 说明已登录
        "ok_marks": [
            "text=创作", "text=我的作品", "text=生成", "textarea",
        ],
    },
    "fanqie": {
        "name": "番茄音频创作平台",
        "url": "https://www.novelfm.com/creator/music/finished/ugc/uploadProduct",
        "profile": "profile_fanqie",
        "login_marks": [
            "text=登录", "text=手机号登录", "text=扫码登录",
        ],
        "ok_marks": [
            "text=添加歌曲", "text=上传作品", "text=作品管理",
        ],
    },
}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def kill_chrome():
    """清掉残留 chrome，避免 profile 被锁住打不开。跨平台（Windows/macOS/Linux）。"""
    try:
        from browser_utils import kill_browsers
        if kill_browsers():
            return
    except Exception:
        pass
    # 兜底：Windows 原生命令
    try:
        subprocess.run(["taskkill", "/f", "/im", "chrome.exe"],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def unlock_profile(profile_dir: Path):
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = profile_dir / name
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass


def shoot(page, tag=""):
    """截图：覆盖 _login_shot.png，并存一张带时间戳的历史图。"""
    try:
        SHOT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOT), full_page=False)
        stamp = datetime.now().strftime("%H%M%S")
        shutil.copy(SHOT, SHOT_DIR / f"{stamp}{('_' + tag) if tag else ''}.png")
    except Exception as e:
        log(f"  (截图失败: {e})")


def visible_any(page, selectors):
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=600):
                return sel
        except Exception:
            continue
    return None


def describe(page):
    """打印页面关键信息，方便判断卡在哪。"""
    try:
        url = page.url
        title = page.title()
        txt = page.evaluate("document.body ? document.body.innerText : ''")
        txt = " / ".join([t.strip() for t in txt.split("\n") if t.strip()][:12])
        wd = page.evaluate("String(navigator.webdriver)")
        log(f"  URL: {url}")
        log(f"  标题: {title}")
        log(f"  navigator.webdriver = {wd}  (期望 undefined)")
        log(f"  页面文字前几行: {txt[:200]}")
    except Exception as e:
        log(f"  (读取页面信息失败: {e})")


def run(target_key: str, minutes: int = 20):
    if target_key not in TARGETS:
        print(f"不认识的平台: {target_key}，可选: {', '.join(TARGETS)}")
        return 2

    t = TARGETS[target_key]
    profile_dir = ROOT / t["profile"]
    profile_dir.mkdir(parents=True, exist_ok=True)

    kill_chrome()
    unlock_profile(profile_dir)

    log(f"目标平台：{t['name']}")
    log(f"登录态目录：{profile_dir}")
    log("已开启反自动化检测（去掉 webdriver 标记 + 真实 UA）")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=STEALTH_ARGS,
            user_agent=REAL_UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport=None,          # 跟随窗口，避免固定小视口暴露特征
            ignore_default_args=["--enable-automation"],
        )
        ctx.add_init_script(STEALTH_JS)

        pages = {"cur": ctx.pages[0] if ctx.pages else ctx.new_page()}

        def on_page(new_page):
            """弹窗/新标签页：抓住并置前，扫码窗口不会跑到后面。"""
            log(f"  ⚑ 检测到新窗口/标签页：{new_page.url or '(about:blank)'}")
            try:
                new_page.bring_to_front()
            except Exception:
                pass
            pages["cur"] = new_page

        ctx.on("page", on_page)

        page = pages["cur"]
        page.set_default_timeout(20000)
        log(f"正在打开：{t['url']}")
        try:
            page.goto(t["url"], wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"⚠️ 打开页面失败（检查网络/代理）: {e}")

        time.sleep(5)
        describe(page)
        shoot(page, "opened")
        log(f"已保存首屏截图：{SHOT.name}")

        print("\n" + "=" * 62)
        print(f"请在弹出的浏览器里登录【{t['name']}】（手机号 / 扫码都行）。")
        print("登录成功后脚本会自动检测并保存，不用回到这个窗口按任何键。")
        print(f"每 5 秒会自动截图到 {SHOT.name}，卡住了可以把图发给 AI 看。")
        print("=" * 62 + "\n")

        deadline = time.time() + minutes * 60
        n = 0
        while time.time() < deadline:
            page = pages["cur"]
            n += 1
            try:
                ok_sel = visible_any(page, t["ok_marks"])
                login_sel = visible_any(page, t["login_marks"])
                if ok_sel and not login_sel:
                    log(f"✅ 已登录（命中标志：{ok_sel}）")
                    shoot(page, "logged_in")
                    break
                if n % 3 == 1:   # 每 ~15 秒报一次状态 + 截图
                    state = "未登录" if login_sel else "判定中"
                    log(f"  等待登录…（{state}；登录入口: {login_sel}；已登录标志: {ok_sel}）")
                    shoot(page, f"wait{n}")
            except Exception as e:
                log(f"  (检测异常: {e})")
            time.sleep(5)
        else:
            log("⚠️ 等待登录超时。请看 _login_shot.png 确认卡在哪一步。")

        # 额外保存一份 storage_state，便于排查
        try:
            ctx.storage_state(path=str(ROOT / f"storage_state_{target_key}.json"))
        except Exception:
            pass

        describe(page)
        shoot(page, "final")
        log(f"登录态已保存到：{profile_dir}")
        log("浏览器 3 秒后关闭…")
        time.sleep(3)
        ctx.close()
    return 0


if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else "minimax"
    sys.exit(run(key))
