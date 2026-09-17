# -*- coding: utf-8 -*-
"""番茄音频上传页 · 学习录制模式（用户示范一遍，脚本全程记录）

用途
────
`fanqie_upload.py` 的自动填表流程里，有一部分是**用户口述补的、没被录制验证过**的：
  · 上传完成后页面上到底出现哪些必填项、顺序如何
  · 「添加自己」这类按钮的真实元素链
  · 第一步「下一步」→「确认上传」→ 独家授权 → 签约身份 → 「确认签署」
  · 「跳转授权」新开标签后的第三方电子签页面结构
这些光靠猜选择器很容易踩空。所以提供一个**纯旁观**的录制器：你按平时的方式
手动操作一遍完整流程，脚本每几秒抓一次真实 DOM + 截图，并单独记录每次点击、
每次输入、每次文件选择、每次页面跳转，形成一条可回放的时间线。

录完把 `learnsession-fanqie/run-<时间戳>/` 交给我，我 diff 后把选择器固化进
`fanqie_upload.py`，之后就不用你再手动操作了。

⚠️ 三个必读的坑（都是踩过的）
────────────────────────────
1. **文件选择对话框会被 Playwright 拦掉。**
   Playwright 默认拦截 `input[type=file]` 的原生文件对话框（改用 setFiles 注入），
   所以如果你手动点「上传」按钮，**系统选文件窗口根本不会弹出来**，你会以为卡住了。
   本脚本启动后立刻通过 CDP 把拦截关掉（`Page.setInterceptFileChooserDialog`
   enabled=false），让原生对话框正常弹出。启动日志里会打印是否成功，若打印失败
   请立刻告诉我，别硬试。

2. **任何脚本都不要 taskkill /f /im chrome.exe 强杀浏览器。**
   2026-09-12 已修：`fanqie_upload.py` 原来启动前会 `taskkill /f /im chrome.exe` 解锁 profile，
   结果**把你自己开着的 Chrome 一起杀了**（你当场发现并要求改掉）。
   现在全仓库统一走 `browser_utils.kill_browsers()` —— 它只杀 exe 路径里带
   `ms-playwright` 的进程（本工具自己拉起的），你自己的 Chrome 在
   `C:\Program Files\Google\Chrome\...`，绝不会被碰。
   录制器则更保守：只删 profile 的 Singleton* 锁文件，不杀任何进程。

3. **Playwright 同步 API 不能放子线程**（妙响侧踩过：显示"录了几百步"实际 0 文件）。
   本脚本用 async API，抓取循环跑在主协程里，不吞异常。

用法
────
    python fanqie_learn.py                 # 打开上传页开始录制（默认最长 120 分钟）
    python fanqie_learn.py --minutes 240   # 自定义最长等待
    python fanqie_learn.py --url <地址>     # 从别的页面开始录

结束方式：**关掉浏览器窗口**（或 Ctrl+C）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from playwright.async_api import async_playwright

from browser_utils import clear_profile_locks
from fanqie_upload import LAUNCH_ARGS, PROFILE, UPLOAD_URL, fit_window_to_screen

ROOT = Path(__file__).resolve().parent
LEARN_DIR = ROOT / "learnsession-fanqie"
LOG_PATH = ROOT / "_fanqie_learn_log.txt"

SNAPSHOT_EVERY = 4      # 每几秒抓一次 DOM+截图
CLICK_DUMP_EVERY = 2    # 每几秒把点击/输入/文件事件落一次盘


def log(s):
    line = f"[{time.strftime('%H:%M:%S')}] {s}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# 页面内注入的事件记录器
#   记录三类事件：click / change(含 input[type=file] 选中的文件名) / blur
#   每条都带「元素链」，方便我据此写出稳定的选择器（而不是猜 class 哈希）
# ─────────────────────────────────────────────────────────────
RECORDER_INIT = r"""
window.__fq = window.__fq || { clicks: [], inputs: [], files: [], nav: [], notes: [] };
if (!window.__fq_bound) {
  window.__fq_bound = true;

  function cls(el) {
    var c = el && el.className;
    if (!c) return '';
    if (c.baseVal !== undefined) return c.baseVal;   // SVG
    return String(c);
  }
  function chainOf(t, depth) {
    var chain = [], p = t, d = 0;
    while (p && d < (depth || 7)) {
      var c = cls(p);
      chain.push(p.tagName + (c ? '.' + c.split(/\s+/)[0] : '')
                 + (p.id ? '#' + p.id : ''));
      p = p.parentElement; d++;
    }
    return chain.join(' < ');
  }
  function desc(t) {
    var c = cls(t);
    return {
      tag: t && t.tagName ? t.tagName : null,
      id: (t && t.id) || null,
      name: (t && t.getAttribute) ? t.getAttribute('name') : null,
      type: (t && t.getAttribute) ? t.getAttribute('type') : null,
      role: (t && t.getAttribute) ? t.getAttribute('role') : null,
      aria: (t && t.getAttribute) ? t.getAttribute('aria-label') : null,
      ph: (t && t.getAttribute) ? t.getAttribute('placeholder') : null,
      cls: c.slice(0, 90),
      text: ((t && (t.innerText || t.textContent)) || '').trim().slice(0, 80),
      chain: chainOf(t, 7)
    };
  }

  document.addEventListener('click', function (e) {
    var t = e.target;
    var r = desc(t);
    r.kind = 'click';
    r.ts = Date.now();
    r.x = Math.round(e.clientX); r.y = Math.round(e.clientY);
    window.__fq.clicks.push(r);
  }, true);

  document.addEventListener('change', function (e) {
    var t = e.target;
    var r = desc(t);
    r.kind = 'change';
    r.ts = Date.now();
    try { r.value = t.value ? String(t.value).slice(0, 160) : ''; } catch (x) {}
    if (t.files && t.files.length) {
      r.files = [];
      for (var i = 0; i < t.files.length; i++) r.files.push(t.files[i].name);
      window.__fq.files.push({ ts: r.ts, name: r.files.join(','), chain: r.chain, id: r.id });
    }
    window.__fq.inputs.push(r);
  }, true);

  document.addEventListener('blur', function (e) {
    var t = e.target;
    if (!t || !t.tagName) return;
    var tag = t.tagName.toUpperCase();
    if (tag !== 'INPUT' && tag !== 'TEXTAREA' && !t.isContentEditable) return;
    var r = desc(t);
    r.kind = 'blur';
    r.ts = Date.now();
    try { r.value = t.value ? String(t.value).slice(0, 160) : (t.isContentEditable ? (t.innerText || '').slice(0, 160) : ''); } catch (x) {}
    window.__fq.inputs.push(r);
  }, true);
}
"""


# ─────────────────────────────────────────────────────────────
# 表单结构探针：把当前页所有「可交互控件」摊平成 JSON
#   这是我从你示范里提取选择器的核心依据 —— 不猜 class 哈希，直接用
#   id / name / placeholder / aria-label / 可见文案 来定位
# ─────────────────────────────────────────────────────────────
FORM_PROBE_JS = r"""() => {
  function cls(el) {
    var c = el && el.className;
    if (!c) return '';
    if (c.baseVal !== undefined) return c.baseVal;
    return String(c);
  }
  function vis(el) {
    try {
      var r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return false;
      var cs = getComputedStyle(el);
      return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
    } catch (e) { return false; }
  }
  function row(el) {
    var c = cls(el);
    return {
      tag: el.tagName,
      id: el.id || null,
      name: (el.getAttribute && el.getAttribute('name')) || null,
      type: (el.getAttribute && el.getAttribute('type')) || null,
      role: (el.getAttribute && el.getAttribute('role')) || null,
      aria: (el.getAttribute && el.getAttribute('aria-label')) || null,
      ph: (el.getAttribute && el.getAttribute('placeholder')) || null,
      cls: c.split(/\s+/).slice(0, 3).join(' ').slice(0, 90),
      value: (el.value !== undefined && el.value !== null) ? String(el.value).slice(0, 120) : null,
      text: ((el.innerText || el.textContent) || '').trim().replace(/\s+/g, ' ').slice(0, 80),
      disabled: (el.getAttribute && el.getAttribute('aria-disabled') === 'true')
                || !!el.disabled || null,
      visible: vis(el)
    };
  }
  var out = { inputs: [], buttons: [], selects: [], texts: [], containers: [] };
  var N = 600;

  document.querySelectorAll('input,textarea,[contenteditable="true"]').forEach(function (el, i) {
    if (i < N) out.inputs.push(row(el));
  });
  document.querySelectorAll('button,[role="button"],a.semi-button,[class*="semi-button"]').forEach(function (el, i) {
    if (i < N) out.buttons.push(row(el));
  });
  document.querySelectorAll('select,[role="combobox"],[class*="semi-select"]').forEach(function (el, i) {
    if (i < N) out.selects.push(row(el));
  });
  // ★ 关键：番茄的「音频/歌词/封面」上传区是**带 id 的容器 div**（如 #songs_0_songFile），
  //   真正的 input[type=file] 可能是全局挂载的、没有业务 id。第一版探针只抓
  //   input/button，把这些容器全漏了 → 特意补上。
  document.querySelectorAll('[id^="songs_"],[class*="upload"],[class*="common-file"],[class*="auth-type"]').forEach(function (el, i) {
    if (i >= N) return;
    out.containers.push({
      tag: el.tagName,
      id: el.id || null,
      cls: cls(el).split(/\s+/).slice(0, 4).join(' ').slice(0, 120),
      text: ((el.innerText || el.textContent) || '').trim().replace(/\s+/g, ' ').slice(0, 60),
      visible: vis(el)
    });
  });
  // 页面主要文案（含各类弹窗/步骤标题），只取「有可见文字且不太长」的块
  document.querySelectorAll('h1,h2,h3,h4,[class*="modal"] [class*="title"],[class*="step"],[class*="tip"],[class*="Tip"]').forEach(function (el, i) {
    if (i >= 120) return;
    var t = ((el.innerText || el.textContent) || '').trim().replace(/\s+/g, ' ');
    if (t && t.length <= 120 && vis(el)) out.texts.push({ cls: cls(el).split(/\s+/)[0], text: t });
  });
  return {
    url: location.href,
    title: document.title,
    bodyLen: (document.body ? (document.body.innerText || '').length : 0),
    controls: out
  };
}"""


async def _dump_events(pg, run_dir, tag):
    """把某个标签页里累积的 click/input/file 事件落盘。

    ⚠️ 2026-09-12 踩坑（必读）：第一版把**所有页面的事件写成同一组文件**
       （clicks.json / inputs.json / ...），而抓取对象是 `ctx.pages[-1]`。
       结果「跳转授权」新开电子签标签后，新标签没有注入脚本 → 读出空数组 →
       **把之前番茄页辛苦录到的点击/输入/文件事件全覆盖成 0**（本次录制就是这么丢的）。
       所以现在改为**按标签页分文件**命名，谁也覆盖不了谁。
    """
    try:
        data = await pg.evaluate("() => window.__fq || null")
    except Exception as e:
        log(f"  ! 事件抓取失败({tag})：{type(e).__name__}: {e}")
        return
    if not data:
        return
    for key in ("clicks", "inputs", "files", "nav"):
        try:
            (run_dir / f"events_{tag}_{key}.json").write_text(
                json.dumps(data.get(key, []), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log(f"  ! events_{tag}_{key}.json 写入失败：{type(e).__name__}: {e}")


async def _tag_for(pg, idx):
    """给标签页起个可读的名字，用于事件文件名（如 0_novelfm / 1_letsign）。"""
    try:
        host = (pg.url or "").split("/")[2]
    except Exception:
        host = ""
    host = "".join(ch for ch in host if ch.isalnum() or ch in ".-")[:24] or "page"
    return f"{idx}_{host}"


async def _allow_native_file_picker(page):
    """关掉 Playwright 对 input[type=file] 原生文件对话框的拦截。

    Playwright 默认会拦掉原生文件选择框，导致用户手动点「上传」时**窗口不弹出**。
    这里通过 CDP 关闭拦截，让用户能正常选文件。选中的文件名仍会被页面内
    注入的 change 监听器记录下来（不依赖 Playwright 的 filechooser 事件）。
    """
    try:
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Page.setInterceptFileChooserDialog", {"enabled": False})
        log("  ✓ 已放行原生文件选择框（手动上传时系统选文件窗口能正常弹出）")
        return True
    except Exception as e:
        log(f"  ⚠️ 放行原生文件选择框失败：{type(e).__name__}: {e}")
        log("     → 若你手动点上传时选文件窗口不弹出，请立刻告诉我，别硬试。")
        return False


async def do_learn(url, max_min):
    run_dir = LEARN_DIR / time.strftime("run-%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    log(f"· 本次录制目录：{run_dir}")

    # 只删 profile 锁文件，**不杀进程**（避免把用户自己开着的 Chrome 一起杀掉）
    removed = clear_profile_locks(PROFILE)
    if removed:
        log(f"· 已清理 profile 锁：{removed}")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE), headless=False, args=LAUNCH_ARGS, viewport=None)

        # 新开的标签页（「跳转授权」那步会开新标签）也要被记录
        cdp_pages = set()

        async def hook(new_page):
            if new_page in cdp_pages:
                return
            cdp_pages.add(new_page)
            try:
                await new_page.add_init_script(RECORDER_INIT)
            except Exception:
                pass
            try:
                new_page.set_default_timeout(20000)
            except Exception:
                pass
            log(f"· 发现新标签页：{new_page.url[:110]}")
            try:
                await _allow_native_file_picker(new_page)
            except Exception:
                pass

        ctx.on("page", lambda pg: asyncio.ensure_future(hook(pg)))

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(20000)
        await page.add_init_script(RECORDER_INIT)
        await fit_window_to_screen(page)

        # 原生 JS 弹窗（alert/confirm）会直接卡死页面 —— 自动接受并记录
        page.on("dialog", lambda d: (log(f"· 原生弹窗[{d.type}]: {d.message[:100]}"),
                                     asyncio.ensure_future(d.accept())))

        tracked = {page}
        await _allow_native_file_picker(page)

        log("")
        log("=" * 64)
        log("【学习录制中】请在浏览器里按你平时的方式，完整走一遍番茄上传+发布：")
        log("  1. 上传音频 / 歌词 → 填歌名 → 词曲/制作人/歌手「添加自己」→ 上传封面")
        log("  2. 第一步「下一步」→「确认上传」")
        log("  3. 选择签约模式（独家授权）+ 签约身份（个人）→「下一步」")
        log("  4. 「预览合同协议」→「确认签署」→ 等「合同生成中」")
        log("  5. 之后会自动新开标签跳到电子签（飞书合同 → 电子牵），走到签完")
        log("     （收验证码签字是你的私密操作，不签也行，录到签署页我就够用了）")
        log("  ★ 番茄没有「发布」按钮：**显示「签署成功」就等于发布成功**")
        log("")
        log(f"  脚本每 {SNAPSHOT_EVERY} 秒抓一次真实 DOM+截图，并单独记录")
        log("  【每次点击】【每次输入】【每次选文件】【每次跳转】。")
        log("  操作完【关掉浏览器窗口】即结束 —— 不用赶时间，")
        log(f"  最长等 {max_min} 分钟（--minutes 可改），期间脚本不会自己关窗。")
        log("=" * 64)
        log("")

        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            log(f"! 打开页面失败：{type(e).__name__}: {e}")
        log("· 等 10 秒让 SPA 渲染完…")
        await asyncio.sleep(10)

        seq = 0
        last_nav = ""
        deadline = time.time() + max_min * 60

        while time.time() < deadline:
            # 用户关窗 → pages 归零 → 结束
            try:
                if len(ctx.pages) == 0:
                    log("· 检测到浏览器已关闭，结束录制")
                    break
            except Exception:
                log("· 浏览器已断开，结束录制")
                break

            seq += 1
            # ★ 每个标签页都抓：番茄页在 tab0、电子签在 tab1，只抓最后一个会丢数据
            try:
                pages_now = list(ctx.pages)
            except Exception:
                pages_now = [page]
            if not pages_now:
                break

            for pi, pg in enumerate(pages_now):
                tag = await _tag_for(pg, pi)
                if pg not in tracked:
                    tracked.add(pg)
                    try:
                        await pg.add_init_script(RECORDER_INIT)
                    except Exception:
                        pass
                try:
                    d = await pg.evaluate(FORM_PROBE_JS)
                    d["_seq"] = seq
                    d["_ts"] = time.strftime("%H:%M:%S")
                    d["_page_index"] = pi
                    name = f"step_{seq:03d}.json" if pi == 0 else f"step_{seq:03d}_p{pi}_{tag}.json"
                    (run_dir / name).write_text(
                        json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
                    if pi == 0 and seq % 2 == 1:
                        await pg.screenshot(path=str(run_dir / f"step_{seq:03d}.png"))
                    if d.get("url") and d["url"] != last_nav:
                        last_nav = d["url"]
                        log(f"  · 跳转 → {last_nav[:110]}")
                except Exception as e:
                    # 不静默吞异常：抓不到要看得见，否则又会出现"录了几百步其实 0 文件"
                    log(f"  ! 第 {seq} 次抓取失败({tag})：{type(e).__name__}: {e}")

            if seq % 2 == 0:
                for pi, pg in enumerate(pages_now):
                    await _dump_events(pg, run_dir, await _tag_for(pg, pi))

            await asyncio.sleep(SNAPSHOT_EVERY)
        else:
            log(f"! 已到 {max_min} 分钟上限，结束录制")

        # 收尾：最后再落一次盘，保证点击/输入/文件记录不丢
        try:
            for pi, pg in enumerate(list(ctx.pages)):
                tag = await _tag_for(pg, pi)
                try:
                    await _dump_events(pg, run_dir, tag)
                except Exception:
                    pass
                # 把每个标签页的最终 DOM 原文也存一份（探针看不到的容器 div 靠它兜底）
                try:
                    html = await pg.content()
                    (run_dir / f"final_{tag}.html").write_text(html, encoding="utf-8")
                except Exception:
                    pass
        except Exception as e:
            log(f"  ! 收尾落盘异常：{type(e).__name__}: {e}")

        try:
            await ctx.close()
        except Exception:
            pass

    saved = len(list(run_dir.glob("step_*.json")))

    def _count(suffix):
        """统计所有标签页事件文件里某个类别的事件总数（按新的 events_* 命名）。"""
        total = 0
        for fp in run_dir.glob(f"events_*_{suffix}.json"):
            try:
                total += len(json.loads(fp.read_text(encoding="utf-8")))
            except Exception:
                pass
        return total

    log("")
    log(f"✓ 录制结束：计数 {seq} 步，实际存下 {saved} 个快照")
    log(f"  点击 {_count('clicks')} 次 / 输入 {_count('inputs')} 次 "
        f"/ 选中文件 {_count('files')} 次")
    log(f"  目录：{run_dir}")
    if seq and not saved:
        log("  ⚠️ 一步都没存下来！抓 DOM 出错，别拿这份数据去写自动化。")
    log("  把目录名告诉我就行，我 diff 后固化选择器。")


def main():
    ap = argparse.ArgumentParser(description="番茄上传页学习录制（用户示范一遍，脚本全程记录）")
    ap.add_argument("--minutes", type=int, default=120, help="最长等待分钟数（默认 120）")
    ap.add_argument("--url", default=UPLOAD_URL, help="起始地址（默认番茄上传页）")
    args = ap.parse_args()
    try:
        LOG_PATH.write_text(
            f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} 番茄学习录制启动 ===\n", encoding="utf-8")
    except Exception:
        pass
    log("番茄上传页 · 学习录制模式启动")
    asyncio.run(do_learn(args.url, args.minutes))


if __name__ == "__main__":
    main()
