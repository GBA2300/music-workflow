# -*- coding: utf-8 -*-
"""只读探针：确认番茄上传页「歌曲卡片」里各上传区的真实 id / class。

为什么需要它
────────────
`fanqie_upload.py` 里 `upload_file` 用的是**容器 div 的 id**（如 `#songs_0_songFile`），
再点容器里的 `[class*='upload-input-container']` 触发文件对话框。
但 2026-09-12 的学习录制里，探针只抓了 input/button，**把容器 div 全漏了**，
所以无法确认这些 id 在平台改版后是否还在。

本探针做的事
────────────
1. 打开上传页（复用 profile_fanqie 登录态）
2. **先 dump 一次**（没有卡片时的静态结构）
3. 点一次「点击添加歌曲」→ 等卡片出现
4. **再 dump 一次**（有卡片时的完整结构）—— 这一步能看到 `songs_0_*` 系列 id
5. 重新加载页面，看卡片还在不在（判断是不是会存草稿）
6. 关掉浏览器

明确的安全边界
──────────────
* **不填任何内容、不上传任何文件、不点「下一步」** → 不会产生任何提交
* **不 taskkill**，只删 profile 锁文件，不会影响你自己开着的 Chrome
* 唯一的写操作是「点一下添加歌曲」造出一张**空卡片**，用来观察结构；
  最后会报告这张卡片是否残留（残留了下一轮自动化会自动复用，不会重复建）
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from browser_utils import clear_profile_locks
from fanqie_upload import LAUNCH_ARGS, PROFILE, UPLOAD_URL, _on_upload_page

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)

# 尽量把「上传区」相关的结构一网打尽：
#   带 id 的元素 / 带 upload 语义的容器 / 所有 input[type=file] 及其父链
DUMP_JS = r"""() => {
  function cls(el) {
    var c = el && el.className;
    if (!c) return '';
    if (c.baseVal !== undefined) return c.baseVal;
    return String(c);
  }
  function parentChain(el, n) {
    var out = [], p = el, i = 0;
    while (p && i < (n || 4)) {
      out.push(p.tagName + (p.id ? '#' + p.id : '') +
               (cls(p) ? '.' + cls(p).split(/\s+/).slice(0, 2).join('.') : ''));
      p = p.parentElement; i++;
    }
    return out.join(' < ');
  }
  var res = { url: location.href, title: document.title };

  res.ids = [...document.querySelectorAll('[id]')].map(function (e) {
    return { tag: e.tagName, id: e.id, cls: cls(e).slice(0, 90) };
  });

  res.uploads = [...document.querySelectorAll('[class*="upload"],[class*="Upload"],[class*="common-file"]')]
    .map(function (e) {
      return {
        tag: e.tagName, id: e.id || null,
        cls: cls(e).slice(0, 110),
        text: ((e.innerText || '') || '').trim().replace(/\s+/g, ' ').slice(0, 40)
      };
    });

  res.fileInputs = [...document.querySelectorAll('input[type=file]')].map(function (e) {
    return {
      id: e.id || null,
      name: e.getAttribute('name'),
      accept: e.getAttribute('accept'),
      cls: cls(e).slice(0, 80),
      parentChain: parentChain(e, 5)
    };
  });

  res.cardCount = document.querySelectorAll('[id$="_songFile"]').length;
  return res;
}"""


async def dump(page, tag):
    try:
        d = await page.evaluate(DUMP_JS)
    except Exception as e:
        print(f"  ! dump({tag}) 失败：{type(e).__name__}: {e}", flush=True)
        return None
    (OUT / f"form_{tag}.json").write_text(
        json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    await page.screenshot(path=str(OUT / f"form_{tag}.png"))
    print(f"  · dump[{tag}] → out/form_{tag}.json（id={len(d['ids'])} "
          f"upload={len(d['uploads'])} file={len(d['fileInputs'])} 卡片={d['cardCount']}）",
          flush=True)
    return d


async def main():
    removed = clear_profile_locks(PROFILE)
    if removed:
        print(f"· 已清理 profile 锁：{removed}", flush=True)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE), headless=False, args=LAUNCH_ARGS, viewport=None)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(20000)
        try:
            await page.goto(UPLOAD_URL, wait_until="domcontentloaded")
        except Exception as e:
            print(f"! 打开失败：{e}", flush=True)
        await asyncio.sleep(8)

        if not await _on_upload_page(page):
            print("⚠️ 没进入上传页（可能登录态失效），停止探针。", flush=True)
            await ctx.close()
            return

        print("① 抓「空页面」结构（没有卡片）", flush=True)
        d0 = await dump(page, "empty")

        print("② 点一次「点击添加歌曲」，造一张空卡片用于观察结构", flush=True)
        loc = page.locator("text=点击添加歌曲")
        if await loc.count() == 0:
            loc = page.locator("text=添加歌曲")
        try:
            await loc.last.click(timeout=8000)
            await page.wait_for_selector("#songs_0_name_input", timeout=20000)
            await asyncio.sleep(3)
            print("  ✓ 卡片已出现，抓结构", flush=True)
            d1 = await dump(page, "card")
        except Exception as e:
            print(f"  ! 造卡片失败：{type(e).__name__}: {e}", flush=True)
            d1 = None

        print("③ 重新加载，看这张空卡片是否会被平台存成草稿", flush=True)
        try:
            await page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(8)
            d2 = await dump(page, "after_reload")
            if d2:
                print(f"  → 重载后卡片数 = {d2['cardCount']}"
                      f"（0 = 没存草稿，干净；>0 = 平台存了草稿，下一轮会自动复用）", flush=True)
        except Exception as e:
            print(f"  ! 重载失败：{type(e).__name__}: {e}", flush=True)

        print("✓ 探针结束，关闭浏览器（未做任何提交）", flush=True)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
