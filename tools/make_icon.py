"""生成 ArchiveMate 的应用图标。

不依赖设计稿：用 Pillow 直接画一个「落到盒子里」的箭头，象征「归巢」。
先按 1024 像素超采样绘制，再缩到各个尺寸，边缘才干净。

用法：python tools/make_icon.py
产物：assets/archivemate.ico、assets/archivemate.png（256px，供 macOS/Linux 用）
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# 与 archivemate/gui/theme.py 的 accent 保持一致
ACCENT = (47, 111, 235, 255)
ACCENT_DARK = (37, 89, 196, 255)
WHITE = (255, 255, 255, 255)

BASE = 1024
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1),
                                           radius=radius, fill=255)
    return mask


def render(size: int = BASE) -> Image.Image:
    """画一枚图标：蓝色圆角方块 + 白色下箭头 + 底部收纳盒。"""
    scale = size / 1024.0

    def px(value: float) -> float:
        return value * scale

    # 渐变底：从上到下略微加深，避免大色块发闷
    gradient = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(gradient)
    for y in range(size):
        t = y / max(1, size - 1)
        colour = tuple(
            round(ACCENT[i] + (ACCENT_DARK[i] - ACCENT[i]) * t) for i in range(3)
        ) + (255,)
        draw.line([(0, y), (size, y)], fill=colour)

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(gradient, (0, 0), _rounded_mask(size, round(px(230))))

    draw = ImageDraw.Draw(canvas)
    stroke = round(px(80))

    def line(points, width=stroke) -> None:
        """带圆头圆角的线段，看起来比默认方头柔和。"""
        draw.line(points, fill=WHITE, width=width, joint="curve")
        for point in (points[0], points[-1]):
            draw.ellipse((point[0] - width / 2, point[1] - width / 2,
                          point[0] + width / 2, point[1] + width / 2), fill=WHITE)

    # 向下的箭头：竖杆 + 两条斜边
    centre_x = size / 2
    line([(centre_x, px(215)), (centre_x, px(520))])

    head_half = px(175)
    tip_y = px(700)
    left = (centre_x - head_half, px(500))
    right = (centre_x + head_half, px(500))
    tip = (centre_x, tip_y)
    line([left, tip])
    line([right, tip])

    # 底部的收纳碟：一条横线，箭头正好落进去
    line([(px(235), px(810)), (size - px(235), px(810))])

    return canvas


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    master = render(BASE)

    png_path = ASSETS / "archivemate.png"
    master.resize((256, 256), Image.LANCZOS).save(png_path)
    print(f"已写入 {png_path}")

    ico_path = ASSETS / "archivemate.ico"
    master.save(ico_path, format="ICO",
                sizes=[(s, s) for s in ICO_SIZES])
    print(f"已写入 {ico_path}（{', '.join(f'{s}x{s}' for s in ICO_SIZES)}）")

    big_path = ASSETS / "archivemate-512.png"
    master.resize((512, 512), Image.LANCZOS).save(big_path)
    print(f"已写入 {big_path}")

    # macOS 的 .app 需要 .icns，Pillow 可以直接写，不用 iconutil
    icns_path = ASSETS / "archivemate.icns"
    try:
        master.resize((1024, 1024), Image.LANCZOS).save(icns_path, format="ICNS")
        print(f"已写入 {icns_path}")
    except Exception as exc:  # pragma: no cover - 只是打包素材，失败不影响开发
        print(f"跳过 .icns（{exc}）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
