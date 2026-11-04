// The tags of the blocks a chat message carries on the wire. Their owner,
// `apps/claude/protocol/wire.ts`, re-exports them; they live here so the
// breakout-context formatter (platform cannot import the chat) can keep quoted
// text from opening or closing any of them.

/** T:4761 — the app-state block's tag. */
export const APP_STATE_TAG = "live-app-state";
/** T:4775 — the annotations block's tag. */
export const ANN_TAG = "annotations";
/** T:4790 — the attached-pictures block's tag. */
export const PANE_SHOT_TAG = "pane-shot";
/** The focused-terminal metadata line agent.py's `_with_terminal_hint` writes
 *  onto a send (D1326). Server-side only: the page never composes it, but the
 *  inbox rows and the CLI's echoed user records carry it back verbatim. */
export const TERMINAL_HINT_TAG = "terminal-hint";
/** The wire block a breakout's first message carries: the selected parent
 *  messages, quoted as background. `wire.ts` strips and parses it. */
export const BREAKOUT_CTX_TAG = "breakout-context";
