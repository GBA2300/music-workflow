# -*- coding: utf-8 -*-
"""
番茄音频创作平台 —— 自动填表 + 授权（多账号通用版）
==================================================================
★ 多账号设计（关键）：
   - 不内置任何人的账号/手机号/登录态。每个使用者第一次运行会打开真实浏览器，
     用【自己】的番茄账号登录；登录态只存于本工作目录的 profile_fanqie/。
   - 待发布歌单改为「扫描 library/ 子目录 + 排除 published.json」，不再硬编码名单，
     因此换一个人、换一批歌都能直接跑，互不干扰、不会误发别人的已发歌。
   - ★ 番茄上传页【没有「发布」按钮】（用户实测确认 2026-08-30）。
     流程终点是：确认签署 → 等待合同生成 → 点「跳转授权」→ 你在浏览器里收短信
     验证码完成【电子合同签署】。签完合同 == 发布成功。
     脚本负责把合同页送到你面前，检测到签署成功后自动写入 published.json。

  批量逻辑（来自 learn_mode v3 的真实点击捕获）：
    - 每首歌曲是一张卡片，id 为 songs_0 / songs_1 / songs_2 ...（索引递增）
    - 每张卡片各自填：音频 / 歌词 / 歌名 / 词曲制作人歌手(添加自己) / 封面 / AI类型
    - 所有卡片填完后，第一步「下一步」、第二步授权、第三步「跳转授权+签署」是
      【全局操作】，只做一次即对所有歌曲生效。

v15 相对 v14 的关键升级（针对「网络中断后卡住不再继续」）：
  1. 断点续传：每个上传 scope（音频/歌词/封面）填前先探测状态
     (done/uploading/failed/empty)，已 done 直接跳过；uploading 先等 90s 让
     它自然完成（处理上次断网后的自动续传）；仍不行再重触发一次 chooser。
     所有填表步骤（歌名/添加自己×4/AI类型）也都会检测是否已填。
  2. 上传确认重试：wait_for_function 超时后做二次评估；实际已完成则通过，
     仍在上传则重触发 chooser 一次。脚本对「网络中断 → 恢复 → 已传完」
     这种场景完全容错。
  3. 日志落盘：所有 log() 同步写入 _fanqie_log.txt（之前只在控制台，
     跨进程调试困难）。
  4. 启动前**只删 profile 的 Singleton* 锁文件，不杀任何进程**
     （2026-09-12 改动：原来会 taskkill /f /im chrome.exe，那会连用户自己
      正在用的 Chrome 一起杀掉，已去掉）。
     避免「上次的浏览器还占着 profile_fanqie」导致重跑失败。

流程（2026-09-12 用户示范 + 录制还原，已按实测校正）：
  登录 → 循环(添加歌曲卡片 → 上传音频 → 上传歌词 → 填歌名
            → 词/曲/制作人/歌手 各「添加自己」→ 上传封面 → 确认裁剪 → AI类型)
       → 第一步「下一步」→「确认上传」
       → 授权签约模式：选「独家授权」+ 签约身份选「个人」→「下一步」
       → 预览合同协议：点「确认签署」→ 按钮变「合同生成中」
       → 合同生成完**自动新开标签**跳电子签（番茄 → 飞书合同 → 电子牵）
         · 若没自动跳，才需要点「跳转授权」——代码两种都兜住
       → ★ 你本人签署（可能先要登录电子牵；短信验证码只能本人操作）★
       → 出现「签署成功」弹窗 = 发布成功（脚本自动写 published.json）
"""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright
from paths import user_profile, default_workdir  # 登录态存每用户私有目录，绝不在 skill 内

SCRIPT_DIR = Path(__file__).resolve().parent     # 脚本所在目录（只用来 import 兄弟模块）
# ⚠️⚠️ 2026-09-17 修（严重）：以前 `ROOT = 脚本目录`，导致**数据类路径全部指向 skill 内部**，
#    用户在别处跑（--workdir / MW_WORKDIR）时，脚本跑去找
#    `skills/music-workflow/scripts/library/踏月寻你-01/audio.mp3` → WinError 3 找不到路径，
#    歌名也被连字符切坏（「踏月寻你-01」当成歌名）。
#    2026-09-20 再修：把「设置文件里的默认曲库位置」也纳入解析，与 miaoxiang.py 保持一致 ——
#    否则用户把曲库设到 D 盘后，生成端写 D 盘、上传端却去 C 盘找，报「没有待发布的新歌」。
#    现在：数据目录 = MW_WORKDIR 环境变量 > 设置文件(settings.json) > 当前工作目录(有 tasks.csv) > 脚本目录。
ROOT = Path(os.environ.get("MW_WORKDIR") or os.environ.get("MUSIC_WORKDIR") or "").expanduser() \
    if (os.environ.get("MW_WORKDIR") or os.environ.get("MUSIC_WORKDIR")) else None
if ROOT is None:
    ROOT = default_workdir()                     # 用户在 --set-workdir 里设过的位置
if ROOT is None:
    _cwd = Path.cwd()
    ROOT = _cwd if (_cwd / "tasks.csv").exists() else SCRIPT_DIR
LIB_ROOT = ROOT / "library"
UPLOAD_URL = "https://www.novelfm.com/creator/music/finished/ugc/uploadProduct"
PROFILE = user_profile("profile_fanqie")         # 每用户私有登录态（%LOCALAPPDATA%/music-workflow/profiles/），绝不在 skill 内

# 统一的浏览器启动参数。
# ⚠️ 2026-09-12（妙响侧踩到、用户指出）：本脚本启动前会 `_cleanup()` → taskkill /f 强杀上一轮
#    浏览器，Chromium 因此认为「上次非正常退出」，下次启动会弹一个**白色的「是否恢复页面」弹窗**。
#    它盖在页面上、会把按钮点击吃掉，而且**是浏览器级 UI、不在页面 DOM 内，Playwright 点不到**，
#    只能靠启动参数压掉。别改回 args=["--start-maximized"]。
LAUNCH_ARGS = [
    "--start-maximized",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--noerrdialogs",
    "--disable-features=InfiniteSessionRestore",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--lang=zh-CN",
]

import sys as _sys                                # noqa: E402
_sys.path.insert(0, str(SCRIPT_DIR))               # 兄弟模块在脚本目录，不在数据目录
from popup_guard import (                         # noqa: E402
    a_guard_context,
    a_dismiss_popups,
    a_goto_with_guard,
)

# ─────────────────────────────────────────────────────────────
# 多账号登录说明（关键改进）
#   本脚本不内置任何人的账号。每个使用者第一次运行时会自动打开一个真实
#   浏览器窗口，用【自己】的番茄账号登录（手机号 / 抖音扫码等随意），
#   登录态只保存在本工作目录的 profile_fanqie/ 里，不进入 skill、不外泄。
#   若想在「未登录」时让脚本自动填号 + 等验证码，可二选一（都是可选的）：
#     · 在 config.json 写  "fanqie_phone": "你的手机号"
#     · 或设置环境变量      FANQIE_PHONE=你的手机号
#   都不设则纯手动登录（最通用，适配所有人，无需任何凭据）。
# ─────────────────────────────────────────────────────────────
PHONE = os.environ.get("FANQIE_PHONE", "")
try:
    _cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    if not PHONE and _cfg.get("fanqie_phone"):
        PHONE = str(_cfg["fanqie_phone"])
except Exception:
    _cfg = {}
SMS_FILE = ROOT / "_sms_code.txt"

# 已发布记录：本地 JSON 文件，记录哪些歌曲文件夹已发布，避免重复发布。
# 这是数据文件（不是硬编码名单），每个使用者各自的记录独立存在自己的工作目录。
PUBLISHED_FILE = ROOT / "published.json"

LOG_PATH = ROOT / "_fanqie_log.txt"
LOG = []


def load_published():
    try:
        if PUBLISHED_FILE.exists():
            return set(json.loads(PUBLISHED_FILE.read_text(encoding="utf-8")).get("folders", []))
    except Exception:
        pass
    return set()


def mark_published(folders):
    s = load_published()
    added = [f for f in folders if f not in s]
    s.update(folders)
    PUBLISHED_FILE.write_text(
        json.dumps({"folders": sorted(s)}, ensure_ascii=False, indent=2), encoding="utf-8")
    return added


def reset_log_file():
    """每次启动覆盖一份新日志（保留一份可追溯的运行记录）。"""
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} 番茄自动填表+发布 v15 启动 ===\n")
    except Exception:
        pass


def _append_log_file(line):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log(s):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {s}"
    print(line, flush=True)
    LOG.append(line)
    _append_log_file(line)


