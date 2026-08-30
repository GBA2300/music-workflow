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
      1. NFKC 归一化会把中文全角标点转成半角，于是「妈，我回来了」里的
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

def try_click(page, selectors, timeout=2500):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
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
    page.goto(cfg["base_url"], wait_until="domcontentloaded", timeout=60000)
    time.sleep(4)
    if _no_login_button(page):
        return True
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
        page.goto(cfg["base_url"], wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        sel = cfg["selectors"]

        # 纯音乐开关
        if instrumental:
            hit = try_click(page, sel["instrumental_toggle"])
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
        hit = try_click(page, sel["generate_button"], timeout=5000)
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
    profile = ROOT / cfg["profile_dir"]
    profile.mkdir(exist_ok=True)

    tasks = load_tasks(cfg)
    log(f"任务表共 {len(tasks)} 条")
    if args.task:
        tasks = [t for t in tasks if t["no"] == args.task]
        if not tasks:
            log(f"任务表里没有第 {args.task} 行")
            return

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=args.headless or cfg.get("headless", False),
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
            slow_mo=200,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        sniffer = Sniffer()
        page.on("response", sniffer.on_response)

        try:
            if args.login:
                log("正在打开登录页面...")
                page.goto(cfg["base_url"], wait_until="domcontentloaded", timeout=60000)
                print("\n" + "=" * 60)
                print("请在刚打开的浏览器窗口里完成登录（微信/手机号都行）。")
                print("登录成功后脚本会自动继续，不需要回到黑窗口按任何键。")
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
