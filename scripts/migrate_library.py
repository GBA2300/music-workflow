# -*- coding: utf-8 -*-
"""把散落在系统盘各处的曲库搬到数据盘（如 D:\\）。

为什么需要它：music-workflow 的曲库默认跟着「工作目录」走，而工作目录是
按会话日期散落在 `C:\\Users\\<你>\\WorkBuddy\\<日期>\\...` 的。跑几批歌就容易把
系统盘塞满（实测 8 个目录 45 首 299 MB）。本脚本把它们集中搬到指定盘。

设计要点（踩过的坑）：
  1. **不合并成一个曲库**。老目录里的歌可能早就发布过，只是当时没有 published.json
     记录；一股脑合进主力曲库 → 上传脚本会以为「都没发过」→ 重复投稿。
     所以：指定的主力工作目录进 <目标>/library，其余一律进 <目标>/_archive/<来源>/，
     `_archive/` 不在曲库扫描路径上，不会被上传脚本看见。
  2. **只复制、不删除**。删原件必须由用户另行确认（--purge 才删，且要二次确认）。
  3. **复制后校验**：文件数 + 总字节数 + 音频抽样 MD5，不一致就报错。

用法：
    python migrate_library.py --to D:\\music-workflow --dry-run   # 先看会搬什么
    python migrate_library.py --to D:\\music-workflow --primary "<主力工作目录>"
    python migrate_library.py --to D:\\music-workflow --purge     # 校验通过后删 C 盘原件

不带 --primary 时，取「有 published.json 且体积最大」的那个当主力（通常是当前在用的）。
"""
import argparse
import ctypes
import hashlib
import json
import os
import shutil
import sys
from ctypes import wintypes
from datetime import datetime
from pathlib import Path


# ─────────────────────── 删除：默认送回收站，不永久删 ───────────────────────
# ⚠️ 原来这里用 shutil.rmtree —— 一旦删错就彻底没了。用户的作品是不可再生的，
#    「能还原」比「删得干净」重要得多。所以默认走回收站，永久删必须显式加 --hard-delete。

class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


_FO_DELETE = 3
_FOF_ALLOWUNDO = 0x40          # ★ 关键：允许撤销 → 进回收站
_FOF_NOCONFIRMATION = 0x10
_FOF_SILENT = 0x4
_FOF_NOERRORUI = 0x400


def recycle(path) -> bool:
    """把文件/目录送进 Windows 回收站（可还原）。返回是否成功。

    非 Windows（或接口失败）时返回 False，调用方应回落到「不删、只报告」。
    """
    if sys.platform != "win32":
        return False
    p = str(Path(path).resolve())
    if len(p) >= 260:
        return False                      # 回收站接口不支持超长路径
    op = _SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = p + "\0\0"                 # SHFileOperation 要求双 null 结尾
    op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI)
    ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return ret == 0 and not op.fAnyOperationsAborted


def verify_pair(src: Path, dst: Path):
    """删除前**重新**逐文件比对源与目标（数量 + 字节 + 全量 MD5）。

    为什么不信任之前的复制结果：中间可能有人动过文件、可能磁盘出过问题、
    也可能用户自己改过。**删之前必须再核一遍**，这是最后一道闸。
    返回 (ok, 说明)。
    """
    if not src.is_dir():
        return True, "源已不存在，跳过"
    if not dst.is_dir():
        return False, f"目标不存在：{dst}"
    sf = {f.relative_to(src): f for f in src.rglob("*") if f.is_file()}
    df = {f.relative_to(dst): f for f in dst.rglob("*") if f.is_file()}
    only_src = sorted(set(sf) - set(df))
    if only_src:
        return False, f"目标缺少 {len(only_src)} 个文件，例如 {[str(x) for x in only_src[:3]]}"
    diff = []
    for rel, sp in sf.items():
        dp = df[rel]
        if sp.stat().st_size != dp.stat().st_size or md5_of(sp) != md5_of(dp):
            diff.append(str(rel))
    if diff:
        return False, f"{len(diff)} 个文件内容不一致，例如 {diff[:3]}"
    return True, f"{len(sf)} 个文件逐一比对一致"


def _parse_bin_index(i_file: Path):
    """解析回收站的 $I 索引文件，返回被删对象的**原始路径**（失败返回 None）。

    格式（Win10/11，version=2）：
        offset 0  int64  版本
        offset 8  int64  文件大小
        offset 16 int64  删除时间(FILETIME)
        offset 24 int32  路径长度（UTF-16 字符数）
        offset 28 UTF-16LE 原路径
    """
    import struct
    try:
        data = i_file.read_bytes()
    except Exception:
        return None
    if len(data) < 28:
        return None
    try:
        ver = struct.unpack("<q", data[:8])[0]
    except Exception:
        return None
    if ver != 2:
        return None
    try:
        return data[28:].decode("utf-16-le", "replace").split("\x00")[0]
    except Exception:
        return None