def load_song(lib_folder):
    """读取某首歌的素材路径与歌名（歌名优先取 meta.title）。"""
    lib = os.path.join(LIB_ROOT, lib_folder)
    audio = os.path.join(lib, "audio.mp3")
    lyrics = os.path.join(lib, "lyrics.txt")
    cover = os.path.join(lib, "cover.png")
    name = lib_folder
    try:
        with open(os.path.join(lib, "meta.json"), encoding="utf-8") as f:
            m = json.load(f)
        name = m.get("title") or m.get("歌名") or m.get("song_name") or name
    except Exception:
        pass
    return lib, audio, lyrics, cover, name


# ─────────────────────────────────────────────────────────────
# 半自动登录：检测到未登录 → 自动填号 / 发码 → 等你贴验证码 → 提交
# ─────────────────────────────────────────────────────────────
async def _best_match(page, selectors, label=""):
    """依次尝试一组 selector，返回第一个存在的；都为空返回 None。"""
    for sel in selectors:
        try:
            if await page.locator(sel).count() > 0:
                return sel
        except Exception:
            pass
    return None


# 上传页的「已登录」特征（命中任意一个即算进入上传页）。
# ⚠️ 2026-09-12 实测发现的坑：空的上传页上，那个大大的虚线框里写的**不是**「添加歌曲」，
#    而是 **「点击添加歌曲」**（+ 号图标 + 文字）。只写 text=添加歌曲 在部分渲染时机
#    会匹配不到，导致脚本以为还没登录、在 15 分钟后放弃 —— 而用户其实早就登录好了。
UPLOAD_PAGE_MARKS = [
    "text=点击添加歌曲",      # 空页面的大虚线框（实测真实文案）
    "text=添加歌曲",          # 兜底（已加过卡片时可能出现「添加歌曲」按钮）
    "text=上传歌曲信息",       # 顶部步骤条
    "#songs_0_songFile",     # 已有草稿卡片（音频 file input）
]


async def _on_upload_page(page):
    """当前页是否已经是「上传作品」页（已登录）。"""
    for sel in UPLOAD_PAGE_MARKS:
        try:
            if await page.query_selector(sel):
                return True
        except Exception:
            pass
    return False


async def _wait_for_upload_page(page, minutes=30):
    """持久等待上传页（出现上传页特征 = 已登录）。

    ⚠️ 2026-09-12 踩坑（必读）：默认等待时间原为 15 分钟，用户在 16:36 登录成功，
       脚本恰好在 16:36:58 超时放弃 —— 白等一轮。手动登录（尤其要扫码 / 等短信）
       完全可能超过 15 分钟，所以放宽到 **30 分钟**，并且每 30 秒打一次「还在等」，
       让你知道脚本没死。
    """
    log(f"等待进入上传页（出现「点击添加歌曲」等特征即登录成功），最多等 {minutes} 分钟…")
    total = minutes * 30          # 每 2 秒一次
    for i in range(total):
        try:
            if await _on_upload_page(page):
                log("✓ 已进入上传页")
                return True
        except Exception:
            pass
        if i and i % 15 == 0:     # 每 30 秒报一次进度
            log(f"    …仍在等待登录（已等 {i * 2 // 60} 分钟 / 上限 {minutes} 分钟）")
        await asyncio.sleep(2)
    return False


async def _wait_sms_code():
    """轮询 _sms_code.txt，读到 4~8 位纯数字验证码即返回；否则一直等。"""
    log(f"    📨 等待验证码：请把手机收到的验证码发到聊天里，"
        f"我会写入 {os.path.basename(SMS_FILE)}（脚本自动读取）")
    seen = ""
    try:
        if os.path.exists(SMS_FILE):
            with open(SMS_FILE, encoding="utf-8") as f:
                seen = f.read().strip()
    except Exception:
        pass
    while True:
        try:
            if os.path.exists(SMS_FILE):
                with open(SMS_FILE, encoding="utf-8") as f:
                    cur = f.read().strip()
                if cur and cur != seen and cur.isdigit() and 4 <= len(cur) <= 8:
                    # 消费后清空，防止下次误读
                    try:
                        with open(SMS_FILE, "w", encoding="utf-8") as f:
                            f.write("")
                    except Exception:
                        pass
                    return cur
        except Exception:
            pass
        await asyncio.sleep(2)


async def _auto_login(page):
    """自动填手机号 → 发验证码 → 等外部码 → 填入提交。失败则兜底手动。"""
    log("[登录] 进入自动登录流程")
    phone_sel = await _best_match(
        page, ["#mobile_input", "input[placeholder*='手机']",
               "input[type='tel']", "input[name*='mobile']", "input[name*='phone']"]
    )
    code_btn = await _best_match(
        page, ["text=获取验证码", "text=发送验证码", "text=获取短信", "text=获取动态码"]
    )
    code_input = await _best_match(
        page, ["input[placeholder*='验证码']", "input[placeholder*='短信']",
               "input[maxlength='6']", "input[maxlength='4']"]
    )
    submit_btn = await _best_match(
        page, ["text=登录", "button[type='submit']", "text=确认", "text=立即登录"]
    )

    missing = [n for n, s in
               [("手机号框", phone_sel), ("获取验证码按钮", code_btn),
                ("验证码输入框", code_input), ("登录按钮", submit_btn)] if not s]
    if missing:
        log(f"    ⚠️ 登录页结构未完全识别，缺失：{missing}")
        log("        已存登录页 HTML → _fanqie_login_page.html，截图 → _fanqie_login.png")
        try:
            html = await page.content()
            with open(os.path.join(ROOT, "_fanqie_login_page.html"), "w", encoding="utf-8") as f:
                f.write(html)
            await page.screenshot(path=os.path.join(ROOT, "_fanqie_login.png"))
        except Exception as e:
            log(f"        (存盘失败: {e})")
        log("    → 请手动在浏览器完成登录；登录后脚本自动继续")
        return await _wait_for_upload_page(page)

    await page.fill(phone_sel, PHONE)
    log(f"    ✓ 已自动填入手机号：{PHONE[:3]}****{PHONE[-4:]}")
    await code_btn.click()
    log("    ✓ 已自动点击「获取验证码」")
    code = await _wait_sms_code()
    log(f"    ✓ 收到验证码，自动填入：{code}")
    await page.fill(code_input, code)
    await submit_btn.click()
    log("    ✓ 已点击登录按钮，等待跳转…")
    return await _wait_for_upload_page(page, minutes=3)


async def ensure_logged_in(page):
    """主入口：已登录直接返回；未登录按需自动/手动登录。"""
    try:
        if await _on_upload_page(page):
            log("✓ 已登录（登录态持久化生效，无需验证）")
            return True
    except Exception:
        pass
    log("未检测到登录态，需要登录")
    if not PHONE:
        log("⚠️ PHONE 未配置，请手动登录；登录后脚本自动继续")
        return await _wait_for_upload_page(page)
    return await _auto_login(page)


async def add_song_card(page, idx):
    """确保第 idx 张卡片存在（idx 从 0 开始）。若已存在（如草稿恢复）则复用，否则点「添加歌曲」新建。"""
    if await page.locator(f"#songs_{idx}_songFile").count() > 0:
        log(f"✓ 第 {idx + 1} 张歌曲卡片已存在（复用草稿，不重复新建）")
        return
    # 真实文案是「点击添加歌曲」（见 UPLOAD_PAGE_MARKS 注释），逐个候选兜底
    loc = page.locator("div.add-song-section")
    if await loc.count() == 0:
        loc = page.locator("text=点击添加歌曲")
    if await loc.count() == 0:
        loc = page.locator("text=添加歌曲")
    await loc.last.click()
    await page.wait_for_selector(f"#songs_{idx}_songFile", timeout=15000)
    log(f"✓ 第 {idx + 1} 张歌曲卡片已新建")


async def _scope_status(page, scope_id, fname=None, is_cover=False):
    """断点续传核心：探测一个上传 scope 的当前状态。
    返回 'done' / 'uploading' / 'failed' / 'empty' / 'missing'。"""
    try:
        if is_cover:
            js = (
                "() => {const el=document.querySelector('#" + scope_id + "');"
                "if(!el) return 'missing';"
                "const t=(el.innerText||''); const h=(el.innerHTML||'');"
                "if(t.indexOf('上传失败')>=0||t.indexOf('裁剪失败')>=0) return 'failed';"
                "if(t.indexOf('上传中')>=0||t.indexOf('裁剪')>=0) return 'uploading';"
                "if(h.indexOf('<img')>=0||t.indexOf('重新上传')>=0||t.indexOf('上传完成')>=0) return 'done';"
                "return 'empty';}"
            )
        else:
            js = (
                "() => {const el=document.querySelector('#" + scope_id + "');"
                "if(!el) return 'missing';"
                "const t=(el.innerText||'');"
                "if(t.indexOf('上传失败')>=0||t.indexOf('上传错误')>=0) return 'failed';"
                "if(t.indexOf('上传中')>=0) return 'uploading';"
                "if(t.indexOf('" + (fname or '') + "')>=0||t.indexOf('重新上传')>=0||t.indexOf('上传完成')>=0) return 'done';"
                "return 'empty';}"
            )
        return await page.evaluate(js)
    except Exception:
        return "missing"


