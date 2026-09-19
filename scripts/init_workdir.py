# -*- coding: utf-8 -*-
"""
music-workflow —— 一键初始化工作目录
==================================================================
为【每一位使用者】创建一套独立、干净、零配置的工作目录：

  <工作目录>/
    miaoxiang.py          ★ 主力生成端：妙响（抖音音乐创作实验室）生成+下载+出封面（含 --login）
    generate.py           ⚠️ 历史备选端：MiniMax 网页端（已停用，仅救急回退用）
    cover.py              封面生成器
    paths.py              登录态目录解析（每用户私有，保证账号不外泄）
    browser_utils.py      跨平台浏览器清理（Windows/macOS/Linux 通用）
    fanqie_upload.py      番茄上传填表+授权（含 --login / --mark-published）
    verify_published.py   只读核对：查已发布的歌在番茄后台的真实 ID/状态
    init_workdir.py       本脚本（可重复运行）
    login_check.py        登录诊断/登录（反自动化+自动截图，专治「点登录没反应」）
    probe_generate.py     生成探针（查「点生成后等不到歌」）
    inspect_buttons.py    按钮探测器（查按钮 selector 该怎么写）
    popup_guard.py        弹窗守卫（自动关掉挡路浮层；被主脚本 import）
    test_popup_guard.py   弹窗守卫自检（真起浏览器跑 14 项）
    config.json           平台 URL、选择器、封面参数（⚠️ 只服务 MiniMax 备选端）
    tasks.csv             歌单模板（改这里写你自己的歌）
    requirements.txt      playwright, pillow
    library/              曲库（初始为空）
    lyrics/               歌词 txt（初始为空）
    published.json        已发布记录（初始 {}，防止重复发布）

★ 隐私红线：登录态（Cookie/凭证）绝不放在本工作目录或 skill 文件夹内。
  它存在「系统每用户私有目录」%LOCALAPPDATA%/music-workflow/profiles/，
  由 scripts/paths.py 的 user_profile() 解析。因此拷贝/分发本工作目录或 skill
  都不会带走任何人的账号——每个人第一次运行时自己登录自己的。

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
#
# ⚠️ 新增任何「被 import 的模块」或「要用户自己跑的脚本」时，必须同步加进这里。
#    漏了不会在仓库里报错（本目录有全套文件），但用户 init 出来的工作目录会缺文件，
#    一运行就 ImportError / 找不到脚本 —— 而且报错发生在别人的电脑上，很难排查。
#    踩过两次：popup_guard.py（被 import 的模块）、miaoxiang.py（主力生成脚本）。
COPY_FILES = [
    "miaoxiang.py",        # ★ 主力生成端（妙响 / 抖音音乐创作实验室）
    "cover.py",            # 封面生成（被 miaoxiang.py / generate.py 调用）
    "fanqie_upload.py",
    "init_workdir.py",
    "verify_published.py",  # 发布结果只读核对（纪律 3）
    "login_check.py",
    "fanqie_learn.py",     # 卡点学习模式的录制器（纪律 2）
    "generate.py",         # ⚠️ 历史备选：MiniMax 网页端（已停用，保留作 fallback）
    "probe_generate.py",
    "inspect_buttons.py",
    "browser_utils.py",
    "popup_guard.py",
    "paths.py",
    "test_popup_guard.py",
    "probe_popup.py",
    "config.json",
    "tasks.csv",
    "requirements.txt",
]

# 需要创建的空目录
MAKE_DIRS = ["library", "lyrics"]


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
    ap = argparse.ArgumentParser(description="music-workflow 一键初始化工作目录（多账号通用；登录态存每用户私有目录，绝不随本目录分发）")
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
    print("✅ 工作目录已初始化：{0}".format(workdir))
    print("   包含脚本：miaoxiang.py（主力生成）/ cover.py / fanqie_upload.py / "
          "verify_published.py / init_workdir.py …")
    print("   已建空目录：library/ lyrics/")
    print("   已创建：tasks.csv（歌单模板）、published.json（空）")
    print("   登录态不在工作目录里，存在系统每用户私有目录")
    print("   （%LOCALAPPDATA%/music-workflow/profiles/），首次运行各自登录自己的账号")
    print("")
    print("接下来三步：")
    print("   1) cd \"{0}\"".format(workdir))
    print("   2) {0} miaoxiang.py --login          # 用你的抖音账号登录妙响".format(py_exe))
    print("   3) {0} fanqie_upload.py --login     # 用你的番茄账号登录".format(py_exe))
    print("   然后编辑 tasks.csv，运行 miaoxiang.py --gen 生成，再 fanqie_upload.py 上传。")


if __name__ == "__main__":
    main()
