// Capability copy for the Local tab's two-pane layout.
//
// `plain` used to carry its own friendly register, distinct from
// `engines.ts`'s `CAPABILITY_LABELS` ("Text generation") — the mockup's own
// `CAPS` array copied verbatim ("Chat & writing" etc). Fix round 3 item 11
// retires that: the user asked for the standard capability names ("the
// standard capability names like text generation etc") everywhere a
// capability is named — nav, pane heading, the "← Back to …" link, the
// Task filter's own menu value (`hubSearchView.ts`'s `activeTask`), and the
// drawer's "Why we suggest it" sentence, which all read `plain` (directly or
// via `CapabilityPane`'s lower-cased `paneLabel`) rather than
// `capabilityLabel` — so the one place to make them agree is this table.
// `plain` is now `capabilityLabel(key)` itself; blurb and searchNoun keep
// their own plain-language register unchanged, since neither was named in
// that ask.
//
// Keyed off `CAPABILITY_ORDER`'s five capability tags (see
// `aiModelGroups.ts`) rather than re-deriving a list, so a capability added
// server-side still renders (falling back to `capabilityLabel` and a blank
// blurb) instead of vanishing from the nav column.
//
// Fix round 3 item 13: `icon` used to be this file's own 24-viewBox
// string-SVG set, rendered via `dangerouslySetInnerHTML` — a second drawing
// of every glyph the Playground sidebar already had its own component for
// (`lib/capabilityIcons.tsx`). The Local nav and the Playground rail are one
// click apart; a reader who has just picked "Text generation" in one should
// see the same mark for it in the other. `icon` is now a `ReactNode`, filled
// with `capabilityIcon(<tag>)` from that shared module for the five
// capabilities it covers — the same component, not a lookalike copy.
import type { ReactNode } from "react";
import { capabilityIcon } from "@apps/ai_models/lib/capabilityIcons";
import { capabilityLabel } from "@apps/ai_models/lib/engines";

export interface CapabilityMeta {
  /** The plain-language name used in the nav column and pane head. */
  plain: string;
  /** One sentence under the pane head explaining what the capability is for. */
  blurb: string;
  /** What a Hub search inside this pane calls its results, e.g. "image models". */
  searchNoun: string;
  /** The nav/pane-head icon — the same component the Playground sidebar
   *  draws for this capability (`lib/capabilityIcons.tsx`), except for
   *  `PARTS_ICON` below, which has no Playground counterpart. */
  icon: ReactNode;
}

/** The "Engine files" bucket's icon — not a capability (no Playground
 *  counterpart to share), so it lives outside `CAPABILITY_META`/
 *  `lib/capabilityIcons.tsx` and is exported on its own for
 *  `CapabilityNav`/`EngineFilesPane`. Same grammar as the shared set
 *  (16 on a 0 0 24 24 viewBox, stroke-only, currentColor, strokeWidth 1.5). */
export const PARTS_ICON: ReactNode = (
  <svg width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <path d="M12 3 3 7.5 12 12l9-4.5z" />
    <path d="m3 12 9 4.5L21 12" />
    <path d="m3 16.5 9 4.5 9-4.5" />
  </svg>
);

const CAPABILITY_META: Record<string, CapabilityMeta> = {
  "text-generation": {
    plain: capabilityLabel("text-generation"),
    blurb: "Ask questions, draft text and write code — all on this Mac, nothing sent anywhere.",
    searchNoun: "text generation models",
    icon: capabilityIcon("text-generation"),
  },
  "text-to-image": {
    plain: capabilityLabel("text-to-image"),
    blurb: "Describe a picture and get one back. Takes a few seconds per image.",
    searchNoun: "image models",
    icon: capabilityIcon("text-to-image"),
  },
  "automatic-speech-recognition": {
    plain: capabilityLabel("automatic-speech-recognition"),
    blurb: "Turn a recording, a voice note or a meeting into text.",
    searchNoun: "transcription models",
    icon: capabilityIcon("automatic-speech-recognition"),
  },
  embeddings: {
    plain: capabilityLabel("embeddings"),
    blurb: "Find things by meaning rather than by exact words — across notes, documents or photos.",
    searchNoun: "embedding models",
    icon: capabilityIcon("embeddings"),
  },
  "text-to-video": {
    plain: capabilityLabel("text-to-video"),
    blurb: "Describe a shot and get a few seconds of video with sound. Large download, slow to render.",
    searchNoun: "video models",
    icon: capabilityIcon("text-to-video"),
  },
};

/** Plain-language metadata for a capability tag, falling back to the terse
 *  Hub label (and an empty blurb/noun) for a capability this file has not
 *  been taught about yet — see the module doc on why that is a fallback
 *  and not a thrown error. */
export function capabilityMeta(capability: string): CapabilityMeta {
  return (
    CAPABILITY_META[capability] ?? {
      plain: capabilityLabel(capability),
      blurb: "",
      searchNoun: "models",
      icon: capabilityIcon(capability),
    }
  );
}
