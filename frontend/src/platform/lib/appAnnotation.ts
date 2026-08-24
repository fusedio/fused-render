// The shape of a /apps composer annotation chip (`@LFM2.5`, Cursor-file-tag
// style): a model reference the Playground hands the composer via `?annot=`.
// Lives in platform because it crosses the apps/ai_models -> apps/builder
// boundary — an app may only import platform + itself.
export interface AppAnnotation {
  id: string;
  name: string;
  detail: string;
}
