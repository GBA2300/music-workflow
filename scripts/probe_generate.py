# -*- coding: utf-8 -*-
"""
生成探针 —— 专治「已点生成，但一直等不到歌」
==================================================
用法（在工作目录里运行）：
    python probe_generate.py

脚本会打开 MiniMax 音乐页，自动填入 tasks.csv 里的第一首歌，
点生成，然后全程盯着：
  · 每 15 秒截图到 _probe_shot.png（历史图在 _probe_shots/）
  · 打印页面文字摘要（能看到「生成中/排队/失败/积分不足」等提示）
  · 记录所有跟 music/audio 有关的后台请求（URL、状态码、返回体预览）
  · 抓到的音频链接直接列出来

跑完看三样东西即可定位问题：
  1. _probe_shot.png / _probe_shots/ 里的截图（最直观）
  2. 终端打印的页面文字（有没有报错提示）
  3. _probe_records.json（后台接口到底返回了什么）
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
SHOT = ROOT / "_probe_shot.png"
SHOT_DIR = ROOT / "_probe_shots"
RECORDS = ROOT / "_probe_records.json"

REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized", "--no-first-run", "--no-default-browser-check",
    "--disable-infobars", "--lang=zh-CN",
]
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
window.chrome = window.chrome || { runtime: {} };
"""

records = []
audio_found = []


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def shoot(page, tag=""):
    try:
        SHOT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOT))
        stamp = datetime.now().strftime("%H%M%S")
        page.screenshot(path=str(SHOT_DIR / f"{stamp}{('_' + tag) if tag else ''}.png"))
    except Exception as e:
        log(f"  (截图失败: {e})")


def text_of(page, n=15):
    try:
        t = page.evaluate("document.body ? document.body.innerText : ''")
        lines = [x.strip() for x in t.split("\n") if x.strip()]
        return " / ".join(lines[:n])
    except Exception as e:
        return f"(读取失败: {e})"


def on_response(resp):
    """记录所有跟 music/audio 相关的请求，方便事后定位接口变没变。"""
    url = resp.url or ""
    low = url.lower()
    if not ("music" in low or "audio" in low):
        return
    ctype = (resp.headers or {}).get("content-type", "")
    body = ""
    if "json" in ctype:
        try:
            body = resp.text()[:1500]
        except Exception:
            body = "(读取失败)"
    else:
        body = f"(非 JSON, content-type={ctype})"
    rec = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "url": url[:200],
        "status": resp.status,
        "content_type": ctype,
        "method": (resp.request.method if resp.request else ""),
        "body_preview": body,
    }
    records.append(rec)
    # 直接在 JSON 里找 mp3/wav 链接
    for ext in (".mp3", ".wav", ".m4a", ".flac"):
        idx = 0
        while True:
            i = body.find(ext, idx)
            if i < 0:
                break
            s = max(0, i - 120)
            seg = body[s:i + len(ext)]
            j = max(seg.rfind("http://"), seg.rfind("https://"))
            if j >= 0:
                u = seg[j:].split('"')[0].split("\\")[0]
                if u not in audio_found:
                    audio_found.append(u)
                    log(f"  🎵 抓到音频链接：{u[:110]}...")
            idx = i + 1


def main():
    cfg = {}
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception as e:
        log(f"⚠️ 读 config.json 失败：{e}")
        return 1

    # 取 tasks.csv 第一行做样本
    task = None
    csv_path = ROOT / "tasks.csv"
    try:
        import csv as _csv
        with open(csv_path, encoding="utf-8-sig") as f:
            rows = list(_csv.DictReader(f))
        task = rows[0] if rows else None
    except Exception as e:
        log(f"⚠️ 读 tasks.csv 失败：{e}")
    if not task:
        log("tasks.csv 是空的，没法探测")
        return 1

    title = (task.get("歌名") or "").strip()
    style = (task.get("风格描述") or "").strip()
    lyrics_file = (task.get("歌词文件") or "").strip()
    log(f"探测样本：《{title}》 风格：{style[:40]}")

    lyrics = ""
    if lyrics_file:
        p = ROOT / cfg.get("lyrics_dir", "lyrics") / lyrics_file
        if not p.exists():
            p = ROOT / cfg.get("lyrics_dir", "lyrics") / (lyrics_file + ".txt")
        if p.exists():
            lyrics = p.read_text(encoding="utf-8").strip()

    try:
        subprocess.run(["taskkill", "/f", "/im", "chrome.exe"],
                       capture_output=True, timeout=10)
    except Exception:
        pass

    profile_dir = ROOT / cfg.get("profile_dir", "profile")
    for n in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            fp = profile_dir / n
            if fp.exists():
                fp.unlink()
        except Exception:
            pass

    sel = cfg.get("selectors", {})
    wait_sec = int(cfg.get("wait_timeout_sec", 420))

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir), headless=False, args=STEALTH_ARGS,
            user_agent=REAL_UA, locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport=None, ignore_default_args=["--enable-automation"],
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(20000)
        page.on("response", on_response)

        log(f"打开 {cfg['base_url']}")
        page.goto(cfg["base_url"], wait_until="domcontentloaded", timeout=60000)
        time.sleep(6)
        shoot(page, "opened")
        log(f"打开后页面文字：{text_of(page)}")

        def fill(key, value, label):
            for s in (sel.get(key) or []):
                try:
                    loc = page.locator(s).first
                    loc.wait_for(state="visible", timeout=4000)
                    loc.click(timeout=3000)
                    loc.fill(value, timeout=6000)
                    log(f"  ✓ 已填{label}（选择器 {s}）")
                    return True
                except Exception:
                    continue
            log(f"  ✗ 找不到{label}输入框")
            return False

        fill("prompt_input", style or title, "风格描述")
        if lyrics:
            fill("lyrics_input", lyrics, "歌词")

        hit = None
        for s in (sel.get("generate_button") or []):
            try:
                loc = page.locator(s).first
                loc.wait_for(state="visible", timeout=4000)
                loc.click(timeout=4000)
                hit = s
                break
            except Exception:
                continue
        log(f"生成按钮：{hit or '✗ 没找到'}")
        shoot(page, "clicked")

        if not hit:
            log("没点到生成按钮，请截图给 AI 看页面长什么样")
            RECORDS.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            time.sleep(5)
            ctx.close()
            return 1

        log(f"已点生成，接下来每 15 秒报一次状态（最多等 {wait_sec} 秒）...")
        start = time.time()
        last = 0
        while time.time() - start < wait_sec:
            elapsed = int(time.time() - start)
            if elapsed - last >= 15:
                last = elapsed
                log(f"  [{elapsed}s] 页面：{text_of(page, 10)}")
                shoot(page, f"t{elapsed}")
            if audio_found:
                log(f"✅ 抓到 {len(audio_found)} 个音频链接，停止等待")
                break
            time.sleep(2)

        shoot(page, "final")
        log(f"最终页面：{text_of(page, 20)}")
        log(f"抓到的音频链接数：{len(audio_found)}")
        for u in audio_found:
            log(f"  - {u}")

        RECORDS.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"后台请求记录已保存：{RECORDS.name}（共 {len(records)} 条）")
        log("浏览器 5 秒后关闭…")
        time.sleep(5)
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