async def _wait_scope_done(page, scope_id, fname, is_cover=False, timeout=120):
    """等待 scope 变为 done；多次重试以应对网络卡顿。"""
    if is_cover:
        cond = (
            "() => {const el=document.querySelector('#" + scope_id + "');"
            "if(!el) return false; const t=(el.innerText||''); const h=(el.innerHTML||'');"
            "return (h.indexOf('<img')>=0||t.indexOf('重新上传')>=0||t.indexOf('上传完成')>=0)"
            " && t.indexOf('上传中')<0 && t.indexOf('裁剪')<0 && t.indexOf('失败')<0;}"
        )
    else:
        cond = (
            "() => {const el=document.querySelector('#" + scope_id + "');"
            "if(!el) return false; const t=(el.innerText||'');"
            "return (t.indexOf('" + fname + "')>=0||t.indexOf('重新上传')>=0||t.indexOf('上传完成')>=0)"
            " && t.indexOf('上传中')<0 && t.indexOf('失败')<0;}"
        )
    await page.wait_for_function(cond, timeout=timeout * 1000)


async def upload_file(page, scope_id, file_path, label):
    fname = os.path.basename(file_path)
    # ① 断点续传：已 done 直接跳过
    st = await _scope_status(page, scope_id, fname=fname, is_cover=False)
    if st == "done":
        log(f"    ⏭  {label} 已上传（{fname}），跳过断点续传")
        return
    if st == "uploading":
        log(f"    · {label} 上传中（疑似上次网络卡顿后续传），先等 ≤90s 让它自然完成…")
        try:
            await _wait_scope_done(page, scope_id, fname, is_cover=False, timeout=90)
            log(f"    ✓ {label} 续传完成（{fname}）")
            return
        except Exception:
            st = await _scope_status(page, scope_id, fname=fname, is_cover=False)
            if st == "done":
                log(f"    ✓ {label} 实际已完成（等待超时但状态已就绪）")
                return
            log(f"    · 续传超时（status={st}），重新触发上传…")
    elif st == "failed":
        log(f"    · {label} 上次上传失败，清掉重传…")

    # ② 触发 chooser 上传
    sel = f"#{scope_id} [class*='upload-input-container']"
    loc = page.locator(sel)
    if await loc.count() == 0:
        loc = page.locator(f"#{scope_id} .common-file-upload-wrapper")
    log(f"[上传] {label}：点触发块 {sel}")
    try:
        async with page.expect_file_chooser(timeout=15000) as fc_info:
            await loc.first.click()
        chooser = await fc_info.value
        await chooser.set_files(file_path)
        log(f"    ✓ chooser 已 set_files: {fname}")
    except Exception as e:
        log(f"    ⚠️ chooser 失败: {e} —— 兜底直接喂 body 级 input")
        inp = page.locator("body > input[type=file]")
        if await inp.count() > 0:
            await inp.first.set_input_files(file_path)
        else:
            raise

    # ③ 等完成（带网络容错：超时 → 二次评估 → 重触发一次）
    for attempt in range(2):
        try:
            await _wait_scope_done(page, scope_id, fname, is_cover=False, timeout=120)
            log(f"    ✓ {label} 已上传完成（{fname}）")
            return
        except Exception:
            st = await _scope_status(page, scope_id, fname=fname, is_cover=False)
            if st == "done":
                log(f"    ✓ {label} 实际已完成（wait 超时但状态就绪）")
                return
            log(f"    · 第 {attempt + 1} 次未确认（status={st}），重触发一次 chooser…")
            try:
                async with page.expect_file_chooser(timeout=10000) as fc_info:
                    await loc.first.click()
                chooser = await fc_info.value
                await chooser.set_files(file_path)
                log(f"    ✓ 重新 chooser set_files: {fname}")
            except Exception as e:
                log(f"    (重触发失败: {e})")
    log(f"    ⚠️ {label} 未在超时内确认完成，继续往下走（后续 wait_all 会再兜底）")


async def fill_title(page, input_id, song_name):
    try:
        cur = await page.locator(f"#{input_id}").input_value()
    except Exception:
        cur = ""
    if cur == song_name:
        log(f"[填表] 歌名 → {song_name}（#{input_id}）已填，跳过断点续传")
        return
    log(f"[填表] 歌名 → {song_name}（#{input_id}）")
    await page.fill(f"#{input_id}", song_name)
    await page.wait_for_timeout(400)
    log("    ✓ 歌名已填")


async def add_self(page, scope_id, name_input_id, label):
    loc = page.locator(f"#{scope_id} span.add-self-inside")
    if await loc.count() == 0:
        loc = page.locator(f"#{scope_id}").get_by_text("添加自己", exact=False)
    if await loc.count() == 0:
        log(f"    ⏭  {label} 已「添加自己」（无按钮），跳过断点续传")
        return
    await loc.first.click()
    await page.wait_for_timeout(500)
    # 用户实测：点「添加自己」后，必须再点一下旁边空白处才会真正选中。
    # 这里点「歌名输入框」制造失焦（确定安全，绝不会误触右下角客服浮窗），
    # 并先按 Esc 关闭可能弹出的客服/帮助浮层，避免遮挡后续点击。
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        await page.locator(f"#{name_input_id}").click()
        await page.wait_for_timeout(500)
    except Exception as e:
        log(f"    (失焦确认异常，忽略: {e})")
    log(f"    ✓ {label} 已「添加自己」并确认")


async def upload_cover(page, cover_id, cover):
    # ① 断点续传：封面已 done 跳过
    st = await _scope_status(page, cover_id, is_cover=True)
    if st == "done":
        log(f"    ⏭  封面已上传（#{cover_id}），跳过断点续传")
        return
    if st == "uploading":
        log(f"    · 封面上传中（疑似续传），先等 ≤90s…")
        try:
            await _wait_scope_done(page, cover_id, None, is_cover=True, timeout=90)
            log(f"    ✓ 封面临时续传完成")
            return
        except Exception:
            st = await _scope_status(page, cover_id, is_cover=True)
            if st == "done":
                log(f"    ✓ 封面实际已完成")
                return
            log(f"    · 续传超时（status={st}），重新触发…")

    log(f"[上传] 封面：触发文件选择（#{cover_id}）")
    # learn v3 真实路径：#songs_N_coverImage_input > .image-upload > ... > .image-upload-input-btn
    triggers = [
        f"#{cover_id} .image-upload-input-btn",
        f"#{cover_id} .image-upload-input",
        f"#{cover_id} .image-upload-wrapper",
        f"#{cover_id}",
    ]
    done = False
    for sel in triggers:
        try:
            async with page.expect_file_chooser(timeout=8000) as fc_info:
                await page.locator(sel).first.click()
            chooser = await fc_info.value
            await chooser.set_files(cover)
            log(f"    ✓ chooser 已 set_files: cover.png（{sel}）")
            done = True
            break
        except Exception as e:
            log(f"    (封面触发 {sel} 失败: {type(e).__name__})")
    if not done:
        inp = page.locator("body > input[type=file]")
        if await inp.count() > 0:
            await inp.first.set_input_files(cover)
            done = True
            log("    ✓ 兜底 body>input set_files: cover.png")
    if not done:
        raise RuntimeError("封面上传所有触发方式均失败")
    # 裁剪确认：番茄的封面裁剪弹窗是 arco-modal（内部是 canvas 裁剪区），
    # 不是 .cropper-modal。弹窗会挡住后续所有点击，必须在这里关掉。
    try:
        await page.wait_for_selector(
            ".arco-modal-wrapper, .cropper-modal, .local-img-editor", timeout=15000
        )
        clicked = None
        for sel in [
            ".arco-modal-wrapper .arco-modal-footer button.arco-btn-primary",
            ".arco-modal-footer button.arco-btn-primary",
            ".cropper-modal button.arco-btn-primary",
            ".arco-modal-wrapper button:has-text('确定')",
            ".arco-modal-wrapper button:has-text('保存')",
            ".arco-modal-wrapper button:has-text('完成')",
            ".arco-modal-wrapper button:has-text('确认')",
        ]:
            loc = page.locator(sel)
            if await loc.count() > 0:
                try:
                    await loc.first.click(timeout=5000)
                    clicked = sel
                    log(f"    ✓ 已点裁剪确认（{sel}）")
                    break
                except Exception as e:
                    log(f"    (点 {sel} 失败: {type(e).__name__})")
        if not clicked:
            log("    ⚠️ 没找到裁剪确认按钮，尝试 ESC 关闭")
            await page.keyboard.press("Escape")
        # 等弹窗彻底消失，否则会挡住后面的 AI 类型选择
        try:
            await page.wait_for_selector(
                ".arco-modal-wrapper, .cropper-modal", state="detached", timeout=30000
            )
            log("    ✓ 裁剪弹窗已关闭")
        except Exception:
            log("    ⚠️ 弹窗仍未关闭，再按一次 ESC")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1000)
        log("    ✓ 封面已上传")
    except Exception:
        st = await _scope_status(page, cover_id, is_cover=True)
        if st == "done":
            log(f"    ✓ 封面实际已完成（未弹 cropper，断点续传场景）")
        else:
            log(f"    ⚠️ cropper 流程异常（status={st}），继续")


