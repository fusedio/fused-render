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

/** WHERE A DRAFT ROW'S PRESS GOES — the New task card, from every view
 *  (Akshil, 2026-09-16), and where its "Back to chat" lands. Re-exported so the
 *  Tasks page presses the same rule the chat's own Recent list does: a draft row
 *  that opened two different places from two views would be two behaviours to
 *  learn. Leaf helpers, so this costs the shell chunk nothing but the
 *  functions. */
export { draftChatUrl, draftHref } from "./ui/list-rows";
