// Plain-language capability copy for the Local tab's two-pane layout.
//
// `engines.ts`'s `CAPABILITY_LABELS` is the Hub's own vocabulary ("Text
// generation") — terse, and shared with the Engines tab where a runner list
// needs a header, not a pitch. This file is the opposite register: the
// capability column and its pane are the first thing a new user reads on
// this tab, so each entry gets a plain name ("Chat & writing"), a one-line
// blurb of what it is for, and the noun a Hub search inside that pane should
// call its results ("chat models"). Nothing here is inferred — every string
// is copied verbatim from the approved mockup's `CAPS` array so the shipped
// copy matches what was reviewed, not a paraphrase of it.
//
// Keyed off `CAPABILITY_ORDER`'s five capability tags (see
// `aiModelGroups.ts`) rather than re-deriving a list, so a capability added
// server-side still renders (falling back to `capabilityLabel` and a blank
// blurb) instead of vanishing from the nav column.
import { capabilityLabel } from "@apps/ai_models/lib/engines";

export interface CapabilityMeta {
  /** The plain-language name used in the nav column and pane head. */
  plain: string;
  /** One sentence under the pane head explaining what the capability is for. */
  blurb: string;
  /** What a Hub search inside this pane calls its results, e.g. "chat models". */
  searchNoun: string;
  /** Inline SVG markup (viewBox 0 0 24 24, stroke-based) for the nav icon. */
  icon: string;
}

const TEXT_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.2A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z"/></svg>';
const IMAGE_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.6"/><path d="m4 17 5-5 4 4 3-2 4 4"/></svg>';
const SPEECH_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M4 10v4M8 6v12M12 3v18M16 7v10M20 10v4"/></svg>';
const EMBED_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="7" r="2.4"/><circle cx="17.5" cy="6" r="2.4"/><circle cx="12" cy="17.5" r="2.4"/><path d="m7.9 8.6 2.6 6.8M15.9 8 13.5 15.6M8.4 6.6l6.7-.4"/></svg>';
const VIDEO_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="5" width="14" height="14" rx="2"/><path d="m16.5 10 5-3v10l-5-3z"/></svg>';

/** The "Engine files" bucket's icon — not a capability, so it lives outside
 *  `CAPABILITY_META` and is exported on its own for `CapabilityNav`/`CapabilityPane`. */
export const PARTS_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 3 7.5 12 12l9-4.5z"/><path d="m3 12 9 4.5L21 12"/><path d="m3 16.5 9 4.5 9-4.5"/></svg>';

const CAPABILITY_META: Record<string, CapabilityMeta> = {
  "text-generation": {
    plain: "Chat & writing",
    blurb: "Ask questions, draft text and write code — all on this Mac, nothing sent anywhere.",
    searchNoun: "chat models",
    icon: TEXT_ICON,
  },
  "text-to-image": {
    plain: "Make images",
    blurb: "Describe a picture and get one back. Takes a few seconds per image.",
    searchNoun: "image models",
    icon: IMAGE_ICON,
  },
  "automatic-speech-recognition": {
    plain: "Transcribe audio",
    blurb: "Turn a recording, a voice note or a meeting into text.",
    searchNoun: "transcription models",
    icon: SPEECH_ICON,
  },
  embeddings: {
    plain: "Search & similarity",
    blurb: "Find things by meaning rather than by exact words — across notes, documents or photos.",
    searchNoun: "embedding models",
    icon: EMBED_ICON,
  },
  "text-to-video": {
    plain: "Make video",
    blurb: "Describe a shot and get a few seconds of video with sound. Large download, slow to render.",
    searchNoun: "video models",
    icon: VIDEO_ICON,
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
      icon: TEXT_ICON,
    }
  );
}
