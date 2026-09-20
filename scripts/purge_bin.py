#!/usr/bin/env python3
"""清回收站里属于指定目录的条目 —— 把「删掉」变成「真正腾出磁盘空间」。

## 为什么需要这个脚本

两个坑叠在一起，会让「删了但空间没少」：

1. **WorkBuddy 主机会把 Python/Node 的删除改道到回收站**
   （通过 `PYTHONPATH` 注入的 `sitecustomize.py` 拦截 `os.remove` /
   `os.rmdir` / `shutil.rmtree`）。所以脚本里写 `shutil.rmtree(d)`，
   实际只是把 d 挪进回收站。
2. **回收站和源文件在同一块盘上** → 挪进去等于没释放（实测 C 盘已用
   204.61 GB → 204.61 GB，释放 -9.6 MB，甚至略升）。

想真正腾出空间，必须在删完之后**再把回收站里对应的条目标永久清掉**。
本脚本干的就是这一步。

## 安全设计

- **只清命中 `--path` 的条目**，回收站里其它东西一条不碰
- 命中判断 = 原始路径 `== 目录` **或** 在其之下（`startswith(目录 + 分隔符)`）。
  两个都要判：主机 shim 会**逐文件**改道，所以删一棵树会生成
  「顶层目录 1 条 + 树内每个文件各 1 条」的一堆条目（实测 8 个目录 → 459 条）
- 默认 `--dry-run` 只统计不删；要真删必须显式 `--yes`
- 删完自动**复核剩余命中数**（应为 0）并**实测磁盘差值**

## 用法

    # 先看会清掉什么（默认就是 dry-run，什么都不删）

    # 按迁移报告清（和 migrate_library.py --empty-bin 等价）
    python purge_bin.py --from-report "D:\\music-workflow" --dry-run
    python purge_bin.py --from-report "D:\\music-workflow" --yes

    # 清任意几个目录（可重复 --path）
    python purge_bin.py --path "C:\\Users\\me\\WorkBuddy\\2026-01-01-00-00-00\\music-factory" --yes

## ⚠️ 运行环境注意

- **必须在前台运行**：主机的批量删除守卫（默认阈值 50 个文件）在
  后台执行时**无法弹出授权提示**，只会打印
  `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]` 然后退出。
  前台运行才会出现授权提示、由用户点「允许」。
- 文件数多时（几千个）会慢，因为每个条目都要走一次守卫检查，属正常。

## 删之前的必做动作（脚本不替你做，但你必须做）

- **逐条交叉校验**：把要删的东西和目标位置对一遍，确认没有「目标侧不存在」
  的孤儿。本项目实测靠这一步救回过一张搬迁前就更早删掉的历史残留封面图。
- **先备份小体积但有价值的东西**（发布记录 / 歌词 / 任务表 / 配置）。
- **确认没有进程在用**（例如浏览器 profile 被占用时删不干净）。
"""
import argparse
import ctypes
import json
import os
import shutil
import struct
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# 回收站 $I 索引解析
# ---------------------------------------------------------------------------

def parse_bin_index(i_file: Path):
    """解析回收站 `$I` 索引，返回被删对象的**原始路径**（失败返回 None）。

    格式（Win10/11，version=2）：
        offset 0  int64   版本
        offset 8  int64   文件大小
        offset 16 int64   删除时间(FILETIME)
        offset 24 int32   路径长度（UTF-16 字符数）
        offset 28 UTF-16LE 原路径
    """
    try:
        data = i_file.read_bytes()
    except Exception:
        return None
    if len(data) < 28:
        return None
    try:
        if struct.unpack("<q", data[:8])[0] != 2:
            return None          # 未知版本，宁可漏也不要猜
    except Exception:
        return None
    try:
        return data[28:].decode("utf-16-le", "replace").split("\x00")[0]
    except Exception:
        return None


def bin_root() -> Path:
    return Path("C:/$Recycle.Bin")


def disk_usage(root: str):
    """返回 (free, total)。判断「是否真释放」只认这个前后差值。"""
    f = ctypes.c_ulonglong(0)
    t = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p(root), ctypes.byref(f), ctypes.byref(t), None)
    return f.value, t.value


