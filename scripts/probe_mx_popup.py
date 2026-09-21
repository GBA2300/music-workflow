# -*- coding: utf-8 -*-
"""妙响（抖音音乐创作实验室）弹窗探针 —— 只读诊断。

**什么时候用它**：妙响页面被自定义浮层挡住时。典型表现：
  - 日志出现 `XPTO intercepts pointer events` / 「点了没反应」
  - 某个按钮 `locator resolved to <span>…</span>` 但点不动、超时
  - 页面 tab 切不过去（如资产页「生成结果」tab 未就绪、重试耗尽）

**它做什么**：
  1. 起真实浏览器（每用户私有 profile），打开妙响创作页 / 资产页
  2. 把「挡路浮层」列出来：tag / class / id / position / z-index / 覆盖比例 / 文字
  3. 列出浮层里**所有可点元素**（button / role=button / aria-label / svg），带 class 与坐标
  4. 用 `elementFromPoint` 抓「点某个按钮时，实际吃到点击的是谁」（这是判定根因的硬证据）
  5. 截图存档
  6. **实测守卫**：真跑一次 `popup_guard.dismiss_popups()`，再看浮层还在不在
     → 只看 DOM 会得出「应该能关」的假结论；能不能关必须真点。

**零额度消耗**：不点「生成歌曲」、不点「导出」、不点「下载」。

用法：
    python probe_mx_popup.py                 # 默认查创作页
    python probe_mx_popup.py --assets        # 查资产页（含「生成结果」tab）
    python probe_mx_popup.py --no-dismiss    # 只抓 DOM，不真点关闭按钮
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright  # noqa: E402

import miaoxiang  # noqa: E402
import popup_guard  # noqa: E402

DUMP_JS = r"""
() => {
  const vw = innerWidth, vh = innerHeight, screenArea = vw * vh;
  const cls = (e) => {
    try {
      if (typeof e.className === 'string') return e.className;
      if (e.className && e.className.baseVal !== undefined) return e.className.baseVal;
    } catch (_) {}
    return '';
  };
  const rectOf = (e) => { const r = e.getBoundingClientRect();
    return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]; };
  const out = [];
  for (const el of document.querySelectorAll('*')) {
    let cs; try { cs = getComputedStyle(el); } catch (_) { continue; }
    if (cs.position !== 'fixed' && cs.position !== 'absolute') continue;
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (parseFloat(cs.opacity || '1') < 0.05) continue;
    const z = parseInt(cs.zIndex || '0', 10) || 0;
    if (z < 100) continue;
    const r = el.getBoundingClientRect();
    const area = r.width * r.height;
    if (area < screenArea * 0.15) continue;
    const kids = [];
    for (const b of el.querySelectorAll(
        'button,[role="button"],[aria-label],a,svg,i,span[class*="close"],span[class*="Close"]')) {
      const br = b.getBoundingClientRect();
      if (br.width === 0 && br.height === 0) continue;
      kids.push({ tag: b.tagName, cls: cls(b).slice(0, 90),
                  aria: b.getAttribute('aria-label'), role: b.getAttribute('role'),
                  id: b.id || null, rect: rectOf(b),
                  text: (b.innerText || '').trim().slice(0, 40) });
      if (kids.length >= 30) break;
    }
    out.push({ tag: el.tagName, cls: cls(el).slice(0, 140), id: el.id || null,
               position: cs.position, zIndex: z, pointerEvents: cs.pointerEvents,
               areaRatio: +(area / screenArea).toFixed(3), rect: rectOf(el),
               text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 160),
               clickables: kids });
  }
  out.sort((a, b) => b.zIndex - a.zIndex);
  return out;
}
"""


def hit_test(page, text, exact=True):
    """在指定文字的按钮中心做 elementFromPoint，看谁吃掉了点击。"""
    js = r"""
    ([txt, exact]) => {
      const all = Array.from(document.querySelectorAll('*'));
      const el = all.find(e => {
        const t = (e.innerText || '').trim();
        return exact ? t === txt : t.includes(txt);
      });
      if (!el) return { found: false };
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) return { found: true, visible: false };
      const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
      const hit = document.elementFromPoint(cx, cy);
      const cls = (e) => { try { return (typeof e.className === 'string' ? e.className
        : (e.className && e.className.baseVal) || ''); } catch (_) { return ''; } };
      const chain = [];
      let n = hit;
      for (let i = 0; i < 5 && n; i++) {
        chain.push({ tag: n.tagName, cls: cls(n).slice(0, 110), id: n.id || null });
        n = n.parentElement;
      }
      return { found: true, visible: true,
               target: { tag: el.tagName, cls: cls(el).slice(0, 90),
                         rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] },
               hitAtCenter: chain };
    }
    """
    return page.evaluate(js, [text, exact])


def main():
    ap = argparse.ArgumentParser(description="妙响弹窗只读探针")
    ap.add_argument("--assets", action="store_true", help="查资产页（默认查创作页）")
    ap.add_argument("--wait", type=int, default=9, help="页面渲染等待秒数（默认 9）")
    ap.add_argument("--no-dismiss", action="store_true", help="只抓 DOM，不真点关闭")
    ap.add_argument("--out", default=None, help="结果 JSON 路径")
    args = ap.parse_args()

    url = miaoxiang.ASSETS_URL if args.assets else miaoxiang.CREATE_URL
    where = "资产页" if args.assets else "创作页"
    workdir = Path(miaoxiang.resolve_workdir(None))
    out_json = Path(args.out) if args.out else workdir / "_mx_popup_probe.json"
    shot = workdir / "_mx_popup_probe.png"
    shot_after = workdir / "_mx_popup_probe_after.png"

    cfg = miaoxiang.load_config(workdir)

    with sync_playwright() as p:
        ctx, page = miaoxiang.open_browser(p)
        popup_guard.guard_context(ctx, log=lambda m: print(m, flush=True))
        try:
            print(f"· 打开{where}：{url}", flush=True)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(args.wait * 1000)
            print(f"· 等渲染 {args.wait}s 完成，开始抓 DOM", flush=True)

            before = page.evaluate(DUMP_JS)
            page.screenshot(path=str(shot), full_page=False)
            print(f"\n===== 挡路浮层（position=fixed/absolute 且 z≥100 且面积≥15%），共 {len(before)} 个 =====",
                  flush=True)
            for i, o in enumerate(before):
                print(f"\n[{i}] <{o['tag']}> class={o['cls'][:100]!r} id={o['id']!r}")
                print(f"    position={o['position']} z={o['zIndex']} pointer-events={o['pointerEvents']}"
                      f" 覆盖={o['areaRatio']*100:.0f}% rect={o['rect']}")
                print(f"    文字：{o['text'][:120]!r}")
                print(f"    内含可点元素 {len(o['clickables'])} 个：")
                for k in o["clickables"]:
                    print(f"      - <{k['tag']}> cls={k['cls'][:70]!r} aria={k['aria']!r} "
                          f"role={k['role']!r} rect={k['rect']} text={k['text']!r}")

            probe_text = "生成结果" if args.assets else "专业模式"
            ht = hit_test(page, probe_text)
            print(f"\n===== 命中测试：点「{probe_text}」时谁吃掉点击 =====", flush=True)
            print(json.dumps(ht, ensure_ascii=False, indent=2), flush=True)

            raw_roots = [o["cls"] for o in before if o["cls"]]
            sugg = []
            for c in raw_roots:
                for tok in c.split():
                    if any(s in tok.lower() for s in ("modal", "popup", "dialog", "guide", "mask", "overlay")):
                        sugg.append(tok)
            print("\n===== 建议加进 config.json → popup_guard.extra_popup_roots 的选择器 =====",
                  flush=True)
            for s in dict.fromkeys(sugg):
                print(f'  "[class*=\'{s}\']"', flush=True)
            if not sugg:
                print("  （没识别出关键词，请人工看上面的 class 名）", flush=True)

            result = {"url": url, "overlays": before, "hit_test": ht,
                      "suggested_roots": list(dict.fromkeys(sugg))}

            if not args.no_dismiss:
                print("\n===== 实测：真跑一次 popup_guard.dismiss_popups() =====", flush=True)
                try:
                    n = popup_guard.dismiss_popups(
                        page, cfg=cfg, log=lambda m: print(m, flush=True))
                    print(f"· dismiss_popups 返回：{n}", flush=True)
                except Exception as e:
                    print(f"· dismiss_popups 抛错：{type(e).__name__}: {e}", flush=True)
                page.wait_for_timeout(1500)
                after = page.evaluate(DUMP_JS)
                page.screenshot(path=str(shot_after), full_page=False)
                print(f"· 清理后剩余挡路浮层：{len(after)} 个（清理前 {len(before)} 个）", flush=True)
                for o in after:
                    print(f"    残留 <{o['tag']}> class={o['cls'][:90]!r} z={o['zIndex']} "
                          f"覆盖={o['areaRatio']*100:.0f}%", flush=True)
                ht2 = hit_test(page, probe_text)
                print(f"· 清理后「{probe_text}」命中测试：", flush=True)
                print(json.dumps(ht2, ensure_ascii=False, indent=2), flush=True)
                result["after_dismiss"] = {"overlays": after, "hit_test": ht2,
                                           "count_before": len(before), "count_after": len(after)}

            out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n✓ 结果已存：{out_json}", flush=True)
            print(f"✓ 截图：{shot}（清理前）｜{shot_after if not args.no_dismiss else '—'}（清理后）",
                  flush=True)
        finally:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
