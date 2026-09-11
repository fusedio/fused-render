// TYPES ONLY from `./ClaudeChat`. A VALUE re-export here would put the whole
// native chat — and its markdown stack — back into the static graph of every
// host that imports this barrel, which is exactly what `ChatMount`'s
// `React.lazy` boundary exists to keep out of the shell's entry chunk.
export type { ClaudeChatProps, ChatParamsSource, ClaudeAsk } from "./ClaudeChat";
export { ChatMount, type ChatMountProps } from "./ChatMount";
// Re-exported for convenience, but a host that wants ONLY the flag should
// import `@apps/claude/feature-flag` directly — this barrel pulls ChatMount.
export { useNativeChatEnabled, useNativeChatFlag } from "./feature-flag";
// The six legacy frame URLs, so a host builds `legacySrc` from the same place
// the parity test pins (legacy-src.ts).
export {
  canvasChatSrc,
  cardFrameSrc,
  contentModeSrc,
  listingPaneSrc,
  peekFrameSrc,
  sideFrameSrc,
} from "./legacy-src";
