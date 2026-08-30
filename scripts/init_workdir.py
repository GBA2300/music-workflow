# -*- coding: utf-8 -*-
"""
music-workflow —— 一键初始化工作目录
==================================================================
为【每一位使用者】创建一套独立、干净、零配置的工作目录：

  <工作目录>/
    generate.py          MiniMax 生成+下载+出封面（含 --login）
    cover.py             封面生成器
    fanqie_upload.py      番茄上传填表+授权（含 --login / --mark-published）
    init_workdir.py       本脚本（可重复运行）
    login_check.py        登录诊断/登录（反自动化+自动截图，专治「点登录没反应」）
    probe_generate.py     生成探针（查「点生成后等不到歌」）
    inspect_buttons.py    按钮探测器（查按钮 selector 该怎么写）
    browser_utils.py      跨平台浏览器清理（Windows/macOS/Linux 通用）
    config.json          平台 URL、选择器、封面参数
    tasks.csv            歌单模板（改这里写你自己的歌）
    requirements.txt     playwright, pillow
    library/             曲库（初始为空）
    lyrics/              歌词 txt（初始为空）
    profile/             MiniMax 登录态（初始为空，首次 --login 自建）
    profile_fanqie/      番茄登录态（初始为空，首次 --login 自建）
    published.json       已发布记录（初始 {}，防止重复发布）

★ 安全：绝不复制任何人的登录态。profile*/ 永远初始为空，由各人自己登录。

用法：
    python init_workdir.py                 # 在当前目录新建 ./music-workflow/
    python init_workdir.py D:/my-music     # 指定目录
    python init_workdir.py --check         # 只检查依赖(playwright/pillow)是否就绪
"""
import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# 需要复制进工作目录的文件（脚本 + 配置 + 模板）
COPY_FILES = [
    "generate.py",
    "cover.py",
    "fanqie_upload.py",
    "init_workdir.py",
    "login_check.py",
    "probe_generate.py",
    "inspect_buttons.py",
    "browser_utils.py",
    "config.json",
    "tasks.csv",
    "requirements.txt",
]

# 需要创建的空目录
MAKE_DIRS = ["library", "lyrics", "profile", "profile_fanqie"]


def check_deps():
    """检查 playwright / pillow 是否可导入；返回 (ok, msg)。"""
    problems = []
    try:
        import playwright  # noqa: F401
    except Exception:
        problems.append("playwright 未安装")
    try:
        import PIL  # noqa: F401
    except Exception:
        problems.append("pillow 未安装")
    return problems


def main():
    ap = argparse.ArgumentParser(description="music-workflow 一键初始化工作目录（多账号通用，不含任何登录态）")
    ap.add_argument("workdir", nargs="?", default="music-workflow",
                    help="工作目录路径（默认当前目录下的 ./music-workflow）")
    ap.add_argument("--check", action="store_true",
                    help="只检查依赖是否就绪，不创建目录")
    args = ap.parse_args()

    # ── 依赖检查 ──
    problems = check_deps()
    py_exe = sys.executable
    if problems:
        print("⚠️ 缺少依赖，请先执行：")
        print(f"    {py_exe} -m pip install -r {os.path.join(ROOT, 'requirements.txt')}")
        print(f"    {py_exe} -m playwright install chromium")
        for p in problems:
            print("   - " + p)
        if args.check:
            return
        print("（仍会继续创建目录；但首次运行前请先装好依赖）")
    else:
        print("✅ 依赖就绪：playwright / pillow 均可导入")

    if args.check:
        return

    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)

    # ── 复制脚本与配置 ──
    for f in COPY_FILES:
        src = os.path.join(ROOT, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(workdir, f))
        else:
            print(f"   (跳过缺失文件: {f})")

    # ── 创建空目录 ──
    for d in MAKE_DIRS:
        os.makedirs(os.path.join(workdir, d), exist_ok=True)

    # ── 初始化 published.json（空，绝不含任何人的发布记录）──
    pj = os.path.join(workdir, "published.json")
    if not os.path.exists(pj):
        with open(pj, "w", encoding="utf-8") as fh:
            json.dump({}, fh, ensure_ascii=False, indent=2)

    # ── 结果 ──
    print("")
    print(f"✅ 工作目录已初始化：{workdir}")
    print("   包含脚本：generate.py / cover.py / fanqie_upload.py / init_workdir.py")
    print("   空目录（登录态留空，等你自己的账号）：library/ lyrics/ profile/ profile_fanqie/")
    print("   已创建：tasks.csv（歌单模板）、published.json（空）")
    print("")
    print("接下来三步：")
    print(f"   1) cd \"{workdir}\"")
    print(f"   2) {py_exe} generate.py --login        # 用你的账号登录 MiniMax")
    print(f"   3) {py_exe} fanqie_upload.py --login   # 用你的番茄账号登录")
    print("   然后编辑 tasks.csv，运行 generate.py 生成，再 fanqie_upload.py 上传。")


if __name__ == "__main__":
    main()