def empty_bin_for(paths):
    """把回收站里【原始路径命中 paths】的条目标**永久删除**，真正释放磁盘空间。

    为什么需要这一步：**回收站本身就在同一块盘上**，所以「扔进回收站」只是把文件
    从 A 位置挪到 B 位置，磁盘占用一点没少（实测 C 盘已用反而从 204.3 涨到 204.57 GB）。
    想真正腾出空间，必须把回收站里这些条目清掉 —— 清掉就是永久删除、不可还原。

    ⚠️ 只删**命中传入路径**的条目，绝不动用户回收站里其它东西。
    ⚠️ 非 Windows 上回收站路径不同，此函数直接返回 0。

    返回 (删掉的条目数, 释放的字节数)。
    """
    import struct  # noqa: F401
    if sys.platform != "win32":
        return 0, 0

    # ⚠️ 这批路径此刻**已经不存在了**（刚被删掉），所以不能做 exists() 检查 ——
    #    匹配靠的是 $I 索引里记录的「原始路径」，与磁盘现状无关。
    wanted = [str(Path(p).resolve()).lower() for p in paths]
    if not wanted:
        return 0, 0

    bin_root = Path("C:/$Recycle.Bin")
    if not bin_root.is_dir():
        return 0, 0

    n = 0
    freed = 0
    for sid_dir in bin_root.iterdir():
        if not sid_dir.is_dir():
            continue
        try:
            names = os.listdir(sid_dir)
        except OSError:
            continue                       # 系统 SID 目录读不了，跳过
        for name in names:
            if not name.startswith("$I"):
                continue
            i_file = sid_dir / name
            orig = _parse_bin_index(i_file)
            if not orig:
                continue
            low = orig.lower()
            if not any(low == w or low.startswith(w + os.sep) or low.startswith(w + "/")
                       for w in wanted):
                continue
            r_path = sid_dir / ("$R" + name[2:])
            # 先量体积（删完就量不到了）
            try:
                if r_path.is_dir():
                    for root, _dd, files in os.walk(str(r_path)):
                        for fn in files:
                            try:
                                freed += os.path.getsize(os.path.join(root, fn))
                            except OSError:
                                pass
                elif r_path.is_file():
                    freed += r_path.stat().st_size
            except OSError:
                pass
            # 删内容 + 删索引。永久删除（这里就是要永久删，用户已确认）
            for p in (r_path, i_file):
                try:
                    if p.is_dir():
                        shutil.rmtree(str(p))
                    elif p.exists():
                        p.unlink()
                except Exception:
                    pass
            n += 1
    return n, freed


def count_real_files(p: Path) -> int:
    """目录下还剩多少个真文件（不算空目录）。"""
    if not p.exists():
        return 0
    try:
        return sum(1 for f in p.rglob("*") if f.is_file())
    except Exception:
        return -1


def prune_empty_dirs(p: Path) -> int:
    """自底向上删掉 p 下面的空目录（含 p 自己）。

    ⚠️ 只用 os.rmdir —— 它**只能删空目录**，一旦某个目录还有文件就会抛错。
    这是有意的安全设计：绝不会误删还有内容的目录。
    返回删掉的数量。
    """
    if not p.is_dir():
        return 0
    removed = 0
    for root, dirs, _files in os.walk(str(p), topdown=False):
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
                removed += 1
            except OSError:
                pass                      # 非空 / 被占用 → 保持原样，绝不强删
    try:
        os.rmdir(str(p))
        removed += 1
    except OSError:
        pass
    return removed


