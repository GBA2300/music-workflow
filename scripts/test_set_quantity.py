"""回归测试：set_generate_quantity 在真实形态（Tailwind 自定义 stepper）下能 2→1。

复现 2026-09-01 的真实页面结构：数量 stepper 是一个自定义组件，嵌在含很多其它
按钮的大容器里；值显示在一个纯数字 span 里，父容器恰好有「减/加」两个按钮。
早期版本用「数量」标签往上找第一个含≥2按钮的祖先，误命中整个大容器，
btns[0] 变成「参考音乐上传」按钮，点减号完全无效 → 页面始终出 2 首。
本测试故意把 stepper 放进含 14 个按钮的大容器，验证新定位逻辑只认 stepper。
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright
from generate import set_generate_quantity, load_config

HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div id="big">
  <button>参考音乐（可选）点击或拖拽上传，生成专属翻唱</button>
  <button class="ant-switch custom-switch"></button>
  <button>流行</button><button>电子鼓</button><button>萨克斯风</button>
  <button>打击乐器</button><button>氛围感</button><button>儿童音乐</button><button>迪斯科</button>
  <div class="bg-bg_gray_15 flex items-center gap-1 rounded-[20px] px-3 py-2" id="stepper">
    <span>数量:&nbsp;</span>
    <button id="minus" class="flex items-center justify-center rounded-sm transition-colors cursor-pointer"><svg width="9" height="2"><path d="M7.58 1.16H0.58"/></svg></button>
    <span id="val" class="text-bg_opacity_60 min-w-[20px] text-center text-[14px] font-[400]">2</span>
    <button id="plus" class="flex items-center justify-center rounded-sm transition-colors cursor-pointer"><svg width="14" height="14"><path d="M10.5 7.58H7.58V10.5"/></svg></button>
  </div>
  <button id="gen">600创作</button>
</div>
<script>
  const val = document.getElementById('val');
  document.getElementById('minus').addEventListener('click', () => {
    val.textContent = String(Math.max(1, Number(val.textContent) - 1));
  });
  document.getElementById('plus').addEventListener('click', () => {
    val.textContent = String(Number(val.textContent) + 1);
  });
</script>
</body></html>"""


def main():
    cfg = load_config()
    tmp = tempfile.mkdtemp(prefix="qty_test_")
    p = Path(tmp) / "index.html"
    p.write_text(HTML, encoding="utf-8")

    results = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        pg.goto(p.as_uri())
        pg.wait_for_timeout(200)

        def cur():
            return pg.evaluate("document.getElementById('val').textContent.trim()")

        # 1) 2 -> 1（主路径：点减号）
        logs = []
        ok = set_generate_quantity(pg, cfg, 1, log=logs.append)
        results.append(("2→1", cur() == "1" and ok, f"值={cur()} 返回={ok}"))

        # 2) 1 -> 3（加号路径）
        logs.clear()
        ok = set_generate_quantity(pg, cfg, 3, log=logs.append)
        results.append(("1→3(加号)", cur() == "3" and ok, f"值={cur()} 返回={ok}"))

        # 3) 3 -> 1（再点减号）
        logs.clear()
        ok = set_generate_quantity(pg, cfg, 1, log=logs.append)
        results.append(("3→1", cur() == "1" and ok, f"值={cur()} 返回={ok}"))

        # 4) 已是 1，保持不动
        logs.clear()
        ok = set_generate_quantity(pg, cfg, 1, log=logs.append)
        results.append(("已是1", cur() == "1" and ok, f"值={cur()} 返回={ok}"))

        b.close()

    print("set_generate_quantity 回归测试（Tailwind stepper / 嵌在大容器里）")
    allok = True
    for name, ok, detail in results:
        print(f"  [{'✅' if ok else '❌'}] {name}: {detail}")
        allok = allok and ok
    print("\n结论:", "全部通过 ✅" if allok else "存在失败 ❌")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