def dir_bytes(p: Path) -> int:
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    if not p.is_dir():
        return 0
    total = 0
    for root, _d, files in os.walk(str(p)):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def collect(wanted):
    """扫回收站，返回命中 wanted 的条目 [(sid_dir, i_name, orig, hit_dir)]。"""
    root = bin_root()
    if not root.is_dir():
        return []
    hits = []
    for sid in root.iterdir():
        if not sid.is_dir():
            continue
        try:
            names = os.listdir(sid)
        except OSError:
            continue                      # 系统 SID 目录读不了，跳过
        for nm in names:
            if not nm.startswith("$I"):
                continue
            orig = parse_bin_index(sid / nm)
            if not orig:
                continue
            low = orig.lower()
            for w in wanted:
                if low == w or low.startswith(w + os.sep) or low.startswith(w + "/"):
                    hits.append((sid, nm, orig, w))
                    break
    return hits


def resolve_targets(args):
    """把 --path / --from-report 统一成绝对路径小写字符串列表。"""
    dirs = []
    for raw in args.path or []:
        dirs.append(Path(raw))
    if args.from_report:
        rpt = Path(args.from_report) / "_迁移报告.json"
        if not rpt.exists():
            print("✗ 找不到迁移报告：%s" % rpt)
            return None
        rep = json.loads(rpt.read_text(encoding="utf-8"))
        dirs.append(Path(rep["primary"]))
        dirs += [Path(a["from"]) for a in rep.get("archived", [])]
    if not dirs:
        print("✗ 没给目标。用 --path 或 --from-report。")
        return None
    return [str(p.resolve()).lower() for p in dirs]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="清回收站里属于指定目录的条目（真正腾出磁盘空间）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("## 用法", 1)[-1] if "## 用法" in __doc__ else None)
    ap.add_argument("--path", action="append", metavar="DIR",
                    help="要清的目录（可重复）。原始路径命中它的条目会被清掉")
    ap.add_argument("--from-report", metavar="WORKDIR",
                    help="读 <WORKDIR>/_迁移报告.json，清它记录的 8 个源目录")
    ap.add_argument("--yes", action="store_true",
                    help="确认执行永久删除。不加则只统计（dry-run）")
    ap.add_argument("--disk", default="C:\\", help="用哪块盘量释放量（默认 C:）")
    args = ap.parse_args()

    if sys.platform != "win32":
        print("此脚本只支持 Windows（回收站路径与 SHFileOperation 均为 Windows 专属）。")
        return 1

    wanted = resolve_targets(args)
    if wanted is None:
        return 1
    if not wanted:
        print("✗ 目标为空。")
        return 1

    f0, t0 = disk_usage(args.disk)
    print("=" * 72)
    print("清回收站里属于以下 %d 个目录的条目" % len(wanted))
    print("=" * 72)
    print("  %s 已用 %.2f GB / 可用 %.2f GB" % (args.disk, (t0 - f0) / 1024**3, f0 / 1024**3))
    print()
    print("  匹配依据：")
    for w in wanted:
        print("     ", w)
    print()

    hits = collect(wanted)
    if not hits:
        print("  命中 0 条 —— 没有需要清的（或者源目录还没删）。")
        return 0

    print("  命中 %d 个条目：" % len(hits))
    total = 0
    for sid, nm, orig, w in hits:
        sz = dir_bytes(sid / ("$R" + nm[2:]))
        total += sz
        print("     %9.1f MB  %s" % (sz / 1024**2, orig))
    print()
    print("  合计可释放 %.1f MB" % (total / 1024**2))
    print()

    if not args.yes:
        print("  [dry-run] 什么都没删。确认无误后加 --yes 执行。")
        print("  ⚠️ --yes 是永久删除，不可还原。")
        return 0

    print("  永久删除这些条目（$R 内容 + $I 索引）...")
    n = 0
    for sid, nm, _orig, _w in hits:
        for p in (sid / ("$R" + nm[2:]), sid / nm):
            try:
                if p.is_dir():
                    shutil.rmtree(str(p))
                elif p.exists():
                    p.unlink()
            except Exception:
                pass
        n += 1
    print("  处理 %d 个条目" % n)

    print()
    f1, t1 = disk_usage(args.disk)
    print("  %s 已用 %.2f GB / 可用 %.2f GB" % (args.disk, (t1 - f1) / 1024**3, f1 / 1024**3))
    print("  实际释放 %.1f MB" % ((f1 - f0) / 1024**2))
    print()
    left = len(collect(wanted))
    print("  复核：回收站里仍命中的条目 = %d 条 %s"
          % (left, "✓" if left == 0 else "✗ 没清干净，再跑一次"))
    print()
    print("  ⚠️ 判据提醒：以磁盘实测差值为准，别只看脚本打印的 MB 数（两者可能对不上）。")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
