// "While you were away" — the one read behind the session-recap fold.
//
// Claude Code generates its recap ON BLUR, so it is ready the instant the
// terminal comes back (`tengu_return_to_session`, `hadRecap`). It can afford
// that: it forks its own live, cache-safe params, so the call is a cache hit it
// has already paid for. We cannot — the server runs a fresh one-shot over a
// plain-text tail of the transcript — so this fires on RETURN instead, and a
// reader who steps away and never comes back costs nothing at all
// (.claude-design/session-recap.md, "Data model (ours)").
//
// `file` and `session_id` are the SAME two values `fetchHistory` sends: the
// server re-reads the transcript this chat is showing, so anything else would
// summarise a different conversation.
//
// AND IT NEVER FAILS OUT LOUD. `text: ""` is the server saying "nothing to
// show" — skipped, timed out, the model refused — and it is a first-class
// answer rather than an error, because a chat that greets a returning reader
// with a red row about a summary they did not ask for is strictly worse than a
// chat that greets them with nothing.
import { getJson } from "@platform/lib/api";
import { isInterruptMark } from "./wire";

/** The server's answer. `text: ""` means "nothing to show" — never an error. */
export interface RecapResponse {
  text: string;
  /** Echoed back, so a late answer can be matched to the turn it was asked
   *  about rather than pinned under whatever is on screen when it lands. */
  for_uuid: string;
  /** ISO-8601, when it was generated. */
  at: string;
}

/** What the hook holds: the server's answer, keyed to the transcript position
 *  it describes. */
export interface Recap {
  text: string;
  forUuid: string;
}

/** The read. Rejects on transport/HTTP failure (the caller counts those and
 *  gives up after two); resolves with `text: ""` on the server's own "nothing
 *  to show", which is not a failure and must not be counted as one. */
export async function fetchRecap(
  file: string,
  sessionId: string,
  forUuid: string,
  signal?: AbortSignal,
): Promise<RecapResponse> {
  const q = new URLSearchParams({ file, session_id: sessionId, for_uuid: forUuid });
  const opts = signal ? { signal } : undefined;
  return await getJson<RecapResponse>(`/api/claude-sessions/recap?${q}`, opts);
}

/** One transcript row, as much of it as an anchor decision needs. */
export interface AnchorableTurn {
  role: string;
  key: string;
  uuid?: string;
  text?: string;
}

/**
 * DOES THE TRANSCRIPT DRAW THIS TURN WITH A `data-msg` TO SCROLL TO?
 *
 * The one definition of that question, read by both halves of it: `ui/Turn.tsx`
 * branches on `isInterruptMark` BEFORE the user bubble and stamps `data-msg`
 * only on the bubble, so a user record carrying the CLI's interrupt marker is
 * drawn as a centred note with no anchor attribute at all — and a user turn with
 * no `uuid` (a live send the history refresh has not yet given an id to) has
 * nothing to stamp.
 *
 * This is the fix for the fold's dead click: `recapAnchor` used to hand back the
 * last user turn's uuid whatever it was, and the commonest way to leave a chat
 * open and walk away is to interrupt the reply first — so the anchor was an
 * interrupt row's uuid, which matches nothing in the log.
 */
export function isAnchorableTurn(turn: AnchorableTurn | undefined): boolean {
  return !!turn && turn.role === "user" && !!turn.uuid && !isInterruptMark(turn.text);
}

/**
 * WHERE THE TRANSCRIPT STANDS, as one uuid.
 *
 * `for_uuid` wants to name the last ASSISTANT turn — that is the reply a recap
 * describes — and it cannot: `HistoryAssistantTurn` has no `uuid` at all
 * (protocol/types.ts; agent.py's `_history` puts one on user rows only). So the
 * last USER turn's uuid stands in, which is the fallback the design calls for
 * and is the same fact for our purposes: a new user turn is a new position, and
 * a reply lands against exactly one of them.
 *
 * A TURN WITH NO UUID IS `null`, NOT ITS KEY, AND NOT THE TURN BEFORE IT. The
 * key (`u:<sendSeq>`) would be a perfectly good client-side cache key, but it is
 * not what the endpoint takes: `for_uuid` is REQUIRED there and blank is a 400,
 * so a live send that the history refresh has not yet given an id to is simply
 * not a position we can ask about. It becomes one on the next refresh, which is
 * long before a minute of absence has passed. Walking PAST it to an older turn
 * would be worse than `null`: the anchor is also what auto-dismisses the fold
 * ("a new user turn is a new position"), and an anchor that a fresh send does
 * not change is a recap left sitting under the message that outdated it.
 *
 * AN INTERRUPT ROW IS SKIPPED, not returned and not `null`. It is a real
 * transcript record with a real uuid — so an older anchor behind it still moves
 * when the reader sends again — but the log draws it as a note with no
 * `data-msg`, so its uuid is not somewhere the fold can carry anyone
 * (`isAnchorableTurn`).
 *
 * `null` also for an empty transcript, or one whose turns are all the agent's.
 */
export function recapAnchor(turns: ReadonlyArray<AnchorableTurn>): string | null {
  for (let i = turns.length - 1; i >= 0; i--) {
    const t = turns[i];
    if (!t || t.role !== "user") continue;
    // The interrupt marker is not a position the log can be scrolled to, so it
    // is not the end of the search either.
    if (isInterruptMark(t.text)) continue;
    if (isAnchorableTurn(t)) return t.uuid ?? null;
    // The last user turn, with nothing to ask the endpoint about yet: a live
    // send whose transcript record has not come back. Not the turn before it.
    return null;
  }
  return null;
}
