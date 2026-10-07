// The AI Playground card and its glyph vocabulary. Moved from Home.tsx unchanged.
import type { ReactNode } from "react";
import { PLAYGROUND_GROUPS, type PlaygroundGroup } from "@apps/ai_models/playground/groups";
import { tabHref } from "@apps/ai_models/routes";
import { softNavigate } from "./strip";

export { PLAYGROUND_GROUPS };

// The AI Playground strip's glyph vocabulary — plain strokes on the current
// color, so the tinted body well colours them for free. Keyed by the THING a
// task reads or writes rather than by the capability, because the card body
// draws each task as the pair it maps between (see PLAYGROUND_FLOWS).
const MEDIA_GLYPHS = {
  // Material's `message` — a squared bubble with a corner tail and three lines
  // of writing — and the SAME geometry the playground rail's Text generation
  // section wears (apps/ai_models/playground/capabilityIcons.tsx). These two
  // surfaces are one click apart and the card is a picture of where the click
  // lands, so a different bubble on each end reads as two different features.
  // It replaces a rounded balloon whose only marks were two short lines: at
  // the 18px this draws at, that read as a speech balloon — someone talking —
  // where every use of this glyph here is about WRITTEN text.
  chat: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 4h14a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H7l-4 4V6a2 2 0 0 1 2-2Z" />
      <path d="M7 7.5h10M7 10.5h10M7 13.5h6" />
    </svg>
  ),
  image: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="4" width="18" height="16" rx="2.5" />
      <circle cx="9" cy="10" r="2" />
      <path d="M3 17.5 8.5 13l4 3.5 3.5-3 5 4.5" />
    </svg>
  ),
  speech: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7" />
    </svg>
  ),
  voice: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 9.5h3l4-3.5v12l-4-3.5H4z" />
      <path d="M15 9.5a3.5 3.5 0 0 1 0 5" />
      <path d="M17.5 7a7 7 0 0 1 0 10" />
    </svg>
  ),
  meaning: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="10.5" cy="10.5" r="7" />
      <path d="M20.5 20.5 15.6 15.6" />
      <circle cx="8" cy="9" r="0.4" />
      <circle cx="12.8" cy="8.2" r="0.4" />
      <circle cx="10.2" cy="13" r="0.4" />
    </svg>
  ),
  video: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="5.5" width="13" height="13" rx="2.5" />
      <path d="M16 10.5 21 7.5v9L16 13.5z" />
    </svg>
  ),
  // A decision: one line forking into two, the taken branch marked. Same
  // figure as `capabilityIcons.tsx`'s text-classification glyph, redrawn at
  // this set's stroke so the card row stays one weight.
  decision: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 12h6" />
      <path d="M10 12c3 0 3-5 6-5h4" />
      <path d="M10 12c3 0 3 5 6 5h4" />
      <circle cx="20" cy="7" r="1.6" fill="currentColor" stroke="none" />
    </svg>
  ),
} satisfies Record<string, ReactNode>;

type PlaygroundMedia = keyof typeof MEDIA_GLYPHS;

// What each task takes in and hands back. The body renders it literally —
// in-glyph, arrow, out-glyph — so the card shows "speech becomes text" without
// leaning on the blurb, and chat → chat still reads as a mapping (rewriting)
// rather than a doubled icon.
const PLAYGROUND_FLOWS: Record<string, [PlaygroundMedia, PlaygroundMedia]> = {
  "text-generation": ["chat", "chat"],
  "text-to-image": ["chat", "image"],
  "text-to-video": ["chat", "video"],
  "automatic-speech-recognition": ["speech", "chat"],
  "text-to-speech": ["chat", "voice"],
  embeddings: ["chat", "meaning"],
  "text-classification": ["chat", "decision"],
};

// The header's single glyph names the task itself, which is not always the
// flow's output — Transcription is filed under the mic, not under text.
const PLAYGROUND_HEADS: Record<string, PlaygroundMedia> = {
  "text-generation": "chat",
  "text-to-image": "image",
  "text-to-video": "video",
  "automatic-speech-recognition": "speech",
  "text-to-speech": "voice",
  embeddings: "meaning",
  "text-classification": "decision",
};

const FLOW_ARROW = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M4 12h15M13.5 6.5 20 12l-6.5 5.5" />
  </svg>
);

// One card per playground task: the header names it, the body says what it
// does. Static on purpose — the strip advertises the SURFACE, not this
// machine's downloads, so it costs Home no catalog fetch. The link carries
// only the capability (`?cap=`); the playground itself resolves that to its
// vetted default model, so the choice lives in one place.
export function PlaygroundPreviewCard({ group }: { group: PlaygroundGroup }) {
  const href = tabHref("playground", `?cap=${encodeURIComponent(group.capability)}`);
  // A group added without a flow still renders (Home must not crash on a
  // vocabulary edit): it falls back to the header glyph on both sides.
  const head = PLAYGROUND_HEADS[group.capability] ?? "chat";
  const flow = PLAYGROUND_FLOWS[group.capability] ?? [head, head];
  return (
    <a className="fhb-card home-pg-card" href={href} onClick={(e) => softNavigate(e, href)}>
      <span className="fhb-card-head">
        <span className="fh-card-icon home-pg-icon" aria-hidden="true">
          {MEDIA_GLYPHS[head]}
        </span>
        <span className="fh-card-text">
          <span className="fh-card-name">{group.label}</span>
          <span className="fh-card-path">Runs on this machine</span>
        </span>
      </span>
      <span className="home-pg-body" aria-hidden="true">
        <span className="home-pg-flow">
          <span className="home-pg-glyph">{MEDIA_GLYPHS[flow[0]]}</span>
          <span className="home-pg-arrow">{FLOW_ARROW}</span>
          <span className="home-pg-glyph">{MEDIA_GLYPHS[flow[1]]}</span>
        </span>
        <span className="home-pg-blurb">{group.blurb}</span>
      </span>
    </a>
  );
}
