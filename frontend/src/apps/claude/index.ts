// TYPES ONLY from `./ClaudeChat`. A VALUE re-export here would put the whole
// native chat — and its markdown stack — back into the static graph of every
// host that imports this barrel, which is exactly what `ChatMount`'s
// `React.lazy` boundary exists to keep out of the shell's entry chunk.
export type { ClaudeChatProps, ChatParamsSource, ClaudeAsk } from "./ClaudeChat";
export { ChatMount, type ChatMountProps } from "./ChatMount";
