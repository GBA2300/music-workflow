# -*- coding: utf-8 -*-
"""
按钮探测器 —— 列出 MiniMax 音乐页面所有可见按钮的精确属性
==========================================================
用法：
    python inspect_buttons.py [output.json]

输出每个 button 的：
  - text（截断到 50 字）
  - visible / enabled
  - class（含 arco-btn / ant-btn 等）
  - type / id / data-* 属性
  - bounding box（位置和大小）

方便针对真实按钮写出精准 selector。
"""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent

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
window.chrome = window.chrome || { runtime: {} };
"""


def main():
    out_path = ROOT / "buttons.json"
    if len(sys.argv) > 1:
        out_path = Path(sys.argv[1])

    cfg = {}
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception as e:
        print(f"读 config.json 失败: {e}")
        return 1

    profile_dir = ROOT / cfg.get("profile_dir", "profile")
    # 跨平台清理残留浏览器（Windows/macOS/Linux 通用）
    try:
        from browser_utils import cleanup
        cleanup(profile_dir)
    except Exception:
        try:
            subprocess.run(["taskkill", "/f", "/im", "chrome.exe"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        for n in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                fp = profile_dir / n
                if fp.exists():
                    fp.unlink()
            except Exception:
                pass

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir), headless=False, args=STEALTH_ARGS,
            user_agent=REAL_UA, locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport=None, ignore_default_args=["--enable-automation"],
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(20000)

        print(f"[{datetime.now():%H:%M:%S}] 打开 {cfg['base_url']}")
        page.goto(cfg["base_url"], wait_until="domcontentloaded", timeout=60000)
        time.sleep(8)  # 等页面稳定

        # 先填一下风格和歌词（否则底部生成按钮可能是禁用的）
        sel = cfg.get("selectors", {})
        for s in (sel.get("prompt_input") or []):
            try:
                loc = page.locator(s).first
                loc.wait_for(state="visible", timeout=4000)
                loc.fill("测试 流行 钢琴 男声 治愈", timeout=4000)
                break
            except Exception:
                continue
        for s in (sel.get("lyrics_input") or []):
            try:
                loc = page.locator(s).first
                loc.wait_for(state="visible", timeout=4000)
                loc.fill("主歌一\n测试歌词第一行\n主歌二\n测试歌词第二行\n副歌\n测试副歌\n", timeout=4000)
                break
            except Exception:
                continue

        time.sleep(3)

        # 列出所有 button 元素（含文本、属性、可见性、位置）
        info = page.evaluate("""
        () => {
          const out = [];
          for (const b of document.querySelectorAll('button')) {
            const r = b.getBoundingClientRect();
            const visible = r.width > 0 && r.height > 0 &&
                            getComputedStyle(b).visibility !== 'hidden' &&
                            getComputedStyle(b).display !== 'none';
            out.push({
              tag: 'BUTTON',
              text: (b.innerText || b.textContent || '').trim().slice(0, 80),
              visible,
              disabled: b.disabled,
              class: (b.className || '').toString().slice(0, 120),
              type: b.type || '',
              id: b.id || '',
              x: Math.round(r.x), y: Math.round(r.y),
              w: Math.round(r.width), h: Math.round(r.height),
              data_attrs: Object.fromEntries(
                [...b.attributes]
                  .filter(a => a.name.startsWith('data-') || a.name.startsWith('aria-'))
                  .map(a => [a.name, a.value])
              ),
            });
          }
          return out;
        }
        """)

        # 按位置排序（页面上→下，左→右）
        info.sort(key=lambda b: (b["y"], b["x"]))
        # 只保留可见的
        visible = [b for b in info if b["visible"]]
        disabled_visible = [b for b in visible if b.get("disabled")]

        print(f"\n共 {len(info)} 个 button，{len(visible)} 个可见，其中 {len(disabled_visible)} 个 disabled")
        print(f"\n=== 可见按钮（按位置排序）===")
        for i, b in enumerate(visible):
            dis = " [DISABLED]" if b.get("disabled") else ""
            print(f"[{i}] {b['text']!r}{dis}")
            print(f"    class={b['class']!r}  type={b['type']}  id={b['id']!r}")
            print(f"    pos=({b['x']},{b['y']}) size={b['w']}x{b['h']}")
            if b.get("data_attrs"):
                print(f"    data/aria={b['data_attrs']}")

        out_path.write_text(json.dumps({"visible": visible, "all": info},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n详细数据已存：{out_path}")
        time.sleep(2)
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())