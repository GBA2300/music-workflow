# -*- coding: utf-8 -*-
"""
歌曲封面生成器 —— 最初默认设计风格
=====================================
番茄音频要求：PNG，分辨率 >= 1440x1440，文件 <= 10 MB。

设计构成（与最初发布那批封面一致）：
  1. 竖向情绪渐变底（深色基调）
  2. 单一发光圆环（细描边 + 弱辉光）
  3. 居中歌名（白字 + 柔光 + 黑投影）
  4. 圆环下方一行风格标签：「mood · 流行情歌 · instrument」
  5. 暗角 + 胶片颗粒（提升质感）

零积分：纯本地 Pillow 生成。
用法：
    make_cover(dest, title, style="", source=None, size=1440)
    - title : 歌名
    - style : 风格描述，用于选配色和提取底部 3 个标签
    - source: 可选自定义底图
"""
import os
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

SIZE = 1440
MAX_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------- 情绪配色
PALETTES = {
    "default":   ((44, 12, 64),  (9, 12, 38),  (255, 64, 120)),
    "sad":       ((26, 30, 58),  (54, 40, 78),  (150, 130, 200)),
    "fire":      ((58, 12, 20),  (26, 10, 46),  (255, 92, 58)),
    "heal":      ((12, 44, 48),  (28, 26, 56),  (88, 214, 196)),
    "guofeng":   ((46, 16, 16),  (14, 18, 44),  (236, 190, 86)),
    "edm":       ((46, 4, 70),   (8, 24, 60),   (255, 64, 168)),
    "love":      ((54, 8, 34),   (28, 12, 56),  (255, 120, 168)),
    "night":     ((12, 16, 30),  (26, 26, 52),  (112, 130, 168)),
    "nostalgia": ((48, 28, 14),  (22, 16, 50),  (226, 170, 92)),
    "calm":      ((10, 28, 38),  (20, 16, 50),  (130, 214, 198)),
}

KEYWORDS = {
    "sad":       "悲伤 忧郁 伤感 离别 思念 遗憾 心碎 孤独 失落 分手 痛 沧桑 无奈 中年 漂泊 打工 苦",
    "fire":      "热血 摇滚 燃 励志 奋斗 逆袭 兄弟 冲 拼 梦想 青春 不服 战 力量 燃向 爆",
    "heal":      "治愈 清新 温柔 温暖 民谣 安静 轻音乐 舒服 阳光 微风 慢 甜 舒适 抚慰",
    "guofeng":   "古风 国风 中国风 江湖 武侠 戏曲 琵琶 二胡 古筝 笛 汉服 长安 江南 水墨 诗词",
    "edm":       "电子 舞曲 EDM 蹦迪 夜店 DJ 派对 节奏 鼓点 幻 迷幻 赛博 电音 律动 卡点",
    "love":      "爱情 情歌 甜蜜 心动 表白 恋人 喜欢 爱你 初恋 浪漫 撩 婚纱 告白",
    "night":     "深夜 emo 失眠 一个人 城市 雨夜 凌晨 街 霓虹 孤独 夜 影子 酒精",
    "nostalgia": "怀旧 回忆 青春 校园 80后 老歌 童年 从前 那年 旧 时光 毕业 同学 故乡",
    "calm":      "纯音乐 钢琴 冥想 助眠 白噪音 大自然 雨声 海浪 森林 静 舒缓 背景 轻",
}

# 底部 3 个标签的中文 mood（与最初封面「伤感 · 流行情歌 · 钢琴」风格一致）
LABEL_MOOD = {
    "default":   "流行金曲",
    "sad":       "伤感歌谣",
    "fire":      "热血励志",
    "heal":      "治愈民谣",
    "guofeng":   "国风古韵",
    "edm":       "动感电音",
    "love":      "甜蜜情歌",
    "night":     "深夜独白",
    "nostalgia": "怀旧情歌",
    "calm":      "轻音乐",
}

