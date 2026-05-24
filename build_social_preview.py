#!/usr/bin/env python3
"""Generate the 1200x630 social-card image at assets/social-preview.png.

Source poster and output path are resolved relative to this script's location,
so it can be re-run after swapping in a different feature poster.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "posters" / "ireland_and_great_britain_grid_paper_grid_20260522_182144.png"
OUT = ROOT / "assets" / "social-preview.png"

W, H = 1200, 630
BG = (250, 250, 247)
INK = (20, 23, 26)
INK_SOFT = (74, 79, 87)
INK_MUTE = (122, 127, 136)
ACCENT = (192, 57, 43)

FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"


def wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for word in words:
        test = (cur + " " + word).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def main():
    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)

    poster = Image.open(SRC).convert("RGBA")
    pad = 30
    poster_h = H - 2 * pad
    poster_w = round(poster.width * poster_h / poster.height)
    poster_resized = poster.resize((poster_w, poster_h), Image.LANCZOS)

    shadow = Image.new("RGBA", (poster_w + 24, poster_h + 24), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle(
        (12, 12, poster_w + 12, poster_h + 12), fill=(20, 23, 26, 40)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))

    poster_x = W - poster_w - pad
    poster_y = pad
    canvas.paste(shadow, (poster_x - 12, poster_y - 6), shadow)
    canvas.paste(poster_resized, (poster_x, poster_y), poster_resized)

    left_x = 60
    content_w = poster_x - left_x - 40

    f_title = ImageFont.truetype(FONT_BOLD, 96)
    f_tag = ImageFont.truetype(FONT_REG, 26)
    f_tag_bold = ImageFont.truetype(FONT_BOLD, 26)
    f_meta = ImageFont.truetype(FONT_REG, 20)
    f_eyebrow = ImageFont.truetype(FONT_BOLD, 16)

    title_y = 180
    x = left_x
    for text, color in [("Grid", INK), ("2", ACCENT), ("Poster", INK)]:
        draw.text((x, title_y), text, font=f_title, fill=color)
        x = draw.textbbox((x, title_y), text, font=f_title)[2]

    sub_y = title_y + 110
    draw.text((left_x, sub_y), "Gallery", font=f_tag_bold, fill=INK_SOFT)

    tagline = (
        "Print-ready posters of electrical transmission grids, "
        "generated from OpenStreetMap data."
    )
    tag_y = sub_y + 50
    for line in wrap(draw, tagline, f_tag, content_w):
        draw.text((left_x, tag_y), line, font=f_tag, fill=INK_SOFT)
        tag_y += 36

    eyebrow_y = 80
    draw.rectangle((left_x, eyebrow_y, left_x + 40, eyebrow_y + 3), fill=ACCENT)
    draw.text(
        (left_x + 56, eyebrow_y - 10),
        "OPEN ENERGY TRANSITION  ·  MAPYOURGRID",
        font=f_eyebrow,
        fill=INK_MUTE,
    )

    draw.text(
        (left_x, H - pad - 30),
        "github.com/open-energy-transition/grid2poster",
        font=f_meta,
        fill=INK_MUTE,
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(OUT, "PNG", optimize=True)
    print(f"Wrote {OUT.relative_to(ROOT)} ({canvas.size})")


if __name__ == "__main__":
    main()
