// The shape of a /apps composer annotation chip (`@LFM2.5`, Cursor-file-tag
// style): a model reference the Playground hands the composer via `?annot=`.
// Lives in platform because it crosses the apps/ai_models -> apps/builder
// boundary — an app may only import platform + itself.
export interface AppAnnotation {
  id: string;
  name: string;
  detail: string;
  // The model's Hub capability tag ("text-to-image", …) — what the composer
  // filters its starter chips by, so a chip row under an attached model offers
  // ideas that model can actually do. OPTIONAL because a `?annot=` link made by
  // an older build carries none, and the composer must still read that chip
  // rather than treat the URL as malformed; absent means "don't filter".
  capability?: string;
}
