// The Playground's capability vocabulary, shared between the tab itself
// (AiModelsPlayground) and the Home page's "AI Playground" strip. Its own
// module rather than an export off AiModelsPlayground because Home is eager
// and AiModels is lazy (App.tsx): importing anything from the playground
// module would pull the whole playground chunk into the front-door bundle.
//
// The labels name the WORK — "Image generation", not "Images" — and the blurbs
// say it in one plain sentence, because this vocabulary exists for the reader
// with no AI vocabulary at all. The naked plural was ambiguous where it
// mattered most: a section called "Images" over a list of models reads as
// pictures it holds, and the two capabilities that generate something are the
// two that needed saying. "Transcription" and "Embeddings" are already the
// name of the work and stay as they are.
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
    label: "Text generation",
    blurb: "Ask questions, write and rewrite text.",
  },
  {
    capability: "text-to-image",
    label: "Image generation",
    blurb: "Turn a description into a picture.",
  },
  {
    capability: "automatic-speech-recognition",
    label: "Transcription",
    blurb: "Turn speech into written words.",
  },
  // Second to last: Apple Silicon only, with no fallback anywhere else — the
  // one card here that can be genuinely unusable on the machine looking at
  // it, which is a reason to let a narrow window drop it before the three
  // above, not a reason to hide it outright (the tab itself explains why).
  {
    capability: "text-to-video",
    label: "Video",
    blurb: "Turn a description into a short video with sound.",
  },
  // Last on purpose: Home renders `slice(0, shown)`, so the array's tail is
  // what a narrow window drops — and this is the card chosen to go first.
  {
    capability: "embeddings",
    label: "Embeddings",
    blurb: "Find text that matches by meaning, not wording.",
  },
];
