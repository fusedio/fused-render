// One glyph per playground capability, worn by the sidebar's section headers
// (D428). Its own file, not `groups.ts`: Home imports that module eagerly and
// JSX here would drag React markup into the front-door bundle — the exact
// pull groups.ts exists to avoid.
//
// Same grammar as platform/ui/MenuIcons: 16px on a 0 0 24 24 viewBox,
// stroke-only, currentColor, strokeWidth 1.5 — so the glyphs sit beside the
// app's other icons without a weight argument.
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

const ICONS: Record<string, ReactNode> = {
  // Chat: a speech bubble.
  "text-generation": (
    <svg {...base}>
      <path d="M21 12a8 8 0 0 1-8 8H5.5L3 22V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z" />
    </svg>
  ),
  // Images: a framed picture with sun and horizon.
  "text-to-image": (
    <svg {...base}>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <circle cx="9" cy="10" r="1.6" />
      <path d="m3.5 17 5-5 4 4 3-3 5 5" />
    </svg>
  ),
  // Transcription: a microphone.
  "automatic-speech-recognition": (
    <svg {...base}>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0" />
      <path d="M12 17.5V21" />
    </svg>
  ),
  // Search by meaning: a magnifier.
  embeddings: (
    <svg {...base}>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="m20 20-4.9-4.9" />
    </svg>
  ),
};

// A capability a future runner adds before this file learns it: a plain
// sparkle, never a blank slot — the tab's posture for unknown runners.
const FALLBACK: ReactNode = (
  <svg {...base}>
    <path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18" />
  </svg>
);

export function capabilityIcon(capability: string): ReactNode {
  return ICONS[capability] ?? FALLBACK;
}