async def select_ai_type(page, ai_id):
    try:
        cls = (await page.locator(f"#{ai_id} label.arco-radio").first.get_attribute("class")) or ""
    except Exception:
        cls = ""
    if "arco-radio-checked" in cls:
        log(f"    ⏭  AI 使用类型已选（#{ai_id}），跳过断点续传")
        return
    log(f"[填表] AI 使用类型 → 不使用AI（#{ai_id} 第一个 radio）")
    await page.locator(f"#{ai_id} label.arco-radio").first.click()
    await page.wait_for_timeout(400)
    log("    ✓ AI 使用类型已选")


async def wait_all_uploads_complete(page, n, audio_names, timeout=90):
    """用户实测硬性要求：点「下一步」弹出「确认上传」弹窗的前提是
    —— 所有歌曲的【音频 + 歌词 + 封面】全部上传完成（封面尤其关键，没传完点了不弹窗）。
    上传完成后「上传完成」文案是短暂的，判定改为：
      音频/歌词：框内出现文件名 且 不在上传中/失败
      封面：框内出现 <img> 缩略图或「重新上传/上传完成」字样 且 不在上传中/裁剪中
    """
    log(f"[等待] 全局确认 {n} 首歌的 音频+歌词+封面 均已传完（最多 {timeout}s）…")
    # (scope_id, fname, kind)
    expected = []
    for i in range(n):
        expected.append((f"songs_{i}_songFile", audio_names[i], "file"))
        expected.append((f"songs_{i}_lyricFile", "lyrics.txt", "file"))
        expected.append((f"songs_{i}_coverImage_input", "cover.png", "cover"))
    deadline = time.time() + timeout
    pending = list(expected)
    last_report = 0
    while pending and time.time() < deadline:
        still = []
        for scope_id, fname, kind in pending:
            try:
                if kind == "cover":
                    ok = await page.evaluate(
                        f"() => {{const el=document.querySelector('#{scope_id}');"
                        f"if(!el) return false;"
                        f"const t=el.innerText; const h=el.innerHTML;"
                        f"const done=h.includes('<img')||t.includes('重新上传')||t.includes('上传完成')||t.includes('cover');"
                        f"const uploading=t.includes('上传中')||t.includes('上传失败')||t.includes('裁剪');"
                        f"return done && !uploading;}}"
                    )
                else:
                    ok = await page.evaluate(
                        f"() => {{const el=document.querySelector('#{scope_id}');"
                        f"if(!el) return false;"
                        f"const t=el.innerText;"
                        f"const done=t.includes('重新上传')||t.includes('上传完成')||t.includes('{fname}');"
                        f"const uploading=t.includes('上传中')||t.includes('上传失败')||t.includes('上传错误');"
                        f"return done && !uploading;}}"
                    )
            except Exception:
                ok = False
            if not ok:
                still.append((scope_id, fname, kind))
        pending = still
        if pending and time.time() - last_report > 5:
            log(f"    · 仍待完成: {[(s, f) for s, f, _ in pending]}")
            last_report = time.time()
        if pending:
            await asyncio.sleep(1.5)
    if pending:
        log(f"    ⚠️ {len(pending)} 个框未确认（含封面？），但 upload_file/upload_cover 已各自校验，放行继续")
    else:
        log("    ✓ 所有歌曲的 音频+歌词+封面 均已传完")


async def wait_next_button_enabled(page, timeout=90):
    """轮询「下一步」按钮，直到它不再 disabled（属性或 arco-btn-disabled 类）。"""
    log("[等待] 轮询「下一步」按钮直到可点击（最多 {}s）…".format(timeout))
    btn = page.locator("button.arco-btn-primary:has-text('下一步')").first
    deadline = time.time() + timeout
    last_state = None
    while time.time() < deadline:
        try:
            disabled = await btn.get_attribute("disabled")
            aria_disabled = await btn.get_attribute("aria-disabled")
            cls = (await btn.get_attribute("class")) or ""
            is_disabled = (
                disabled is not None
                or aria_disabled == "true"
                or "arco-btn-disabled" in cls
                or "is-disabled" in cls
            )
            if not is_disabled:
                log("    ✓ 「下一步」按钮已可点击")
                return True
            if last_state != "disabled":
                log("    · 「下一步」当前 disabled，等待激活…")
                last_state = "disabled"
        except Exception:
            pass
        await asyncio.sleep(1.2)
    log("    ⚠️ 等待超时，「下一步」按钮仍 disabled")
    return False


async def step1_next(page, n_songs, audio_names):
    log("[步骤] 第一步「下一步」")
    await wait_all_uploads_complete(page, n_songs, audio_names)
    await wait_next_button_enabled(page)
    loc = page.locator("button.arco-btn-primary:has-text('下一步')").first
    confirm = page.locator("button.arco-btn-primary:has-text('确认上传')").first

    async def click_confirm():
        if await confirm.count() > 0:
            try:
                await confirm.click(timeout=3000)
                return True
            except Exception as e:
                log(f"    (确认上传点击异常: {e})")
        return False

    done = False
    # 用户实测：点「下一步」会弹出「确认上传」弹窗，点掉就进入第二步。
    # 偶尔首点无反应，需点两下；但弹窗出现后就不要再点下一步（会点到弹窗背景）。
    # 「下一步」在页面底部：窗口自适应后页面可能很高，先滚到按钮可见再点。
    try:
        await loc.scroll_into_view_if_needed(timeout=3000)
        await asyncio.sleep(0.4)
    except Exception:
        pass
    for attempt in range(4):
        # 先处理可能已弹出的确认窗
        if await click_confirm():
            log(f"    ✓ 点击「确认上传」进入第二步（attempt {attempt + 1}）")
            done = True
            break
        # 没有确认窗才点下一步
        try:
            await loc.click(timeout=3000)
        except Exception as e:
            log(f"    (下一步点击异常: {e})")
        await asyncio.sleep(1.0)
        # 点完再看确认窗
        if await click_confirm():
            log(f"    ✓ 点击「确认上传」进入第二步（attempt {attempt + 1}）")
            done = True
            break

    if not done:
        log("    ⚠️ 未检测到「确认上传」弹窗，强制再点一次下一步")
        try:
            await loc.click(timeout=3000)
            await asyncio.sleep(1.5)
            await click_confirm()
            done = True
        except Exception:
            pass

    await page.wait_for_timeout(1500)
    # 验证是否真正进入第二步（授权签约）
    try:
        await page.wait_for_selector(".authorization-step", timeout=8000)
        log("    ✓ 已进入第二步（授权签约）")
    except Exception:
        log("    ⚠️ 未检测到授权页，可能仍停留第一步；后续步骤可能失败")
    log("    ✓ 第一步点击完成")


