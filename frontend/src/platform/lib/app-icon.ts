// Writing an app's `icon.svg` from an icon-picker pick — the one place the
// pick → disk rule lives, because two surfaces now offer it: the sidebar's
// Projects row (its glyph is the picker's toggle) and the app page's header
// mark. The READ side needs no sharing; every surface already draws the file
// through `appIconUrl`.
import type { IconPick } from "@platform/ui/IconPicker";
import { removeAppIcon, setAppIcon } from "@platform/lib/api";
import { iconStyleBlock } from "@platform/lib/icon-color";
import { announceCurrentAppsChanged } from "@platform/lib/tasksChanged";

/** Pixel size of the emoji raster baked into the svg: the sidebar draws it at
 *  24px and the app page at 26px, so 128 is 4–5× — crisp on a retina display
 *  at a tolerable file size (measured 2026-09-20 in Chrome: 10–32 KB as a
 *  data URL, flags smallest, detailed glyphs largest). */
const EMOJI_RASTER_PX = 128;

/** The picked emoji as a standalone icon.svg document — square viewBox, no
 *  fixed size, and NO plate (owner, 2026-09-20): an emoji is a full-colour
 *  tile of its own, and a plate behind it read as a box around a sticker.
 *  A lucide pick keeps its plate (IconPicker.glyphIconSvg) because a bare
 *  stroke glyph needs the contrast.
 *
 *  The glyph is a PNG, not `<text>` (owner, 2026-09-20). The text version
 *  centred on `text-anchor="middle"`, which hangs on the engine's idea of the
 *  emoji's advance width — right in Chrome, wrong in WebKit, where the glyph
 *  landed off-centre and cropped. A canvas raster sidesteps font metrics at
 *  render time entirely: the glyph is drawn once here, cropped to its INK
 *  bounds (measureText's actualBoundingBox*, so "remove all padding" is
 *  literal — no em-box slack), and every reader — the shell, Finder, GitHub,
 *  a bare tab — just paints pixels. The cost is that the file bakes in the
 *  picking OS's emoji font; a pick made on a Mac stays Apple-styled on
 *  Windows, which for an app's own icon is a feature.
 *
 *  `data-fused-color="default"` stays so readIconColor still recognises the
 *  file as picker-written; there are no currentColor strokes, so themeIconSvg
 *  leaves the image untouched. Files written before this keep their `<text>`
 *  (or plate); no migration (owner). */
export async function emojiIconSvg(emoji: string): Promise<string> {
  const png = rasterizeEmoji(emoji);
  const body = png
    ? `<image href="${png}" width="64" height="64"/>`
    : // No canvas (a non-DOM caller): the old text glyph, best effort.
      '<text x="32" y="32" text-anchor="middle" dominant-baseline="central" ' +
      `font-size="60">${escapeText(emoji)}</text>`;
  return (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" data-fused-color="default">' +
    iconStyleBlock("default") +
    body +
    "</svg>"
  );
}

function escapeText(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** The emoji as a PNG data URL, EMOJI_RASTER_PX square, its ink cropped to
 *  the square's edges (aspect kept, centred on the short axis). Null when
 *  there is no canvas to draw on. */
function rasterizeEmoji(emoji: string): string | null {
  if (typeof document === "undefined") return null;
  const size = EMOJI_RASTER_PX;
  // Measure at a generous size so the crop math has sub-pixel headroom; the
  // draw below scales the ink to the target, so this number only sets the
  // measurement's precision.
  const probe = size * 2;
  const font = `${probe}px system-ui, "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif`;

  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.font = font;
  ctx.textBaseline = "alphabetic";
  ctx.textAlign = "left";
  const m = ctx.measureText(emoji);
  // Ink box relative to the pen position (x = 0, y = baseline). Older engines
  // lack these and report 0 for all four; fall back to the em box then.
  const left = m.actualBoundingBoxLeft ?? 0;
  const right = m.actualBoundingBoxRight ?? 0;
  const ascent = m.actualBoundingBoxAscent ?? 0;
  const descent = m.actualBoundingBoxDescent ?? 0;
  let inkX = -left;
  let inkY = -ascent;
  let inkW = left + right;
  let inkH = ascent + descent;
  if (inkW <= 0 || inkH <= 0) {
    inkX = 0;
    inkY = -probe * 0.8;
    inkW = m.width || probe;
    inkH = probe;
  }

  // Fit the ink box into the square: one scale for both axes (no stretching),
  // the short axis centred.
  const scale = size / Math.max(inkW, inkH);
  const dx = (size - inkW * scale) / 2;
  const dy = (size - inkH * scale) / 2;
  ctx.setTransform(scale, 0, 0, scale, dx - inkX * scale, dy - inkY * scale);
  ctx.font = font;
  ctx.fillText(emoji, 0, 0);
  return canvas.toDataURL("image/png");
}

/** Apply an icon picker's answer to the app folder at `path`: `null` removes
 *  the file (back to the generic mark), an icon pick is stored as the finished
 *  grey-glyph svg it arrives as, and an emoji gets the standalone wrapper above.
 *
 *  Announces the desk change on success, and that is the reason this is shared
 *  rather than two call sites: the sidebar's Projects row for this app is on
 *  screen while the app page's own mark is picked, and it only refetches on
 *  that event — without it the row keeps yesterday's glyph until something
 *  else pokes it. Throws whatever the write threw; the caller reports it (a
 *  failed write leaves the old icon, and a refetch shows the truth). */
export async function applyIconPick(
  path: string,
  pick: IconPick | null,
): Promise<void> {
  if (pick === null) await removeAppIcon(path);
  else if (pick.kind === "icon") await setAppIcon(path, pick.svg);
  else await setAppIcon(path, await emojiIconSvg(pick.emoji));
  announceCurrentAppsChanged();
}
