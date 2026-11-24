// `contractHome` / `useHome` live in platform now (`@platform/lib/home-path`):
// the chat's MarkdownView (apps/claude) links `~/…` paths with them too, and
// an app may not import another app. The explorer's callers keep this path.
export { contractHome, useHome, resetHome } from "@platform/lib/home-path";