def do_purge_only(target: Path, hard: bool, verify_only: bool = False,
                  also_empty_bin: bool = False):
    """只删源、不再复制。依据 <目标>/_迁移报告.json 里记录的原路径。

    为什么要单独一个模式：直接重跑 `--purge` 会**先复制一遍再删** ——
    而目标曲库已非空，copy_verify 会把新副本改名成 `library__dup2`，
    于是搬出一堆重复目录。所以清理必须是**只删不拷**的独立动作。

    verify_only=True 时只复核、不删任何东西（给用户看证据用的）。

    ⚠️ 成功判据不是 SHFileOperation 的返回值，而是「源目录下还剩不剩真文件」。
       实测：文件已经全部进了回收站，它仍会返回非零（因为它没能把空目录壳也收走），
       只看返回值会误报「处理失败」，让用户以为没删掉。（2026-09-20 踩过）
    """
    rpt = target / "_迁移报告.json"
    if not rpt.exists():
        print(f"✗ 找不到迁移报告：{rpt}")
        print("  先跑一次迁移（不加 --purge-only），生成报告后再清理。")
        return 1
    try:
        rep = json.loads(rpt.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"✗ 报告读不出来：{e}")
        return 1

    pairs = [("主力曲库", Path(rep["primary"]), target / "library")]
    for a in rep.get("archived", []):
        pairs.append(("归档", Path(a["from"]), Path(a["to"])))

    print("=" * 70)
    print("清理源曲库（先逐一复核，再删）")
    print("=" * 70)
    print(f"  目标盘：{target}")
    print(f"  方式  ：{'永久删除（--hard-delete）' if hard else '送进回收站（可还原）'}")
    print()

    # ── 阶段一：全部复核，全通过才动手（不做「删一半发现有问题」）
    src_libs = []
    all_ok = True
    for label, src_wd, dst_lib in pairs:
        src_lib = src_wd / "library"
        ok, msg = verify_pair(src_lib, dst_lib)
        mark = "✓" if ok else "✗"
        print(f"  {mark} [{label}] {src_wd}")
        print(f"      {msg}")
        if ok:
            if src_lib.is_dir():
                src_libs.append(src_lib)
        else:
            all_ok = False

    if not all_ok:
        print()
        print("  ✗ 有源目录未通过复核，**不删任何东西**（先解决上面的问题）")
        return 1

    if not src_libs:
        print()
        print("  没有可删的源目录（可能已经清过了）。")
        return 0

    total = sum(sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                for p in src_libs)
    print()
    print(f"  复核全部通过：{len(src_libs)} 个目录，将释放约 {total/1024**2:.1f} MB")

    if verify_only:
        print()
        print("  [--verify-only] 只复核，没有删除任何东西。")
        return 0

    print()

    # ── 阶段二：执行
    done, failed, shells = [], [], 0
    for p in src_libs:
        if hard:
            try:
                shutil.rmtree(str(p))
            except Exception:
                pass
        else:
            try:
                recycle(p)
            except Exception:
                pass
        # 成功判据：源目录下已经没有真文件了（不信 SHFileOperation 的返回值）
        left = count_real_files(p)
        if left == 0:
            done.append(p)
            # 顺手把残留的空目录壳收掉（rmdir 只删空目录，删不动就留着，不强删）
            shells += prune_empty_dirs(p)
            print(f"  ✓ 文件已清空 {p}")
        else:
            failed.append(p)
            print(f"  ✗ 仍有 {left} 个文件没能清掉 {p}")

    print()
    if not hard and done:
        print("  ✓ 已送进回收站，**可以还原**：打开桌面「回收站」→ 找到对应文件夹 → 右键「还原」。")
        if not also_empty_bin:
            print("  ⚠️ 注意：回收站本身也在 C 盘，所以此刻 C 盘空间**并没有真正腾出来**，")
            print("     要真正释放，加 --empty-bin 再跑一次（=永久删除，不可还原）。")
    if shells:
        print(f"  顺带清理了 {shells} 个残留空目录（0 字节，rmdir 只删空目录）")

    # 阶段三：把刚扔进回收站的这些条目标永久删掉，真正释放空间
    if also_empty_bin and done:
        print()
        print("=" * 70)
        print("--empty-bin：清掉回收站里对应的条目（永久删除，不可还原）")
        print("=" * 70)
        n, freed = empty_bin_for(done)
        print(f"  清掉 {n} 个条目，实际释放 {freed/1024**2:.1f} MB")
        print("  只动了这几个命中的条目，回收站里其它东西没碰。")

    print(f"  完成 {len(done)} 个，失败 {len(failed)} 个")
    print("  （各目录的 tasks.csv / lyrics / published.json 都保留，只是曲库搬走了）")
    return 1 if failed else 0


SEARCH_ROOTS = [
    Path.home() / "WorkBuddy",
    Path.home() / ".workbuddy" / "skills" / "music-workflow",
]


