// An app's icon as a rounded-square tile — the Marketplace's cards, rows and
// detail hero, and (phase 2) the `.fused` install page. Generic on purpose:
// it takes a src and a name, never a catalog entry.
//
// No icon (most community apps ship none), a failed load, or an image still
// decoding: a TINTED TILE WITH INITIALS, its hue hashed from `seed` (the slug)
// so an app keeps its colour across every surface and reload. The hues are the
// `--app-icon-*` tokens, so both themes repaint them. The image fades in over
// the tile once decoded, so a slow icon never flashes an empty square.
//
// A picked-glyph `icon.svg` (data-fused-color) is recoloured for the live
// theme through the same hook every other app-icon surface uses.
import { useState, type CSSProperties } from "react";
import { useThemedIconSrc } from "@platform/lib/app-icon-src";

/** Number of tint classes (`.as-tint-0` … in marketplace.css). */
const TINTS = 10;

/** Stable small hash (FNV-1a, then murmur3's finalizer to spread the low
 *  bits — plain FNV mod 10 clustered similar slugs like `local-*` onto one
 *  hue). The tile's hue must not change between reloads, so no Math.random
 *  and no object identity. */
function hash(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  h ^= h >>> 16;
  h = Math.imul(h, 0x85ebca6b);
  h ^= h >>> 13;
  h = Math.imul(h, 0xc2b2ae35);
  h ^= h >>> 16;
  return h >>> 0;
}

/** "Local Chat" → "LC"; "overture-explorer" → "OE"; "py" → "PY"; one long
 *  word → its first two letters. */
export function initialsOf(name: string): string {
  const words = name
    .replace(/[_-]+/g, " ")
    .split(/\s+/)
    .map((w) => w.replace(/[^\p{L}\p{N}]/gu, ""))
    .filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

export function tintIndex(seed: string): number {
  return hash(seed) % TINTS;
}

export interface AppIconProps {
  /** Icon URL (svg or raster), or null/undefined for the initials tile. */
  src?: string | null;
  /** The app's display name — initials and the image's alt text. */
  name: string;
  /** What the tint is hashed from (default: `name`). Pass something stable
   *  like a slug or a path. */
  seed?: string;
  /** Edge length in px (default 44). The corner radius follows (~22.5%, the
   *  Apple squircle proportion) unless `radius` is given. */
  size?: number;
  radius?: number;
  /** Raised (soft drop shadow) — the detail hero's large icon. */
  raised?: boolean;
  className?: string;
}

export function AppIcon({ src, name, seed, size = 44, radius, raised, className }: AppIconProps) {
  const themed = useThemedIconSrc(src ?? null);
  const [loaded, setLoaded] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const showImg = !!themed && failed !== themed;
  const style: CSSProperties = {
    width: size,
    height: size,
    borderRadius: radius ?? Math.round(size * 0.225),
    fontSize: Math.max(10, Math.round(size * 0.36)),
  };
  return (
    <span
      className={
        "as-icon as-tint-" +
        tintIndex(seed ?? name) +
        (raised ? " is-raised" : "") +
        (showImg && loaded === themed ? " has-img" : "") +
        (className ? " " + className : "")
      }
      style={style}
      aria-hidden={showImg ? undefined : true}
    >
      <span className="as-icon-initials">{initialsOf(name)}</span>
      {showImg && (
        <img
          key={themed}
          src={themed}
          alt=""
          draggable={false}
          className={"as-icon-img" + (loaded === themed ? " is-loaded" : "")}
          onLoad={() => setLoaded(themed)}
          onError={() => setFailed(themed)}
        />
      )}
    </span>
  );
}

export default AppIcon;
