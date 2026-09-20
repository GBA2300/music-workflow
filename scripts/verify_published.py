# -*- coding: utf-8 -*-
"""番茄「成品发行」列表 **只读核对** —— 验证歌曲到底有没有真的发布上去。

为什么需要它
────────────
`fanqie_upload.py` 的判断链是「合同页出现签署成功字样 → 记入 published.json」。
这个判断合理，但**无法自证**：万一误判，歌会被永久跳过（published.json 防重复），
而用户并不知情。用户明确要求过「不要只听你说，我要能核实」。
所以每批发布完，都该跑一次这个脚本，去番茄后台的作品列表里点对点核对。

它做什么
────────
1. 用 `profile_fanqie` 打开 https://www.novelfm.com/creator/music/finished/ugc
   （左侧「作品管理 → 成品发行」，这是**唯一**能看到真实作品状态的地方）；
2. 打印列表里的「作品名 / 作品ID / 作品状态 / 创建时间 / 上架时间」；
3. 逐条核对目标歌名是否在列表里，并输出 ✓/✗。

它**绝不**做什么
────────────────
- 不点「下一步」「确认上传」「确认签署」「签署」等任何提交类按钮；
- 不写 published.json；不关用户的浏览器之外的东西。
纯粹只读 + 截图。

状态说明（2026-09-12 用户确认）
──────────────────────────────
番茄没有「发布」按钮，签完电子合同就等于发布成功。签完立刻看列表是
**「审核中」**，这是发布后的正常审核阶段（往期实测约 16~25 分钟转「已上架」）。
所以：
    「审核中」 = 已发布成功（在审核）
    「已上架」 = 审核通过，已上线
    「未通过」 = 审核被拒，需要处理
**看到「审核中」就不要再重传一遍**，那会变成重复作品。

用法
────
    # 核对 published.json 里记录的歌（默认：最近 4 条）
    python verify_published.py

    # 核对指定歌名（逗号分隔，支持模糊包含匹配）
    python verify_published.py --songs "心宽路就宽,好日子咱一起走"

    # 核对 library/ 下全部文件夹
    python verify_published.py --all

    # 只看列表、不核对（想知道后台现在都有啥）
    python verify_published.py --list-only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright  # noqa: E402
from browser_utils import clear_profile_locks, window_args  # noqa: E402

try:
    from paths import user_profile  # 登录态存每用户私有目录
except Exception:                                    # pragma: no cover
    def user_profile(name):                          # 兜底
        return os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "music-workflow", "profiles", name)

ROOT = Path(__file__).resolve().parent.parent          # skill 根目录
WORKS_URL = "https://www.novelfm.com/creator/music/finished/ugc"

# 番茄自家的域名（创作中心是 www.novelfm.com，不含 "fanqie" 字样）
FANQIE_HOST_MARKS = ("fanqie", "novelfm", "bytedance", "douyin", "snssdk")


def log(m):
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def workdir() -> Path:
    """工作目录：环境变量 > config.json > 设置文件(settings.json) > 当前目录(有 tasks.csv) > skill 目录。

    ⚠️ 2026-09-17 修：原来只认环境变量/config，跑在不带 MW_WORKDIR 的环境里时
       会静默退回 skill 目录 —— 那里没有 `library/`，
       于是 `folder_display_title()` 拿不到 meta.json、退回目录名，
       导致 `踏月寻你-02` 被当成 `踏月寻你` 匹配（两行同 ID 的乌龙）。
       → 加「当前目录有 tasks.csv 就是工作目录」这条自愈规则，与其他脚本一致。
    ⚠️ 2026-09-20 补：把 `--set-workdir` 写的设置文件也纳入，否则曲库挪到 D 盘后
       本工具会又跑回 C 盘 skill 目录去找，正是上面那个乌龙的翻版。
    """
    env = os.environ.get("MW_WORKDIR") or os.environ.get("MUSIC_WORKDIR")
    if env:
        return Path(env)
    cfg = ROOT / "config.json"
    if cfg.exists():
        try:
            d = json.loads(cfg.read_text(encoding="utf-8"))
            if d.get("workdir"):
                return Path(d["workdir"])
        except Exception:
            pass
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from paths import default_workdir
        d = default_workdir()
        if d is not None:
            return d
    except Exception:
        pass
    cwd = Path.cwd()
    if (cwd / "tasks.csv").exists() or (cwd / "library").is_dir():
        return cwd
    return ROOT


def published_folders() -> list[str]:
    p = workdir() / "published.json"
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text(encoding="utf-8")).get("folders", []))
    except Exception:
        return []


def library_folders() -> list[str]:
    lib = workdir() / "library"
    if not lib.is_dir():
        return []
    return sorted(d.name for d in lib.iterdir() if d.is_dir())


def folder_display_title(folder: str) -> str:
    """把目录名映射成**平台上显示的歌名**。

    目录是 `library/歌名-01` / `library/歌名-02`（番茄端按目录扫描），
    但平台上的作品名取自 `meta.json` 的 `title`（第 2 版带「（动听版）」）。
    直接拿目录名去匹配后台列表会张冠李戴（-01/-02 都命中同一行）。

    ⚠️ 兜底也必须**去掉 `-01`/`-02` 后缀**，否则退回原文时仍会误匹配。
    """
    try:
        meta = workdir() / "library" / folder / "meta.json"
        if meta.is_file():
            t = json.loads(meta.read_text(encoding="utf-8")).get("title")
            if t:
                return str(t).strip()
    except Exception:
        pass
    # 兜底：去掉结尾的 -01 / -02（含半角/全角连字符），至少不误配短名
    import re as _re
    return _re.sub(r"[-－—]\d{1,2}$", "", folder).strip()


def parse_rows(body: str) -> list[dict]:
    """从列表正文里抠出每条作品：歌名 / 作品ID / 状态 / 创建时间 / 上架时间。

    列结构是「作品名 / 作品ID：xxx / 状态 / 创建时间 / 上架时间」，一行一字段，
    所以按行扫，遇到「作品ID：」就把前一行当歌名。
    """
    lines = [ln.strip() for ln in body.splitlines()]
    rows, i, n = [], 0, len(lines)
    statuses = {"审核中", "已上架", "未通过", "已下架", "待授权", "审核不通过", "已删除"}
    while i < n:
        if lines[i].startswith("作品ID"):
            rid = lines[i].split("：", 1)[-1].split(":", 1)[-1].strip()
            name = lines[i - 1] if i > 0 else "?"
            status, ctime, utime = "", "", ""
            j = i + 1
            while j < n and j < i + 8:
                t = lines[j]
                if t in statuses and not status:
                    status = t
                elif t.count("-") >= 2 and len(t) >= 10 and t[:4].isdigit():
                    if not ctime:
                        ctime = t
                    elif not utime:
                        utime = t
                elif t.startswith("作品ID"):
                    break
                j += 1
            rows.append({"name": name, "id": rid, "status": status,
                         "created": ctime, "online": utime})
            i = j
        else:
            i += 1
    return rows


async def main():
    ap = argparse.ArgumentParser(description="番茄成品发行列表只读核对")
    ap.add_argument("--songs", default="", help="要核对的歌名，逗号分隔")
    ap.add_argument("--all", action="store_true", help="核对 library/ 下全部文件夹")
    ap.add_argument("--list-only", action="store_true", help="只打印列表")
    ap.add_argument("--recent", type=int, default=4, help="默认核对 published.json 最近 N 条")
    ap.add_argument("--profile", default="profile_fanqie", help="用的浏览器 profile 名")
    args = ap.parse_args()

    # 目标清单
    if args.all:
        targets = library_folders()
    elif args.songs:
        targets = [s.strip() for s in args.songs.split(",") if s.strip()]
    else:
        targets = published_folders()[-args.recent:]
    if args.list_only:
        targets = []

    profile_dir = user_profile(args.profile)
    clear_profile_locks(profile_dir)
    out_png = workdir() / "_verify_works.png"

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            profile_dir, headless=False, args=window_args(), viewport=None
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(25000)
        await page.goto(WORKS_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)

        # 多滚几屏，尽量把列表读全
        for _ in range(5):
            try:
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(1200)
            except Exception:
                break
        try:
            await page.mouse.wheel(0, -30000)
            await page.wait_for_timeout(1200)
        except Exception:
            pass

        body = await page.evaluate("document.body.innerText") or ""
        cur = page.url
        if any(k in cur.lower() for k in FANQIE_HOST_MARKS) is False:
            log(f"⚠️ 当前页可能不是番茄后台：{cur}")
        try:
            await page.screenshot(path=str(out_png))
            log(f"✓ 截图 {out_png}")
        except Exception as e:
            log(f"(截图失败 {e})")

        await ctx.close()

    if "登录" in body[:400] and "作品管理" not in body:
        log("⚠️ 看起来没登录（profile 里登录态可能过期）。先跑 fanqie_upload.py --login")
        return 2

    rows = parse_rows(body)
    print("=" * 70)
    print(f"后台作品共解析出 {len(rows)} 条：")
    for r in rows[:30]:
        print(f"  {r['name']:<24} {r['status']:<6} 创建 {r['created']:<18} "
              f"上架 {r['online'] or '-':<18} ID {r['id']}")
    print("=" * 70)

    if not targets:
        return 0

    print("核对目标：")
    ok_n = 0
    for t in targets:
        # ⚠️ 2026-09-17 修：目标可能是**目录名**（`歌名-01` / `歌名-02`），
        #   而后台列表里是**歌名**（`歌名` / `歌名（动听版）`）。
        #   直接拿目录名去匹配，会让 -01 和 -02 双双命中同一行（本次就出现两行同 ID）。
        #   → 先按目录的 meta.json 取真实歌名（这才是后台显示的名字），取不到再退回目录名。
        want = folder_display_title(t)
        hit = next((r for r in rows if r["name"] == want), None)
        if hit is None:
            cands = [r for r in rows if want in r["name"] or r["name"] in want]
            if cands:
                hit = max(cands, key=lambda r: len(r["name"]))
        if hit:
            r = hit
            ok_n += 1
            extra = ""
            if r["status"] == "审核中":
                extra = "  ← 已发布成功，正在审核（往期约 16~25 分钟后转「已上架」）"
            elif r["status"] == "已上架":
                extra = "  ← 审核通过，已上线"
            elif r["status"] in ("未通过", "审核不通过"):
                extra = "  ← ⚠️ 审核未通过，需要处理"
            print(f"  ✓ {t:<26} [{r['status']}] ID {r['id']}{extra}")
        else:
            print(f"  ✗ {t:<26} 列表里没找到 —— 可能没发布成功，或还没刷新出来")
    print("=" * 70)
    print(f"结果：{ok_n}/{len(targets)} 在后台列表里")
    return 0 if ok_n == len(targets) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
