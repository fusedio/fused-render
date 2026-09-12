// WHICH UNDRAINED FOLLOW-UPS THIS CHAT STILL HAS TO DRAW — the pure half of
// `PollResponse.inbox` (the project queue, prefs `queue.enabled`).
//
// THE SHAPE OF THE PROBLEM. A line typed into a chat whose turn is already
// running is not queued and is not scheduled: the live host absorbs it, the CLI
// holds it in its own stdin queue, and the model answers it when the current
// turn ends. For that window the message exists in two places — the CLI's queue,
// and this page's optimistic bubble — and only one of them survives a reload.
// The transcript cannot help: nothing has consumed the message, so it is in no
// JSONL row. So a reload (or the standing watch's `refreshHistory`, which
// replaces `turns` wholesale four times a minute) wiped the reader's own words
// off the screen while the CLI sat holding them.
//
// THE RUN HAS THE LIST, SO THE RUN REPORTS IT, and this module is the whole rule
// for reading it: which entries still need a bubble of their own, given what the
// page has already drawn.
//
// THREE WAYS AN ENTRY STOPS NEEDING ONE, and all three are somebody else drawing
// the same message:
//
//   * AN OPTIMISTIC BUBBLE IS ALREADY UP for it. This page posted one the moment
//     the reader pressed Enter (`run-controller`'s `queued` list), and that is
//     the same message — matched by id where the server echoes one back, and by
//     TEXT otherwise, because the optimistic row is minted before any id exists.
//   * THE TRANSCRIPT HAS GAINED ITS ROW. The model got to it: there is now a
//     real user turn saying exactly this, and a second copy under the log would
//     be the message appearing twice.
//   * IT LEFT THE INBOX. Nothing to decide — the list is the state, and an entry
//     that is no longer in it is no longer waiting.
//
// PURE, AND NOTHING BUT A FILTER. It takes what the poll said, what the
// controller is drawing and what the transcript holds, and answers with a
// subset of the first. No React, no fetches, no clock.
import type { InboxMessage } from "./types";

/** One drawn row: an inbox entry that nothing else on screen is saying. */
export interface InboxBubble {
  /** The entry's own id — the React key, stable across polls. */
  id: string;
  text: string;
}

/** The comparison both dedupes are made on. Whitespace only: a follow-up's wire
 *  form can gain a trailing newline on the way through stdin, and two bubbles
 *  differing by one is the bug this module exists to avoid. Deliberately NOT
 *  case- or punctuation-insensitive — two genuinely different messages that
 *  differ only in case are two messages. */
export function inboxKey(text: string): string {
  return String(text ?? "").trim();
}

/**
 * The rows to draw under the transcript, in the order the host took them.
 *
 * @param inbox      `PollResponse.inbox` — undrained follow-ups, newest last.
 * @param optimistic The texts this page is already drawing as its own bubbles
 *                   (`ChatState.queued`), which is what stops a message the
 *                   reader just typed appearing twice for one poll lap.
 * @param turnTexts  The user turns in the transcript. An entry the model has
 *                   answered has a row of its own now and gets no second one.
 */
export function inboxBubbles(
  inbox: readonly InboxMessage[] | null | undefined,
  optimistic: readonly string[] | null | undefined,
  turnTexts: readonly string[] | null | undefined,
): InboxBubble[] {
  const rows = Array.isArray(inbox) ? inbox : [];
  if (!rows.length) return [];
  const drawn = new Set<string>();
  for (const t of optimistic || []) drawn.add(inboxKey(t));
  for (const t of turnTexts || []) drawn.add(inboxKey(t));
  const seen = new Set<string>();
  const out: InboxBubble[] = [];
  for (const row of rows) {
    if (!row) continue;
    const text = String(row.text ?? "");
    const key = inboxKey(text);
    // A WORDLESS ENTRY IS NOT A BUBBLE. Pictures alone have no typed line, and
    // an empty bubble under the log says nothing a reader can read.
    if (!key) continue;
    // ONE ROW PER MESSAGE, even if the host lists the same words twice: the
    // second copy is the same fact said again.
    if (seen.has(key) || drawn.has(key)) continue;
    seen.add(key);
    out.push({ id: String(row.id || key), text });
  }
  return out;
}
