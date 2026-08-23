// The glyphs the stages' sample cards wear (D452). One shared set, NOT one
// drawing per sample: thirty-two samples across four stages would mean
// thirty-two hand-drawn paths to maintain for a decoration, and a small
// vocabulary reused across stages reads as a family rather than a zoo. The
// icon's job on a card is to make the row scannable and to hint at the subject
// — "an email", "a picture of a place", "money" — not to illustrate the prompt.
//
// Same grammar as capabilityIcons and platform/ui/MenuIcons: 16px on a
// 0 0 24 24 viewBox, stroke-only, currentColor, strokeWidth 1.5. A card's
// glyph therefore sits at the same weight as every other icon in the app.
import type { ReactNode } from "react";

const base = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

/** The vocabulary. Keys are subjects, not stages — `code` is worn by a text
 *  prompt about an error and by a transcribe script full of library names. */
export const StarterIcons: Record<string, ReactNode> = {
  bulb: (
    <svg {...base}>
      <path d="M12 3a6 6 0 0 1 3.5 10.9c-.6.4-1 1.1-1 1.9v.2h-5v-.2c0-.8-.4-1.5-1-1.9A6 6 0 0 1 12 3Z" />
      <path d="M10 19h4M11 21.5h2" />
    </svg>
  ),
  mail: (
    <svg {...base}>
      <rect x="3" y="5.5" width="18" height="13" rx="2" />
      <path d="m3.8 7 8.2 6 8.2-6" />
    </svg>
  ),
  bowl: (
    <svg {...base}>
      <path d="M3.5 11h17a8.5 8.5 0 0 1-17 0Z" />
      <path d="M9 8c0-1.2 1.4-1.6 1.4-3.2M14 8c0-1.2 1.4-1.6 1.4-3.2" />
    </svg>
  ),
  code: (
    <svg {...base}>
      <path d="m9 8-5 4 5 4M15 8l5 4-5 4" />
      <path d="m13.5 5.5-3 13" />
    </svg>
  ),
  list: (
    <svg {...base}>
      <path d="M8.5 6.5h11M8.5 12h11M8.5 17.5h11" />
      <path d="M4.5 6.5h.01M4.5 12h.01M4.5 17.5h.01" />
    </svg>
  ),
  plane: (
    <svg {...base}>
      <path d="M20.5 3.5 11 20l-2-7.5L1.5 10.5Z" />
      <path d="m20.5 3.5-11.5 9" />
    </svg>
  ),
  pen: (
    <svg {...base}>
      <path d="m4 20 1.2-4.2L16.5 4.5l3 3L8.2 18.8Z" />
      <path d="m14.5 6.5 3 3" />
    </svg>
  ),
  chart: (
    <svg {...base}>
      <path d="M4 3.5v17h16" />
      <path d="M8.5 20.5v-5M13 20.5v-9.5M17.5 20.5v-3.5" />
    </svg>
  ),
  landscape: (
    <svg {...base}>
      <path d="M3 19.5h18" />
      <path d="m4 19.5 6-8.5 4 5.5 2-2.5 4 5.5" />
      <circle cx="17" cy="6.5" r="2" />
    </svg>
  ),
  cube: (
    <svg {...base}>
      <path d="m12 3 8.5 4.8v9.4L12 21l-8.5-3.8V7.8Z" />
      <path d="m3.5 7.8 8.5 4.8 8.5-4.8M12 12.6V21" />
    </svg>
  ),
  robot: (
    <svg {...base}>
      <rect x="4.5" y="8.5" width="15" height="10.5" rx="2.5" />
      <path d="M12 8.5V5" />
      <circle cx="12" cy="3.8" r="1.2" />
      <path d="M9.5 13h.01M14.5 13h.01M10 16.5h4" />
    </svg>
  ),
  camera: (
    <svg {...base}>
      <path d="M4 8.5h3.2L8.7 6h6.6l1.5 2.5H20a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1Z" />
      <circle cx="12" cy="14" r="3.2" />
    </svg>
  ),
  map: (
    <svg {...base}>
      <path d="m3.5 6.5 5.5-2.5 6 2.5 5.5-2.5v13.5l-5.5 2.5-6-2.5-5.5 2.5Z" />
      <path d="M9 4v13.5M15 6.5V20" />
    </svg>
  ),
  sparkle: (
    <svg {...base}>
      <path d="M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9L12 17.5l-1.9-5.1L5 10.5l5.1-1.9Z" />
      <path d="M18.5 16.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8Z" />
    </svg>
  ),
  leaf: (
    <svg {...base}>
      <path d="M20.5 3.5C10 3.5 3.5 10 3.5 20.5c10.5 0 17-6.5 17-17Z" />
      <path d="M3.5 20.5 15 9" />
    </svg>
  ),
  heart: (
    <svg {...base}>
      <path d="M12 20.5S3.5 15.2 3.5 9.4A4.4 4.4 0 0 1 12 7.6a4.4 4.4 0 0 1 8.5 1.8c0 5.8-8.5 11.1-8.5 11.1Z" />
    </svg>
  ),
  music: (
    <svg {...base}>
      <path d="M9 17.5V5.5l10-2v12" />
      <circle cx="6.5" cy="18" r="2.5" />
      <circle cx="16.5" cy="16" r="2.5" />
    </svg>
  ),
  book: (
    <svg {...base}>
      <path d="M12 6c-2-1.6-4.7-2.2-8.5-2.2v13.6c3.8 0 6.5.6 8.5 2.2 2-1.6 4.7-2.2 8.5-2.2V3.8C16.7 3.8 14 4.4 12 6Z" />
      <path d="M12 6v13.6" />
    </svg>
  ),
  globe: (
    <svg {...base}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M3.5 12h17" />
      <path d="M12 3.5a13 13 0 0 1 0 17 13 13 0 0 1 0-17Z" />
    </svg>
  ),
};
