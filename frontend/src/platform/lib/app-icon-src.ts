// The `src` to draw an app's icon.svg from, recoloured for the live theme —
// and, for an `icon.png` (the lower-priority raster fallback, app_listing.
// ICON_NAMES), the raw URL as is plus the knowledge that it IS a raster, so a
// host can clip it to a rounded square where an svg is drawn untouched.
//
// Every surface that shows an app icon — the sidebar's Projects row, the app
// page's header mark, the /apps card, the tab favicon — draws the file through
// an `<img>` or a `<link rel="icon">`, where the shell's CSS cannot reach. A
// picked lucide glyph (IconPicker.glyphIconSvg) therefore names its colour
// instead of baking it (`data-fused-color`, icon-color.ts), and this hook
// fetches the file once, swaps `currentColor` for the theme's hex, and hands
// back a data: URL. Files without the marker — emoji glyphs, hand-authored
// icons — come back as the raw URL they always were: drawn as is, the owner's
// standing rule (2026-08-27).
//
// Until the fetch lands (and for anything without a marker) the answer is the
// raw URL itself, never null: null would flash the AppStar fallback, and the
// raw file's own `prefers-color-scheme` fallback already paints the right
// colour whenever the app follows the OS, so the swap is usually invisible.
import { useEffect, useState } from "react";

import { readIconColor, svgDataUrl, themeIconSvg } from "@platform/lib/icon-color";
import { useResolvedTheme, type Theme } from "@platform/lib/theme";

// One fetch per URL: the URL carries the file's mtime as a cache key
// (appIconUrl / current-apps-lib.iconUrlFor), so a changed file is a new key
// and an unchanged one is answered from here across every row and remount.
// `null` = fetched, no marker (or unreadable): use the raw URL.
const texts = new Map<string, Promise<string | null>>();

/** True when the icon URL (api.appIconUrl / current-apps-lib.iconUrlFor: a
 *  `/api/fs/raw?path=<file>&v=<mtime>` address, or the LAN page's copy of the
 *  same) names an `icon.png` — the raster fallback the shell FITS to a rounded
 *  square (object-fit cover + radius), unlike an svg, which owns its own
 *  plate and is drawn as is (owner, 2026-08-27). Decided off the FILE name in
 *  the query, never off a data: URL, so pass the raw url, not `iconSrc`. */
export function isRasterIconUrl(url: string | null): boolean {
  if (!url || url.startsWith("data:")) return false;
  const q = url.indexOf("?");
  if (q < 0) return /\.png$/i.test(url);
  const path = new URLSearchParams(url.slice(q + 1)).get("path");
  return /\.png$/i.test(path ?? "");
}

function iconText(url: string): Promise<string | null> {
  // A png carries no colour marker to read — and reading its bytes as text
  // is a wasted round-trip on every row and remount.
  if (isRasterIconUrl(url)) return Promise.resolve(null);
  let p = texts.get(url);
  if (!p) {
    p = fetch(url)
      .then((r) => (r.ok ? r.text() : null))
      .then((svg) => (svg && readIconColor(svg) ? svg : null))
      .catch(() => null);
    texts.set(url, p);
  }
  return p;
}

/** The recoloured src for `url` under `theme` — a data: URL for a marked svg,
 *  the raw URL otherwise. Pure once the text is known. */
export function themedIconSrc(url: string, svg: string | null, theme: Theme): string {
  return svg ? svgDataUrl(themeIconSvg(svg, theme)) : url;
}

/** `url` is the raw icon URL (or null for an app without an icon, passed
 *  through). The result re-resolves when the theme changes. */
export function useThemedIconSrc(url: string | null): string | null {
  const theme = useResolvedTheme();
  const [svg, setSvg] = useState<{ url: string; text: string | null } | null>(null);
  useEffect(() => {
    if (!url) return;
    let live = true;
    iconText(url).then((text) => {
      if (live) setSvg({ url, text });
    });
    return () => {
      live = false;
    };
  }, [url]);
  if (!url) return null;
  // A stale answer (the URL changed under us) must not recolour the NEW file
  // with the OLD file's text — fall back to the raw URL until its own lands.
  const text = svg && svg.url === url ? svg.text : null;
  return themedIconSrc(url, text, theme);
}
