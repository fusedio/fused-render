// THE TASK CHAT'S TWO JUMP BUTTONS (Akshil 2026-11-21).
//
// One button that flipped between ↓ and ↑ read as a guess. Now each has one
// direction and one place:
//   * ↑ at the top of the log: back to the top of your last message, shown
//     once that top has scrolled off;
//   * ↓ above the composer, shown away from the end: to the top of your last
//     message while it is below you (reading older stuff), else to the end.
// Unread replies grow the button on THEIR side into the "N new" pill, and the
// pill goes to the NEW rule (above your message), not past it.
import { END_GAP_PX } from "./chatSeen";

/** Slack for sub-pixel layout and for the landing offset, in px. */
export const JUMP_SLACK = 16;
/** Where a jump lands: the target's top this far below the port's top. */
export const MSG_LAND_PX = 12;

type Port = { clientHeight: number; scrollHeight: number; scrollTop: number };

export const atEnd = (port: Port): boolean => port.scrollHeight - port.scrollTop - port.clientHeight <= END_GAP_PX;

/** ↑: your last message's top (relative to the port's visible top, null with
 *  no message of yours) has scrolled off above. */
export const upShown = (msgTop: number | null): boolean => msgTop !== null && msgTop < -JUMP_SLACK;

/** ↓: away from the end. */
export const downShown = (port: Port): boolean => !atEnd(port);

/** Where ↓ goes (Akshil 2026-11-21): to the top of your last message while it
 *  is below the view (reading older stuff), so the question is read before
 *  the answer; to the end otherwise. */
export const downToMsg = (msgTop: number | null, port: Port): boolean =>
  msgTop !== null && msgTop > port.clientHeight - JUMP_SLACK && !atEnd(port);

/** Which way a pill points for a target partly on screen: toward where the
 *  click lands (its top), so ↓ never scrolls up and ↑ never scrolls down. */
export const landingSide = (top: number): "above" | "below" => (top < 0 ? "above" : "below");

/** Which side of the view a box is on (top/bottom relative to the port's
 *  visible top, `height` the port's): its pill goes on that side. */
export function sideOf(top: number, bottom: number, height: number): "above" | "below" | "on" {
  if (bottom <= JUMP_SLACK) return "above";
  if (top >= height - JUMP_SLACK) return "below";
  return "on";
}
