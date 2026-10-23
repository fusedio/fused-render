"""Render the DMG window background: a light canvas with the app's sparkle
glyph scattered around the two icons, the way a good installer image looks
(one icon, one folder, nothing to read). Run by build_dmg.sh step 6, which
hands the result to dmgbuild; `<out>` and `<out>@2x.png` are both written,
and dmgbuild folds the pair into one HiDPI TIFF so the image is crisp on a
Retina display and still right on a plain one.

    python scripts/dmg_background.py build/dmg-background.png [--caption TEXT]

Everything here is drawn, not loaded: the four-pointed sparkle is the same
shape build_dmg.sh draws for the app icon, in the app's yellow, the shell's
near-black and a warm grey, each with a soft shadow. The window is
WIDTH x HEIGHT points; the icon centers and label row are the coordinates
build_dmg.sh's dmgbuild settings use, so the clear areas below line up with
where Finder draws the icons. Rendered at SUPERSAMPLE x and downsampled, so
edges are smooth at both scales. Needs Pillow (in the build venv).
"""
import argparse
import math
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 760, 496           # points; the Finder window's content area
SUPERSAMPLE = 4

CANVAS = (251, 251, 248, 255)      # warm white, the download page's light paper
YELLOW = (217, 239, 58, 255)       # the icon's #E5FF44, a touch darker to read on white
INK = (27, 29, 33, 255)            # the icon card / shell --bg-alt
GREY = (179, 178, 170, 255)        # download page --ink-2 (light theme)
CAPTION = (139, 138, 130, 255)     # download page --ink-3 (light theme)

# (center x, center y, outer radius, rotation in degrees, color) in points.
# Kept clear of the two icon boxes (centers (225, 218) and (535, 218), 160pt
# icons) and the label row under them.
SPARKLES = [
    (118, 72, 15, 12, YELLOW),
    (372, 54, 21, -18, INK),
    (655, 86, 12, 28, GREY),
    (64, 292, 9, 0, GREY),
    (380, 236, 13, 14, YELLOW),
    (696, 322, 17, -8, INK),
    (152, 416, 11, 24, YELLOW),
    (568, 438, 23, -32, GREY),
    (418, 398, 7, 0, INK),
]

FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",                 # macOS, the build host
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/opentype/inter/Inter-Regular.otf",   # Linux previews
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def sparkle_polygon(cx, cy, outer_r, rotation_deg):
    """The icon's four-pointed sparkle: tips at N/E/S/W joined by concave
    quadratic arcs whose control points sit near the center."""
    waist_r = outer_r * (0.09 / 0.34)
    tips = [(-90, outer_r), (0, outer_r), (90, outer_r), (180, outer_r)]
    waists = [(-45, waist_r), (45, waist_r), (135, waist_r), (-135, waist_r)]

    def point(angle_deg, r):
        a = math.radians(angle_deg + rotation_deg)
        return (cx + r * math.cos(a), cy + r * math.sin(a))

    poly = []
    for i in range(4):
        tip = point(*tips[i])
        nxt = point(*tips[(i + 1) % 4])
        ctrl = point(*waists[i])
        poly.append(tip)
        for t in [x / 16 for x in range(1, 16)]:
            x = (1 - t) ** 2 * tip[0] + 2 * (1 - t) * t * ctrl[0] + t**2 * nxt[0]
            y = (1 - t) ** 2 * tip[1] + 2 * (1 - t) * t * ctrl[1] + t**2 * nxt[1]
            poly.append((x, y))
    return poly


def _font(size_px):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size_px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size_px)   # Pillow >= 10.1 bundles a face
    except TypeError:
        return ImageFont.load_default()


def render(caption):
    s = SUPERSAMPLE
    w, h = WIDTH * s, HEIGHT * s
    img = Image.new("RGBA", (w, h), CANVAS)

    # Shadows first, on their own layer, blurred and offset down a little.
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for cx, cy, r, rot, _color in SPARKLES:
        sd.polygon(sparkle_polygon(cx * s, (cy + 1.5) * s, r * s, rot), fill=(20, 20, 16, 46))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=2.2 * s))
    img.alpha_composite(shadow)

    d = ImageDraw.Draw(img)
    for cx, cy, r, rot, color in SPARKLES:
        d.polygon(sparkle_polygon(cx * s, cy * s, r * s, rot), fill=color)

    if caption:
        font = _font(13 * s)
        d.text((WIDTH * s / 2, 456 * s), caption, fill=CAPTION, font=font, anchor="mm")
    return img


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", help="1x PNG path; the @2x sibling is written beside it")
    ap.add_argument("--caption", default="Drag Fused Render into Applications",
                    help="small grey line at the bottom; '' for none")
    args = ap.parse_args(argv)
    if not args.out.endswith(".png"):
        sys.exit("dmg_background.py: the output must be a .png (dmgbuild pairs it with @2x.png)")

    img = render(args.caption)
    img.resize((WIDTH * 2, HEIGHT * 2), Image.LANCZOS).save(args.out[:-4] + "@2x.png", optimize=True)
    img.resize((WIDTH, HEIGHT), Image.LANCZOS).save(args.out, optimize=True)


if __name__ == "__main__":
    main()