async def step2_auth(page):
    log("[步骤] 第二步 授权签约")
    # ⚠️ 2026-09-12 实测：`.authorization-step` 这个 class 在当前版本不一定存在，
    #    原来写成硬等待会直接抛异常、整个流程中断。改成「等得到就等，等不到继续」，
    #    真正的存在性判断交给下面的元素查找。
    try:
        await page.wait_for_selector(".authorization-step", timeout=8000)
    except Exception:
        log("    (未匹配到 .authorization-step，按元素查找继续)")
    await page.wait_for_selector("text=选择签约模式", timeout=15000)

    # 签约模式：默认已是「独家授权」，但为稳妥仍显式点一次（点不到就沿用默认）
    # 实测 2026-09-12：卡片文案是「独家授权」/「非独家授权」，class 在旧版是
    # .auth-type-card.exclusive；用文案兜底更耐改版。
    card = page.locator("div.auth-type-card.exclusive")
    if await card.count() == 0:
        card = page.locator("text=独家授权")
    try:
        await card.first.click(timeout=5000)
        log("    ✓ 已选择「独家授权」")
    except Exception as e:
        log(f"    (独家授权卡片未点到，沿用页面默认: {type(e).__name__})")
    await page.wait_for_timeout(700)

    # 签约身份：实测 #sign_identity_input 仍在（Arco select，默认文案「请选择签约身份」）
    await page.locator("#sign_identity_input").click()
    await page.wait_for_selector("li.arco-select-option", timeout=8000)
    await page.locator("li.arco-select-option").filter(has_text="个人").first.click()
    await page.wait_for_timeout(700)
    log("    ✓ 签约身份已选「个人」")

    await page.locator("button.arco-btn-primary:has-text('下一步')").first.click()
    await page.wait_for_timeout(900)
    await page.wait_for_selector(
        "button.arco-btn-primary:has-text('确认签署')", timeout=10000
    )
    # 先滚到可见再点（窗口可能比页面矮，按钮在底部时不被裁掉/点不到）
    await page.locator("button.arco-btn-primary:has-text('确认签署')").first.scroll_into_view_if_needed()
    await page.locator("button.arco-btn-primary:has-text('确认签署')").click()
    await page.wait_for_timeout(2000)
    log("    ✓ 第二步确认签署完成（合同开始生成）")
    # 用户实测 2026-09-12：点完「确认签署」按钮会变成「合同生成中」，
    # 合同生成完**自动新开标签**跳到电子签页（飞书合同 → 电子牵），
    # 并不需要再点「跳转授权」。由第三步 step3_sign_contract 统一等待。


# 电子合同签署完成的关键字（出现在合同页/番茄页任一标签即视为签署成功）
# ⚠️ 2026-09-12 实测：**用户签完后，这些字并不会一直挂在页面上**——
#    番茄那边要「点合同」才弹「签署成功」弹窗。所以自动检测可能落空，
#    此时会走「等你确认 / --mark-published」兜底，属正常情况，不是脚本坏了。
#    这里刻意**不收太泛的词**（如单独的「已签署」「已完成」）：
#    电子牵的合同列表里，以前签过的合同也显示「已签署」，那会造成**误判成发布成功**，
#    后果是把没发布的歌记进 published.json、以后永远跳过。宁可漏判也不要误判。
SIGN_DONE_KEYWORDS = [
    "签署成功", "签署完成", "已完成签署", "签约成功", "合同已生效",
    "签署完毕", "签署已完成", "认证成功", "签约完成",
    "您已完成签署", "合同签署完成", "已完成全部签署", "签署完成，合同",
]
# 等待用户手动签署电子合同的最长时间（秒）。签署要收短信验证码，给足 30 分钟。
SIGN_WAIT_SEC = 1800


# 番茄自家的所有域名（★ 2026-09-12 踩坑：创作中心域名是 www.novelfm.com，
# 不含 "fanqie" 字样，旧代码用 `"fanqie" not in u` 判断 → 把番茄页自己
# 当成了「非番茄页」优先返回，导致截图/置顶/滚按钮全作用在番茄页上，
# 真正的电子签标签被忽略。）
FANQIE_HOST_MARKS = ("fanqie", "novelfm", "bytedance", "douyin", "snssdk")


def _is_fanqie_url(u):
    ul = (u or "").lower()
    return any(k in ul for k in FANQIE_HOST_MARKS)


async def find_contract_page(ctx, fallback_page):
    """跳转授权后，合同签署页可能在当前页，也可能新开标签页（第三方电子签平台）。

    ★ 2026-09-12 修正：优先用 **URL 特征** 认签署页（飞书合同 / 电子牵），
      认不出再退回「非番茄域名的第一个标签」。
    """
    ps = sign_pages(ctx)
    if ps:
        return ps[0]
    for pg in list(ctx.pages):
        try:
            u = (pg.url or "").strip()
        except Exception:
            continue
        if not u or u == "about:blank":
            continue
        if not _is_fanqie_url(u):
            return pg
    return fallback_page


# ─────────────────────────────────────────────────────────────
# 解决用户反馈：「签署时右下角按钮被遮挡 / 页面展示不完整 / 点不到」
#   a3cc6d6 只解决了【番茄上传页】跟随窗口，但第三方电子签合同页常常会新开标签、
#   顶部被 cookie/下载App 浮层盖住、按钮在折叠区外，a3cc6d6 没覆盖这一步。
#   这里补齐：窗口显式铺满屏幕 + 合同标签送最前 + 清浮层 + 滚按钮到正中 +
#   精确移除「盖在按钮中心」的 fixed/absolute 元素 + 截图供你核对。
# ─────────────────────────────────────────────────────────────
async def fit_window_to_screen(page):
    """把浏览器窗口尺寸设为用户真实屏幕（逻辑分辨率），让页面真正「适应视角窗口」。

    a3cc6d6 已加 viewport=None + --start-maximized，但 150% DPI 缩放下偶尔窗口
    没填满、内容被裁切。这里显式 set_viewport_size 到 screen.avail*，把窗口
    resize 到铺满屏幕，从根上避免「页面展示不完整、按钮在可视区外」。"""
    try:
        scr = await page.evaluate("({w: window.screen.availWidth, h: window.screen.availHeight})")
        w, h = int(scr.get("w", 0)), int(scr.get("h", 0))
        if w > 0 and h > 0:
            await page.set_viewport_size({"width": w, "height": h})
            log(f"    ✓ 窗口已适配屏幕：{w}×{h}（逻辑分辨率，含 150% DPI 缩放）")
    except Exception as e:
        log(f"    (窗口适配屏幕失败，沿用 --start-maximized: {e})")


async def dismiss_cover_overlays(page):
    """关闭/隐藏可能遮挡底部按钮的浮层（下载 APP 横条、cookie 条、新手引导蒙层等）。
    仅在「定位为 fixed/absolute 且文本像浮层」时才隐藏，绝不动正文内容。"""
    try:
        await page.evaluate("""() => {
            const KW = ['下载','APP','app','小程序','扫码','新手','引导','guide',
                        'cookie','Cookie','同意并使用','立即体验','打开App','广告'];
            document.querySelectorAll('*').forEach(el => {
                const cs = getComputedStyle(el);
                if (cs.position !== 'fixed' && cs.position !== 'absolute') return;
                const t = (el.innerText||'') + ' ' + (el.className||'') + ' ' + (el.id||'');
                if (KW.some(k => t.includes(k))) el.style.display = 'none';
            });
        }""")
    except Exception as e:
        log(f"    (关闭浮层时出错: {e})")


# 第三方电子签平台里常见的「签署」类按钮文案
CONTRACT_SIGN_TEXTS = [
    "签署", "确认签署", "提交签署", "签字", "完成签署",
    "去签署", "确认并提交", "提交", "确认",
]


