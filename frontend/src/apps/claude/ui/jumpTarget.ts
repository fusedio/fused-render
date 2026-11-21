// WHICH WAY THE TASK CHAT'S JUMP BUTTON POINTS (Akshil 2026-11-21).
//
// The bots button only ever goes to the end. A task chat is read around YOUR
// last message — the question, then the long answer under it — so the button
// turns toward it:
//   * reading older stuff (your message is below the fold): ↓ to your message;
//   * your message's top on screen, not at the end:          ↓ to the end;
//   * its top scrolled off (in the reply under it, or at the end): ↑ back to it.
// So the end is two clicks from anywhere above, and the message you asked is
// one click from anywhere. With no message of yours it is the bots button.
import { END_GAP_PX } from "./chatSeen";

export type JumpMode = "none" | "down-msg" | "down-end" | "up-msg";

/** Slack for sub-pixel layout and for the landing offset, in px. */
export const JUMP_SLACK = 16;
/** Where a jump to your message lands: its top this far below the port's top. */
export const MSG_LAND_PX = 12;

/**
 * `msgTop` is your last message's top relative to the port's visible top
 * (negative = above it), or null when the log has no message of yours.
 */
export function jumpMode(
  msgTop: number | null,
  port: { clientHeight: number; scrollHeight: number; scrollTop: number },
): JumpMode {
  const atEnd = port.scrollHeight - port.scrollTop - port.clientHeight <= END_GAP_PX;
  if (msgTop === null) return atEnd ? "none" : "down-end";
  // Below the fold — unless the port cannot scroll any further toward it.
  if (msgTop > port.clientHeight - JUMP_SLACK) return atEnd ? "none" : "down-msg";
  if (msgTop < -JUMP_SLACK) return "up-msg";
  return atEnd ? "none" : "down-end";
}

/** Where the click goes: the pill (unread replies below you) always goes to
 *  the end; otherwise the mode decides. */
export function jumpTo(mode: JumpMode, newCount: number): "msg" | "end" {
  if (newCount > 0) return "end";
  return mode === "down-msg" || mode === "up-msg" ? "msg" : "end";
}
