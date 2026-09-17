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
    # ⚠️ 2026-09-12（用户指出 + 实测）：脚本启动前会 taskkill /f 强杀上一轮浏览器，
    #    Chromium 因此认为"上次非正常退出"，下次启动会弹一个**白色的「是否恢复页面」弹窗**。
    #    它盖在页面上，会把右上角按钮（如编辑器「导出」）的点击吃掉 →
    #    表现为"点了导出没反应、弹窗不出来"。下面这些参数把崩溃恢复气泡/报错弹窗压掉：
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--noerrdialogs",
    "--disable-features=InfiniteSessionRestore",
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


def _win_list_procs():
    """列出 [(pid, exe完整路径)]。纯 ctypes 实现。

    ★ 为什么不用 wmic / tasklist：
      用户环境里这些系统工具被安全策略禁用，且 wmic 在新版 Windows 已移除。
      ctypes 调 kernel32 的进程快照最稳、零依赖。
    """
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    out = []
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE:
            return out
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not k32.Process32FirstW(snap, ctypes.byref(pe)):
                return out
            while True:
                pid = int(pe.th32ProcessID)
                path = ""
                try:
                    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                    if h:
                        buf = ctypes.create_unicode_buffer(2048)
                        size = wintypes.DWORD(2048)
                        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                            path = buf.value
                        k32.CloseHandle(h)
                except Exception:
                    pass
                out.append((pid, path))
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
        finally:
            k32.CloseHandle(snap)
    except Exception:
        pass
    return out


def kill_browsers(verbose=False):
    """★ 2026-09-12 用户明确指出并实测过一次事故 —— 这里原来是一刀切：

        taskkill /f /im chrome.exe      # ← 会连用户**自己正在用的 Chrome** 一起杀掉！

    事故经过：脚本清理残留进程时把用户本人的 Chrome 全杀了（用户当场发现）。
    根因：Playwright 拉起的浏览器进程名也叫 chrome.exe，光看进程名分不清敌我。

    现在改为**按可执行文件路径精确匹配**：只杀 exe 路径里带 ms-playwright 的，
    也就是只有本工具自己拉起来的那些 Playwright Chromium。
    用户自己的 Chrome 在 C:\\Program Files\\Google\\Chrome\\... ，绝不会被碰。

    返回 True 表示至少杀掉了 1 个（调用方据此决定是否再清锁文件）。
    """
    if platform.system() != "Windows":
        # 非 Windows 不做进程清理：靠正常退出 + clear_profile_locks 兜底，
        # 绝不 pkill 全杀用户浏览器。
        return False

    killed = []
    for pid, path in _win_list_procs():
        lp = (path or "").lower().replace("/", "\\")
        if not lp:
            continue
        if not (lp.endswith("\\chrome.exe") or lp.endswith("\\chromium.exe")):
            continue
        if "ms-playwright" not in lp:
            continue                      # 不是本工具的浏览器 → 放过
        try:
            subprocess.run(["taskkill", "/f", "/pid", str(pid)],
                           capture_output=True, timeout=10)
            killed.append(pid)
        except Exception:
            continue

    if verbose and killed:
        print(f"· 已清理 {len(killed)} 个本工具的 Playwright 浏览器进程")
    return bool(killed)


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
