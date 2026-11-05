"""Vytvoří ikony aplikace: reclight.ico (Windows) a reclight.icns (macOS) z loga v gui.py."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent


def logo(size: int) -> Image.Image:
    k = 4
    s = size * k
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill="#26282c")
    pad = s * 0.17
    d.ellipse((pad, pad, s - pad, s - pad), outline=(226, 75, 74, 130), width=max(2, int(s * 0.03)))
    c = s * 0.30
    d.ellipse((c, c, s - c, s - c), fill="#E24B4A")
    h = s * 0.40
    d.ellipse((h, h, h + s * 0.09, h + s * 0.09), fill="#F5A5A3")
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    big = logo(1024)
    big.save(HERE / "reclight.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(HERE / "reclight.icns", format="ICNS")
    big.resize((512, 512), Image.LANCZOS).save(HERE / "reclight.png")
    print("ikony hotové")


if __name__ == "__main__":
    main()