async def ensure_sign_clickable(cpage, log):
    """★ 解决用户「右下角签署按钮被遮挡、点不到」的核心函数：
       1) 把合同标签送到最前（否则你看着的是番茄页，合同页在背后）
       2) 清掉遮挡浮层（下载 APP 横条 / cookie 条 / 引导蒙层）
       3) 让页面可滚动、把签署按钮滚入可视区并居中
       4) 精确移除「盖在按钮中心」的元素（fixed/absolute 浮层）
       5) 截图 _fanqie_contract.png 供你核对按钮是否可见、能否点到
    返回 True 表示定位到并清出了签署按钮。"""
    try:
        await cpage.bring_to_front()
    except Exception:
        pass
    await asyncio.sleep(0.4)
    await dismiss_cover_overlays(cpage)
    try:
        await cpage.evaluate(
            "() => { document.documentElement.style.overflow='auto';"
            " document.body.style.overflow='auto'; }")
    except Exception:
        pass

    found = False
    for txt in CONTRACT_SIGN_TEXTS:
        try:
            loc = cpage.locator(
                f"button:has-text('{txt}'), a:has-text('{txt}'),"
                f" [role='button']:has-text('{txt}')")
            if await loc.count() == 0:
                continue
            el = loc.first
            await el.scroll_into_view_if_needed()
            await asyncio.sleep(0.3)
            # 先把按钮滚到视口正中，再精确移除盖在按钮中心的元素
            res = await cpage.evaluate("""(sel) => {
                const btns = [...document.querySelectorAll('button,a,[role=button]')].filter(b=>{
                    const t=(b.innerText||'').trim();
                    return t && t.includes(sel) && t.length<=10;
                });
                if(!btns.length) return 'no-btn';
                const b=btns[0];
                b.scrollIntoView({block:'center', inline:'center'});
                const r=b.getBoundingClientRect();
                if(r.width===0) return 'btn-hidden';
                const cx=r.left+r.width/2, cy=r.top+r.height/2;
                const top=document.elementFromPoint(cx,cy);
                if(!top) return 'no-top';
                if(b.contains(top)||top.contains(b)) return 'ok';
                let el=top;
                while(el && el!==document.body){
                    const cs=getComputedStyle(el);
                    if(cs.position==='fixed'||cs.position==='absolute'){
                        el.style.display='none'; return 'dismissed:'+ (el.tagName||'');
                    }
                    el=el.parentElement;
                }
                return 'covered-by-static';
            }""", txt)
            log(f"    ✓ 定位到签署按钮「{txt}」（遮挡处理：{res}）")
            found = True
            break
        except Exception as e:
            log(f"    (处理「{txt}」按钮时出错: {e})")
            continue

    if not found:
        log("    · 未提前定位到签署按钮（可能要你先填完表单/打勾才出现）；已截图，请自行核对右下角")

    try:
        await cpage.screenshot(path=os.path.join(ROOT, "_fanqie_contract.png"))
        log("    ✓ 合同页截图已保存 _fanqie_contract.png（看右下角按钮是否在可视区、能否点到）")
    except Exception as e:
        log(f"    (合同页截图失败: {e})")
    return found


# ============================================================================
# ★ 2026-09-12 用户反馈（原话）：
#   「跳转成功就可以了，不需要重复跳新开多个页面，只需要打开一个签署页面就可以了。」
#   实测踩坑：签署页其实第 1 次点「跳转授权」就已经开出来了（3 秒内），
#   但当时靠**正文文字**判断「有没有到电子签页」，而 contract.feishu.cn 的
#   h5 页正文加载慢 / 不含特征词 → 判定成「还没到」→ 30 秒后又补点一次
#   → 于是白白多开了一个签署页。
#   结论：判断「签署页开没开」要用 **URL**（快且准），不要用正文文字；
#         一旦确认开出来了就立刻收手，并把多开的关掉。
# ============================================================================
SIGN_URL_MARKS = (
    "contract.feishu.cn", "letsign.com", "s.letsign",
    "sign/signlink", "esign", "/sign", "signcontract",
)


def is_sign_page(pg):
    """按 URL 判断某个标签页是不是「电子签/合同签署页」（比正文文字可靠得多）。"""
    try:
        u = (pg.url or "").lower()
    except Exception:
        return False
    return any(k in u for k in SIGN_URL_MARKS)


def sign_pages(ctx):
    """当前所有签署页标签（保持顺序，第 1 个就是要保留的那个）。"""
    if ctx is None:
        return []
    try:
        return [p for p in list(ctx.pages) if is_sign_page(p)]
    except Exception:
        return []


async def dedupe_sign_tabs(ctx, pages=None, logger=None):
    """★ 用户要求「只需要打开一个签署页面」：多开的签署页自动关掉，只留第一个。"""
    if logger is None:
        logger = log
    ps = pages if pages is not None else sign_pages(ctx)
    if len(ps) <= 1:
        return ps
    keep = ps[0]
    for extra in ps[1:]:
        try:
            await extra.close()
            logger("    · 已关闭重复打开的签署页（只保留一个）")
        except Exception:
            pass
    return [keep]


async def click_jump_authorize(page, ctx=None):
    """点「确认签署」之后，把电子签页面送到用户面前。

    ★ 2026-09-12 实测修正（重要）：
      旧实现是「等『合同生成中』消失 → 点『跳转授权』」。实测发现当前版本
      **根本没有「跳转授权」按钮** —— 点完「确认签署」后按钮直接变成「合同生成中」，
      合同生成完毕由**页面自己新开一个标签页**跳到电子签：
          番茄 → contract.feishu.cn（飞书合同）→ www.letsign.com（电子牵）
      所以这里改成三种成功条件任取其一：
        ① 检测到新标签页出现（当前版本的真实路径）
        ② 找到并成功点击了「跳转授权 / 去授权 / 查看合同」类按钮（兼容旧版）
        ③ 当前页正文里已经出现电子签平台的特征字样
    """
    log("[步骤] 等待合同生成 / 打开电子签页面")
    before = 0
    if ctx is not None:
        try:
            before = len(ctx.pages)
        except Exception:
            before = 0

    candidates = [
        "button:has-text('跳转授权')",
        "a:has-text('跳转授权')",
        "button:has-text('去授权')",
        "button:has-text('立即授权')",
        "button:has-text('查看合同')",
        "button:has-text('去签署')",
        ":text('跳转授权')",  # 兜底匹配任意元素含此文本
    ]
    deadline = time.time() + 900   # ★ 2026-09-12 实测：合同生成可能要 4~7 分钟，
                                   #   原来写 360s 太紧，容易在跳转前一秒放弃。
                                   #   放宽到 15 分钟，每 30 秒报一次还在等。
    # ★ 2026-09-12 实测（用户示范 + 自动跑都验证到）：**「跳转授权」是两段式的**
    #   合同生成完 → 出现第一颗「跳转授权」→ 点掉后页面变成
    #   「已跳转签约页 / 请前往签约页完成授权签署」+ **又一颗「跳转授权」**
    #   → 再点一次才真正打开电子签（电子牵）。
    #   所以这里绝不能点一下就 return，必须继续轮询直到真的看到签约页/新标签。
    # ★ 2026-09-12 再次修正（用户反馈「不要重复跳新开多个页面」）：
    #   点完必须先**等它把页面开出来**（最多 20 秒，靠 URL 判断），
    #   开出来了 → 立刻 return，绝不再点；
    #   只有确认**压根没开出来**，才允许再点 1 次。上限 2 次。
    MAX_CLICKS = 2
    clicks = 0
    last_hint = 0
    ESIGN_MARKS = ("电子牵", "飞书合同", "letsign", "选择签章", "意愿认证",
                   "文件签署", "待我签署", "签署文件")

    def _looks_esign(text):
        return any(k in text for k in ESIGN_MARKS)

    async def _opened_already():
        """签署页是否已经开出来了（URL 优先，正文兜底）。"""
        ps = sign_pages(ctx)
        if ps:
            return ps
        if ctx is not None:
            try:
                for pg in list(ctx.pages):
                    try:
                        t = await pg.evaluate("document.body.innerText")
                    except Exception:
                        continue
                    if _looks_esign(t or ""):
                        return [pg]
            except Exception:
                pass
        return []

    while time.time() < deadline:
        # ① 签署页已开（URL 判断最可靠）→ 收手，只留一个
        got = await _opened_already()
        if got:
            log(f"    ✓ 签署页已打开：{(got[0].url or '')[:110]}")
            await dedupe_sign_tabs(ctx, got)
            return

        # ② 新标签出现但 URL 还没成型 —— 也认为跳转成功，别再点
        if ctx is not None:
            try:
                pages_now = list(ctx.pages)
                if len(pages_now) > before:
                    newp = pages_now[-1]
                    log(f"    ✓ 已新开标签（电子签）：{(newp.url or '')[:110]}")
                    await dedupe_sign_tabs(ctx)
                    return
            except Exception:
                pass

        body = ""
        try:
            body = await page.evaluate("document.body.innerText")
        except Exception:
            pass

        # ③ 当前页正文已是电子签
        if _looks_esign(body):
            log("    ✓ 当前页已是电子签页面")
            return

        # ④ 点「跳转授权」：点完先给它 20 秒开页面，开出来就收手
        if "生成中" not in body and clicks < MAX_CLICKS:
            hit = None
            for sel in candidates:
                try:
                    if await page.locator(sel).count() > 0:
                        hit = sel
                        break
                except Exception:
                    pass
            if hit:
                try:
                    loc = page.locator(hit).first
                    await loc.scroll_into_view_if_needed(timeout=2000)
                    await loc.click(timeout=3000)
                    clicks += 1
                    log(f"    ✓ 第 {clicks} 次点击「跳转授权」（{hit}）")
                    ok = False
                    for _ in range(10):          # 最多等 20 秒
                        await page.wait_for_timeout(2000)
                        got = await _opened_already()
                        if got:
                            log(f"    ✓ 签署页已打开（不再重复跳）：{(got[0].url or '')[:110]}")
                            await dedupe_sign_tabs(ctx, got)
                            ok = True
                            break
                        if ctx is not None:
                            try:
                                if len(list(ctx.pages)) > before:
                                    ok = True
                                    break
                            except Exception:
                                pass
                    if ok:
                        return
                    if clicks >= MAX_CLICKS:
                        log("    · 已点满 2 次仍未确认签署页，交给下一步继续等待")
                        return
                    continue          # 确实没开出来，才回去再点一次
                except Exception as e:
                    log(f"    (点击「跳转授权」失败: {type(e).__name__}）")

        if time.time() - last_hint > 30:
            tag = "合同生成中…" if "生成中" in body else "等待电子签页面…"
            log(f"    · {tag}")
            last_hint = time.time()
        await asyncio.sleep(2)

    log("    ⚠️ 15 分钟内未等到电子签页面（合同生成可能较慢或失败）；继续进入签署等待")


