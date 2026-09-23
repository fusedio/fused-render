// The Playground's capability vocabulary, shared between the tab itself
// (AiModelsPlayground) and the Home page's "AI Playground" strip. Its own
// module rather than an export off AiModelsPlayground because Home is eager
// and AiModels is lazy (App.tsx): importing anything from the playground
// module would pull the whole playground chunk into the front-door bundle.
//
// Item 11 extension (fix round 3): `label` used to be a second, hand-picked
// name for each capability ("Transcription", "Video") that disagreed with
// the standard name every other AI Models tab now shows for the same
// capability ("Speech to text", "Video generation" — `engines.ts`'s
// `capabilityLabel`, D-standard-capability-names). One capability, one name:
// `label` is derived from `capabilityLabel` below, not hand-written, so this
// list can no longer drift from Playground/Benchmark/Engines/Local's own
// headings. The blurbs stay hand-written — they're a sentence explaining the
// work, not a name for it.
// A capability missing here still renders on the tab (capabilityLabel
// fallback); it just gets no Home card, which is deliberate: the Home strip
// only advertises tasks the playground actually has a UI for.
import { capabilityLabel } from "@apps/ai_models/lib/engines";

export type PlaygroundGroup = {
  capability: string;
  label: string;
  blurb: string;
};

// The HEAD of this list tracks `CAPABILITY_ORDER` (lib/aiModelGroups.ts) by
// hand, not by import: that list is capability strings only, and these carry
// the label and blurb Home's cards are made of. A strip that leads with a
// different capability than the tab it opens is the front door disagreeing
// with the room, so the leading entries are edited together — text generation
// leads (D807), matching the AI Models two-pane mockup's reading order.
//
// The TAIL deliberately does not match `CAPABILITY_ORDER`: only here does the
// order decide what a narrow window DROPS (Home renders `slice(0, shown)`), so
// the last three are arranged by how much a first visit loses without them,
// not by that list's grouping (see the three notes below).
export const PLAYGROUND_GROUPS: PlaygroundGroup[] = [
  {
    capability: "text-generation",
    label: capabilityLabel("text-generation"),
    blurb: "Ask questions, write and rewrite text.",
  },
  {
    capability: "text-to-image",
    label: capabilityLabel("text-to-image"),
    blurb: "Turn a description into a picture.",
  },
  {
    capability: "automatic-speech-recognition",
    label: capabilityLabel("automatic-speech-recognition"),
    blurb: "Turn speech into written words.",
  },
  // Third from last: Apple Silicon only, with no fallback anywhere else — the
  // one card here that can be genuinely unusable on the machine looking at
  // it, which is a reason to let a narrow window drop it before the three
  // above, not a reason to hide it outright (the tab itself explains why).
  {
    capability: "text-to-video",
    label: capabilityLabel("text-to-video"),
    blurb: "Turn a description into a short video with sound.",
  },
  // Second to last: a building block more than a destination.
  {
    capability: "embeddings",
    label: capabilityLabel("embeddings"),
    blurb: "Find text that matches by meaning, not wording.",
  },
  // Last on purpose — the first card a narrow window drops. Decisions is the
  // least familiar task here (the onboarding Models step skips it for the
  // same reason, `modelPicks.ts`), and it is Apple Silicon only like video.
  {
    capability: "text-classification",
    label: capabilityLabel("text-classification"),
    blurb: "Ask typed questions about a text and get probabilities back.",
  },
];
