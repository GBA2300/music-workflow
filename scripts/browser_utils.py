# -*- coding: utf-8 -*-
"""跨平台的浏览器清理工具（Windows / macOS / Linux 通用）。

为什么需要它：
    Playwright 用的是「持久化 profile 目录」保存登录态。如果上一次的浏览器没关干净，
    profile 会被 SingletonLock 锁住，导致下次启动报「profile 已被占用」或直接卡住。
    因此每次启动前要清理残留进程 + 锁文件。

    Windows 用 taskkill，macOS/Linux 用 pkill —— 各平台命令不同，统一封装在这里，
    避免脚本在别人电脑上（尤其是 Mac）静默失效。
"""
import os
import platform
import subprocess

# Chromium 系进程名（各平台可能不同，都列出来）
PROCESS_NAMES = ["chrome", "chrome.exe", "chromium", "chromium.exe",
                 "Google Chrome", "msedge", "msedge.exe"]

# 反自动化 + 常规参数（各脚本统一从这里取，别各自复制）
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--lang=zh-CN",
]


def window_args(headless=False):
    """统一的浏览器窗口参数，返回 args 列表。

    有头模式：加 --start-maximized（最大化窗口）。
    配合 viewport_for() 的 viewport=None，页面就会**跟随窗口大小自适应**——
    不同分辨率、不同 DPI 缩放的设备打开后布局都完整，不会出现
    "固定 1440×900 视口在小屏上溢出、底部按钮被挤出屏幕点不到"的问题。

    headless 模式没有窗口，不传 --start-maximized。
    """
    args = list(STEALTH_ARGS)
    if not headless:
        args.append("--start-maximized")
    return args


def viewport_for(headless=False, fallback=(1440, 900)):
    """launch_persistent_context 的 viewport 参数。

    有头模式返回 None —— 不固定视口，页面按最大化窗口的实际尺寸渲染（自适应）。
    headless 模式没有窗口，必须给一个固定视口兜底。
    """
    if headless:
        return {"width": fallback[0], "height": fallback[1]}
    return None


def scroll_into_view(page, locator, timeout=1500):
    """把元素滚到可视区域内（防"底部按钮在视口外点不到"）。

    页面自适应窗口后，长页面底部的内容可能还在视口外，
    点击前先滚一下；滚不动（比如被 fixed 层盖住）也不抛异常，
    由调用方继续处理。
    """
    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
        return True
    except Exception:
        return False


def kill_browsers(verbose=False):
    """关掉残留的 Chromium/Chrome/Edge 进程。返回是否执行成功（失败也不抛异常）。"""
    system = platform.system()
    ok = False
    for name in PROCESS_NAMES:
        try:
            if system == "Windows":
                cmd = ["taskkill", "/f", "/im", name]
            else:  # Darwin(macOS) / Linux
                cmd = ["pkill", "-f", name]
            r = subprocess.run(cmd, capture_output=True, timeout=10)
            if r.returncode == 0:
                ok = True
        except FileNotFoundError:
            # 该平台没有这个命令（比如 macOS 上跑 taskkill），直接跳过
            break
        except Exception:
            continue
    if verbose and ok:
        print("· 已清理残留浏览器进程")
    return ok


def clear_profile_locks(profile_dir):
    """删掉 profile 目录里的 Singleton* 锁文件。"""
    removed = []
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = os.path.join(str(profile_dir), name)
        try:
            if os.path.exists(p):
                os.remove(p)
                removed.append(name)
        except Exception:
            pass
    return removed


def cleanup(profile_dir=None, verbose=False):
    """启动浏览器前的标准清理：关残留进程 + 删锁文件。"""
    kill_browsers(verbose=verbose)
    if profile_dir:
        clear_profile_locks(profile_dir)


if __name__ == "__main__":
    # 单独运行：python browser_utils.py   → 只做一次清理，方便排错
    print(f"当前系统：{platform.system()}")
    cleanup(verbose=True)
    print("✓ 清理完成")
