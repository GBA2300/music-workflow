"""每个用户私有的浏览器登录态目录。

⚠️ 隐私红线：登录态（Cookie / 登录凭证 / LocalStorage）必须存在
「系统每用户私有目录」，绝不能存在 skill 文件夹内。

原因：skill 文件夹会被拷贝、分发、上传给人用。一旦登录态躺在 skill 目录里，
把文件夹发出去 = 把你的账号交给别人。所以所有脚本解析登录态目录都必须走
`user_profile(name)`，绝不再写 `ROOT / "profile"` 这类代码。

MiniMax 登录态 name="profile"，番茄登录态 name="profile_fanqie"。
"""
import os
from pathlib import Path


def user_profile(name: str) -> Path:
    """返回名为 name 的浏览器登录态目录（每用户私有、不在 skill 内）。

    - Windows      : %LOCALAPPDATA%/music-workflow/profiles/<name>
    - Linux/macOS : $XDG_CACHE_HOME/music-workflow/profiles/<name> 或
                    ~/music-workflow/profiles/<name>
    目录不存在会自动创建。该路径位于系统用户目录，与 skill 文件夹完全分离，
    因此拷贝/分发 skill 永远不会带走任何人的登录态。
    """
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_CACHE_HOME")
        or str(Path.home())
    )
    p = Path(base) / "music-workflow" / "profiles" / name
    p.mkdir(parents=True, exist_ok=True)
    return p