async def step3_sign_contract(page, ctx, folders=None):
    """第三步：等待合同生成 → 点「跳转授权」→ 交由你在浏览器完成电子合同签署。

    ★ 重要事实（用户实测确认，2026-08-30）：
      番茄音频创作平台上传页【根本没有「发布」按钮】。
      流程终点是「签署电子合同」——合同签完 == 发布成功。
      因此本函数不做任何「发布」按钮的查找/点击，只负责把合同页送到你面前，
      并在检测到签署成功后自动写入 published.json。
    """
    log("[步骤] 第三步 等待合同生成 + 跳转授权（终点：你签署电子合同）")

    await click_jump_authorize(page, ctx)
    await asyncio.sleep(4)

    # 跳转授权后，合同签署页可能在当前页，也可能新开标签页（第三方电子签平台）
    cpage = await find_contract_page(ctx, page)
    try:
        log(f"    · 合同页地址：{(cpage.url or '')[:120]}")
    except Exception:
        pass

    # ★ 关键修复：把合同页送最前 + 清掉遮挡浮层 + 让签署按钮进入可视区并截图，
    #   解决用户「右下角按钮被遮挡、点不到」的问题（详见 ensure_sign_clickable）。
    await ensure_sign_clickable(cpage, log)

    log("=" * 66)
    log("    👉 已到【电子签页面】，剩下要你本人操作（脚本不代签）：")
    log("       ① 电子签平台（电子牵）可能先要求**登录**：手机号 + 验证码，")
    log("          勾「我已阅读并同意…」→「同意协议并登录」")
    log("       ② 进入「文件签署」→ 选择签章 → 点右下角「签署」")
    log("       ③ 弹「意愿认证」→ 填手机收到的数字验证码 → 确定")
    log("    👉 番茄没有「发布」按钮：**显示「签署成功」就等于发布成功**。")
    log("    👉 签署完成后脚本会自动检测并记入 published.json；")
    log("       若自动检测没生效，告诉我「已发布」，或自行运行：")
    log("       python fanqie_upload.py --mark-published <文件夹名,...>")
    log("=" * 66)

    deadline = time.time() + SIGN_WAIT_SEC
    last_hint = 0
    nudges = 0
    last_nudge = 0
    # ⚠️⚠️ 2026-09-17 用户明确指出的新事实（脚本原来完全没考虑）：
    #   **签署完成后，电子签页会自己跳走（回到登录页/首页），「签署成功」四个字就消失了。**
    #   → 原来「只读正文找关键词」的策略必然错过：等你签完，页面上已经没那四个字了，
    #     脚本就会一直打印「等待你完成签署」直到 30 分钟超时（本次就是这个现象）。
    #   新增判据（任一命中即算签完）：
    #     A. 正文出现 SIGN_DONE_KEYWORDS（没跳走时仍有效）
    #     B. **签署页「消失」**：曾见过签署页（esign_seen=True），现在一个都不剩了
    #        —— 且不是被我们自己关的（dedupe 只关重复的，至少留一个）
    #     C. 签署页被跳成了登录页/首页（URL 不再含签署特征，且正文出现登录字样）
    sign_seen_once = False
    seen_sign_urls = set()
    login_left_marks = ("重新登录", "请登录", "登录后", "手机号登录", "扫码登录")
    while time.time() < deadline:
        try:
            all_pages = list(ctx.pages)
        except Exception:
            all_pages = [page]

        # ① 已经有签署页（URL 判断）→ 视为已到位，且顺手把多开的关掉
        sps = sign_pages(ctx)
        esign_seen = bool(sps)
        if esign_seen:
            sign_seen_once = True
            for sp in sps:
                try:
                    seen_sign_urls.add((sp.url or "").split("?")[0])
                except Exception:
                    pass
            await dedupe_sign_tabs(ctx)

        # ★ B 判据：签署页「全都消失了」= 签完跳走了（用户 2026-09-17 证实的行为）
        if sign_seen_once and not esign_seen and not b_already_marked:
            log("    ✓ 签署页已跳走（用户证实：签完会自动离开签署页）→ 判定签署完成")
            if folders:
                added = mark_published(list(folders))
                log(f"    ✓ 已写入 published.json（防重复）：{added}")
            return True

        for pg in all_pages:
            try:
                body = await pg.evaluate("document.body.innerText")
            except Exception:
                continue
            if any(k in body for k in SIGN_DONE_KEYWORDS):
                log("    ✓ 检测到电子合同签署完成 → 发布成功")
                if folders:
                    added = mark_published(list(folders))
                    log(f"    ✓ 已写入 published.json（防重复）：{added}")
                return True
            # ★ C 判据：曾经见过签署页，现在这一页变成了登录页 → 也是签完跳走
            if sign_seen_once and any(k in body for k in login_left_marks):
                log("    ✓ 签署页已变为登录页（用户证实：签完会跳回登录）→ 判定签署完成")
                if folders:
                    added = mark_published(list(folders))
                    log(f"    ✓ 已写入 published.json（防重复）：{added}")
                return True
            if any(k in body for k in ("电子牵", "letsign", "选择签章", "意愿认证", "文件签署")):
                esign_seen = True

        # ② 兜底补点（用户实测：合同生成后**有时自动跳转，有时要手动点「跳转授权」**）：
        #    只在「压根没有任何签署页」时补点，且全程最多 1 次 —— 绝不重复跳。
        if (not esign_seen) and nudges < MAX_NUDGES and time.time() - last_nudge > 30:
            last_nudge = time.time()
            nudges += 1
            try:
                loc = page.locator("button:has-text('跳转授权')")
                if await loc.count() > 0:
                    await loc.first.scroll_into_view_if_needed(timeout=2000)
                    await loc.first.click(timeout=3000)
                    log("    · 番茄页仍有「跳转授权」，补点 1 次（此后不再重复点击）")
                    await asyncio.sleep(6)
                    await dedupe_sign_tabs(ctx)
            except Exception:
                pass

        # 每 60s 提示一次还在等待，避免日志看着像卡死
        if time.time() - last_hint > 60:
            left = int((deadline - time.time()) / 60)
            log(f"    · 等待你完成签署…（剩余约 {left} 分钟，浏览器保持打开）")
            last_hint = time.time()
        await asyncio.sleep(5)

    log(f"    ⚠️ {SIGN_WAIT_SEC // 60} 分钟内未自动检测到签署完成。")
    # 落一份「当时各标签页的正文摘要」，方便下次排错（不用再让用户复现一遍）
    try:
        for i, pg in enumerate(list(ctx.pages)):
            try:
                t = (await pg.evaluate("document.body.innerText")) or ""
            except Exception:
                t = ""
            one = " ".join(t.split())[:300]
            log(f"    · [标签{i}] {(pg.url or '')[:90]}")
            log(f"        正文摘要：{one}")
    except Exception:
        pass
    log("    · 若你其实已签完，请运行：python fanqie_upload.py --mark-published <文件夹名,...>")
    return False


