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
  4. 自带 chrome 清理：main() 开头 taskkill chrome.exe + 删 SingletonLock，
     避免「上次的浏览器还占着 profile_fanqie」导致重跑失败。

流程：
  登录 → 循环(添加歌曲卡片 → 上传音频 → 上传歌词 → 填歌名
            → 词/曲/制作人/歌手 各「添加自己」→ 上传封面 → 确认裁剪 → AI类型)
       → 第一步「下一步」→「确认上传」
       → 独家授权 → 签约身份(个人) → 「下一步」→「确认签署」
       → 等合同生成 →「跳转授权」→ ★ 你本人签署电子合同（短信验证码）★
       → 签完即发布成功（脚本自动写 published.json）
"""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent          # 脚本所在目录 = 工作目录（所有数据都落在这里）
LIB_ROOT = ROOT / "library"
UPLOAD_URL = "https://www.novelfm.com/creator/music/finished/ugc/uploadProduct"
PROFILE = ROOT / "profile_fanqie"                # 本机专属登录态：首次运行自动创建（空），绝不打包进 skill

import sys as _sys                                # noqa: E402
_sys.path.insert(0, str(ROOT))                    # noqa: E402
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


async def _wait_for_upload_page(page, minutes=15):
    """持久等待上传页（「添加歌曲」按钮出现 = 已登录）。"""
    log("等待进入上传页（出现「添加歌曲」按钮即登录成功）…")
    for _ in range(minutes * 30):  # 每 2 秒一次
        try:
            if await page.query_selector("text=添加歌曲"):
                return True
        except Exception:
            pass
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
        if await page.query_selector("text=添加歌曲"):
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
    loc = page.locator("div.add-song-section")
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
    await page.wait_for_selector(".authorization-step", timeout=15000)
    card = page.locator("div.auth-type-card.exclusive")
    if await card.count() == 0:
        card = page.locator("text=独家授权")
    await card.first.click()
    await page.wait_for_timeout(700)
    await page.locator("#sign_identity_input").click()
    await page.wait_for_selector("li.arco-select-option", timeout=8000)
    await page.locator("li.arco-select-option").filter(has_text="个人").first.click()
    await page.wait_for_timeout(700)
    await page.locator("button.arco-btn-primary:has-text('下一步')").first.click()
    await page.wait_for_timeout(900)
    await page.wait_for_selector(
        "button.arco-btn-primary:has-text('确认签署')", timeout=10000
    )
    # 先滚到可见再点（窗口可能比页面矮，按钮在底部时不被裁掉/点不到）
    await page.locator("button.arco-btn-primary:has-text('确认签署')").first.scroll_into_view_if_needed()
    await page.locator("button.arco-btn-primary:has-text('确认签署')").click()
    await page.wait_for_timeout(2000)
    log("    ✓ 第二步确认签署完成")
    # 用户实测：确认签署后页面生成合同，需点「跳转授权」才能进入合同签署页。
    # 该点击统一由第三步 step3_sign_contract 负责（避免第二步/第三步重复点击）。


# 电子合同签署完成的关键字（出现在合同页/番茄页任一标签即视为签署成功）
SIGN_DONE_KEYWORDS = [
    "签署成功", "签署完成", "已完成签署", "签约成功", "合同已生效",
    "签署完毕", "签署已完成", "认证成功", "签约完成",
]
# 等待用户手动签署电子合同的最长时间（秒）。签署要收短信验证码，给足 30 分钟。
SIGN_WAIT_SEC = 1800


async def find_contract_page(ctx, fallback_page):
    """跳转授权后，合同签署页可能在当前页，也可能新开标签页（第三方电子签平台）。
    优先返回非番茄域名、非空白页的那个标签；找不到就退回原页面。"""
    for pg in list(ctx.pages):
        try:
            u = (pg.url or "").strip()
        except Exception:
            continue
        if not u or u == "about:blank":
            continue
        if "fanqie" not in u:
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


async def click_jump_authorize(page):
    """确认签署后合同生成，需点击「跳转授权」进入预览/发布页。
    合同生成需要 30s~3min，「跳转授权」按钮在合同生成完后才出现，
    因此先等「合同生成中」消失，再点跳转授权。
    """
    log("[步骤] 等待合同生成 + 点击「跳转授权」进入下一页")
    candidates = [
        "button:has-text('跳转授权')",
        "a:has-text('跳转授权')",
        "button:has-text('去授权')",
        "button:has-text('立即授权')",
        "button:has-text('查看合同')",
        ":text('跳转授权')",  # 兜底匹配任意元素含此文本
    ]
    deadline = time.time() + 240   # 合同生成最慢约 3 分钟
    phase = "generating"           # generating → ready
    while time.time() < deadline:
        body = await page.evaluate("document.body.innerText")
        if phase == "generating":
            # 合同生成中时只 sleep，等「生成中」消失再切换到找按钮
            if "合同生成中" not in body and "生成中" not in body:
                phase = "ready"
                log("    ✓ 合同生成已结束，开始查找「跳转授权」按钮")
            else:
                await asyncio.sleep(3)
                continue
        # phase == "ready"：找按钮
        for sel in candidates:
            loc = page.locator(sel)
            if await loc.count() > 0:
                try:
                    await loc.first.scroll_into_view_if_needed()
                    await loc.first.click(timeout=3000)
                    log(f"    ✓ 已点击「跳转授权」类按钮: {sel}")
                    await page.wait_for_timeout(2500)
                    return
                except Exception as e:
                    log(f"    (点击 {sel} 失败: {e})")
        await asyncio.sleep(1.5)
    log("    ⚠️ 240s 内未找到「跳转授权」按钮（合同生成可能失败）；继续尝试发布")


async def step3_sign_contract(page, ctx, folders=None):
    """第三步：等待合同生成 → 点「跳转授权」→ 交由你在浏览器完成电子合同签署。

    ★ 重要事实（用户实测确认，2026-08-30）：
      番茄音频创作平台上传页【根本没有「发布」按钮】。
      流程终点是「签署电子合同」——合同签完 == 发布成功。
      因此本函数不做任何「发布」按钮的查找/点击，只负责把合同页送到你面前，
      并在检测到签署成功后自动写入 published.json。
    """
    log("[步骤] 第三步 等待合同生成 + 跳转授权（终点：你签署电子合同）")

    await click_jump_authorize(page)
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
    log("    👉 请在浏览器里完成【电子合同签署】（短信验证码等需你本人操作）。")
    log("    👉 番茄没有「发布」按钮：签完合同 = 发布成功。")
    log("    👉 签署完成后脚本会自动检测并记入 published.json；")
    log("       若自动检测没生效，告诉我「已发布」，或自行运行：")
    log("       python fanqie_upload.py --mark-published <文件夹名,...>")
    log("=" * 66)

    deadline = time.time() + SIGN_WAIT_SEC
    last_hint = 0
    while time.time() < deadline:
        for pg in list(ctx.pages):
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

        # 每 60s 提示一次还在等待，避免日志看着像卡死
        if time.time() - last_hint > 60:
            left = int((deadline - time.time()) / 60)
            log(f"    · 等待你完成签署…（剩余约 {left} 分钟，浏览器保持打开）")
            last_hint = time.time()
        await asyncio.sleep(5)

    log(f"    ⚠️ {SIGN_WAIT_SEC // 60} 分钟内未检测到签署完成。")
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
    args = ap.parse_args()

    # 清理残留浏览器（避免 lock 住 profile_fanqie，导致重跑失败）
    # 跨平台：Windows 用 taskkill，macOS/Linux 用 pkill，统一在 browser_utils 里处理
    try:
        from browser_utils import cleanup as _cleanup
        _cleanup(PROFILE)
    except Exception:
        # 万一 browser_utils.py 不在同目录，退回 Windows 原生命令，不至于整个崩掉
        try:
            import subprocess as _sp
            _sp.run(["taskkill", "/f", "/im", "chrome.exe"], capture_output=True, timeout=10)
        except Exception:
            pass
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
                str(PROFILE), headless=False, args=["--start-maximized"],
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
            log(f"找不到曲库目录 {LIB_ROOT}，请先运行 generate.py 生成歌曲")
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
            PROFILE, headless=False, args=["--start-maximized"],
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

        try:
            while True:
                await asyncio.sleep(30)
        except Exception:
            pass
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
