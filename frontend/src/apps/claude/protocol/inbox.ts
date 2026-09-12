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
//     the same message — matched by TEXT, because the optimistic row is minted
//     before any id exists and never gains one.
//   * THE TRANSCRIPT HAS GAINED ITS ROW. The model got to it: there is now a
//     real user turn saying exactly this, and a second copy under the log would
//     be the message appearing twice.
//   * IT LEFT THE INBOX. Nothing to decide — the list is the state, and an entry
//     that is no longer in it is no longer waiting.
//
// TEXT IS ONLY EVER MATCHED AGAINST THOSE TWO, AND ONE FOR ONE (🔴 review
// 2026-09-12). The inbox's own rows are deduped by ID, because the host's list is
// a list of MESSAGES and not a set of strings: a reader who sends "go on" twice
// while a turn runs has sent two follow-ups, both are held, and folding them into
// one bubble showed the second one going missing. And a text that IS already
// drawn retires exactly one row, not every row saying it — two "go on"s with one
// optimistic bubble up is one bubble drawn here, not none.
//
// A DRAINED ENTRY DRAWS THE SAME. The server lists follow-ups the host has taken
// but the transcript has not echoed (`InboxMessage.drained`); for the reader that
// is the same fact as an undrained one, and only the echo — a real user turn —
// retires the bubble.
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
  // HOW MANY COPIES OF EACH TEXT SOMEBODY ELSE IS ALREADY DRAWING — a count, not
  // a set, so one optimistic bubble retires one inbox row and the reader's second
  // identical follow-up keeps its own.
  const drawn = new Map<string, number>();
  const note = (t: string) => {
    const key = inboxKey(t);
    if (!key) return;
    drawn.set(key, (drawn.get(key) ?? 0) + 1);
  };
  for (const t of optimistic || []) note(t);
  for (const t of turnTexts || []) note(t);
  /** The entry ids already drawn — the ONLY dedupe inside the inbox itself. */
  const seen = new Set<string>();
  /** How many id-less rows have already been drawn for each text, for their
   *  fallback keys. */
  const unnamed = new Map<string, number>();
  const out: InboxBubble[] = [];
  for (const row of rows) {
    if (!row) continue;
    const text = String(row.text ?? "");
    const key = inboxKey(text);
    // A WORDLESS ENTRY IS NOT A BUBBLE. Pictures alone have no typed line, and
    // an empty bubble under the log says nothing a reader can read.
    if (!key) continue;
    // ONE ROW PER ENTRY. The host listing one entry twice in a payload is the
    // same fact said twice; two entries saying the same words are two messages.
    const id = String(row.id || "");
    if (id && seen.has(id)) continue;
    // …and a copy somebody else is drawing is spent here, once.
    const already = drawn.get(key) ?? 0;
    if (already > 0) {
      drawn.set(key, already - 1);
      continue;
    }
    if (id) seen.add(id);
    // WITH NO ID NAMED, the words are the key — and a second entry saying the
    // same words takes a suffix, because React keys have to be unique and these
    // two rows are two messages. Stable while the list's order is.
    const nth = (unnamed.get(key) ?? 0) + 1;
    unnamed.set(key, nth);
    out.push({ id: id || (nth === 1 ? key : `${key}#${nth}`), text });
  }
  return out;
}