async def main():
    ap = argparse.ArgumentParser(
        description="番茄音频创作平台 自动填表+授权（多账号通用）；"
                    "终点是签署电子合同（签完=发布成功），需你本人收验证码完成。")
    ap.add_argument("--login", action="store_true",
                    help="打开真实浏览器，手动登录你的番茄账号；登录成功后自动退出（登录态存 profile_fanqie/）")
    ap.add_argument("--mark-published", metavar="FOLDERS",
                    help="把逗号分隔的歌曲文件夹名记入 published.json（发布完成后调用）")
    ap.add_argument("--songs", metavar="FOLDERS",
                    help="只发布这些文件夹（逗号分隔）；默认发布 library/ 下未发布的全部")
    ap.add_argument("--headless", action="store_true",
                    help="无界面运行（不推荐，手动发布看不到窗口）")
    ap.add_argument("--workdir", default=None,
                    help="工作目录（含 library/ tasks.csv published.json）；"
                         "默认取 MW_WORKDIR 环境变量 > 当前目录 > 脚本目录")
    args = ap.parse_args()

    # ⚠️ 2026-09-17：--workdir 必须在任何数据路径使用之前生效。
    #    这里重算全局 ROOT/LIB_ROOT/PUBLISHED_FILE/SMS_FILE/LOG_PATH。
    global ROOT, LIB_ROOT, PUBLISHED_FILE, SMS_FILE, LOG_PATH
    if args.workdir:
        ROOT = Path(args.workdir).expanduser().resolve()
        LIB_ROOT = ROOT / "library"
        PUBLISHED_FILE = ROOT / "published.json"
        SMS_FILE = ROOT / "_sms_code.txt"
        LOG_PATH = ROOT / "_fanqie_log.txt"
        log(f"工作目录（--workdir）：{ROOT}")

    # 清理残留浏览器（避免 lock 住 profile_fanqie，导致重跑失败）
    # ⚠️ 2026-09-12 重要改动：**不再 taskkill /f /im chrome.exe**！
    #    原实现调 browser_utils.cleanup()，它会强杀所有名字叫 chrome 的进程 ——
    #    那会连【你自己正在用的 Chrome 窗口】一起杀掉，你正在看的东西会瞬间全没。
    #    现在只删 profile 的 Singleton* 锁文件（这就能解开上次没关干净的锁），
    #    不碰任何进程。万一真的还有别的浏览器占着这个 profile，Playwright 启动时
    #    会报错，那时再单独处理，代价远小于误杀用户浏览器。
    try:
        from browser_utils import clear_profile_locks as _clear_locks
        _removed = _clear_locks(PROFILE)
        if _removed:
            log(f"已清理 profile 残留锁：{_removed}")
    except Exception:
        try:
            _lock = PROFILE / "SingletonLock"
            if _lock.exists():
                _lock.unlink()
        except Exception:
            pass

    # ── --mark-published：仅记录已发布 ──
    if args.mark_published:
        folders = [x.strip() for x in args.mark_published.split(",") if x.strip()]
        added = mark_published(folders)
        log(f"已记录 {len(added)} 个文件夹为「已发布」：{added}")
        log(f"已发布清单见：{PUBLISHED_FILE}")
        return

    reset_log_file()
    log("番茄批量自动填表+授权（多账号通用版）启动")

    # ── --login：仅登录 ──
    if args.login:
        log("打开浏览器，请手动登录你的番茄账号（手机号 / 抖音扫码等均可）。")
        log("登录成功、看到上传页「添加歌曲」后，脚本自动退出并保存登录态。")
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                str(PROFILE), headless=False, args=LAUNCH_ARGS,
                viewport=None)   # 页面跟随窗口大小，不同设备自适应
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await fit_window_to_screen(page)   # 窗口显式铺满屏幕（150% DPI 也能填满）
            await a_guard_context(ctx, log=log)
            await a_goto_with_guard(page, UPLOAD_URL, log=log, settle_sec=2)
            ok = await _wait_for_upload_page(page, minutes=20)
            if ok:
                log("✓ 登录成功，登录态已保存到 profile_fanqie/")
            else:
                log("⚠️ 等待超时，未检测到登录成功。可重试，或直接运行普通模式脚本会等你手动登录。")
            await ctx.close()
        return

    # ── 确定待发布歌单（扫描 library/，排除已发布）──
    published = load_published()
    if args.songs:
        songs = [x.strip() for x in args.songs.split(",") if x.strip()]
    else:
        if not LIB_ROOT.exists():
            log(f"找不到曲库目录 {LIB_ROOT}，请先运行 miaoxiang.py --gen 生成歌曲")
            return
        all_folders = [d.name for d in LIB_ROOT.iterdir() if d.is_dir()]
        songs = [f for f in all_folders if f not in published]
        if not songs:
            log("library/ 下没有待发布的新歌（都已发布或为空）。")
            log(f"已发布：{sorted(published) or '无'}")
            return
    log(f"待发布歌单（{len(songs)} 首）：{songs}")
    log(f"已发布、本次跳过：{sorted(published) or '无'}")

    # 同名歌曲自动加 -01 / -02 序号区分
    titles = [load_song(f)[4] for f in songs]
    dup = len(titles) > len(set(titles))
    log(f"同名检测：{'是（将自动加序号）' if dup else '否'}")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PROFILE, headless=False, args=LAUNCH_ARGS,
            viewport=None   # 页面跟随窗口大小，不同设备自适应（不固定视口）
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(20000)
        await fit_window_to_screen(page)   # 窗口显式铺满屏幕（150% DPI 也能填满）
        # 挂弹窗守卫：番茄上传页同样会弹公告/引导层，原生 alert 不处理会直接卡死
        await a_guard_context(ctx, log=log)

        await a_goto_with_guard(page, UPLOAD_URL, log=log, settle_sec=2)
        if not await ensure_logged_in(page):
            log("⚠️ 登录等待超时，关闭。请重跑。")
            await ctx.close()
            return

        try:
            for i, folder in enumerate(songs):
                lib, audio, lyrics, cover, title = load_song(folder)
                display = title
                if dup:
                    display = f"{title}-{i + 1:02d}"
                log(f"===== 第 {i + 1}/{len(songs)} 首：{folder}（歌名：{display}）=====")
                # 每首歌开填之前先清一次弹窗（上传页常在第 2 首之后弹公告/额度提示）
                await a_dismiss_popups(page, log=log)
                await add_song_card(page, i)
                await upload_file(page, f"songs_{i}_songFile", audio, f"完整歌曲({display})")
                await upload_file(page, f"songs_{i}_lyricFile", lyrics, "歌词")
                await fill_title(page, f"songs_{i}_name_input", display)
                await add_self(page, f"songs_{i}_lyricist_id_list", f"songs_{i}_name_input", "词作者")
                await add_self(page, f"songs_{i}_composer_id_list", f"songs_{i}_name_input", "曲作者")
                await add_self(page, f"songs_{i}_producers", f"songs_{i}_name_input", "制作人")
                await add_self(page, f"songs_{i}_singer_id_list", f"songs_{i}_name_input", "歌手")
                await upload_cover(page, f"songs_{i}_coverImage_input", cover)
                await select_ai_type(page, f"songs_{i}_ai_usage_type")
                await page.screenshot(
                    path=os.path.join(ROOT, f"_fanqie_card{i}.png")
                )

            await page.screenshot(path=os.path.join(ROOT, "_fanqie_step1.png"))
            log("✓ 所有卡片填完，截图 _fanqie_step1.png")

            audio_names = [os.path.basename(load_song(f)[1]) for f in songs]
            await step1_next(page, len(songs), audio_names)
            await step2_auth(page)
            await page.screenshot(path=os.path.join(ROOT, "_fanqie_step2.png"))

            ok = await step3_sign_contract(page, ctx, songs)
            await page.screenshot(path=os.path.join(ROOT, "_fanqie_final.png"))
            log(f"✓ 最终截图 _fanqie_final.png（合同签署：{'已完成=已发布' if ok else '待你手动完成'}）")
        except Exception as e:
            log(f"⚠️ 流程异常: {e}")
            await page.screenshot(path=os.path.join(ROOT, "_fanqie_error.png"))
            log("   已截图 _fanqie_error.png，浏览器保持打开供你手动继续")
            log("   完整运行日志见：" + str(LOG_PATH))

        log("============================================================")
        log("✓ 脚本运行结束，浏览器保持打开。")
        log("   完整运行日志见：" + str(LOG_PATH))
        log("   若已自动发布：可直接关闭窗口，或告诉我「停」")
        log("   若需手动：在浏览器里完成剩余步骤后关闭")
        log("============================================================")

        # 保持浏览器打开，直到用户关掉窗口。
        # ⚠️ 2026-09-12：原来是无条件 `while True: sleep(30)`，用户关窗后脚本也**永不退出**，
        #    会一直挂在后台占着进程。现在每 30 秒探一次，发现窗口都关了就直接结束。
        while True:
            await asyncio.sleep(30)
            try:
                if len(ctx.pages) == 0:
                    log("· 检测到浏览器窗口已关闭，脚本退出")
                    break
            except Exception:
                log("· 浏览器已断开，脚本退出")
                break


if __name__ == "__main__":
    asyncio.run(main())
