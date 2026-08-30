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