INSTRUMENTS = [
    "钢琴", "木吉他", "吉他", "电吉他", "电贝斯",
    "古筝", "琵琶", "笛", "二胡", "小提琴",
    "电音", "电子", "鼓", "鼓点", "弦乐",
]
DEFAULT_INSTRUMENT = {
    "default": "钢琴", "sad": "钢琴", "fire": "电吉他", "heal": "木吉他",
    "guofeng": "古筝", "edm": "电音", "love": "钢琴", "night": "钢琴",
    "nostalgia": "木吉他", "calm": "钢琴",
}


def pick_palette(text):
    """返回 (调色板名, (top, bottom, accent))"""
    t = str(text or "")
    best, best_score = "default", 0
    for name, words in KEYWORDS.items():
        if not words:
            continue
        score = sum(1 for w in words.split() if w and w in t)
        if score > best_score:
            best, best_score = name, score
    return best, PALETTES.get(best, PALETTES["default"])


def _style_tags(text, palette_name):
    """底部风格标签：「mood · 流行情歌 · instrument」"""
    mood = LABEL_MOOD.get(palette_name, "流行金曲")
    inst = next((x for x in INSTRUMENTS if x in str(text or "")), None)
    if not inst:
        inst = DEFAULT_INSTRUMENT.get(palette_name, "钢琴")
    return f"{mood} \u00b7 流行情歌 \u00b7 {inst}"  # \u00b7 = ·


def _font(size, bold=True):
    cands = []
    if bold:
        cands += [
            r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\msyhl.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        ]
    cands += [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for fp in cands:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(text, font, draw, max_width):
    lines, cur = [], ""
    for ch in str(text):
        if ch == "\n":
            lines.append(cur); cur = ""; continue
        test = cur + ch
        if draw.textlength(test, font=font) > max_width and cur:
            lines.append(cur); cur = ch
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


def _radial_glow(size, cx, cy, radius, color, strength=1.0):
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    steps = 60
    for i in range(steps, 0, -1):
        r = radius * i / steps
        a = int(255 * (1 - i / steps) * strength)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color + (a,))
    return layer.filter(ImageFilter.GaussianBlur(size * 0.05))


def _vignette(size, strength=0.55):
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    steps = 80
    for i in range(steps, 0, -1):
        inset = int(size * 0.5 * i / steps)
        a = int(255 * (1 - i / steps) ** 2 * strength)
        d.rectangle([inset, inset, size - inset, size - inset],
                    outline=(0, 0, 0, a), width=max(1, size // 200))
    return layer.filter(ImageFilter.GaussianBlur(size * 0.04))


def _grain(size, sigma=22, alpha=24):
    noise = Image.effect_noise((size, size), sigma).convert("RGB")
    g = Image.new("RGB", (size, size), (128, 128, 128))
    return Image.blend(g, noise, alpha / 255.0)


def _save_png_under_limit(im, dest):
    im.convert("RGB").save(dest, "PNG", optimize=True)
    if dest.stat().st_size <= MAX_BYTES:
        return
    im.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=256).save(dest, "PNG", optimize=True)
    if dest.stat().st_size <= MAX_BYTES:
        return
    for q in (128, 64, 32):
        im.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=q).save(dest, "PNG", optimize=True)
        if dest.stat().st_size <= MAX_BYTES:
            return


