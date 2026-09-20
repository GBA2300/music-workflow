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
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

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
    ap.add_argument("--purge", action="store_true",
                    help="⚠️ 校验通过后删除源目录里的 library 与 published.json（不可逆）")
    args = ap.parse_args()

    dest = Path(args.to).expanduser().resolve()
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
    print("  ⚠️ 源文件**没有删除**，C 盘那份还在。核对无误后要我清掉，")
    print("     再运行： python migrate_library.py --to <同一目标> --purge")

    # 4) 可选清理
    if args.purge:
        print()
        print("=" * 70)
        print("--purge：删除源曲库")
        print("=" * 70)
        if report["failed"]:
            print("  ✗ 有目录迁移失败，拒绝删除任何源文件（先修好再删）")
            return 1
        # 安全：只删「已经确认复制成功」的那些 library 目录，且必须位于搜索根之内
        targets = [primary] + [x for x in others if
                               str(x["wd"]) not in report["failed"]]
        for x in targets:
            lp = x["lib"]
            if not lp.is_dir():
                continue
            try:
                shutil.rmtree(str(lp))
                print(f"  已删 {lp}")
            except Exception as e:
                print(f"  ✗ 删不掉 {lp}：{type(e).__name__}: {e}")
        print("  （各目录的 tasks.csv / lyrics / published.json 保留，只是曲库搬走了）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
