# -*- coding: utf-8 -*-
"""
妙响（抖音音乐创作实验室）生成端
https://music.douyin.com/studio?panel=my-assets

用途：替代 generate.py（MiniMax），但**产出格式与 generate.py 完全一致**，
      这样 fanqie_upload.py（番茄上传端）一行都不用改：

    library/<歌名>/
        audio.mp3     音频
        lyrics.txt    歌词
        cover.png     封面 1440×1440（复用 cover.make_cover）
        meta.json     {title, style, instrumental, source_url, audio_file,
                       size_bytes, generated_at, task_no}

⚠️⚠️ 生成规则（用户 2026-09-04 明确，自动化必须照此执行，不得偏离）：
    妙响：1 首歌词 → 点 1 次「生成歌曲」→ 默认出 **2 个不同版本**（数量不可改）。
      * 用户要 N 首歌 = N 个不同歌词 = 点 N 次生成。
      * 不要试图改数量（平台不允许）。

⚠️ 下载策略（用户 2026-09-05 补充）：这 2 个版本是「都下载」还是「只下载第一版」，
    **在生成之前先问用户**，按用户的选择执行（见 ask_download_mode / --download-mode）。

⚠️ 当前阶段：登录 / 抓 DOM / 学习录制 已就绪；生成+下载逻辑**等看到真实流程再写**，
   绝不猜选择器（MiniMax 上次猜 DOM，导致"数量设 1 却仍出两首"的假修复）。

用法：
    python miaoxiang.py --login    # 打开浏览器，你登录抖音，登录完直接关掉窗口
    python miaoxiang.py --probe    # 抓真实页面结构 → _mx_probe.json + _mx_shot.png
    python miaoxiang.py --walk     # 逐步点开创作入口抓 DOM（只导航，不点生成，不烧额度）
    python miaoxiang.py --learn    # 你按平时方式完整操作一遍，脚本每几秒录一次 DOM+截图
    python miaoxiang.py --gen --download-mode first|both   # 生成+下载（待学习后实现）

登录态位置（隐私红线：绝不放在 skill 文件夹内）：
    %LOCALAPPDATA%/music-workflow/profiles/douyin
"""
import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from paths import (  # noqa: E402
    user_profile, default_workdir, work_temp_dir, settings_file, save_settings,
)
from browser_utils import clear_profile_locks, window_args, viewport_for  # noqa: E402

MX_URL = "https://music.douyin.com/studio?panel=my-assets"
CREATE_URL = "https://music.douyin.com/studio/create"   # 经典创作模式点进去的创作页
ASSETS_URL = "https://music.douyin.com/studio/assets"   # 资产页（下载歌曲的地方）
PROFILE_NAME = "douyin"
PROBE_JSON = ROOT / "_mx_probe.json"
PROBE_SHOT = ROOT / "_mx_shot.png"

# ── 生成规则（用户 2026-09-04 明确）────────────────────────────
# 妙响：1 首歌词 → 点 1 次「生成歌曲」→ 默认出 2 个不同版本（数量不可改）。
#   * 用户要 N 首歌 = N 个不同歌词 = 点 N 次生成。
#   * 不要试图改数量（平台不允许）。
GEN_VERSIONS_PER_LYRIC = 2

# ── 下载策略（用户 2026-09-05 补充：生成前由用户选，不写死）────────
# 一次生成出的 2 个版本，是「都下载」还是「只下载第一版」，
# **在生成之前问用户**，按用户的选择执行：
#   first → 只下载第一个版本（默认，省额度、出歌快）
#   both  → 两个版本都下载（同一份歌词的两版分别落到 <歌名>-01 / <歌名>-02）
DOWNLOAD_MODE_FIRST = "first"
DOWNLOAD_MODE_BOTH = "both"
DEFAULT_DOWNLOAD_MODE = DOWNLOAD_MODE_FIRST
DOWNLOAD_VERSION_INDEX = 0  # 「只下载第一版」时取第几个版本（0 = 第一个）

# ── 默认模型（用户 2026-09-05 明确：用 Sway v5.5）──────────────
# 妙响「模型选择」下拉（Semi UI），选项文本形如：
#   "Sway v5.5音乐性和音质俱佳..."  → 匹配子串 "Sway v5.5" 即可点中。
# 注意：用户演练那次模型下拉一直停在「自动」（录制数据 step1–168 全是「自动」），
#       所以「选 Sway v5.5」这一步是用户口述补的，不是从录制学的。
DEFAULT_MODEL = "Sway v5.5"


def log(msg):
    print(msg, flush=True)


def wait_browser_closed(ctx, max_min=20):
    """等到用户把浏览器窗口关掉。

    用守护线程去查 ctx.pages —— 因为浏览器被关掉的方式五花八门，
    主线程直接访问 ctx.pages 有可能永久卡住（Playwright 传输层已死但没抛错），
    那样脚本就永远退出不了。放线程里查，主线程只睡+看标志，到点一定退出。
    """
    state = {"closed": False}

    def watch():
        try:
            while True:
                if len(ctx.pages) == 0:
                    state["closed"] = True
                    return
                time.sleep(1)
        except Exception:
            state["closed"] = True

    threading.Thread(target=watch, daemon=True).start()
    deadline = time.time() + max_min * 60
    while time.time() < deadline:
        if state["closed"]:
            return True
        time.sleep(0.5)
    return False


