# -*- coding: utf-8 -*-
"""
步骤 1：批量生成 AI 音乐 —— 自动操控 MiniMax Audio 网页端，生成并下载到本地曲库。

用法（在本文件夹里打开命令行 / 或双击 run.bat 选 1）：

    python generate.py --login      # 第一次运行：打开浏览器，你手动登录，登录后脚本自动保存并退出
    python generate.py              # 正式批量生成
    python generate.py --probe      # 只做一次生成并把网页的网络请求存成 probe_report.json（用于排错）
    python generate.py --task 3     # 只跑任务表里第 3 行（序号从 1 开始）

生成的东西放在 library/<歌名>/ 下：
    audio.mp3   音频
    cover.png   封面（1440×1440 PNG，自动生成，配色跟着风格描述走）
    lyrics.txt  歌词
    meta.json   这首歌的全部信息（生成时间、参数、来源链接）
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("缺少 playwright。请先运行： pip install playwright && playwright install chromium")
    sys.exit(1)

from popup_guard import (  # noqa: E402
    guard_context,
    dismiss_popups,
    goto_with_guard,
)
from browser_utils import window_args, viewport_for  # noqa: E402


# ---------------------------------------------------------------- 基础工具

def load_config():
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def log(msg):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def safe_name(name, maxlen=60):
    """把歌名变成能当文件夹名的安全字符串。

    ⚠️ 两个坑（详见 references/LEARNED.md）：
      1. NFKC 归一化会把中文全角标点转成半角，于是「晚风，寄的信」里的
         全角逗号「，」会变成半角「,」。
      2. fanqie_upload.py 的 --mark-published / --songs 是用【英文逗号】分隔参数的。
         文件夹名一旦含半角逗号，这两个命令会被静默拆成两个错误的歌名，
         防重复记录随之失效（不报错，但下次照样重复发）。
    因此这里把半角逗号换回全角逗号：既保住中文排版和可读性，又不会被参数分隔误伤。
    """
    name = unicodedata.normalize("NFKC", str(name)).strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    name = name.replace(",", "，")  # 半角逗号 → 全角，避免与参数分隔符冲突
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or "untitled")[:maxlen]


def load_tasks(cfg):
    path = ROOT / cfg["tasks_file"]
    if not path.exists():
        print(f"找不到任务表：{path}")
        sys.exit(1)
    rows = []
    skipped_demo = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=1):
            title = (row.get("歌名") or "").strip()
            if not title or title.startswith("#"):
                continue
            # 「示例-」开头的是格式示范行，不会真的拿去生成（避免新用户一跑就白烧额度）
            if title.startswith("示例-") or title.startswith("示例－"):
                skipped_demo += 1
                continue
            rows.append({
                "no": i,
                "title": title,
                "style": (row.get("风格描述") or "").strip(),
                "lyrics_file": (row.get("歌词文件") or "").strip(),
                "count": int((row.get("生成数量") or "1").strip() or 1),
                "instrumental": str(row.get("纯音乐", "")).strip().lower() in ("1", "y", "yes", "true", "是"),
                "auto_lyrics": str(row.get("AI写词", "")).strip().lower() in ("1", "y", "yes", "true", "是"),
            })
    if skipped_demo:
        print(f"· 已跳过 {skipped_demo} 行「示例-」开头的示范行（不会真的生成）。")
        print("  把你自己的歌名写进 tasks.csv 即可开始；删掉示例行或保留都不影响。")
    if not rows:
        print("")
        print(f"⚠️ {path.name} 里没有可生成的歌曲。")
        print("   请照着示例行的格式，写你自己的：歌名,风格描述,歌词文件,生成数量,纯音乐")
    return rows


def read_lyrics(cfg, lyrics_file):
    if not lyrics_file:
        return ""
    p = ROOT / cfg["lyrics_dir"] / lyrics_file
    if not p.exists():
        p = ROOT / cfg["lyrics_dir"] / (lyrics_file + ".txt")
    if not p.exists():
        log(f"  警告：找不到歌词文件 {lyrics_file}，将跳过歌词（由 AI 自动写词）")
        return ""
    return p.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------- 页面操作

def try_click(page, selectors, timeout=2500, cfg=None):
    """依次尝试一组选择器，点到第一个可见的为止。

    全部点不上时，八成是被弹窗挡住了（表现为 "intercepts pointer events"）。
    这时先清一次弹窗再重试一轮 —— 这是修复「打开页面就卡住」的关键。
    """
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            # 窗口自适应后页面可能很高，先滚到元素可见再点（防底部按钮在视口外）
            try:
                loc.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            loc.click(timeout=timeout)
            return sel
        except Exception:
            continue

    # 一轮全灭：清弹窗后再试一次
    if dismiss_popups(page, cfg=cfg, log=log, force_mask=True):
        log("  ⚠ 点击被挡住，已清掉挡路弹窗，重试…")
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=timeout)
                try:
                    loc.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                loc.click(timeout=timeout)
                return sel
            except Exception:
                continue
    return None


def try_set_contenteditable(page, sel, text, timeout=3000):
    """为 contenteditable div 写内容"""
    try:
        loc = page.locator(sel).first
        loc.wait_for(state="visible", timeout=timeout)
        try:
            loc.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        loc.click(timeout=timeout)
        loc.evaluate("el => { el.innerText = ''; }")
        page.keyboard.insert_text(text)
        return sel
    except Exception:
        return None


def try_fill(page, selectors, text, timeout=2500):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            try:
                loc.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            tag = loc.evaluate("el => el.tagName")
            if tag == "DIV":
                # contenteditable div：用剪贴板更稳
                loc.click(timeout=timeout)
                loc.evaluate("el => { el.innerText = ''; }")
                page.keyboard.insert_text(text)
            else:
                loc.fill(text, timeout=timeout)
            return sel
        except Exception:
            continue
    return None


# MiniMax 页面数量控件的默认选择器（config.json 的 selectors.quantity 可覆盖/扩展）
_DEFAULT_QTY_INPUT_SELS = [
    "input[type='number']",
    "input[aria-label*='数量']",
    "input[aria-label*='生成']",
    "input[placeholder*='数量']",
    "[class*='quantity'] input",
    "[class*='count'] input",
    "[class*='stepper'] input",
    "[class*='num-input']",
    "[class*='number-input']",
]
_DEFAULT_QTY_MINUS_SELS = [
    "button[aria-label*='减']",
    "button[aria-label*='减少']",
    "[aria-label*='decrease']",
    "[class*='minus']",
    "[class*='decrease']",
    "[class*='stepper-minus']",
    "[class*='sub-btn']",
]
_DEFAULT_QTY_PLUS_SELS = [
    "button[aria-label*='加']",
    "button[aria-label*='增加']",
    "[aria-label*='increase']",
    "[class*='plus']",
    "[class*='increase']",
    "[class*='stepper-plus']",
    "[class*='add-btn']",
]


def set_generate_quantity(page, cfg, want, log=None):
    """把 MiniMax/Hailuo 页面上的「生成数量」改成 want（默认 1）。

    背景：MiniMax 页面默认生成数量是 2（点一次生成出两首歌），脚本要按
    quantity_per_round 来控制，必须先把页面数量改过来，否则额度会浪费。

    页面形态：Next.js + Tailwind 的自定义 stepper，结构大致是
        <div ...>                      ← 容器
          <span>数量:&nbsp;</span>     ← 标签
          <button><svg/></button>      ← 减（svg 横线图标，无任何 class/aria 提示）
          <span>2</span>               ← 当前值（数字 span）
          <button><svg/></button>      ← 加
        </div>
    没有 input、没有 aria-label、按钮 class 是通用 flex 类——所以早期按
    antd/number-input 猜的选择器全匹配不上，函数一直静默失败。

    策略（按稳到快）：
      ① 用「数量:」标签定位 stepper 容器，点减/加按钮从当前值走到 want（主路径）
      ② 兜底：config 里的通用 input / 加减按钮选择器（兼容 antd 等其它形态）
    找不到控件就返回 False（不阻塞流程，按页面实际数量继续，日志里会提示）。
    """
    # ① 主路径：靠「数量」值所在的纯数字 span 定位 stepper 容器
    #    （之前用「数量」标签往上找第一个含≥2按钮的祖先，会误命中整块输入区的大容器，
    #      导致 btns[0] 变成「参考音乐上传」之类的按钮，点减号根本没作用）
    # 关键修复：必须用「数量」标签锚定到正确的 stepper。
    # 旧逻辑只找"纯数字 span + 父容器有2个按钮"，会误命中页面上其他数字控件
    # （比如某处标签数字），点了减号但真正的数量没变——日志却报"已设为1"。
    # 新逻辑：先找含"数量"文字的标签，再从它旁边定位数字 span + 减/加按钮。
    JS_FIND = """() => {
        // ① 先找「数量」标签（兼容 "数量：" / "数量:" / "数量" 等写法）
        let qtyLabel = null;
        for (const el of document.querySelectorAll('span, div, label, p')) {
            const t = (el.textContent || '').trim();
            if (/^数量\\s*[:：]?$/.test(t)) { qtyLabel = el; break; }
        }
        if (!qtyLabel) {
            // 兜底：页面可能改了标签文字，用旧逻辑找"数字 span + 2 按钮"
            const nums = [...document.querySelectorAll('span')]
                .filter(s => /^\\d+$/.test((s.textContent||'').trim()));
            for (const s of nums) {
                const par = s.parentElement;
                if (par && par.querySelectorAll('button').length === 2) return par;
            }
            for (const s of nums) {
                const par = s.parentElement;
                if (par && par.querySelectorAll('button').length >= 2) return par;
            }
            return null;
        }
        // ② 从标签的父/兄弟容器里找数字 span（stepper 的当前值）
        const walk = (el, depth) => {
            if (depth > 3) return null;
            // 检查自身及所有后代
            const spans = el.querySelectorAll('span');
            for (const s of spans) {
                if (/^\\d+$/.test((s.textContent || '').trim())) {
                    const par = s.parentElement;
                    if (par && par.querySelectorAll('button').length >= 2) return par;
                }
            }
            // 检查父容器
            if (el.parentElement) return walk(el.parentElement, depth + 1);
            // 检查下一个兄弟
            if (el.nextElementSibling) {
                const sibSpans = el.nextElementSibling.querySelectorAll('span');
                for (const s of sibSpans) {
                    if (/^\\d+$/.test((s.textContent || '').trim())) {
                        const par = s.parentElement;
                        if (par && par.querySelectorAll('button').length >= 2) return par;
                    }
                }
            }
            return null;
        };
        return walk(qtyLabel, 0);
    }"""
    try:
        handle = page.evaluate_handle(JS_FIND)
    except Exception:
        handle = None
    elc = handle.as_element() if handle else None

    if elc is not None:
        try:
            btns = elc.query_selector_all("button")
            if len(btns) >= 2:
                minus, plus = btns[0], btns[-1]

                def _read():
                    v = elc.evaluate("""e => {
                        const s = [...e.querySelectorAll('span')]
                            .find(x => /^\\d+$/.test((x.textContent||'').trim()));
                        return s ? (s.textContent||'').trim() : '';
                    }""")
                    try:
                        return int(v)
                    except Exception:
                        return -1

                cur = _read()
                if cur < 0:
                    if log:
                        log("  ⚠ 数量控件读不出当前值，跳过数量设置")
                    return False
                if cur == want:
                    if log:
                        log(f"  ✓ 生成数量已是 {want}")
                    return True
                step = minus if cur > want else plus
                for _ in range(abs(cur - want)):
                    try:
                        # 用原生 .click() 触发 React 合成事件，绕过 Playwright
                        # 的可点击性/遮罩检查（这类 Tailwind 自定义按钮常被误判为点不到）
                        step.evaluate("b => b.click()")
                    except Exception:
                        try:
                            step.click(timeout=1000, force=True)
                        except Exception:
                            break
                    time.sleep(0.4)
                final = _read()
                if final == want:
                    if log:
                        log(f"  ✓ 生成数量已设为 {want}（原 {cur}）")
                    return True
                if log:
                    log(f"  ⚠ 数量未设成 {want}（当前 {final}），按页面实际数量继续")
                return False
        except Exception as e:
            if log:
                log(f"  ⚠ 自定义 stepper 操作异常: {e}")

    # ② 兜底：config 里的通用选择器（antd / number input 等形态）
    q = (cfg.get("selectors") or {}).get("quantity") or {}
    num_sels = list(q.get("input") or _DEFAULT_QTY_INPUT_SELS)
    minus_sels = list(q.get("minus") or _DEFAULT_QTY_MINUS_SELS)
    plus_sels = list(q.get("plus") or _DEFAULT_QTY_PLUS_SELS)

    num_loc = None
    for s in num_sels:
        try:
            loc = page.locator(s).first
            loc.wait_for(state="visible", timeout=1500)
            num_loc = loc
            break
        except Exception:
            continue
    if num_loc is None:
        if log:
            log("  ⚠ 找不到生成数量控件，按页面默认数量执行（可能多生成）")
        return False

    def _read_val():
        try:
            v = str(num_loc.input_value()).strip()
        except Exception:
            v = ""
        if v:
            try:
                return int(v)
            except Exception:
                pass
        try:
            return int((num_loc.inner_text() or "").strip())
        except Exception:
            return -1

    try:
        tag = num_loc.evaluate("el => el.tagName")
        if tag in ("INPUT", "TEXTAREA"):
            num_loc.fill(str(want), timeout=1500)
            time.sleep(0.3)
            if _read_val() == want:
                if log:
                    log(f"  ✓ 生成数量已设为 {want}")
                return True
    except Exception:
        pass

    cur = _read_val()
    if cur >= 0 and cur != want:
        step_sels = minus_sels if cur > want else plus_sels
        for _ in range(abs(cur - want)):
            clicked = False
            for s in step_sels:
                try:
                    btn = page.locator(s).first
                    btn.wait_for(state="visible", timeout=1000)
                    btn.click(timeout=1000)
                    clicked = True
                    time.sleep(0.35)
                    break
                except Exception:
                    continue
            if not clicked:
                break
        final = _read_val()
        if final == want:
            if log:
                log(f"  ✓ 生成数量已设为 {want}（原 {cur}）")
            return True
        if log:
            log(f"  ⚠ 生成数量未设成 {want}（当前 {final}），按页面实际数量继续")
        return False

    if cur == want:
        return True
    if log:
        log("  ⚠ 数量控件读不出当前值，跳过数量设置")
    return False


def looks_like_audio_url(s):
    if not isinstance(s, str):
        return False
    if not s.startswith(("http://", "https://")):
        return False
    s_low = s.split("?")[0].lower()
    return any(s_low.endswith(ext) for ext in (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"))


def walk_find_audio(obj, found, depth=0):
    """递归在数据里翻找音频链接"""
    if depth > 8:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and looks_like_audio_url(v):
                found.add(v)
            else:
                walk_find_audio(v, found, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            walk_find_audio(v, found, depth + 1)


class Sniffer:
    """监听网页后台请求，把返回的音频链接全抓下来"""

    def __init__(self):
        self.audio_urls = []
        self.probe_records = []
        self._seen = set()

    def on_response(self, response):
        url = response.url
        ctype = (response.headers or {}).get("content-type", "")
        try:
            if "json" in ctype:
                body = response.text()
            else:
                return
        except Exception:
            return

        # 探路模式：记录所有跟 music 相关的请求，方便以后查接口
        if "music" in url.lower() or "audio" in url.lower():
            self.probe_records.append({
                "url": url,
                "status": response.status,
                "request_headers_preview": {k: v for k, v in (response.request.headers or {}).items()
                                            if k.lower() in ("authorization", "cookie", "content-type", "token", "x-token")},
                "request_post_data": (response.request.post_data or "")[:2000],
                "response_preview": body[:2000],
            })

        try:
            data = json.loads(body)
        except Exception:
            return
        found = set()
        walk_find_audio(data, found)
        for u in found:
            if u not in self._seen:
                self._seen.add(u)
                self.audio_urls.append(u)
                log(f"  嗅探到音频链接：{u[:90]}...")

    def reset(self):
        self.audio_urls = []
        self._seen = set()


def fetch_history(page, cfg):
    """主动查 MiniMax「历史作品」接口，返回 music_list。

    每一项含 music_id / title / audio_url / idea(风格) / lyrics。
    为什么需要它：生成完成后页面**不会**自动刷新历史列表，音频链接只在这个
    接口里返回，只靠嗅探页面请求永远等不到。直接查接口比等 DOM 稳得多。
    """
    url = cfg.get("history_api") or (
        "https://www.minimaxi.com/v1/api/music/history_list"
        "?is_favorite=false&page=1&page_size=20"
        "&device_platform=web&app_id=3001&version_code=22201&biz_id=1"
    )
    try:
        resp = page.request.get(url, timeout=30000)
        data = resp.json()
        return data.get("data", {}).get("music_list", []) or []
    except Exception as e:
        log(f"  (查历史作品接口失败: {e})")
        return []


# ---------------------------------------------------------------- 主流程

_LOGIN_TEXTS = ["text=登录", "text=立即登录", "text=Log in", "text=Sign in"]


def _no_login_button(page):
    """页面上没有可见的登录入口，即认为已登录。"""
    for sel in _LOGIN_TEXTS:
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                return False
        except Exception:
            continue
    return True


def wait_logged_in(page, cfg, minutes=20):
    """自动轮询等待用户在浏览器里完成登录（无需在终端按回车）。

    先等页面初始加载，再检测「登录」入口消失即视为登录成功。
    返回 True/False。
    """
    # 等页面初始加载完成，避免误判
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    time.sleep(6)
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        if _no_login_button(page):
            return True
        time.sleep(3)
    return False


def ensure_login(page, cfg):
    goto_with_guard(page, cfg["base_url"], cfg=cfg, log=log, settle_sec=3)
    if _no_login_button(page):
        return True
    # 有弹窗挡着的话，"登录"按钮也可能被盖住导致误判未登录
    dismiss_popups(page, cfg=cfg, log=log)
    # 未登录：自动轮询等待用户在浏览器里登录，无需在黑窗口按回车
    print("\n" + "=" * 60)
    print("检测到还未登录。请在刚打开的浏览器窗口里完成登录（微信/手机号都行）。")
    print("登录成功后脚本会自动继续，不需要回到黑窗口按任何键。")
    print("=" * 60)
    return wait_logged_in(page, cfg, minutes=20)


def do_one_task(page, context, cfg, task, sniffer):
    """提交一首歌的生成任务并下载结果，返回生成的歌曲目录列表"""
    title = task["title"]
    lyrics = read_lyrics(cfg, task["lyrics_file"])
    instrumental = task["instrumental"] or cfg.get("is_instrumental", False)
    want = max(1, int(task["count"] or 1))
    per_round = max(1, int(cfg.get("quantity_per_round", 2)))
    rounds = (want + per_round - 1) // per_round

    log(f"▶ 开始：《{title}》 想要 {want} 首（每轮生成 {per_round} 首，需要 {rounds} 轮）")

    all_saved = []

    for r in range(rounds):
        need = min(per_round, want - len(all_saved))
        log(f"\n--- 第 {r+1}/{rounds} 轮（这轮要 {need} 首）---")
        # 每次进页面都先清一遍弹窗：活动公告/引导层会盖住输入框和生成按钮
        goto_with_guard(page, cfg["base_url"], cfg=cfg, log=log, settle_sec=4)

        sel = cfg["selectors"]
        dismiss_popups(page, cfg=cfg, log=log)

        # 纯音乐开关
        if instrumental:
            hit = try_click(page, sel["instrumental_toggle"], cfg=cfg)
            log(f"  纯音乐开关：{hit or '未找到'}")
            time.sleep(0.8)
        else:
            # 先确保开关是关的（页面可能记住上次状态）
            try:
                checked = page.locator(sel["instrumental_toggle"][0]).first.get_attribute("aria-checked")
                if checked and checked != "false":
                    page.locator(sel["instrumental_toggle"][0]).first.click()
                    time.sleep(0.5)
            except Exception:
                pass

        # 歌名（即便官方不显示在 UI 也设置一下）
        try_fill(page, sel["title_input"], title, timeout=2000)

        # 风格描述
        prompt = task["style"] or title
        hit = try_fill(page, sel["prompt_input"], prompt, timeout=4000)
        if not hit:
            log("  ✗ 找不到「风格描述」输入框")
            continue
        log("  ✓ 已填风格描述")
        time.sleep(0.5)

        # 歌词
        if lyrics and not instrumental:
            hit = try_fill(page, sel["lyrics_input"], lyrics, timeout=4000)
            log(f"  {'✓ 已填歌词' if hit else '✗ 找不到歌词输入框'}")
            time.sleep(0.5)

        # 点生成
        sniffer.reset()
        # 先记下已有的作品 id，稍后用「新出现的 id」判断这首歌生成好了
        known_ids = {m.get("music_id") for m in fetch_history(page, cfg)}
        # ★ 把页面上的生成数量改成"这轮要几首"（默认 1）
        #   MiniMax 页面默认数量是 2，不设置的话点一次生成会出两首，浪费额度
        set_generate_quantity(page, cfg, need, log)
        # 生成按钮是整条流水线的咽喉：点不中这首歌就彻底没了，所以点之前再清一次弹窗
        dismiss_popups(page, cfg=cfg, log=log)
        hit = try_click(page, sel["generate_button"], cfg=cfg, timeout=5000)
        if not hit:
            log("  ✗ 找不到「生成」按钮，跳过这首歌")
            continue
        log("  ✓ 已点生成，等待出歌（通常 1-3 分钟，量大时请耐心等）...")

        # 主动轮询 MiniMax 的「历史作品」接口取结果。
        # 为什么不能只等嗅探器：生成完成后页面**不会**自动刷新历史列表，
        # 音频链接只在 history_list 接口里返回，干等永远等不到（老脚本靠轮询
        # 页面状态文字才能拿到，这里直接查接口，更稳也更快）。
        deadline = time.time() + cfg.get("wait_timeout_sec", 420)
        poll = max(4, int(cfg.get("poll_interval_sec", 4)))
        seen_new = set()      # 已发现的新作品 id
        fresh_urls = []       # 已拿到直链的新作品
        while time.time() < deadline:
            for m in fetch_history(page, cfg):
                mid = m.get("music_id")
                if not mid or mid in known_ids:
                    continue
                # 歌名对得上，或风格描述对得上，都算这首歌的新成果
                matched = (title and m.get("title") == title) or (prompt and m.get("idea") == prompt)
                if not matched:
                    continue
                if mid not in seen_new:
                    seen_new.add(mid)
                    log(f"  ✓ 新作品已产生：{m.get('title')}（music_id={mid}），等音频转码…")
                # 刚生成完那会儿 audio_url 还是空的，要等它就绪
                au = m.get("audio_url")
                if au and au not in fresh_urls:
                    fresh_urls.append(au)
                    log(f"  ✓ 音频已就绪：{m.get('title')}（{len(fresh_urls)}/{need}）")
            if len(fresh_urls) >= need:
                break
            time.sleep(poll)
        time.sleep(3)

        urls = fresh_urls[:need]
        # 接口没拿到就退回嗅探器结果（双保险）
        if not urls and sniffer.audio_urls:
            urls = sniffer.audio_urls[:need]
        if not urls:
            log("  ✗ 超时，没拿到音频链接。可以重试或加 --probe 看看网页返回了什么。")
            continue

        for idx, url in enumerate(urls, start=1):
            seq = len(all_saved) + 1
            folder_name = safe_name(title) if (rounds == 1 and need == 1) else f"{safe_name(title)}-{seq:02d}"
            dest_dir = ROOT / cfg["library_dir"] / folder_name
            dest_dir.mkdir(parents=True, exist_ok=True)
            try:
                resp = context.request.get(url, timeout=180000)
                data = resp.body()
            except Exception as e:
                log(f"  ✗ 下载失败：{e}")
                continue
            if len(data) < cfg["download"].get("min_audio_bytes", 200000):
                log(f"  ✗ 文件太小（{len(data)} 字节），像是坏文件，跳过")
                continue
            ext = os.path.splitext(url.split("?")[0])[1].lower() or ".mp3"
            dest = dest_dir / ("audio" + ext)
            dest.write_bytes(data)
            (dest_dir / "lyrics.txt").write_text(lyrics, encoding="utf-8")
            meta = {
                "title": title,
                "style": task["style"],
                "instrumental": instrumental,
                "source_url": url,
                "audio_file": dest.name,
                "size_bytes": len(data),
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "task_no": task["no"],
            }
            (dest_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"  ✓ 已保存：{dest.relative_to(ROOT)}  ({len(data)/1024/1024:.1f} MB)")

            # 顺手把封面也做了：PNG / 1440×1440 / ≤10MB
            try:
                from cover import make_cover
                cov = make_cover(dest_dir / "cover.png", title, task["style"])
                if cov:
                    kb = cov.stat().st_size / 1024
                    log(f"  ✓ 封面已生成：cover.png  (1440×1440 PNG, {kb:.0f} KB)")
            except Exception as e:
                log(f"  ⚠ 封面生成失败（不影响音频）：{e}")

            all_saved.append(dest_dir)

        time.sleep(cfg.get("pause_between_tasks_sec", 3))

    if not all_saved:
        log(f"  ✗ 《{title}》没能拿到任何歌")
    return all_saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="只打开浏览器让你登录，登录成功后自动保存并退出")
    ap.add_argument("--probe", action="store_true", help="探路模式：记录网页网络请求到 probe_report.json")
    ap.add_argument("--task", type=int, default=0, help="只跑任务表第 N 行")
    ap.add_argument("--headless", action="store_true", help="无界面运行（不推荐，登录态可能失效）")
    args = ap.parse_args()

    cfg = load_config()
    from paths import user_profile  # 登录态存每用户私有目录，绝不在 skill 内
    profile = user_profile(cfg["profile_dir"])
    profile.mkdir(exist_ok=True)

    tasks = load_tasks(cfg)
    log(f"任务表共 {len(tasks)} 条")
    if args.task:
        tasks = [t for t in tasks if t["no"] == args.task]
        if not tasks:
            log(f"任务表里没有第 {args.task} 行")
            return

    with sync_playwright() as p:
        is_headless = args.headless or cfg.get("headless", False)
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=is_headless,
            # ★ 窗口自适应设备：有头模式最大化 + 不固定视口，页面跟随窗口大小。
            #   之前固定 1440×900，在小屏笔记本上页面渲染不全、底部按钮被挤出屏幕，
            #   换设备/换分辨率就点不到。现在任何设备打开都是完整的。
            args=window_args(is_headless),
            viewport=viewport_for(is_headless),
            accept_downloads=True,
            slow_mo=200,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        sniffer = Sniffer()
        page.on("response", sniffer.on_response)
        # 挂弹窗守卫：原生 alert/confirm 不处理会让页面一直挂起；
        # window.open 开的新窗口（授权页等）也会自动带上守卫。
        guard_context(ctx, log=log)

        try:
            if args.login:
                log("正在打开登录页面...")
                # ★ 这个模式就是专门用来登录的，绝对不能清弹窗（会关掉登录框）
                goto_with_guard(page, cfg["base_url"], cfg=cfg, log=log,
                                settle_sec=2, dismiss=False)
                print("\n" + "=" * 60)
                print("请在刚打开的浏览器窗口里完成登录（微信/手机号都行）。")
                print("登录成功后脚本会自动继续，不需要回到黑窗口按任何键。")
                print("（登录期间脚本不会去关任何弹窗，以免把登录框关掉）")
                print("=" * 60)
                if wait_logged_in(page, cfg, minutes=20):
                    log("✓ 检测到已登录")
                else:
                    log("⚠️ 等待登录超时，仍会保存当前浏览器状态后退出")
                ctx.storage_state(path=str(ROOT / "storage_state.json"))
                log("登录态已保存，下次就不用再登录了。")
                return

            if not ensure_login(page, cfg):
                print("\n" + "=" * 60)
                print("登录等待超时，已关闭浏览器。请先运行： python generate.py --login")
                print("登录成功后再运行： python generate.py")
                print("=" * 60)
                return

            for task in tasks:
                dest_dirs = []
                for attempt in range(cfg.get("retry_times", 2) + 1):
                    dest_dirs = do_one_task(page, ctx, cfg, task, sniffer)
                    if dest_dirs:
                        break
                    log(f"  第 {attempt+1} 次没成功，准备重试...")
                    time.sleep(5)
                if args.probe:
                    (ROOT / "probe_report.json").write_text(
                        json.dumps(sniffer.probe_records, ensure_ascii=False, indent=2), encoding="utf-8")
                    log("探路报告已写入 probe_report.json")
                    break
                time.sleep(cfg.get("pause_between_tasks_sec", 3))

            log("全部任务跑完了。接下来运行： python review.py 打开审核页面挑歌。")
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
