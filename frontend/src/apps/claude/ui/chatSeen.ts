// THE TASK CHAT'S "SEEN" MARK — the bots thread's unread bookkeeping
// (apps/bots/lib/unread.ts) for a task chat.
//
//   seen      session id → WHERE the reader last sat at the end of it, with the
//             chat on screen. Server-held (ui_layout.json `chatSeen`), for the
//             same reason bots moved `seen_seq` off localStorage (D1386): the
//             desktop app's port walk empties storage.
//   newMark   the seen mark as of OPENING the session: the NEW rule sits above
//             the first reply past it, and stays put until another session opens.
//   base      the turn index past which a reply counts toward the
//             "N new messages" pill (in memory, per open).
//
// A POSITION, NOT A TRANSCRIPT UUID. Bots have a `seq` on every event; a task
// chat has nothing that is the same live and restored — a live turn carries no
// transcript uuid at all (only a history read stamps one, and a long-open chat
// may never re-read). What IS the same both ways is how many messages you sent.
// So the mark is `k:d:r`:
//   k  how many of your messages the log held,
//   d  1 when the run had settled (the reply to message k was finished), else 0,
//   r  how many replies sat after message k.
//
// Pure functions over plain data, plus the two calls into the layout document.
import type { Turn } from "../protocol/controller-api";
import { isInterruptMark } from "../protocol/wire";
import { CHAT_SEEN_MAX, getUiLayout, patchUiLayout, uiLayoutReady } from "@platform/lib/ui-layout";

/** A reply the reader is told about: the assistant's words. Your own lines,
 *  notes, compact dividers and interrupt marks are never "new" — and nor is an
 *  error row, which the live log retires when the next reply lands while a
 *  history read may still return it, so counting it would shift the mark. */
export const isNews = (t: Turn): boolean => t.role === "assistant";

/** One of YOUR messages: a user turn that was sent (not a "not sent" bubble)
 *  and is not the "Interrupted by you" status line. */
export const isMine = (t: Turn): boolean =>
  t.role === "user" && !(t as { pending?: unknown }).pending && !isInterruptMark(t.text);

/** The mark for the log as it stands now (`k:d:r`, header). */
export function seenMarkOf(turns: readonly Turn[], settled: boolean): string {
  let k = 0;
  let r = 0;
  for (const t of turns) {
    if (isMine(t)) {
      k++;
      r = 0;
    } else if (isNews(t)) r++;
  }
  return `${k}:${settled ? 1 : 0}:${r}`;
}

function parseMark(mark: string | null | undefined): { k: number; d: boolean; r: number } | null {
  const m = /^(\d+):([01]):(\d+)$/.exec(mark ?? "");
  return m ? { k: Number(m[1]), d: m[2] === "1", r: Number(m[3]) } : null;
}

/**
 * Where the NEW rule goes on open (`newMarkFor` + `firstNewIndex` in bots):
 * the first reply the reader had not seen. -1 when the session was never seen
 * here (nothing to catch up on), the mark does not fit this log, or nothing is
 * past it.
 *   * the reply to a LATER message of yours is new;
 *   * left mid-reply (`d` 0): that reply finished while you were away, so it is
 *     new — the same reply, not the one after it;
 *   * settled, and nothing of yours since: a reply that arrived on its own (a
 *     scheduled run, a background job's report) is new past the `r` you saw.
 */
export function firstNewIndex(turns: readonly Turn[], mark: string | null | undefined): number {
  const p = parseMark(mark);
  if (!p) return -1;
  let mine = 0;
  let after = -1; // the index of your k-th message (-1 = before the first)
  if (p.k > 0) {
    after = -2;
    for (let i = 0; i < turns.length; i++) {
      if (isMine(turns[i]) && ++mine === p.k) {
        after = i;
        break;
      }
    }
    if (after === -2) return -1; // a log with fewer of your messages: not this mark's
  }
  let r = 0;
  for (let i = after + 1; i < turns.length; i++) {
    const t = turns[i];
    if (isMine(t)) {
      // Your next message: whatever answers it is new.
      for (let j = i + 1; j < turns.length; j++) if (isNews(turns[j])) return j;
      return -1;
    }
    if (!isNews(t)) continue;
    r++;
    // Left mid-reply: the reply you were watching is the news.
    if (!p.d && r >= Math.max(1, p.r)) return i;
    if (p.d && r > p.r) return i;
  }
  return -1;
}

/**
 * Where a catch-up lands for the reply at `first` (Akshil 2026-11-21: "new
 * should be above my message, not above my response"): the message of yours
 * it answers, so the question is read before the answer. Steps back over
 * notes and the like; a reply in between (one that came on its own after a
 * reply you had seen) keeps it on the reply. -1 for -1.
 */
export function newRuleIndex(turns: readonly Turn[], first: number): number {
  if (first < 0) return -1;
  for (let j = first - 1; j >= 0; j--) {
    if (isMine(turns[j])) return j;
    if (isNews(turns[j])) break;
  }
  return first;
}

/** The index of the `ord`-th reply (1-based), or -1. */
export function newsIndex(turns: readonly Turn[], ord: number): number {
  let n = 0;
  for (let i = 0; i < turns.length; i++) if (isNews(turns[i]) && ++n === ord) return i;
  return -1;
}

/** How many replies the log holds before index `end` (all of them by default).
 *  The pill counts in REPLIES, not turn indices: a history re-read at run end
 *  swaps the live turns for the transcript's (dropping live-only notes, adding
 *  history-only rows) with no new conversation, and an index taken before it
 *  points somewhere else after. */
export function newsBefore(turns: readonly Turn[], end: number = turns.length): number {
  let n = 0;
  for (let i = 0; i < end && i < turns.length; i++) if (isNews(turns[i])) n++;
  return n;
}

/** The stored mark for a session, or null. */
export function getChatSeen(sessionId: string | null | undefined): string | null {
  if (!sessionId) return null;
  return getUiLayout().chatSeen?.[sessionId] ?? null;
}

/** Record the mark. A no-op when unchanged (this runs on every commit at the
 *  tail) and before the layout document has hydrated (a write then would
 *  replay over a document this page has not read). The entry moves to the end,
 *  so the cap drops the sessions seen longest ago. */
export function setChatSeen(sessionId: string | null | undefined, uuid: string | null | undefined): void {
  if (!sessionId || !uuid || !uiLayoutReady) return;
  if (getUiLayout().chatSeen?.[sessionId] === uuid) return;
  patchUiLayout((d) => {
    const next: Record<string, string> = {};
    for (const [k, v] of Object.entries(d.chatSeen ?? {})) if (k !== sessionId) next[k] = v;
    next[sessionId] = uuid;
    const keys = Object.keys(next);
    const keep = keys.length > CHAT_SEEN_MAX ? keys.slice(-CHAT_SEEN_MAX) : keys;
    return { ...d, chatSeen: Object.fromEntries(keep.map((k) => [k, next[k]])) };
  });
}

/** How close to the end counts as at the end (bots threadDom `END_GAP`): the
 *  button shows past it, and sitting inside it marks the conversation seen. */
export const END_GAP_PX = 80;
