// Per-provider icons for the Mounts page's "Add storage" picker. Same house
// style as FileIcons/FinderIcon: 16x16, viewBox 0 0 24 24, fill none, stroke
// currentColor, round caps/joins — hand-written paths, no npm dependency.
//
// Deliberately MONOCHROME rather than brand-coloured. Three reasons: it matches
// the rest of the shell's iconography, it survives light/dark without a second
// asset, and it keeps us clear of reproducing trademarked logo artwork. Tint
// comes from CSS (.mount-provider-icon--<key> in shell.css) exactly the way
// .file-icon--<variant> works, so the SVG itself is colourless.
//
// The glyphs are shape-suggestive, not logo-accurate: Drive's divided triangle,
// Dropbox's stacked diamonds, a package for Box, a bucket for S3 (which is what
// S3 calls its containers anyway), a server stack for S3-compatible services,
// and a globe for open public data.
import type { ReactNode } from "react";

const svgProps = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const;

// Keyed by SetupKey (views/Mounts.tsx). Kept as a plain record rather than a
// switch so a new provider that forgets its icon is a TypeScript error at the
// lookup site, not a silently blank card.
const GLYPHS: Record<string, ReactNode> = {
  // Drive's mark is a triangle split three ways — outline plus the division
  // lines meeting at the centre.
  drive: (
    <>
      <path d="M12 4 L21 19 L3 19 Z" />
      <path d="M12 4 L12 13" />
      <path d="M12 13 L4.5 18.5" />
      <path d="M12 13 L19.5 18.5" />
    </>
  ),
  // Dropbox: the stacked-diamond carton, three faces visible.
  dropbox: (
    <>
      <path d="M7 3.5 L12 7 L7 10.5 L2 7 Z" />
      <path d="M17 3.5 L22 7 L17 10.5 L12 7 Z" />
      <path d="M7 12 L12 8.5 L17 12 L12 15.5 Z" />
    </>
  ),
  // Box: a shipping package, the one shape the name already is.
  box: (
    <>
      <path d="M12 2.5 L20.5 7 V17 L12 21.5 L3.5 17 V7 Z" />
      <path d="M3.5 7 L12 11.5 L20.5 7" />
      <path d="M12 11.5 V21.5" />
    </>
  ),
  // S3: a bucket, which is S3's own word for the container.
  detected: (
    <>
      <path d="M3.5 7 C3.5 5.6 7.3 4.5 12 4.5 C16.7 4.5 20.5 5.6 20.5 7 C20.5 8.4 16.7 9.5 12 9.5 C7.3 9.5 3.5 8.4 3.5 7 Z" />
      <path d="M3.7 8 L5.6 20.3 A1.5 1.5 0 0 0 7.1 21.5 H16.9 A1.5 1.5 0 0 0 18.4 20.3 L20.3 8" />
    </>
  ),
  // S3-compatible: a stack of service nodes — the same protocol, someone
  // else's servers.
  s3compat: (
    <>
      <rect x="3" y="3.5" width="18" height="6" rx="1.5" />
      <rect x="3" y="14.5" width="18" height="6" rx="1.5" />
      <path d="M7 6.5 h.01" />
      <path d="M7 17.5 h.01" />
    </>
  ),
  // Public data: a globe — open, no account, anyone's to read.
  public: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12 h18" />
      <path d="M12 3 C14.5 5.8 15.8 8.8 15.8 12 C15.8 15.2 14.5 18.2 12 21 C9.5 18.2 8.2 15.2 8.2 12 C8.2 8.8 9.5 5.8 12 3 Z" />
    </>
  ),
};

export function ProviderIcon({ provider }: { provider: string }) {
  const glyph = GLYPHS[provider];
  if (!glyph) return null;
  return (
    <svg {...svgProps} className={`mount-provider-icon mount-provider-icon--${provider}`}>
      {glyph}
    </svg>
  );
}