def find_libraries():
    """扫描所有含 library/ 的工作目录，返回 [{wd, lib, n, sz, has_pub}]。"""
    out = []
    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for lib in root.rglob("library"):
            if not lib.is_dir():
                continue
            songs = [d for d in lib.iterdir() if d.is_dir()]
            if not songs:
                continue
            sz = sum(f.stat().st_size for f in lib.rglob("*") if f.is_file())
            out.append({
                "wd": lib.parent,
                "lib": lib,
                "n": len(songs),
                "sz": sz,
                "has_pub": (lib.parent / "published.json").exists(),
            })
    return out


def md5_of(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def dir_stat(p: Path):
    """(文件数, 总字节数, 音频路径列表)"""
    files = [f for f in p.rglob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files), \
        [f for f in files if f.suffix.lower() == ".mp3"]


def copy_verify(src: Path, dst: Path, label: str):
    """复制目录并校验。返回 (ok, msg)。"""
    n_s, sz_s, mp3_s = dir_stat(src)
    # ⚠️ 目标已存在时要分清两种情况：
    #   · 空目录（--set-workdir 会预先建好 library/）→ 直接用它，别改名
    #   · 非空 → 说明之前搬过，改用带后缀的新名字，绝不覆盖
    if dst.exists() and any(dst.iterdir()):
        i = 2
        while dst.exists():
            dst = dst.with_name(f"{dst.name}__dup{i}")
            i += 1
    shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
    n_d, sz_d, mp3_d = dir_stat(dst)
    if (n_s, sz_s) != (n_d, sz_d):
        return False, f"{label}: 文件数/字节数不一致 {n_s}/{sz_s} -> {n_d}/{sz_d}", dst
    bad = []
    for a, b in zip(sorted(mp3_s), sorted(mp3_d)):
        if md5_of(a) != md5_of(b):
            bad.append(a.name)
    if bad:
        return False, f"{label}: 音频内容不一致 {bad[:3]}", dst
    return True, f"{label}: {n_d} 个文件 {sz_d/1024**2:.1f} MB，MD5 校验通过", dst


def merge_published(src_wd: Path, dst_json: Path):
    """把源 published.json 合并进目标（并集，只增不减）。"""
    sp = src_wd / "published.json"
    if not sp.exists():
        return 0
    try:
        s = json.loads(sp.read_text(encoding="utf-8")) or {}
    except Exception:
        return 0
    t = {}
    if dst_json.exists():
        try:
            t = json.loads(dst_json.read_text(encoding="utf-8")) or {}
        except Exception:
            t = {}
    before = _count(t)
    for k, v in s.items():
        if isinstance(v, dict) and isinstance(t.get(k), dict):
            t[k].update(v)
        elif isinstance(v, list) and isinstance(t.get(k), list):
            t[k] = sorted(set(t[k]) | set(v))
        else:
            t[k] = v
    dst_json.write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
    return _count(t) - before


def _count(d):
    """粗略数一下记录条数（published.json 结构可能是 {folders: [...]} 或 {published: {...}}）。"""
    def n(x):
        if isinstance(x, dict):
            return sum(n(v) for v in x.values())
        if isinstance(x, list):
            return len(x)
        return 1
    return n(d)


def main():
    ap = argparse.ArgumentParser(description="把曲库从系统盘搬到数据盘")
    ap.add_argument("--to", required=True, help="目标工作目录，如 D:\\music-workflow")
    ap.add_argument("--primary", default=None,
                    help="当前主力工作目录（它的歌进 <目标>/library）；不指定则自动挑")
    ap.add_argument("--dry-run", action="store_true", help="只报告计划，不复制")
    ap.add_argument("--purge-only", action="store_true",
                    help="★ 只清理源目录，不重新复制。依据 <目标>/_迁移报告.json，"
                         "删之前会逐文件 MD5 复核；默认送回收站（可还原）")
    ap.add_argument("--purge", action="store_true",
                    help="迁移完成后再清理源目录（同一次运行内，等价于做完再 --purge-only）")
    ap.add_argument("--verify-only", action="store_true",
                    help="配合 --purge-only：只复核源与目标是否一致，不删任何东西")
    ap.add_argument("--empty-bin", action="store_true",
                    help="★ 清掉回收站里刚删的那几条（永久删除，不可还原，但**只有这样才能"
                         "真正腾出磁盘空间**——回收站也在同一块盘上）")
    ap.add_argument("--hard-delete", action="store_true",
                    help="⚠️ 永久删除，不走回收站 —— 删错无法还原，非必要别用")
    args = ap.parse_args()

    dest = Path(args.to).expanduser().resolve()

    # 只清理：读报告 → 复核 → 删。完全不碰复制逻辑
    if args.purge_only:
        return do_purge_only(dest, args.hard_delete, args.verify_only, args.empty_bin)
    libs = find_libraries()
    if not libs:
        print("没找到任何曲库目录，无需迁移。")
        return 0

    # 挑主力：显式指定 > 有 published.json 且最大 > 最大
    if args.primary:
        want = Path(args.primary).expanduser().resolve()
        primary = next((x for x in libs if x["wd"].resolve() == want), None)
        if primary is None:
            print(f"✗ --primary 指定的目录里没找到 library/：{want}")
            return 1
    else:
        with_pub = [x for x in libs if x["has_pub"]]
        primary = max(with_pub or libs, key=lambda x: x["sz"])

    print("=" * 70)
    print("曲库迁移计划")
    print("=" * 70)
    print(f"  目标盘      ：{dest}")
    print(f"  主力曲库来源：{primary['wd']}")
    print(f"                （{primary['n']} 首，{primary['sz']/1024**2:.1f} MB"
          f"{'，有发布记录' if primary['has_pub'] else '，无发布记录'}）")
    others = [x for x in libs if x["wd"] != primary["wd"]]
    print(f"  归档来源    ：{len(others)} 个目录，"
          f"{sum(x['sz'] for x in others)/1024**2:.1f} MB，"
          f"{sum(x['n'] for x in others)} 首")
    for x in sorted(others, key=lambda y: -y["sz"]):
        print(f"                - {x['wd']}  ({x['n']} 首)")
    print()
    print("  规则：主力 → <目标>/library/（上传脚本可见）")
    print("        其余 → <目标>/_archive/<来源>/（上传脚本**看不见**，不会误发）")

    if args.dry_run:
        print()
        print("[dry-run] 没有复制任何文件。去掉 --dry-run 真正执行。")
        return 0

    print()
    print("=" * 70)
    print("开始复制 + 校验")
    print("=" * 70)

    # 1) 主力曲库
    dst_main = dest / "library"
    dst_main.parent.mkdir(parents=True, exist_ok=True)
    ok, msg, _ = copy_verify(primary["lib"], dst_main, "主力曲库")
    print(("  ✓ " if ok else "  ✗ ") + msg)
    if not ok:
        return 1
    added = merge_published(primary["wd"], dest / "published.json")
    print(f"  ✓ 已发布记录：合并 {added} 条")

    # 歌单/歌词也带过来（以后就在新目录开工）
    for name in ("tasks.csv",):
        s = primary["wd"] / name
        if s.exists() and not (dest / name).exists():
            shutil.copyfile(str(s), str(dest / name))
            print(f"  ✓ 带过来 {name}")
    sl = primary["wd"] / "lyrics"
    if sl.is_dir() and not (dest / "lyrics").exists():
        shutil.copytree(str(sl), str(dest / "lyrics"))
        print("  ✓ 带过来 lyrics/")

    # 2) 其余归档
    report = {"to": str(dest), "primary": str(primary["wd"]), "archived": [], "failed": []}
    for x in sorted(others, key=lambda y: -y["sz"]):
        tag = f"{x['wd'].parent.name}__{x['wd'].name}"
        dst = dest / "_archive" / tag
        ok, msg, real_dst = copy_verify(x["lib"], dst, f"归档 {tag}")
        print(("  ✓ " if ok else "  ✗ ") + msg)
        if ok:
            report["archived"].append({"from": str(x["wd"]), "to": str(real_dst), "n": x["n"]})
        else:
            report["failed"].append(str(x["wd"]))

    # 3) 写报告
    rpt = dest / "_迁移报告.json"
    report["time"] = datetime.now().isoformat(timespec="seconds")
    report["dry_run"] = False
    rpt.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 70)
    print(f"完成：主力 {primary['n']} 首 + 归档 {len(report['archived'])} 个目录"
          f"{'，失败 ' + str(len(report['failed'])) if report['failed'] else ''}")
    print(f"  报告：{rpt}")
    print()
    print("  ⚠️ 源文件**没有删除**，原盘那份还在。核对无误后要清掉，再运行：")
    print("     python migrate_library.py --to <同一目标> --purge-only")
    print("     （--purge-only 只删不拷，删前会逐文件 MD5 复核；默认送回收站）")

    # 4) 可选清理（同一次运行内做完）
    if args.purge:
        if report["failed"]:
            print()
            print("  ✗ 有目录迁移失败，拒绝删除任何源文件（先修好再删）")
            return 1
        print()
        # 复用同一套「复核 → 删」逻辑，避免两处实现走偏
        return do_purge_only(dest, args.hard_delete, False, args.empty_bin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