def open_browser(p):
    """用每用户私有 profile 起一个跟随窗口大小自适应的浏览器。"""
    profile = user_profile(PROFILE_NAME)
    log(f"· 登录态目录（每用户私有，不在 skill 内）：{profile}")
    # 只删这个 profile 自己的 Singleton 锁文件。
    # 注意：不要用 browser_utils.cleanup() —— 它会 kill 掉所有 Chrome/Edge 进程，
    # 会把用户自己正在用的浏览器窗口一起杀掉。
    clear_profile_locks(profile)
    ctx = p.chromium.launch_persistent_context(
        str(profile),
        headless=False,
        args=window_args(False),          # --start-maximized
        viewport=viewport_for(False),     # None → 页面跟随窗口自适应
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(20000)
    return ctx, page


# ─────────────────────────────────────────────── 登录
def do_login(p):
    """打开妙响，让你手动登录；**关掉浏览器窗口**即视为完成并保存登录态。"""
    ctx, page = open_browser(p)
    try:
        page.goto(MX_URL, wait_until="domcontentloaded")
        log("")
        log("=" * 62)
        log("浏览器已打开妙响音乐创作平台。")
        log("  1) 在浏览器里用你的抖音账号登录（扫码 / 手机号都行）")
        log("  2) 登录成功、能看到创作页面后，直接【关掉整个浏览器窗口】")
        log("  3) 脚本会自动保存登录态并结束")
        log("     （之后自动化就能免登了，登录态只存在你自己电脑里）")
        log("=" * 62)
        log("等待中：最长等 20 分钟，你关掉浏览器窗口即完成…")

        if wait_browser_closed(ctx, max_min=20):
            log("· 检测到浏览器已关闭")
        else:
            log("! 已到 20 分钟上限，将结束并保存当前登录态")
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    log("")
    log("✓ 登录流程结束，登录态已保存到每用户私有目录。")
    log("  下一步：python miaoxiang.py --probe   （抓真实页面结构）")


# ─────────────────────────────────────────────── DOM 探测
PROBE_JS = """() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'
        && s.display !== 'none' && parseFloat(s.opacity) > 0.05;
  };
  const rectOf = (el) => {
    const r = el.getBoundingClientRect();
    return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
  };
  const desc = (el) => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type') || '',
    id: el.id || '',
    name: el.getAttribute('name') || '',
    placeholder: el.getAttribute('placeholder') || '',
    aria: el.getAttribute('aria-label') || '',
    cls: ((el.className && el.className.toString) ? el.className.toString() : '').slice(0, 160),
    text: ((el.innerText || el.value || el.getAttribute('title') || '') + '').trim().slice(0, 60),
    visible: vis(el),
    rect: rectOf(el),
  });

  const out = {url: location.href, title: document.title, inputs: [], buttons: [], keywords: []};

  document.querySelectorAll('input, textarea, [contenteditable="true"], [contenteditable="plaintext-only"]')
      .forEach(el => out.inputs.push(desc(el)));

  document.querySelectorAll('button, [role="button"], [class*="btn"], [class*="button"]')
      .forEach(el => {
        const d = desc(el);
        if (d.text || d.aria || d.placeholder) out.buttons.push(d);
      });

  // 关键词命中：帮我们定位"生成/歌词/风格/下载/数量"等入口在哪
  const kws = ['生成', '创作', '歌词', '风格', '描述', '下载', '试听', '上传',
               '数量', '纯音乐', '标题', '歌名', '人声', '灵感', '写词', '作品'];
  const seen = new Set();
  document.querySelectorAll('div, span, p, label, a, li, h1, h2, h3, h4').forEach(el => {
    const t = ((el.innerText || '') + '').trim();
    if (!t || t.length > 30) return;
    if (el.children.length > 2) return;          // 只看比较"叶子"的节点
    if (!kws.some(k => t.includes(k))) return;
    if (seen.has(t)) return;
    seen.add(t);
    out.keywords.push({tag: el.tagName.toLowerCase(), text: t,
                       cls: ((el.className && el.className.toString) ? el.className.toString() : '').slice(0, 120),
                       rect: rectOf(el)});
  });
  return out;
}"""


def do_probe(p):
    """打开妙响，把页面真实结构（输入框/按钮/关键词节点）抓下来存 JSON + 截图。"""
    ctx, page = open_browser(p)
    try:
        page.goto(MX_URL, wait_until="domcontentloaded")
        log("· 页面已打开，等 12 秒让前端渲染完（这类创作页都是 SPA，要等）…")
        time.sleep(12)

        data = page.evaluate(PROBE_JS)
        PROBE_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            page.screenshot(path=str(PROBE_SHOT), full_page=False)
        except Exception as e:
            log(f"  (截图失败: {e})")

        log("")
        log("=" * 62)
        log(f"抓到：输入框 {len(data['inputs'])} 个 / 按钮 {len(data['buttons'])} 个 / 关键词节点 {len(data['keywords'])} 个")
        log(f"结构 → {PROBE_JSON}")
        log(f"截图 → {PROBE_SHOT}")
        log("=" * 62)

        log("\n--- 输入框（前 20）---")
        for d in data["inputs"][:20]:
            flag = "可见" if d["visible"] else "隐藏"
            label = d["placeholder"] or d["aria"] or d["name"] or d["id"] or d["text"]
            log(f"  [{flag}] {d['tag']} type={d['type'] or '-'} «{label[:38]}» cls={d['cls'][:48]}")

        log("\n--- 按钮（前 25）---")
        for d in data["buttons"][:25]:
            flag = "可见" if d["visible"] else "隐藏"
            label = d["text"] or d["aria"] or d["placeholder"]
            log(f"  [{flag}] {d['tag']} «{label[:34]}» cls={d['cls'][:48]}")

        log("\n--- 关键词节点（前 30）---")
        for d in data["keywords"][:30]:
            log(f"  «{d['text'][:34]}» ({d['tag']}) cls={d['cls'][:48]}")

        log("\n（完整数据见 JSON。浏览器保持打开，方便你对照页面看。）")
        log("看完关掉浏览器窗口即可结束探测。")
        wait_browser_closed(ctx, max_min=10)
    finally:
        try:
            ctx.close()
        except Exception:
            pass


WALK_OUT = ROOT / "_mx_walk.json"


def do_walk(p):
    """逐步走查创作页：点开「创建新作品 / 经典创作模式」，每一步抓一次真实 DOM。

    只用【文字】定位，不用 class —— 妙响的 class 全是构建哈希
    （如 navigation-item-wrapper-BSpR89、tabActive-SqkBlT），一发版就变，
    拿它写选择器必错（MiniMax 那次就是吃了这个亏）。

    只做导航，**绝不点「生成」**，不烧额度。
    """
    ctx, page = open_browser(p)
    steps = []
    try:
        page.goto(CREATE_URL, wait_until="domcontentloaded")
        log("· 已打开创作页，等 12 秒让 SPA 渲染完…")
        time.sleep(12)

        def snap(tag):
            d = page.evaluate(PROBE_JS)
            d["_step"] = tag
            steps.append(d)
            try:
                page.screenshot(path=str(ROOT / f"_mx_walk_{tag}.png"), full_page=False)
            except Exception:
                pass
            log(f"  ✓ 抓「{tag}」：输入框{len(d['inputs'])} 按钮{len(d['buttons'])} 关键词{len(d['keywords'])}")

        snap("01_create")

        # 我们自带歌词 → 重点确认「专业模式」里有没有自定义歌词入口，
        # 以及右侧「歌词」标签页是干什么的。
        for i, label in enumerate(["专业模式", "歌词"], start=2):
            try:
                page.get_by_text(label, exact=False).first.click(timeout=6000)
                log(f"· 已点「{label}」，等 5 秒…")
                time.sleep(5)
                snap(f"{i:02d}_{label}")
            except Exception as e:
                log(f"  ✗ 点「{label}」失败：{type(e).__name__}: {e}")

        WALK_OUT.write_text(json.dumps(steps, ensure_ascii=False, indent=2), encoding="utf-8")
        log("")
        log("=" * 62)
        log(f"走查完成，共 {len(steps)} 步 → {WALK_OUT}")
        log("截图：_mx_walk_*.png（浏览器保持打开，方便你对照）")
        log("看完关掉浏览器窗口即结束。")
        log("=" * 62)
        wait_browser_closed(ctx, max_min=10)
    finally:
        try:
            ctx.close()
        except Exception:
            pass


# ─────────────────────────────────────────────── 下载策略
def ask_download_mode(default=DEFAULT_DOWNLOAD_MODE):
    """生成**之前**问一次：两版都要，还是只下第一版。

    返回 DOWNLOAD_MODE_FIRST / DOWNLOAD_MODE_BOTH。
    非交互环境（stdin 不是终端，例如后台运行/被别的程序拉起）不会卡住等输入，
    直接用 default（也就是 --download-mode 传进来的值）。
    """
    log("")
    log("=" * 62)
    log(f"妙响规则：一份歌词点 1 次生成 → 产出【{GEN_VERSIONS_PER_LYRIC} 个不同版本】（数量不可改）。")
    log("  1) 只下载第一个版本   ← 默认，省额度、出歌快")
    log("  2) 两个版本都下载     → 同一份歌词的两版各自保存")
    log("=" * 62)
    try:
        if not sys.stdin or not sys.stdin.isatty():
            log(f"（非交互运行，按「{default}」执行；想改请用 --download-mode first|both）")
            return default
        ans = input("请选择 [1/2]，直接回车=1：").strip()
    except (EOFError, OSError):
        log(f"（读不到输入，按「{default}」执行）")
        return default

    if ans in ("2", "both", "all", "y"):
        return DOWNLOAD_MODE_BOTH
    return DOWNLOAD_MODE_FIRST


def song_dir_for(title, version_index, mode):
    """这首歌该落到 library/ 下的哪个目录名（番茄端 load_song() 的契约）。

    只下第一版 → <歌名>
    两版都下   → <歌名>-01 / <歌名>-02（version_index 从 0 开始）
    """
    from generate import safe_name  # 延迟导入：避开 generate.py 的模块级副作用
    base = safe_name(title)
    if mode == DOWNLOAD_MODE_BOTH:
        return f"{base}-{version_index + 1:02d}"
    return base


# ─────────────────────────────────────────────── 学习录制
LEARN_DIR = ROOT / "learnsession"

# 学习录制的最长等待时间（分钟）。
# ⚠️ 2026-09-05 踩坑：原来写死 30 分钟，用户还在排错就被超时逻辑**强制关掉浏览器**，
#    没保存的页面内容全丢了。用户手动排错一定要给足时间，默认放宽到 120 分钟，
#    并且可以用 --learn-minutes 改。结束条件以"用户关掉浏览器窗口"为准。
LEARN_MAX_MINUTES = 120


CLICK_INIT = r"""
window.__mx_clicks = window.__mx_clicks || [];
if (!window.__mx_click_bound) {
  window.__mx_click_bound = true;
  document.addEventListener('click', function(e){
    var t = e.target;
    function cls(el){ var c = el && el.className; if (!c) return '';
      if (c.baseVal !== undefined) return c.baseVal; return String(c); }
    var chain = [], p = t, depth = 0;
    while (p && depth < 6) {
      chain.push(p.tagName + (cls(p) ? '.' + String(cls(p)).split(' ')[0] : ''));
      p = p.parentElement; depth++;
    }
    window.__mx_clicks.push({
      ts: Date.now(), tag: t && t.tagName,
      text: ((t && (t.innerText || t.textContent)) || '').trim().slice(0, 60),
      aria: (t && t.getAttribute) ? t.getAttribute('aria-label') : null,
      cls: cls(t).slice(0, 70),
      x: Math.round(e.clientX), y: Math.round(e.clientY),
      chain: chain.join(' < ')
    });
  }, true);
}
"""


def do_learn(p, mode=DEFAULT_DOWNLOAD_MODE, max_min=LEARN_MAX_MINUTES, start_url=None):
    """学习模式：打开指定页，你按平时的方式**完整操作一遍**
    （粘贴歌词 → 点生成 → 等两版 → 下载第一版 …），脚本每隔几秒抓一次真实 DOM + 截图，
    存到 learnsession/step_NNN.json|png，形成可回放的时间线。

    操作完关掉浏览器窗口即结束。之后我 diff 各步，提取点击序列 + 结果 DOM，
    再固化成「生成+下载」自动化（对齐 library 契约，番茄端零改动）。

    ⚠️ 2026-09-05 踩坑（必读）：**Playwright 的同步 API 不能在子线程里调用**。
       早先版本把抓 DOM 的循环丢进 threading.Thread，结果每次 page.evaluate()
       都抛 greenlet.error，被 except 静默吞掉 —— 界面上显示"录了 450 步"，
       learnsession/ 里却是 **0 个文件**。所以现在抓取循环一律跑在主线程。

    ⚠️ 2026-09-05 踩坑 2（必读）：**不要用 shutil.rmtree 清目录**。
       本环境（WorkBuddy sitecustomize）把 rmtree 换成了"安全删除"版，
       要求走回收站，而沙箱里没有回收站 → 直接抛
       `OSError: [safe-delete][SAFE_DELETE_FAIL_CLOSED]` 脚本秒退。
       所以改成**每次录制新建带时间戳的子目录**，压根不删东西
       （learnsession/ 已在 .gitignore 里，攒着也不会进仓库）。

    注意：本模式会真实点「生成」，会消耗平台额度 ——
          若平台报 429（额度耗尽，约 23:30 重置），生成会失败，但 UI 导航仍能录到。
    """
    run_dir = LEARN_DIR / time.strftime("run-%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    log(f"· 本次录制目录：{run_dir}")

    ctx, page = open_browser(p)
    page.add_init_script(CLICK_INIT)
    downloads = []
    page.on("download", lambda d: downloads.append(
        {"ts": round(time.time(), 2), "name": d.suggested_filename}))
    seq = 0
    try:
        page.goto(start_url or CREATE_URL, wait_until="domcontentloaded")
        log(f"· 已打开 {'资产页' if start_url == ASSETS_URL else '创作页'}，等 12 秒让 SPA 渲染完…")
        time.sleep(12)
        log("")
        log("=" * 62)
        log("【学习录制中】请在浏览器里按你平时的方式完整操作一遍：")
        if start_url == ASSETS_URL:
            log("  【本次只学「下载」这一段】：进一首歌的「编辑导出」→ 走到下载完成")
            log("  目标：把「资产页的歌怎么下下来」这条路录全（生成那段已经学过了）")
        elif mode == DOWNLOAD_MODE_BOTH:
            log("  粘贴歌词 → 点「生成歌曲」→ 等出 2 个版本 → 【两个版本都下载】")
        else:
            log("  粘贴歌词 → 点「生成歌曲」→ 等出 2 个版本 → 只下载【第一个版本】")
        log(f"  （本次下载策略：{mode}；用 --download-mode first|both 可改）")
        log("  脚本每 4 秒录一次 DOM+截图，并单独记录【每次点击】和【每次下载】。")
        log("  操作完【关掉浏览器窗口】即结束录制 —— 不用赶时间，")
        log(f"  最长会等 {max_min} 分钟（--learn-minutes 可改），期间不会自己关窗。")
        log("=" * 62)

        deadline = time.time() + max_min * 60
        while time.time() < deadline:
            # 用户关窗 → pages 归零 → 结束。同步 API，只能主线程查，所以不开线程。
            try:
                if len(ctx.pages) == 0:
                    log("· 检测到浏览器已关闭")
                    break
            except Exception:
                log("· 浏览器已断开")
                break

            seq += 1
            try:
                d = page.evaluate(PROBE_JS)
                d["_seq"] = seq
                d["_url"] = page.url
                (run_dir / f"step_{seq:03d}.json").write_text(
                    json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                page.screenshot(path=str(run_dir / f"step_{seq:03d}.png"), full_page=False)
                # 点击轨迹 + 下载事件：单独落盘，学习「编辑导出→下载」的关键证据
                try:
                    clicks = page.evaluate("() => window.__mx_clicks || []")
                except Exception:
                    clicks = []
                (run_dir / "clicks.json").write_text(
                    json.dumps(clicks, ensure_ascii=False, indent=2), encoding="utf-8")
                (run_dir / "downloads.json").write_text(
                    json.dumps(downloads, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                # 不再静默吞掉：写不出来要看得见，否则又会出现"录了几百步其实 0 文件"
                log(f"  ! 第 {seq} 次抓取失败：{type(e).__name__}: {e}")
            time.sleep(4)
        else:
            log(f"! 已到 {max_min} 分钟上限，结束录制（浏览器会关闭）")
    finally:
        try:
            ctx.close()
        except Exception:
            pass

    saved = len(list(run_dir.glob("step_*.json")))
    log("")
    log(f"✓ 录制结束：计数 {seq} 步，实际存下 {saved} 个快照 → {run_dir}")
    if seq and not saved:
        log("  ⚠️ 一步都没存下来！抓 DOM 出错，别拿这份数据去写自动化。")
    log("  下一步：把这份时间线交给我，我 diff 后写出生成+下载自动化。")


# ─────────────────────────────────────────────── 生成 + 下载
# 以下选择器全部来自真实页面（2026-09-05 抓的 DOM），不猜：
#   创作页·专业模式  歌词 → div.ql-editor（Quill，全页只有一个 contenteditable）
#                   曲风 → textarea[placeholder^="输入你的创作灵感或歌曲风格"]
#                   生成 → 文字「生成歌曲」，class 含 disabled 时说明还没填够
#   资产页           先点 tab「生成结果」（默认落在「对话记录」，不切没有卡片）
#                   卡片 → [class*="generatedSongListItem"]
#                   ⋯    → 卡片内 [aria-label="更多操作"]
#                   菜单 → 文字「下载」
# ⚠️ aria-label（更多操作/收藏歌曲）是语义化的；构建哈希的 class 会变，aria 不会。

CARDS_JS = """() => Array.from(
  document.querySelectorAll('[class*="generatedSongListItem"]')
).map(el => (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 90))"""

# ⚠️ 2026-09-17 血泪修：`CARDS_JS` 会把**还在渲染的骨架卡**也吐出来（innerText=''）。
#    批 3 第 1 首《踏月寻你》就是这样：卡片数量够了，但新卡签名全是空串
#    → export_via_editor 拿 sig='' 去 locate_card_index，key 为空 → 报「找不到卡片：」
#      （冒号后面什么都没有，就是这个特征）。
#    修法：只要「签名非空 **且** 含 mm:ss / 日期」的真卡（骨架卡两者都没有）。
#    ⚠️ 注意：真卡的 innerText 本身**已经含**「编辑器导出」四字（实测
#      '同路一程又一程 编辑器导出 03:22 · 2026-09-17 19:04'），所以这里**不要**再补后缀，
#      否则会变成「…编辑器导出…编辑器导出」，前缀匹配反而对不上。
CARDS_JS_STRICT = """() => Array.from(
  document.querySelectorAll('[class*="generatedSongListItem"]')
).map(el => (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 90))
 .filter(t => t.length > 0 && (/\\d{1,2}:\\d{2}/.test(t) || /\\d{4}-\\d{2}-\\d{2}/.test(t)))"""

SET_LYRIC_JS = """([sel, text]) => {
  const el = document.querySelector(sel);
  if (!el) return -1;
  const html = text.split(/\\n+/).map(s => '<p>' + s + '</p>').join('');
  el.innerHTML = html;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return (el.innerText || '').trim().length;
}"""


# 本次实际使用的曲库目录。resolve_workdir() 里设一次，给「下载中转目录」用——
# 这样无论靠 --workdir 还是靠设置文件选中的目录，下载中的临时文件都落在同一块盘上，
# 不会出现「曲库在 D 盘、下载峰值却把 C 盘写满」。
_ACTIVE_WORKDIR = None


def _dl_tmp_dir():
    """下载中转目录：跟着本次实际使用的曲库目录走。"""
    return work_temp_dir(_ACTIVE_WORKDIR)


def resolve_workdir(arg=None):
    """决定任务表/歌词/曲库目录。

    优先级：--workdir 参数 > settings.json 里的 default workdir
            > 当前目录(有 tasks.csv) > 脚本目录

    ⚠️ settings.json 的优先级**高于**「当前目录」是有意的：用户设过一次
    「我东西都放 D 盘」之后，无论在哪个目录敲命令，曲库都该落在 D 盘，
    否则批量下载又会悄悄写满 C 盘（这正是 2026-09-20 用户提的问题）。
    """
    global _ACTIVE_WORKDIR
    if arg:
        _ACTIVE_WORKDIR = Path(arg).expanduser().resolve()
    else:
        cfg_dir = default_workdir()
        if cfg_dir is not None:
            _ACTIVE_WORKDIR = cfg_dir.expanduser().resolve()
        else:
            cwd = Path.cwd()
            _ACTIVE_WORKDIR = cwd if (cwd / "tasks.csv").exists() else ROOT
    return _ACTIVE_WORKDIR


def load_song_tasks(workdir, only_title=None):
    """读 workdir/tasks.csv；跳过空行和「示例-」开头的示例。"""
    import csv
    tasks = []
    p = workdir / "tasks.csv"
    if not p.exists():
        log(f"✗ 找不到任务表：{p}")
        return tasks
    with open(p, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            title = (row.get("歌名") or "").strip()
            if not title or title.startswith("示例"):
                continue
            tasks.append({
                "no": len(tasks) + 1,
                "title": title,
                "style": (row.get("风格描述") or "").strip(),
                "lyrics_file": (row.get("歌词文件") or "").strip(),
            })
    if only_title:
        tasks = [t for t in tasks if t["title"] == only_title]
    return tasks


def load_lyrics(workdir, lyrics_dir, lyrics_file):
    d = workdir / lyrics_dir
    p = d / lyrics_file
    if not p.suffix:
        p = d / (lyrics_file + ".txt")
    if not p.exists():
        raise FileNotFoundError(f"歌词文件不存在：{p}")
    return p.read_text(encoding="utf-8").strip()


def select_model(page, model):
    """选模型：点开「模型选择」下拉 → 点文本含 model 的 option。

    model 例：'Sway v5.5' / 'sway v5.5'。Playwright 的 has_text 大小写不敏感。
    选项结构（探针实测）：div.semi-select-option-custom，文本含模型名+描述。
    ⚠️ 下拉列表是 overflow 滚动容器，目标项可能不在可见区 → 点前要先 scrollIntoView。
    """
    m = (model or "").strip()
    if not m:
        return
    # 打开：模型下拉触发器 = class 含 "modelSelectionSelect"（前缀稳定，不受构建哈希影响；
    #   注意「模型选择」只是它容器里的 label 文本，不在触发器 innerText 里，不能靠文本过滤）
    try:
        page.locator('[class*="modelSelectionSelect"]').first.click(timeout=8000)
    except Exception:
        # 回退：找含「模型选择」文本的容器，再点它里面的 .semi-select
        page.evaluate("""() => {
          const all = Array.from(document.querySelectorAll('*'));
          const lab = all.find(e => (e.innerText||'').trim().startsWith('模型选择'));
          if (!lab) return;
          let n = lab;
          for (let i=0; i<6 && n; i++) {
            const s = n.querySelector('[class*="semi-select"]');
            if (s) { s.click(); return; }
            n = n.parentElement;
          }
        }""")
    page.wait_for_timeout(1500)

    # ⚠️ 关键：模型下拉顶部有一个「自动」开关（class 含 autoSwitch，开启时 semi-switch-checked）。
    # 开启时只显示"自动推荐"，真正的模型列表被隐藏（display:none）；必须先关掉它，列表才出现。
    # （用户手动选 Sway v5.5 时也是先关这个开关 —— 录制数据里没这一步，是这次探针补出来的）
    auto_on = page.evaluate("""() => {
      const sw = document.querySelector('[class*="autoSwitch"]');
      return sw ? sw.className.includes('semi-switch-checked') : false;
    }""")
    if auto_on:
        page.locator('[class*="autoSwitch"]').first.click(timeout=8000)
        page.wait_for_timeout(1500)
        log("  · 已关掉「自动」开关，模型列表已展开")

    # 用真实鼠标坐标点击：先 JS 定位+滚动到中心，取中心坐标，再 page.mouse.click。
    # （Semi 选项靠 pointer/mouse 事件；关掉自动开关后选项才可见，scrollIntoView 才能生效）
    box = page.evaluate("""(txt) => {
      const norm = s => (s || '').toLowerCase();
      const t = norm(txt);
      const opts = Array.from(document.querySelectorAll('[class*="semi-select-option-custom"]'));
      const el = opts.find(o => norm(o.innerText).includes(t));
      if (!el) return null;
      el.scrollIntoView({block: 'center'});
      const r = el.getBoundingClientRect();
      return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
    }""", m)
    if not box:
        # 回退：只按首词匹配（如 "Sway"）
        first = m.split()[0] if m.split() else m
        opt = page.locator('[class*="semi-select-option-custom"]').filter(has_text=first).first
        if opt.count() == 0:
            raise RuntimeError(f"模型下拉里找不到含「{m}」的选项")
        opt.scroll_into_view_if_needed(timeout=5000)
        box = opt.bounding_box()
    if not box:
        raise RuntimeError(f"模型下拉里找不到含「{m}」的选项（无法定位点击坐标）")
    page.mouse.click(box["x"], box["y"])
    page.wait_for_timeout(1200)
    page.wait_for_timeout(800)
    # 校验：模型触发器（modelSelectionSelect）现在应显示已选模型名
    shown = page.evaluate("""() => {
      const el = document.querySelector('[class*="modelSelectionSelect"]');
      return el ? el.innerText.trim() : '';
    }""")
    log(f"  · 已选模型：{m}（触发器显示：{shown[:40]}）")
    if shown and m.lower() not in shown.lower():
        log(f"  ⚠️ 触发器显示「{shown}」未含「{m}」，模型选择可能没生效")


def fill_create_page(page, lyric, style, model=DEFAULT_MODEL, wait_ms=6000):
    """创作页 → 专业模式 → 选模型 → 填歌词+曲风 → 点生成歌曲。"""
    page.goto(CREATE_URL, wait_until="domcontentloaded")
    log("· 创作页已打开，等渲染…")
    page.wait_for_timeout(wait_ms)

    log("· 切到「专业模式」")
    page.get_by_text("专业模式", exact=True).first.click(timeout=10000)
    page.wait_for_timeout(2500)

    log(f"· 选模型「{model}」")
    select_model(page, model)

    # ── 歌词：Quill 编辑器，先试真实键盘输入
    editor = page.locator("div.ql-editor").first
    editor.wait_for(timeout=10000)
    editor.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.keyboard.insert_text(lyric)
    page.wait_for_timeout(400)
    got = (editor.inner_text() or "").strip()
    log(f"  歌词：填入 {len(lyric)} 字 / 编辑器回读 {len(got)} 字")
    if len(got) < max(20, len(lyric) * 0.5):
        # 回退：直接写 DOM（insert_text 在 Quill 上偶发不生效）
        n = page.evaluate(SET_LYRIC_JS, ["div.ql-editor", lyric])
        log(f"  ! 键盘输入没吃进去，改用直接写 DOM，回读 {n} 字")
        page.wait_for_timeout(400)
        got2 = (editor.inner_text() or "").strip()
        if len(got2) < max(20, len(lyric) * 0.5):
            raise RuntimeError(f"歌词填不进去（回读只有 {len(got2)} 字）")

    # ── 曲风
    page.get_by_placeholder("输入你的创作灵感或歌曲风格", exact=False).first.fill(style)
    page.wait_for_timeout(600)
    log(f"  曲风：{len(style)} 字")

    # ── 生成（等按钮从 disabled 变可点）
    btn = page.get_by_text("生成歌曲", exact=True).first
    for _ in range(40):
        if "disabled" not in (btn.get_attribute("class") or ""):
            break
        page.wait_for_timeout(1000)
    else:
        raise RuntimeError("「生成歌曲」一直不可点（多半是歌词/曲风没填进去）")
    btn.click()
    log("  ✓ 已点「生成歌曲」")


def ensure_genresult_tab(page, tries=4):
    """进资产页并**确认**停在「生成结果」tab，切不过来就重试。

    ⚠️ 这是 egWPo9 下载失败的根因：资产页默认落在「对话记录」tab，
       那里的卡片**没有**「更多操作」按钮。若没切到「生成结果」，
       外层卡片 [class*="generatedSongListItem"] 找得到、内层按钮找不到，
       报错形如 waiting for locator(...).first.locator('[aria-label="更多操作"]')。
       → 必须用「第一张卡片里有没有更多操作按钮」来**验证** tab 真的切过来了。
    """
    page.goto(ASSETS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(7000)
    for t in range(tries):
        try:
            page.get_by_text("生成结果", exact=True).first.click(timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        ok = page.evaluate("""() => {
          const c = document.querySelector('[class*="generatedSongListItem"]');
          if (!c) return false;
          const m = c.querySelector('[aria-label="更多操作"]');
          return !!m && getComputedStyle(m).display !== 'none';
        }""")
        if ok:
            return True
        log(f"  （生成结果 tab 未就绪，重试 {t + 1}/{tries}）")
    return False


def open_assets_and_tab(page):
    """进资产页并切到「生成结果」tab（带校验+重试）。"""
    return ensure_genresult_tab(page)


def wait_new_cards(page, before, need, max_min, poll=30):
    """轮询资产页，等这一轮的新卡片够数。返回新卡片签名（newest first）。

    ⚠️ 2026-09-17 修：必须用 CARDS_JS_STRICT —— 宽松版会把骨架卡（innerText=''）
       算进来，导致「够数了但签名是空串」，下游 export_via_editor 定位必失败。
    """
    before_set = set(before)
    last = []
    deadline = time.time() + max_min * 60
    while time.time() < deadline:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        try:
            page.get_by_text("生成结果", exact=True).first.click(timeout=6000)
            page.wait_for_timeout(2500)
        except Exception:
            pass
        cur = _cards_strict(page)
        new = [c for c in cur if c not in before_set]
        last = new
        if len(new) >= need:
            return new
        log(f"  …新卡片 {len(new)}/{need}，继续等（每 {poll}s 一查，上限 {max_min} 分钟）")
        page.wait_for_timeout(poll * 1000)
    try:
        return [c for c in _cards_strict(page) if c not in before_set]
    except Exception:
        return last


def _cards_strict(page):
    """取「真卡」签名（骨架卡/空签名已被 JS 侧过滤掉）。"""
    try:
        rows = page.evaluate(CARDS_JS_STRICT)
        return [r for r in rows if (r or "").strip()]
    except Exception:
        return [c for c in page.evaluate(CARDS_JS) if (c or "").strip()]


def click_download(page):
    """菜单里点「下载」，把 Download 对象交回来。"""
    tries = (
        ("role=menuitem 下载", lambda: page.get_by_role("menuitem", name="下载").first),
        ("text=下载", lambda: page.get_by_text("下载", exact=True).first),
    )
    for desc, mk in tries:
        try:
            loc = mk()
            if loc.count() == 0:
                continue
            with page.expect_download(timeout=60000) as di:
                loc.click(timeout=6000)
            return di.value, desc
        except Exception as e:
            log(f"    （{desc} 没成：{type(e).__name__}）")
    return None, None


def download_card(page, idx):
    """下载第 idx 张卡片（0 = 最新）。返回 (临时路径, 平台标题)。"""
    card = page.locator('[class*="generatedSongListItem"]').nth(idx)
    try:
        card.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    card.hover(timeout=8000)           # Semi 图标按钮 hover 后才稳定可点
    page.wait_for_timeout(600)
    more = card.locator('[aria-label="更多操作"]').first
    try:
        more.click(timeout=8000)
    except Exception:
        more.click(timeout=8000, force=True)
    page.wait_for_timeout(1000)
    plat_title = ((card.inner_text() or "").split("编辑器导出")[0]).strip().split("\n")[0][:40]
    dl, how = click_download(page)
    if dl is None:
        raise RuntimeError(f"第 {idx} 张卡片点不出下载")
    log(f"    ✓ 触发下载（{how}）：{dl.suggested_filename}")
    tmp = Path(tempfile.mkdtemp(dir=str(_dl_tmp_dir()))) / dl.suggested_filename
    dl.save_as(str(tmp))
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    return tmp, plat_title


# ─────────────────────────── 「编辑导出 → 下载」真实链路（2026-09-12 用户示范学到）
# 真相：资产页「生成结果」里**原始生成**的歌，⋯→「下载」永远是 aria-disabled=true。
#      必须先走一遍：卡片「编辑」→ 下拉「编辑器」→ 编辑器顶部「导出」→ 弹窗底部「导出」，
#      平台会产出一张**新的**资产，卡片上带「编辑器导出」标记，且**时长相同**。
#      只有这张「编辑器导出」的卡，⋯→「下载」才是可用的。
# 证据：用户示范 15:18 产出「编辑器导出」卡 → ⋯→下载 → toast「下载已开始」→ 醉了才敢哭.mp3
#
# 选择器（来自 clicks.json 的真实元素链 + 步骤快照）：
#   卡片「编辑」  SPAN.editMenuTrigger（hover 后出现在 DIV.hoverActions 内）
#   下拉「编辑器」 DIV.semi-dropdown-content → itemLabel 文本=编辑器
#   编辑器「导出」 DIV.controlBarWrapper 内 button 文本=导出（右上，~1188,22）
#   弹窗           DIV.semi-modal → DIV.exportContent
#   弹窗确认导出   DIV.exportFooter 内 button 文本=导出
#   ⋯ / 下载      卡片 [aria-label="更多操作"] / 下拉 文本=下载

def locate_card_index(page, sig):
    """按 CARDS_JS 的签名文本定位"原始生成卡"的序号；找不到返回 -1。

    ⚠️ 2026-09-17 修：以前只做 startsWith 精确匹配，卡片文本稍有漂移（重新渲染后
       多出「生成中」徽标、或 innerText 归一化后空格不同）就全盘失败。现在分三层：
         ① 前缀精确匹配（startsWith 前 60 字）
         ② 降级：剥离「· 时间」「时长」后的**歌名**做包含匹配
         ③ 降级：歌名匹配多条时取第一条
    """
    key = (sig or "").split(" 编辑器导出")[0].strip()
    if not key:
        raise RuntimeError(
            "卡片签名为空——多半是 wait_new_cards 把骨架卡当成了新卡。"
            "请检查 CARDS_JS_STRICT 是否生效（应为 _cards_strict 取卡）。")
    return page.evaluate(
        """(key) => {
          const norm = el => ((el.innerText || '').replace(/\\s+/g, ' ').trim());
          const cards = [...document.querySelectorAll('[class*="generatedSongListItem"]')]
                          .map((el, i) => ({i, t: norm(el)}))
                          .filter(x => x.t.length > 0);
          const head = key.slice(0, 60);
          // ① 前缀精确
          let hit = cards.find(x => x.t.slice(0, 90).startsWith(head));
          if (hit) return hit.i;
          // ② 用歌名（去掉「· 日期时间」和结尾 mm:ss）做包含匹配
          const name = key.replace(/\\s*·\\s*\\d{4}-\\d{2}-\\d{2}.*$/, '')
                          .replace(/\\s*\\d{1,2}:\\d{2}\\s*$/, '').trim();
          if (!name) return -1;
          hit = cards.find(x => x.t.includes(name));
          return hit ? hit.i : -1;
        }""", key)


def dismiss_overlays(page, rounds=4):
    """关掉编辑器里挡路的弹窗/遮罩/气泡。

    ⚠️ 2026-09-12 用户指出 + 实测证实：进编辑器后顶部会有一个**全宽**的
       「正在添加素材到轨道…／已添加到轨道」气泡（.semi-toast-wrapper
       rect=[0,0,1280,0]），外加一层 semi-modal-mask（rect=[0,0,1280,720]、
       pointer-events:auto、z-index:1000）在淡出。它们把右上角的「导出」
       按钮压住 → 点击被吞掉、弹窗出不来。必须先把它们关掉。
       × 按钮 = [class*="semi-toast"] [aria-label="close"]。
    """
    for _ in range(rounds):
        acted = False
        try:
            mask = page.locator('[class*="semi-modal-mask"]').first
            if mask.count() and mask.is_visible():
                closer = page.locator(
                    '[class*="semi-modal"] [aria-label="close"], '
                    '[class*="semi-modal"] [aria-label="关闭"]').first
                if closer.count() and closer.is_visible():
                    closer.click(timeout=3000)
                else:
                    page.keyboard.press("Escape")
                acted = True
                page.wait_for_timeout(1500)
                continue
        except Exception:
            pass
        try:
            x = page.locator('[class*="semi-toast"] [aria-label="close"]').first
            if x.count() and x.is_visible():
                x.click(timeout=3000)
                acted = True
                page.wait_for_timeout(1000)
                continue
        except Exception:
            pass
        if not acted:
            break
    page.wait_for_timeout(600)


VERSION2_SUFFIX = "（动听版）"


def version_display_title(title, version_index):
    """两版必须有区分名（用户 2026-09-12 明确要求）：
        第 1 版（version_index=0）→ 原歌名
        第 2 版（version_index=1）→ 原歌名（动听版）
    ⚠️ 番茄端 `fanqie_upload.load_song()` **用 meta.title 当歌名**，
       且第 1043 行有重名检测（`dup = len(titles) > len(set(titles))`）——
       两版同名会被判重、没法区分。所以平台导出名 + meta.title 都要带这层区分。
    """
    title = (title or "").strip()
    if version_index <= 0:
        return title
    if version_index == 1:
        return f"{title}{VERSION2_SUFFIX}"
    return f"{title}（动听版{version_index}）"


def export_via_editor(page, sig, title=None):
    """把某张"原始生成卡"走一遍「编辑 → 编辑器 → 导出」。
    成功后会多出一张带「编辑器导出」标记的新卡（那张才能下载）。

    title：导出弹窗「歌曲名」输入框要填的名字（= 我们的歌名）。
    ⚠️ 用户 2026-09-12 明确要求：导出前必须先把「歌曲名」填成歌名，
       否则平台产出的导出件**全叫「新项目」**，事后分不清下载的是哪首歌。"""
    title = (title or (sig or "").split(" ")[0]).strip()
    idx = locate_card_index(page, sig)
    if idx < 0:
        raise RuntimeError(f"找不到卡片：{sig[:40]!r}（页面卡片数 {page.locator('[class*=\"generatedSongListItem\"]').count()}）")
    card = page.locator('[class*="generatedSongListItem"]').nth(idx)
    card.scroll_into_view_if_needed(timeout=5000)
    # 卡片上的「编辑」（hover 才出现）
    # ⚠️ 2026-09-17 修：以前直接 get_by_text("编辑", exact=True).click() 一次，
    #    实测会因为 hover 状态丢失 / 命中错节点而 8s 超时（批 3 第 1 版就死在这）。
    #    现在分三层重试，并且每次都重新 hover 把按钮「唤」出来。
    edit_ok = False
    for attempt in range(3):
        try:
            card.scroll_into_view_if_needed(timeout=5000)
            card.hover(timeout=8000)
            page.wait_for_timeout(900)
            # 优先用注释里记的真实类名，退回文本匹配
            btn = card.locator('[class*="editMenuTrigger"]').first
            if btn.count() == 0 or not btn.is_visible():
                btn = card.get_by_text("编辑", exact=True).first
            btn.click(timeout=6000)
            edit_ok = True
            break
        except Exception as e:
            log(f"    （第 {attempt + 1} 次点「编辑」没成功：{type(e).__name__}）")
            page.wait_for_timeout(1500)
    if not edit_ok:
        raise RuntimeError(f"卡片「编辑」按钮点不动（卡片序 {idx}）")
    page.wait_for_timeout(1300)
    # 下拉里的「编辑器」
    page.locator('[class*="semi-dropdown-content"]').get_by_text(
        "编辑器", exact=True).first.click(timeout=8000)
    page.wait_for_url("**/playground**", timeout=60000)
    # ⚠️ 进编辑器后必须等加载充分：实测 7 秒时点「导出」弹窗不出来，
    #    用户实际操作也是进编辑器约 18 秒后才点。
    log("    · 已进编辑器，等加载（18s）…")
    page.wait_for_timeout(18000)
    modal = page.locator('[class*="semi-modal-body"]').first
    top_export = page.locator('[class*="controlBarWrapper"] button').filter(has_text="导出").first
    opened = False
    for attempt in range(3):
        try:
            top_export.click(timeout=10000)
        except Exception:
            top_export.click(timeout=10000, force=True)
        try:
            modal.wait_for(state="visible", timeout=12000)
            opened = True
            break
        except Exception:
            log(f"    （第 {attempt + 1} 次点「导出」没弹窗，重试）")
            page.wait_for_timeout(3000)
    if not opened:
        raise RuntimeError("编辑器「导出」弹窗始终没出来")
    page.wait_for_timeout(1500)
    # ⚠️ 必做：把「歌曲名」改成我们的歌名（默认是「新项目」，不改就分不清哪首）
    if title:
        inp = page.locator('[class*="exportContent"] input.semi-input').first
        if inp.count() == 0:
            inp = page.locator('[class*="exportContent"] input[type="text"]').first
        if inp.count() > 0:
            inp.click(timeout=5000)
            inp.fill("")
            inp.fill(title)
            got = inp.input_value()
            if got != title:                       # React 受控组件兜底：真键盘输入
                inp.click()
                page.keyboard.press("Control+A")
                inp.press_sequentially(title, delay=40)
                got = inp.input_value()
            log(f"    · 歌曲名已填：{got!r}")
        else:
            log("    ! 没找到「歌曲名」输入框，导出件可能仍叫「新项目」")
    log("    · 导出弹窗已出，按默认「并轨导出」确认")
    page.locator('[class*="exportFooter"] button').filter(has_text="导出").first.click(timeout=8000)
    # 等弹窗关闭 = 导出受理。
    # ⚠️ 2026-09-17 修：这里超时**不代表失败** —— 平台导出要几分钟，弹窗可能一直挂着进度，
    #    也可能被后续弹窗替换导致 `modal` 这个旧句柄永远等不到 hidden。
    #    所以超时只记一笔，真正的「导出好了没」交给 download_exported 轮询新卡判断。
    page.set_default_timeout(30000)
    try:
        modal.wait_for(state="hidden", timeout=60000)
    except Exception:
        log("    （导出弹窗 60s 未关闭——不影响，改由「等新导出卡出现」判定）")
    page.wait_for_timeout(2000)


def download_exported(page, sig, title=None, before_exported=None, max_wait_min=6):
    """回资产页，找**本次刚产出**的那张「编辑器导出」卡 → ⋯ → 下载。

    title：这张导出件的名字（= 版本区分名，如「歌名（动听版）」）。

    ⚠️⚠️ 2026-09-17 严重修复（张冠李戴 → 三个文件 MD5 完全相同）：
       旧实现找不到同名卡时会 `cards.first` **静默兜底**，抓上一轮遗留的导出卡，
       结果两首歌入库了同一个音频（实测三份文件 MD5 均 27efa0b8…）。
       现在改为：
         ① 先用 `title` 精确匹配；
         ② 匹配不到就**等**（导出要几分钟），等到出现「不在 before_exported 里」的新卡；
         ③ 实在等不到 → **报错**，绝不拿别的卡顶包。
    before_exported：调用前已经存在的导出卡集合（用于识别「本次新增」）。
    """
    open_assets_and_tab(page)
    title = (title or (sig or "").strip().split(" ")[0]).strip()
    before = set(before_exported or [])

    def exported_sigs():
        try:
            rows = page.evaluate(CARDS_JS_STRICT)
            return [r for r in rows if "编辑器导出" in (r or "")]
        except Exception:
            return []

    deadline = time.time() + max_wait_min * 60
    card = None
    why = ""
    while True:
        # ① 优先按我们填的歌名精确找（导出件名 = 我们填的 disp）
        if title:
            cand = page.locator(
                '[class*="generatedSongListItem"]', has_text="编辑器导出").filter(
                has_text=title)
            if cand.count() > 0:
                card = cand.first
                why = f"按名匹配《{title}》"
                break
        # ② 退一步：本次新增的导出卡（before 里没有的）
        new_exported = [s for s in exported_sigs() if s not in before]
        if new_exported:
            target = new_exported[0]
            idx = locate_card_index(page, target)
            if idx >= 0:
                card = page.locator('[class*="generatedSongListItem"]').nth(idx)
                why = f"按新增导出卡匹配（{target[:28]}）"
                break
        if time.time() >= deadline:
            break
        log(f"    …导出件还没出现，等 20s 再看（上限 {max_wait_min} 分钟）")
        page.wait_for_timeout(20000)
        open_assets_and_tab(page)

    if card is None:
        raise RuntimeError(
            f"等不到本次的「编辑器导出」卡（{max_wait_min} 分钟内）——"
            f"**拒绝拿别的卡顶包**，请检查平台导出是否完成")

    log(f"    · 定位导出卡：{why}")
    card.scroll_into_view_if_needed(timeout=5000)
    card.hover(timeout=8000)
    page.wait_for_timeout(600)
    card.locator('[aria-label="更多操作"]').first.click(timeout=8000)
    page.wait_for_timeout(1000)
    plat_title = ((card.inner_text() or "").split("编辑器导出")[0]).strip().split("\n")[0][:40]
    dl, how = click_download(page)
    if dl is None:
        raise RuntimeError("「编辑器导出」卡点不出下载（下载可能仍被禁用）")
    log(f"    ✓ 已触发下载（{how}）：{dl.suggested_filename}")
    tmp = Path(tempfile.mkdtemp(dir=str(_dl_tmp_dir()))) / dl.suggested_filename
    dl.save_as(str(tmp))
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    return tmp, plat_title


def save_song(workdir, cfg, task, lyric, mode, items, model=DEFAULT_MODEL):
    """按 library 契约落盘。

    items = [(tmp_path, platform_title[, display_title])]
      display_title：写进 meta.json 的歌名。第 1 版=原歌名，第 2 版=原歌名（动听版），
                     番茄端按 meta.title 取歌名，两版不能同名。
    """
    from datetime import datetime
    from cover import make_cover

    lib = workdir / cfg.get("library_dir", "library")
    lib.mkdir(parents=True, exist_ok=True)
    min_bytes = cfg.get("download", {}).get("min_audio_bytes", 200000)
    saved = []
    seen_md5 = {}                      # md5 → folder，防同一音频被存成两首
    for i, it in enumerate(items):
        tmp, plat_title = it[0], it[1]
        disp_title = it[2] if len(it) > 2 and it[2] else version_display_title(task["title"], i)
        # ⚠️ 2026-09-17 修：version_index 以前用 enumerate 的 i，但某版失败时序号会错位，
        #    出现「title=（动听版） 却 version_index=0」的矛盾记录。
        #    → 改为**从 disp_title 反推**，它才是权威（带「（动听版）」就是第 2 版）。
        v_idx = 1 if VERSION2_SUFFIX in disp_title else 0
        folder = song_dir_for(task["title"], v_idx, mode)
        dest_dir = lib / folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        size = tmp.stat().st_size
        if size < min_bytes:
            log(f"  ✗ {folder}：只有 {size} 字节（< {min_bytes}），像坏文件，跳过")
            continue
        # ⚠️ 2026-09-17 新增防重护栏：批 3 曾把同一个音频存成两首歌
        #    （三份文件 MD5 均 27efa0b8…）。入库前算 MD5，本批内撞了就拒收。
        import hashlib
        md5 = hashlib.md5(Path(tmp).read_bytes()).hexdigest()
        if md5 in seen_md5:
            log(f"  ✗ {folder}：音频与 {seen_md5[md5]} 完全相同（md5 {md5[:8]}）"
                f"——疑似下载张冠李戴，**拒收**，请检查导出卡定位")
            continue
        # 再跟 library 里已存在的目录比一遍（跨批次也能拦）
        dup_of = None
        for other in lib.iterdir():
            if not other.is_dir() or other.name == folder:
                continue
            oa = other / "audio.mp3"
            if oa.exists() and oa.stat().st_size == size:
                try:
                    if hashlib.md5(oa.read_bytes()).hexdigest() == md5:
                        dup_of = other.name
                        break
                except Exception:
                    pass
        if dup_of:
            log(f"  ✗ {folder}：音频与已有 {dup_of} 完全相同（md5 {md5[:8]}）"
                f"——**拒收**，请检查导出卡定位")
            continue
        seen_md5[md5] = folder
        dest = dest_dir / "audio.mp3"   # ⚠️ fanqie_upload.load_song 硬编码找 audio.mp3
        shutil.copyfile(str(tmp), str(dest))
        (dest_dir / "lyrics.txt").write_text(lyric, encoding="utf-8")
        meta = {
            "title": disp_title,          # 番茄端按此取名；第2版带「（动听版）」防重名
            "style": task["style"],
            "instrumental": False,
            "source_url": ASSETS_URL,
            "audio_file": "audio.mp3",
            "size_bytes": size,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "task_no": task["no"],
            "platform": "miaoxiang",
            "platform_title": plat_title,  # 妙响自动起的名字，仅留档
            "model": model,                # 用的哪个模型（用户要求 Sway v5.5）
            "version_index": v_idx,
        }
        (dest_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            make_cover(dest_dir / "cover.png", task["title"], task["style"])
        except Exception as e:
            log(f"  ⚠ 封面失败（不影响音频）：{e}")
        log(f"  ✓ {folder}/  audio.mp3 ({size/1024/1024:.1f} MB)  平台名《{plat_title}》")
        saved.append(dest_dir)
    return saved


def load_config(workdir):
    """读工作目录的 config.json；**读不到就返回 {}**，不报错。

    ⚠️ 2026-09-20 修：原先这里是硬读 —— `json.loads((workdir/"config.json").read_text())`。
    但 config.json 自己的 `_说明` 就写着「本文件只服务 MiniMax 历史备选端，
    妙响用固定选择器、不读本文件」，而 cfg 在本文件里**通篇只用 .get() 带默认值**
    （library_dir / lyrics_dir / download.min_audio_bytes），所以缺文件根本不影响运行。

    实测踩坑：`D:\\music-workflow` 是 migrate_library.py 搬出来的目录、没跑过
    init_workdir.py（config.json 只在 init 的 COPY_FILES 里），于是 `--gen` 直接
    `FileNotFoundError: 'D:\\music-workflow\\config.json'` —— 报错看不出跟妙响有什么关系，
    用户完全不知道该建什么文件。对齐 login_check.py 的同一套做法（「读不到就返回空字典」）。
    """
    p = Path(workdir) / "config.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log(f"提示：{p} 不存在，按默认配置继续"
            f"（妙响不读它；缺省 library_dir=library、lyrics_dir=lyrics）")
        return {}
    except Exception as e:
        log(f"⚠️ {p} 解析失败（{e}），按默认配置继续")
        return {}


def do_gen(p, workdir, mode, max_wait_min, only_title=None, model=DEFAULT_MODEL):
    tasks = load_song_tasks(workdir, only_title)
    if not tasks:
        log("没有待生成的任务（tasks.csv 里没有非「示例-」开头的行）。")
        return
    cfg = load_config(workdir)
    need = GEN_VERSIONS_PER_LYRIC if mode == DOWNLOAD_MODE_BOTH else 1

    log("")
    log("=" * 62)
    log(f"工作目录：{workdir}")
    log(f"待生成：{len(tasks)} 首  |  下载策略：{mode}（每首下 {need} 个版本）")
    log("=" * 62)

    ctx, page = open_browser(p)
    page.set_default_timeout(30000)
    total = 0

    def browser_alive():
        """浏览器窗口被关掉（用户手动关 / 崩溃）时返回 False。

        ⚠️ 2026-09-17 加：批 3 第 2 首第 2 版死在 TargetClosedError 上，
           错误信息完全看不出「窗口被关了」，得靠这一层给出人话提示。
        """
        try:
            return len(ctx.pages) > 0
        except Exception:
            return False

    try:
        for task in tasks:
            if not browser_alive():
                log("")
                log("✗ 浏览器窗口已被关闭，剩下的歌不再执行。")
                log("  （若是你手动关的：重新跑一次即可，已入库的不会重复下）")
                break
            log("")
            log(f"──── 第 {task['no']} 首《{task['title']}》────")
            try:
                lyric = load_lyrics(workdir, cfg.get("lyrics_dir", "lyrics"), task["lyrics_file"])
            except Exception as e:
                log(f"  ✗ {e}，跳过")
                continue
            log(f"  歌词 {len(lyric)} 字，来源 {task['lyrics_file']}")

            open_assets_and_tab(page)
            before = _cards_strict(page)
            log(f"  生成前资产页已有 {len(before)} 张卡片")

            fill_create_page(page, lyric, task["style"], model)
            log("· 等待生成（约 5–6 分钟）…")
            try:
                page.wait_for_url("**/playground**", timeout=120000)
                log("  ✓ 已进入结果页 playground")
            except Exception:
                log("  ! 没等到 playground，直接去资产页看")

            open_assets_and_tab(page)
            new = wait_new_cards(page, before, need, max_wait_min)
            if len(new) < need:
                log(f"  ✗ 只等到 {len(new)} 张新卡片（要 {need} 张），跳过")
                continue
            log(f"  ✓ 这一轮新出 {len(new)} 张，逐版走「编辑导出 → 下载」")

            items = []
            for i, sig in enumerate(new[:need]):
                disp = version_display_title(task["title"], i)
                log(f"  ── 第 {i+1} 版：{sig[:36]} → 命名《{disp}》──")
                try:
                    # 导出前先记下「已有哪些导出卡」，导出后只认新增的那张（防张冠李戴）
                    try:
                        _before_exp = [s for s in _cards_strict(page) if "编辑器导出" in s]
                    except Exception:
                        _before_exp = []
                    export_via_editor(page, sig, disp)
                    tmp, _exported_card_name = download_exported(
                        page, sig, disp, before_exported=_before_exp)
                    # platform_title 要存**平台自动起的原名**（从原始生成卡签名取），
                    # 不是「编辑器导出」卡的名字（那是我们自己填的 disp，会跟 title 重复）
                    plat = platform_name_from_sig(sig) or _exported_card_name
                    items.append((tmp, plat, disp))
                except Exception as e:
                    log(f"  ✗ 第 {i+1} 版失败：{type(e).__name__}: {e}")
                    if not browser_alive():
                        log("     ↑ 浏览器窗口没了，本首剩下的版本跳过")
                        break
            if items:
                total += len(save_song(workdir, cfg, task, lyric, mode, items, model))
            elif not browser_alive():
                break
    finally:
        try:
            ctx.close()
        except Exception:
            pass

    log("")
    log("=" * 62)
    log(f"完成：共入库 {total} 首 → {workdir / cfg.get('library_dir', 'library')}")
    log("下一步：fanqie_upload.py 上传（番茄端不用改）")
    log("=" * 62)


def platform_name_from_sig(sig):
    """从卡片签名里取出**平台自动起的歌名**。

    卡片签名（CARDS_JS 产出）形如「日子亮堂堂 02:41 · 2026-09-12 18:47」，
    平台每次会自动起名，跟用户在导出弹窗里填的歌名**不是一回事**。

    ⚠️ 2026-09-12 修：`download_exported()` 返回的是「编辑器导出」卡的名字 ——
    而那张卡的名字正是**我们自己填进去的** `disp`，拿它当 platform_title 等于重复记录
    `title`，平台原名反而丢了。平台原名只能从**原始生成卡的签名** `sig` 里取。
    """
    import re
    s = (sig or "").split("·")[0].strip()          # 去掉「· 日期时间」
    s = re.sub(r"\s*\d{1,2}:\d{2}\s*$", "", s)     # 去掉结尾的 mm:ss 时长
    return s.strip()[:40]


def do_redownload(p, workdir, mode, only_title=None, model=DEFAULT_MODEL, picks=None):
    """不重新生成，只把资产页「生成结果」里指定的卡片走「编辑导出 → 下载」入库。

    用于补救/验证下载环节（例如「生成了但没下下来」的情况）。

    ⚠️ 2026-09-17 重写：以前是「取最新 need 张原始卡」，但批 3 证明这样会**张冠李戴** ——
       第 1 首的卡晚到，被第 2 首的开盘快照当成本轮新卡，结果第 2 首入库了第 1 首的音频
       （`meta.platform_title` 记成了「月照归期」就是铁证）。
       → 现在必须**显式指定卡片签名**，一张一歌，不靠「最新 N 张」猜。

    picks: [(卡片签名, 显示歌名), ...]。传 None 时退回旧行为（最新 need 张，仅调试用）。
    """
    tasks = load_song_tasks(workdir, only_title)
    if not tasks:
        log("没有匹配的任务。")
        return
    task = tasks[0]
    cfg = load_config(workdir)
    need = GEN_VERSIONS_PER_LYRIC if mode == DOWNLOAD_MODE_BOTH else 1
    lyric = load_lyrics(workdir, cfg.get("lyrics_dir", "lyrics"), task["lyrics_file"])

    log("")
    log("=" * 62)
    if picks:
        log(f"按签名补下载：{task['title']}  |  指定 {len(picks)} 张卡片")
    else:
        log(f"补下载：{task['title']}  |  取「生成结果」最新 {need} 张卡片")
    log("=" * 62)

    ctx, page = open_browser(p)
    page.set_default_timeout(30000)

    def alive():
        try:
            return len(ctx.pages) > 0
        except Exception:
            return False

    try:
        if not ensure_genresult_tab(page):
            log("  ✗ 没能切到「生成结果」tab，放弃")
            return
        if picks:
            plan = list(picks)
        else:
            sigs = _cards_strict(page)
            originals = [s for s in sigs if "编辑器导出" not in s][:need]
            plan = [(s, version_display_title(task["title"], i))
                    for i, s in enumerate(originals)]

        # 先把目标签名在页面上核对一遍，核不到的立刻报出来（不浪费导出额度）
        present = _cards_strict(page)
        for sig, disp in plan:
            if not any(sig[:36] in p_ or p_[:36] in sig for p_ in present):
                log(f"  ⚠ 页面上核不到这张卡：{sig[:48]!r}")

        items = []
        for i, (sig, disp) in enumerate(plan):
            if not alive():
                log("  ✗ 浏览器窗口没了，停止补下载")
                break
            log(f"  ── {i+1}/{len(plan)}：{sig[:36]} → 命名《{disp}》──")
            try:
                try:
                    _before_exp = [s for s in _cards_strict(page) if "编辑器导出" in s]
                except Exception:
                    _before_exp = []
                export_via_editor(page, sig, disp)
                tmp, _exported_card_name = download_exported(
                    page, sig, disp, before_exported=_before_exp)
                plat = platform_name_from_sig(sig) or _exported_card_name
                items.append((tmp, plat, disp))
            except Exception as e:
                log(f"  ✗ 失败：{type(e).__name__}: {e}")
                if not alive():
                    log("     ↑ 浏览器窗口没了")
                    break
        if items:
            saved = save_song(workdir, cfg, task, lyric, mode, items, model)
            log(f"  共入库 {len(saved)} 首 → {workdir / cfg.get('library_dir', 'library')}")
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def do_set_workdir(path_str):
    """把「默认曲库位置」设到指定盘（例如 D:\\music-workflow），一次设定长期生效。

    背景（2026-09-20 用户提出）：批量下载的音频/封面越攒越多，默认落在系统盘
    （C 盘）容易挤爆。把曲库 + 下载中转一起挪到数据盘，以后所有脚本
    （生成 / 上传 / 核对）不用加任何参数就都写在新位置。

    做的事：
      1. 建好目标目录骨架：library/ lyrics/ .tmp/
      2. 若目标没有 tasks.csv，从 skill 里拷一份模板过去
      3. 把 workdir / temp_dir 写进本机设置文件（不进仓库、不分发）
      4. 打印前后对照 + 目标盘剩余空间，证明真的换过去了

    只创建目录和写一个 json，**不动任何已有歌曲**（迁移老歌用 --migrate）。
    """
    import shutil as _sh

    p = Path(path_str).expanduser().resolve()
    print("=" * 64)
    print("设置默认曲库位置")
    print("=" * 64)

    before = _ACTIVE_WORKDIR or resolve_workdir(None)
    print(f"  现在（旧）：{before}")

    try:
        for d in ("library", "lyrics", ".tmp"):
            (p / d).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"  ✗ 建目录失败：{type(e).__name__}: {e}")
        print(f"    请检查 {p} 是否可写（盘符是否存在、有没有权限）")
        return 1

    # 新位置没有歌单模板就拷一份，免得用户面对空目录不知道从哪开始
    tpl = ROOT / "tasks.csv"
    if not (p / "tasks.csv").exists() and tpl.exists():
        try:
            _sh.copyfile(str(tpl), str(p / "tasks.csv"))
            print("  · 已从 skill 拷入歌单模板 tasks.csv")
        except Exception as e:
            print(f"  · 歌单模板拷贝失败（不影响，可手建）：{e}")

    try:
        sf = save_settings(workdir=str(p), temp_dir=str(p / ".tmp"))
    except Exception as e:
        print(f"  ✗ 写设置失败：{type(e).__name__}: {e}")
        return 1

    print(f"  以后（新）：{p}")
    print(f"  设置文件  ：{sf}")
    print()
    print("  新位置的子目录：")
    for d in ("library", "lyrics", ".tmp"):
        print(f"    {d:<9} {(p / d)}")

    # 证明换盘成功：打印目标盘容量
    try:
        import shutil as _sh2
        t, u, f = _sh2.disk_usage(str(p.anchor))
        print()
        print(f"  {p.anchor} 总 {t/1024**3:.1f} GB / 剩余 {f/1024**3:.1f} GB")
    except Exception:
        pass

    print()
    print("  ✓ 完成。从现在起，miaoxiang.py / fanqie_upload.py / verify_published.py")
    print("    不加任何参数都会用这个目录；下载中转也走它下面的 .tmp/，不再占系统盘。")
    print("    （要改回去：再运行一次本命令，指向你想要的目录即可）")
    return 0


def do_list_cards(p, workdir):
    """只读：打印资产页「生成结果」里所有卡片的**签名**，供 --card 复制使用。

    用来给 `--redownload --card "签名=歌名"` 抄参数。
    **不点生成、不点导出、不点下载** —— 纯读页面文本，零额度消耗。

    为什么要有它：以前靠一个下划线开头的临时探针脚本（`_diag_cards.py`）干这事，
    那脚本还被 .gitignore 的 `_diag_*` 规则拦住没法提交，用户拿不到。
    现在并进主脚本，随 `miaoxiang.py` 一起分发。
    """
    log("")
    log("=" * 66)
    log(f"只读列出「生成结果」卡片签名 | 工作目录：{workdir}")
    log("=" * 66)

    ctx, page = open_browser(p)
    page.set_default_timeout(30000)
    try:
        if not ensure_genresult_tab(page):
            log("  ✗ 没能切到「生成结果」tab，放弃")
            return
        sigs = _cards_strict(page)
        log(f"共 {len(sigs)} 张卡片（新→旧）：")
        log("")
        for i, s in enumerate(sigs):
            kind = "导出卡" if "编辑器导出" in s else "生成卡"
            log(f"  [{i:>2}] ({kind}) {s}")
        log("")
        log("─" * 66)
        log("要用哪几张，就这样写（注意整串签名都要带上，含 · 和日期时间）：")
        log('  --card "月照归期 03:22 · 2026-09-17 18:57=踏月寻你" \\')
        log('  --card "踏月寻你 03:02 · 2026-09-17 18:57=踏月寻你（动听版）"')
        log("")
        log("⚠️ 「生成卡」才能走导出链路；「导出卡」是导出后的产物，直接下载即可。")
        log("⚠️ 签名里的时间戳是**平台创建时间**，不是下载时间 —— 认卡别认错。")
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="妙响（抖音音乐创作实验室）生成端")
    ap.add_argument("--login", action="store_true", help="打开浏览器手动登录（关掉窗口即完成）")
    ap.add_argument("--probe", action="store_true", help="抓真实页面结构并存 JSON+截图")
    ap.add_argument("--walk", action="store_true",
                    help="逐步点开创作入口并抓真实 DOM（只导航，不点生成，不烧额度）")
    ap.add_argument("--learn", action="store_true",
                    help="你按平时方式完整操作一遍，脚本每几秒录一次 DOM+截图（会真实点生成）")
    ap.add_argument("--download-mode", choices=[DOWNLOAD_MODE_FIRST, DOWNLOAD_MODE_BOTH],
                    default=DEFAULT_DOWNLOAD_MODE,
                    help="两版都下载还是只下第一版；非交互运行时用它，交互运行时仍会先问你")
    ap.add_argument("--gen", action="store_true",
                    help="生成+下载（按 tasks.csv；生成前会问你要几个版本）")
    ap.add_argument("--redownload", action="store_true",
                    help="不重新生成，只把资产页「生成结果」里指定/最新的卡片补下载入库"
                         "（补救「生成了但没下下来」）")
    ap.add_argument("--card", action="append", default=None,
                    help="配合 --redownload：显式指定卡片签名 → 显示歌名，格式 "
                         "'卡片签名=显示歌名'，可重复。这是最可靠的补下载方式"
                         "（不指定时才退回「最新 N 张」，有张冠李戴风险）")
    ap.add_argument("--list-cards", action="store_true",
                    help="只读列出资产页「生成结果」的所有卡片签名（给 --card 抄参数用；"
                         "不点生成/导出/下载，零额度消耗）")
    ap.add_argument("--set-workdir", default=None, metavar="目录",
                    help="把默认曲库位置设到指定盘（如 D:\\music-workflow），一次设定长期生效；"
                         "下载中转也一起挪过去，批量下载不再占系统盘。不动已有歌曲")
    ap.add_argument("--workdir", default=None,
                    help="工作目录（含 tasks.csv / lyrics / library）；默认取当前目录")
    ap.add_argument("--song", default=None, help="只生成指定歌名的那一首")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"妙响「模型选择」下拉的模型（默认 {DEFAULT_MODEL}）。"
                         f"下拉里匹配子串即可，如 'Sway v5.5'")
    ap.add_argument("--max-wait", type=int, default=25,
                    help="等一首歌生成完的最长分钟数（默认 25）")
    ap.add_argument("--learn-minutes", type=int, default=LEARN_MAX_MINUTES,
                    help=f"--learn 最长等待分钟数（默认 {LEARN_MAX_MINUTES}，别设太短，"
                         f"否则你还在排错就被强制关窗）")
    ap.add_argument("--learn-start", choices=["create", "assets"], default="create",
                    help="--learn 从哪个页面开始录：create=创作页（默认）／"
                         "assets=资产页（只学「编辑导出→下载」这一段）")
    args = ap.parse_args()
    if args.card:
        args.redownload = True          # --card 天然属于补下载动作

    # --set-workdir 纯本地操作，不起浏览器、不上网、不烧额度
    if args.set_workdir:
        return do_set_workdir(args.set_workdir)

    if not args.login and not args.probe and not args.walk \
            and not args.learn and not args.gen and not args.redownload \
            and not args.card and not args.list_cards \
            and not args.set_workdir:
        ap.print_help()
        print("\n提示：第一次用先 --login，然后 --walk / --learn，熟悉后用 --gen。")
        return

    with sync_playwright() as p:
        if args.login:
            do_login(p)
        elif args.probe:
            do_probe(p)
        elif args.walk:
            do_walk(p)
        elif args.learn:
            start = ASSETS_URL if args.learn_start == "assets" else None
            do_learn(p, args.download_mode, args.learn_minutes, start)
        elif args.gen:
            workdir = resolve_workdir(args.workdir)
            mode = ask_download_mode(args.download_mode)
            do_gen(p, workdir, mode, args.max_wait, args.song, args.model)
        elif args.list_cards:
            workdir = resolve_workdir(args.workdir)
            do_list_cards(p, workdir)
        elif args.redownload:
            workdir = resolve_workdir(args.workdir)
            mode = ask_download_mode(args.download_mode)
            picks = None
            if args.card:
                picks = []
                for spec in args.card:
                    if "=" not in spec:
                        log(f"✗ --card 格式应为 '卡片签名=显示歌名'，收到：{spec!r}")
                        return
                    sig, disp = spec.split("=", 1)
                    picks.append((sig.strip(), disp.strip()))
                log(f"· 本次按 {len(picks)} 条显式卡片映射补下载")
            do_redownload(p, workdir, mode, args.song, args.model, picks)


if __name__ == "__main__":
    sys.exit(main())