def make_cover(dest, title, style="", source=None, size=SIZE):
    """
    生成 1440x1440 PNG 封面（最初默认设计风格）。
    """
    if not HAS_PIL:
        print("  提示：没装 Pillow，跳过封面（pip install pillow）")
        return None

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 自定义底图：裁方后只叠歌名
    src = Path(source) if source else None
    if src and src.exists():
        im = Image.open(src).convert("RGB")
        w, h = im.size
        s = min(w, h)
        im = im.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
        im = im.resize((size, size), Image.LANCZOS)
        # 歌名 + 底部标签（用默认 accent）
        _, (_, _, accent) = pick_palette(f"{style} {title}")
        _draw_text_block(im, title, _style_tags(style, pick_palette(f"{style} {title}")[0]),
                         accent, size)
        _save_png_under_limit(im, dest)
        return dest

    # 自动设计
    palette_name, (top, bottom, accent) = pick_palette(f"{style} {title}")

    # 1) 竖向渐变底
    base = Image.new("RGB", (size, size))
    dr = ImageDraw.Draw(base)
    for y in range(size):
        t = y / size
        dr.line([(0, y), (size, y)], fill=(
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
        ))
    base = base.convert("RGBA")

    # 2) 主光晕（右上，让圆环在中心暗底上清晰可见 —— 最初设计如此）
    glow_main = _radial_glow(size, int(size * 0.72), int(size * 0.26),
                             int(size * 0.55), accent, strength=0.85)
    base.alpha_composite(glow_main)
    # 辅助弱光晕（左下，提亮右下）
    glow_sub = _radial_glow(size, int(size * 0.22), int(size * 0.82),
                            int(size * 0.40), (255, 255, 255), strength=0.18)
    base.alpha_composite(glow_sub)

    # 3) 单一发光圆环（细描边 + 弱辉光）
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    radius = int(size * 0.40)
    for i, wk in enumerate((10, 4)):
        rr = radius - i * int(size * 0.03)
        rd.ellipse([size // 2 - rr, size // 2 - rr, size // 2 + rr, size // 2 + rr],
                   outline=accent + (60 if i == 0 else 150,), width=wk)
    ring = ring.filter(ImageFilter.GaussianBlur(size * 0.012))
    base.alpha_composite(ring)

    # 4) 暗角 + 颗粒
    base.alpha_composite(_vignette(size))
    base_rgb = base.convert("RGB")
    base_rgb = Image.blend(base_rgb, _grain(size), 0.06)
    base = base_rgb.convert("RGBA")

    # 5) 歌名 + 底部风格标签
    final = _draw_text_block(base.convert("RGB"), title,
                              _style_tags(style, palette_name), accent, size)
    final = final.filter(ImageFilter.UnsharpMask(radius=2, percent=40, threshold=3))
    _save_png_under_limit(final, dest)
    return dest


def _draw_text_block(base_rgb, title, tag_line, accent, size):
    """在已渲染的底图上叠加：居中歌名 + 圆环下方一行风格标签"""
    base = base_rgb.convert("RGBA")
    cx = size // 2

    # ---- 歌名 ----
    f_title = _font(int(size * 0.105), bold=True)
    max_w = int(size * 0.74)
    lines = _wrap(title, f_title, ImageDraw.Draw(Image.new("RGBA", (1, 1))), max_w)[:3]
    line_h = int(size * 0.125)
    total_h = len(lines) * line_h
    y = int(size * 0.42) - total_h // 2

    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for ln in lines:
        bb = ld.textbbox((0, 0), ln, font=f_title)
        tw = bb[2] - bb[0]
        x = cx - tw // 2
        # 柔和辉光（多层模糊）
        for blur_r, alpha in [(size * 0.02, 110), (size * 0.008, 200)]:
            glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            ImageDraw.Draw(glow).text((x, y), ln, font=f_title, fill=(255, 255, 255, int(alpha)))
            glow = glow.filter(ImageFilter.GaussianBlur(blur_r))
            layer.alpha_composite(glow)
        # 投影
        ImageDraw.Draw(layer).text((x, y + size * 0.008), ln, font=f_title, fill=(0, 0, 0, 130))
        ld.text((x, y), ln, font=f_title, fill=(255, 255, 255, 255))
        y += line_h

    # ---- 底部风格标签（圆环外下方，accent 色） ----
    f_tag = _font(int(size * 0.034), bold=False)
    tag_w = ld.textlength(tag_line, font=f_tag)
    tag_x = cx - tag_w // 2
    tag_y = int(size * 0.84)
    # 标签投影
    ImageDraw.Draw(layer).text((tag_x + 2, tag_y + 2), tag_line, font=f_tag, fill=(0, 0, 0, 140))
    ld.text((tag_x, tag_y), tag_line, font=f_tag, fill=accent + (235,))

    base.alpha_composite(layer)
    return base.convert("RGB")
