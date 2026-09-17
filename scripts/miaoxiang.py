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

from paths import user_profile  # noqa: E402
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

SET_LYRIC_JS = """([sel, text]) => {
  const el = document.querySelector(sel);
  if (!el) return -1;
  const html = text.split(/\\n+/).map(s => '<p>' + s + '</p>').join('');
  el.innerHTML = html;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return (el.innerText || '').trim().length;
}"""


def resolve_workdir(arg=None):
    """决定任务表/歌词/素材目录：--workdir > 当前目录(有 tasks.csv) > 脚本目录。"""
    if arg:
        return Path(arg).expanduser().resolve()
    cwd = Path.cwd()
    if (cwd / "tasks.csv").exists():
        return cwd
    return ROOT


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
    """轮询资产页，等这一轮的新卡片够数。返回新卡片签名（ newest first）。"""
    before_set = set(before)
    deadline = time.time() + max_min * 60
    while time.time() < deadline:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        try:
            page.get_by_text("生成结果", exact=True).first.click(timeout=6000)
            page.wait_for_timeout(2500)
        except Exception:
            pass
        cur = page.evaluate(CARDS_JS)
        new = [c for c in cur if c not in before_set]
        if len(new) >= need:
            return new
        log(f"  …新卡片 {len(new)}/{need}，继续等（每 {poll}s 一查，上限 {max_min} 分钟）")
        page.wait_for_timeout(poll * 1000)
    return [c for c in page.evaluate(CARDS_JS) if c not in before_set]


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
    tmp = Path(tempfile.mkdtemp()) / dl.suggested_filename
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
    """按 CARDS_JS 的签名文本定位"原始生成卡"的序号；找不到返回 -1。"""
    key = (sig or "").split(" 编辑器导出")[0].strip()[:60]
    if not key:
        return -1
    return page.evaluate(
        """(key) => {
          const cards = [...document.querySelectorAll('[class*="generatedSongListItem"]')];
          return cards.findIndex(c =>
            ((c.innerText || '').replace(/\\s+/g, ' ').trim()).slice(0, 90).startsWith(key));
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
        raise RuntimeError(f"找不到卡片：{sig[:40]}")
    card = page.locator('[class*="generatedSongListItem"]').nth(idx)
    card.scroll_into_view_if_needed(timeout=5000)
    card.hover(timeout=8000)
    page.wait_for_timeout(600)
    # 卡片上的「编辑」（hover 才出现）
    card.get_by_text("编辑", exact=True).first.click(timeout=8000)
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
    # 等弹窗关闭 = 导出受理
    try:
        modal.wait_for(state="hidden", timeout=180000)
    except Exception:
        log("    （等弹窗关闭超时，继续）")
    page.wait_for_timeout(3000)


def download_exported(page, sig, title=None):
    """回资产页，找那张带「编辑器导出」的卡 → ⋯ → 下载。返回 (tmp, 平台标题)。

    title：这张导出件的名字（= 版本区分名，如「歌名（动听版）」），用于精确定位卡片。
    """
    open_assets_and_tab(page)
    title = (title or (sig or "").strip().split(" ")[0]).strip()
    cards = page.locator('[class*="generatedSongListItem"]', has_text="编辑器导出")
    card = None
    if title:
        cand = cards.filter(has_text=title).first
        if cand.count() > 0:
            card = cand
    if card is None:
        cand = cards.first
        if cand.count() > 0:
            card = cand
    if card is None:
        raise RuntimeError("找不到「编辑器导出」卡片（导出可能还没完成）")
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
    tmp = Path(tempfile.mkdtemp()) / dl.suggested_filename
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
    for i, it in enumerate(items):
        tmp, plat_title = it[0], it[1]
        disp_title = it[2] if len(it) > 2 and it[2] else version_display_title(task["title"], i)
        folder = song_dir_for(task["title"], i, mode)
        dest_dir = lib / folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        size = tmp.stat().st_size
        if size < min_bytes:
            log(f"  ✗ {folder}：只有 {size} 字节（< {min_bytes}），像坏文件，跳过")
            continue
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
            "version_index": i,
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


def do_gen(p, workdir, mode, max_wait_min, only_title=None, model=DEFAULT_MODEL):
    tasks = load_song_tasks(workdir, only_title)
    if not tasks:
        log("没有待生成的任务（tasks.csv 里没有非「示例-」开头的行）。")
        return
    cfg = json.loads((workdir / "config.json").read_text(encoding="utf-8"))
    need = GEN_VERSIONS_PER_LYRIC if mode == DOWNLOAD_MODE_BOTH else 1

    log("")
    log("=" * 62)
    log(f"工作目录：{workdir}")
    log(f"待生成：{len(tasks)} 首  |  下载策略：{mode}（每首下 {need} 个版本）")
    log("=" * 62)

    ctx, page = open_browser(p)
    page.set_default_timeout(30000)
    total = 0
    try:
        for task in tasks:
            log("")
            log(f"──── 第 {task['no']} 首《{task['title']}》────")
            try:
                lyric = load_lyrics(workdir, cfg.get("lyrics_dir", "lyrics"), task["lyrics_file"])
            except Exception as e:
                log(f"  ✗ {e}，跳过")
                continue
            log(f"  歌词 {len(lyric)} 字，来源 {task['lyrics_file']}")

            open_assets_and_tab(page)
            before = page.evaluate(CARDS_JS)
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
                    export_via_editor(page, sig, disp)
                    tmp, _exported_card_name = download_exported(page, sig, disp)
                    # platform_title 要存**平台自动起的原名**（从原始生成卡签名取），
                    # 不是「编辑器导出」卡的名字（那是我们自己填的 disp，会跟 title 重复）
                    plat = platform_name_from_sig(sig) or _exported_card_name
                    items.append((tmp, plat, disp))
                except Exception as e:
                    log(f"  ✗ 第 {i+1} 版失败：{type(e).__name__}: {e}")
            if items:
                total += len(save_song(workdir, cfg, task, lyric, mode, items, model))
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


def do_redownload(p, workdir, mode, only_title=None, model=DEFAULT_MODEL):
    """不重新生成，只把资产页「生成结果」里最新的 need 张卡片下载入库。
    用于补救/验证下载环节（例如 egWPo9 那种「生成了但没下下来」的情况）。"""
    tasks = load_song_tasks(workdir, only_title)
    if not tasks:
        log("没有匹配的任务。")
        return
    task = tasks[0]
    cfg = json.loads((workdir / "config.json").read_text(encoding="utf-8"))
    need = GEN_VERSIONS_PER_LYRIC if mode == DOWNLOAD_MODE_BOTH else 1
    lyric = load_lyrics(workdir, cfg.get("lyrics_dir", "lyrics"), task["lyrics_file"])

    log("")
    log("=" * 62)
    log(f"补下载：{task['title']}  |  取「生成结果」最新 {need} 张卡片")
    log("=" * 62)

    ctx, page = open_browser(p)
    page.set_default_timeout(30000)
    try:
        if not ensure_genresult_tab(page):
            log("  ✗ 没能切到「生成结果」tab，放弃")
            return
        # 取最新 need 张"原始生成卡"（排除已带「编辑器导出」的），逐张走导出→下载
        sigs = page.evaluate(CARDS_JS)
        originals = [s for s in sigs if "编辑器导出" not in s][:need]
        items = []
        for i, sig in enumerate(originals):
            disp = version_display_title(task["title"], i)
            log(f"  ── 第 {i+1} 版：{sig[:36]} → 命名《{disp}》──")
            try:
                export_via_editor(page, sig, disp)
                tmp, _exported_card_name = download_exported(page, sig, disp)
                plat = platform_name_from_sig(sig) or _exported_card_name
                items.append((tmp, plat, disp))
            except Exception as e:
                log(f"  ✗ 第 {i+1} 版失败：{type(e).__name__}: {e}")
        if items:
            saved = save_song(workdir, cfg, task, lyric, mode, items, model)
            log(f"  共入库 {len(saved)} 首 → {workdir / cfg.get('library_dir', 'library')}")
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
                    help="不重新生成，只把资产页「生成结果」最新 N 张卡片补下载入库"
                         "（补救「生成了但没下下来」）")
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

    if not args.login and not args.probe and not args.walk \
            and not args.learn and not args.gen and not args.redownload:
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
        elif args.redownload:
            workdir = resolve_workdir(args.workdir)
            mode = ask_download_mode(args.download_mode)
            do_redownload(p, workdir, mode, args.song, args.model)


if __name__ == "__main__":
    main()
