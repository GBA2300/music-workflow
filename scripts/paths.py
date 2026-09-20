"""每个用户私有的浏览器登录态目录 + 存储位置设置。

⚠️ 隐私红线：登录态（Cookie / 登录凭证 / LocalStorage）必须存在
「系统每用户私有目录」，绝不能存在 skill 文件夹内。

原因：skill 文件夹会被拷贝、分发、上传给人用。一旦登录态躺在 skill 目录里，
把文件夹发出去 = 把你的账号交给别人。所以所有脚本解析登录态目录都必须走
`user_profile(name)`，绝不再写 `ROOT / "profile"` 这类代码。

登录态目录名约定：
    妙响（主力生成端）  name="douyin"
    番茄（上传/发布端）  name="profile_fanqie"
    MiniMax（历史备选端）name="profile"

────────────────────────────────────────────────────────
存储位置设置（2026-09-20 新增）
────────────────────────────────────────────────────────
批量下载的音频、封面会越攒越多，默认全落在系统盘（C 盘）容易撑爆。
本模块提供「把数据盘挪到别的盘」的能力，一次设定、所有脚本生效：

    <python> miaoxiang.py --set-workdir "D:\\music-workflow"

设置写在 `%LOCALAPPDATA%/music-workflow/settings.json`（本机私有，不进仓库）：

    {
      "workdir":  "D:\\music-workflow",        # 曲库/歌词/歌单的默认位置
      "temp_dir": "D:\\music-workflow\\.tmp"   # 下载中转（省系统盘峰值占用）
    }

解析优先级（`default_workdir()` / `work_temp_dir()`）：
    --workdir 参数  >  settings.json  >  当前目录(含 tasks.csv)  >  脚本目录
"""
import json
import os
import tempfile
from pathlib import Path


def _base_dir() -> Path:
    """本机每用户私有根目录（Windows 为 %LOCALAPPDATA%）。"""
    return Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_CACHE_HOME")
        or str(Path.home())
    )


def user_profile(name: str) -> Path:
    """返回名为 name 的浏览器登录态目录（每用户私有、不在 skill 内）。

    - Windows      : %LOCALAPPDATA%/music-workflow/profiles/<name>
    - Linux/macOS : $XDG_CACHE_HOME/music-workflow/profiles/<name> 或
                    ~/music-workflow/profiles/<name>
    目录不存在会自动创建。该路径位于系统用户目录，与 skill 文件夹完全分离，
    因此拷贝/分发 skill 永远不会带走任何人的登录态。
    """
    p = _base_dir() / "music-workflow" / "profiles" / name
    p.mkdir(parents=True, exist_ok=True)
    return p


# ─────────────────────── 存储位置设置（本机私有） ───────────────────────

def settings_file() -> Path:
    """设置文件路径：%LOCALAPPDATA%/music-workflow/settings.json"""
    return _base_dir() / "music-workflow" / "settings.json"


def load_settings() -> dict:
    """读设置；文件不存在或内容坏了都返回空 dict（绝不抛异常打断主流程）。"""
    p = settings_file()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def save_settings(**kv) -> Path:
    """合并写入设置（只覆盖传入的键，不动其他键）。"""
    p = settings_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = load_settings()
    cur.update({k: v for k, v in kv.items() if v is not None})
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def default_workdir():
    """设置里指定的默认工作目录；没设或为空则返回 None。

    ⚠️ 这里**故意不做 exists() 检查** —— 目录还没建是正常的（调用方会建）。
    若在此静默回落，会让人「以为写到 D 盘，其实又写回 C 盘」，比报错更难查。
    """
    w = (load_settings().get("workdir") or "").strip()
    if not w:
        return None
    try:
        return Path(w).expanduser()
    except Exception:
        return None


def work_temp_dir(workdir=None) -> Path:
    """下载中转目录（音频先落这里，再进曲库）。

    优先放在工作目录旁边的 `.tmp/`，这样批量下载的峰值占用也不进系统盘；
    既没有 workdir 参数、也没设过默认工作目录时，回落到系统 TEMP。
    """
    raw = (load_settings().get("temp_dir") or "").strip()
    if raw:
        try:
            p = Path(raw).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass                        # 配错了就退回默认，不打断下载

    base = Path(workdir) if workdir else default_workdir()
    p = (base / ".tmp") if base is not None else (Path(tempfile.gettempdir()) / "music-workflow")
    p.mkdir(parents=True, exist_ok=True)
    return p
