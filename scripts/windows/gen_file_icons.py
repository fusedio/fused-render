"""Generate per-category .ico files for the Windows "Open with" associations.

Explorer draws one icon per registered ProgID; winopen.py points each
extension's ProgID at the category .ico produced here so a file's Explorer
icon matches the glyph fused-render's own listing shows for it.

The glyphs and tints are transcribed from frontend/src/components/FileIcons.tsx
and shell.css — keep the two in sync. Rendering goes through Playwright's
headless Chromium (crisp SVG at 512px), then Pillow packs the standard icon
sizes into one multi-resolution .ico.

Run with the venv interpreter (needs playwright + pillow):
    .venv/Scripts/python.exe scripts/windows/gen_file_icons.py
"""
import io
import os

from PIL import Image
from playwright.sync_api import sync_playwright

OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "fused_render",
    "assets",
    "file_icons",
)
RENDER_SIZE = 512
ICO_SIZES = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]

# variant -> (tint, [svg element markup]). Paths mirror FileIcons.tsx; tints
# mirror the .file-icon--<variant> rules in shell.css.
GLYPHS: dict[str, tuple[str, list[str]]] = {
    "folder": ("#9fb1d2", [
        '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    ]),
    "code": ("#7fb2a6", [
        '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>',
        '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
        '<path d="m10 12.5-2 2.5 2 2.5"/>',
        '<path d="m14 12.5 2 2.5-2 2.5"/>',
    ]),
    "data": ("#a898cb", [
        '<rect width="18" height="18" x="3" y="3" rx="2"/>',
        '<path d="M3 9h18"/>',
        '<path d="M3 15h18"/>',
        '<path d="M12 3v18"/>',
    ]),
    "json": ("#c3a96b", [
        '<path d="M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1"/>',
        '<path d="M16 21h1a2 2 0 0 0 2-2v-5c0-1.1.9-2 2-2a2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1"/>',
    ]),
    "html": ("#cd9d75", [
        '<circle cx="12" cy="12" r="10"/>',
        '<path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/>',
        '<path d="M2 12h20"/>',
    ]),
    "image": ("#cd93b6", [
        '<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/>',
        '<circle cx="9" cy="9" r="2"/>',
        '<path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
    ]),
    "doc": ("#9aa0a6", [
        '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>',
        '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
        '<path d="M16 13H8"/>',
        '<path d="M16 17H8"/>',
        '<path d="M10 9H8"/>',
    ]),
    "media": ("#d0939b", [
        '<rect width="18" height="18" x="3" y="3" rx="2"/>',
        '<path d="M7 3v18"/>',
        '<path d="M3 7.5h4"/>',
        '<path d="M3 12h18"/>',
        '<path d="M3 16.5h4"/>',
        '<path d="M17 3v18"/>',
        '<path d="M17 7.5h4"/>',
        '<path d="M17 16.5h4"/>',
    ]),
    "geo": ("#88bf95", [
        '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/>',
        '<circle cx="12" cy="10" r="3"/>',
    ]),
    "archive": ("#baa386", [
        '<rect width="20" height="5" x="2" y="3" rx="1"/>',
        '<path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/>',
        '<path d="M10 12h4"/>',
    ]),
    "db": ("#82abbf", [
        '<ellipse cx="12" cy="5" rx="9" ry="3"/>',
        '<path d="M3 5v14a9 3 0 0 0 18 0V5"/>',
        '<path d="M3 12a9 3 0 0 0 18 0"/>',
    ]),
    "model": ("#6fbfc9", [
        '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/>',
        '<path d="m3.3 7 8.7 5 8.7-5"/>',
        '<path d="M12 22V12"/>',
    ]),
    "file": ("#868c93", [
        '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>',
        '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
    ]),
}


def _svg(color: str, body: list[str]) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{RENDER_SIZE}" '
        f'height="{RENDER_SIZE}" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round">{"".join(body)}</svg>'
    )


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": RENDER_SIZE, "height": RENDER_SIZE})
        for variant, (color, body) in GLYPHS.items():
            svg = _svg(color, body)
            page.set_content(
                f'<body style="margin:0">{svg}</body>',
                wait_until="networkidle",
            )
            png = page.screenshot(omit_background=True)
            img = Image.open(io.BytesIO(png)).convert("RGBA")
            out = os.path.join(OUT_DIR, f"{variant}.ico")
            img.save(out, format="ICO", sizes=ICO_SIZES)
            print(out)
        browser.close()


if __name__ == "__main__":
    main()
