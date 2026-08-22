// The Playground's capability vocabulary, shared between the tab itself
// (AiModelsPlayground) and the Home page's "AI Playground" strip. Its own
// module rather than an export off AiModelsPlayground because Home is eager
// and AiModels is lazy (App.tsx): importing anything from the playground
// module would pull the whole playground chunk into the front-door bundle.
//
// The labels name what a person DOES, the blurbs say it in one plain sentence
// — this vocabulary exists for the reader with no AI vocabulary at all.
// A capability missing here still renders on the tab (capabilityLabel
// fallback); it just gets no Home card, which is deliberate: the Home strip
// only advertises tasks the playground actually has a UI for.
export type PlaygroundGroup = {
  capability: string;
  label: string;
  blurb: string;
};

export const PLAYGROUND_GROUPS: PlaygroundGroup[] = [
  {
    capability: "text-generation",
    label: "Chat",
    blurb: "Ask questions, write and rewrite text.",
  },
  {
    capability: "text-to-image",
    label: "Images",
    blurb: "Turn a description into a picture.",
  },
  {
    capability: "automatic-speech-recognition",
    label: "Transcription",
    blurb: "Turn speech into written words.",
  },
  // Last on purpose: Home renders `slice(0, shown)`, so the array's tail is
  // what a narrow window drops — and this is the card chosen to go first.
  {
    capability: "embeddings",
    label: "Search by meaning",
    blurb: "Find text that matches by meaning, not wording.",
  },
];
