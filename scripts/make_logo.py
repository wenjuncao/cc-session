# -*- coding: utf-8 -*-
"""
生成 Claude Code Session 管理器 的应用图标。
设计：暖珊瑚色圆角方形徽标 + 终端提示符 ">_" + 右上角 Claude 星芒点缀。
输出：logo.png(256) 与 logo.ico(16/32/48/64/128/256 多尺寸)。

依赖 Pillow。重新生成图标： python make_logo.py
"""

import os
from PIL import Image, ImageDraw

S = 4                      # 超采样倍率（先大画再缩小，得到平滑抗锯齿）
N = 256                    # 最终基准尺寸
W = N * S                  # 画布尺寸


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def rounded_mask(size, radius):
    """圆角方形遮罩(L)。"""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def vertical_gradient(size, top, bottom):
    """竖直渐变(RGB)。"""
    img = Image.new("RGB", (size, size), top)
    d = ImageDraw.Draw(img)
    for y in range(size):
        d.line([(0, y), (size, y)], fill=lerp(top, bottom, y / (size - 1)))
    return img


def draw_chevron(d, apex, arm, w, color):
    """画一个圆头的 ">" 折线：apex 为右侧尖点，arm 为上/下两臂端点距 apex 的偏移。"""
    ax, ay = apex
    top = (ax - arm[0], ay - arm[1])
    bot = (ax - arm[0], ay + arm[1])
    d.line([top, apex], fill=color, width=w)
    d.line([apex, bot], fill=color, width=w)
    r = w // 2                               # 三个顶点补圆，得到圆角圆头
    for (px, py) in (top, apex, bot):
        d.ellipse([px - r, py - r, px + r, py + r], fill=color)


def draw_sparkle(d, cx, cy, r, color):
    """四角星芒（Claude 风格的小闪点）。"""
    thin = r * 0.30
    d.polygon([
        (cx, cy - r), (cx + thin, cy - thin), (cx + r, cy),
        (cx + thin, cy + thin), (cx, cy + r), (cx - thin, cy + thin),
        (cx - r, cy), (cx - thin, cy - thin),
    ], fill=color)


def build():
    # 画布（透明底，便于圆角外抠掉）
    canvas = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    # 1) 暖珊瑚渐变背景 + 圆角遮罩
    top_c = (0xEC, 0x95, 0x73)     # 暖珊瑚(上)
    bot_c = (0xBE, 0x57, 0x33)     # 赤陶(下)
    grad = vertical_gradient(W, top_c, bot_c).convert("RGBA")
    mask = rounded_mask(W, radius=int(58 * S))
    canvas.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(canvas)

    # 2) 顶部柔光高光（轻微玻璃质感）
    gloss = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gloss)
    gd.ellipse([int(-40 * S), int(-150 * S), int(296 * S), int(90 * S)],
               fill=(255, 255, 255, 38))
    canvas = Image.alpha_composite(canvas, Image.composite(
        gloss, Image.new("RGBA", (W, W), (0, 0, 0, 0)), mask))
    d = ImageDraw.Draw(canvas)

    ink = (0xFF, 0xF7, 0xF1, 255)  # 柔白

    # 3) 终端提示符 ">"（圆头折线）
    draw_chevron(
        d,
        apex=(int(150 * S), int(126 * S)),
        arm=(int(46 * S), int(40 * S)),
        w=int(26 * S),
        color=ink,
    )

    # 4) 光标下划线 "_"
    d.rounded_rectangle(
        [int(120 * S), int(168 * S), int(196 * S), int(186 * S)],
        radius=int(9 * S), fill=ink,
    )

    # 5) 右上角星芒点缀
    draw_sparkle(d, int(196 * S), int(66 * S), int(20 * S),
                 (0xFF, 0xF7, 0xF1, 235))
    draw_sparkle(d, int(218 * S), int(96 * S), int(9 * S),
                 (0xFF, 0xF7, 0xF1, 170))

    # 6) 缩放到基准尺寸
    icon = canvas.resize((N, N), Image.LANCZOS)

    here = os.path.dirname(os.path.abspath(__file__))
    assets = os.path.normpath(os.path.join(here, "..", "assets"))
    os.makedirs(assets, exist_ok=True)
    png_path = os.path.join(assets, "logo.png")
    ico_path = os.path.join(assets, "logo.ico")
    icon.save(png_path)
    icon.save(ico_path, sizes=[(16, 16), (32, 32), (48, 48),
                               (64, 64), (128, 128), (256, 256)])
    print("已生成:", png_path)
    print("已生成:", ico_path)


if __name__ == "__main__":
    build()
