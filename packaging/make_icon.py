"""生成应用图标（打包用）：圆角渐变方块 + 白色「课」字。

    python packaging/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).with_name("app.ico")


def rounded(size: int, radius: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 竖向渐变 #6d6bf0 → #9d7bff
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = (
            int(0x6D + (0x9D - 0x6D) * ratio),
            int(0x6B + (0x7B - 0x6B) * ratio),
            int(0xF0 + (0xFF - 0xF0) * ratio),
            255,
        )
        draw.line([(0, y), (size, y)], fill=color)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    img.putalpha(mask)
    return img


def main() -> None:
    size = 256
    img = rounded(size, int(size * 0.24))
    draw = ImageDraw.Draw(img)

    font = None
    for candidate in (
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ):
        if Path(candidate).exists():
            try:
                font = ImageFont.truetype(candidate, int(size * 0.56))
                break
            except OSError:
                continue
    if font is None:  # 兜底：画一个方块
        draw.rectangle([size * 0.3, size * 0.3, size * 0.7, size * 0.7], fill=(255, 255, 255, 235))
    else:
        text = "课"
        box = draw.textbbox((0, 0), text, font=font)
        width, height = box[2] - box[0], box[3] - box[1]
        draw.text(
            ((size - width) / 2 - box[0], (size - height) / 2 - box[1] - size * 0.02),
            text, font=font, fill=(255, 255, 255, 245),
        )

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(OUT, format="ICO", sizes=sizes)
    print(f"写入 {OUT}")


if __name__ == "__main__":
    main()
