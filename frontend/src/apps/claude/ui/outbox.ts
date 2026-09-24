// THE PAGE'S OUTBOX — the lines the reader typed while this page could not
// hand them to the run yet.
//
// Claude Code's own terminal never refuses Enter: a line typed while it works
// goes into a visible queue above the input, the box clears, and the queue
// drains in order. This page had no such thing — `dispatchSend` held a latch
// for the start round trip / the pane capture / the queue admission, and the
// composer's `submit` returned `false` for the whole window with no sign at all.
// The words stayed in the box, the reader kept typing, and the next Enter sent
// two messages glued together (multi-send QA, 2026-09-19: "2nd lost", "3rd
// swallowed").
//
// This module is the queue itself and nothing else: an ordered list with the
// four moves the page makes on it. Pure and React-free so the ordering rules
// are unit-tested without mounting a chat. Every function returns a NEW array
// (the page mirrors the list into state for rendering, and a mutated array is a
// paint React never sees).
//
// ORDER RULES, the whole contract:
//   * `pushBack`  — a line typed while busy waits its turn, behind the others.
//   * `pushFront` — a line handed BACK (a send that failed, an interrupt's
//                   `still_queued`) goes ahead of everything typed since: it was
//                   said first.
//   * `shiftOldest` — the drain takes from the front, one at a time.
//   * `popNewest`   — ↑ in an empty box pulls back the LAST thing typed, the
//                   way Claude Code's ↑ does (up again for an older one).

export interface OutboxEntry<T> {
  /** Stable id — the React key of the queued bubble and the address a
   *  hand-back names. */
  id: string;
  /** The words as typed. What goes back in the box on ↑. */
  text: string;
  /** Whatever the page needs to actually send it later (options, taken
   *  attachments, the optimistic bubble's key). Opaque here. */
  payload: T;
  /** A line that was handed BACK rather than typed: a failed send, an
   *  interrupted follow-up. Drawn as "not sent" so the reader knows the run
   *  never saw it, and never silently merged into what they are typing now. */
  notSent?: boolean;
}

export function pushBack<T>(list: readonly OutboxEntry<T>[], entry: OutboxEntry<T>): OutboxEntry<T>[] {
  return [...list, entry];
}

export function pushFront<T>(list: readonly OutboxEntry<T>[], entry: OutboxEntry<T>): OutboxEntry<T>[] {
  return [entry, ...list];
}

/** Several handed-back lines at once (a stop's `still_queued`), AHEAD of the
 *  list and in the order they were said — `pushFront` one by one would reverse
 *  them. */
export function pushFrontAll<T>(
  list: readonly OutboxEntry<T>[],
  entries: readonly OutboxEntry<T>[],
): OutboxEntry<T>[] {
  return [...entries, ...list];
}

/** The next line to send, and the list without it. `entry` is null on an
 *  empty outbox. */
export function shiftOldest<T>(
  list: readonly OutboxEntry<T>[],
): { entry: OutboxEntry<T> | null; rest: OutboxEntry<T>[] } {
  if (!list.length) return { entry: null, rest: [] };
  return { entry: list[0]!, rest: list.slice(1) };
}

/** The line to pull back into the box on ↑, and the list without it. */
export function popNewest<T>(
  list: readonly OutboxEntry<T>[],
): { entry: OutboxEntry<T> | null; rest: OutboxEntry<T>[] } {
  if (!list.length) return { entry: null, rest: [] };
  return { entry: list[list.length - 1]!, rest: list.slice(0, -1) };
}

/** The next line the DRAIN may send — the oldest one that is not "not sent" —
 *  and the list without it. A `notSent` line is the reader's to resend (the run
 *  may already have read those words before a stop), so the drain steps over
 *  it and leaves it in place, order kept. */
export function shiftOldestSendable<T>(
  list: readonly OutboxEntry<T>[],
): { entry: OutboxEntry<T> | null; rest: OutboxEntry<T>[] } {
  const i = list.findIndex((e) => !e.notSent);
  if (i < 0) return { entry: null, rest: [...list] };
  return { entry: list[i]!, rest: [...list.slice(0, i), ...list.slice(i + 1)] };
}

/** One entry by id — a click on its bubble — and the list without it. */
export function takeById<T>(
  list: readonly OutboxEntry<T>[],
  id: string,
): { entry: OutboxEntry<T> | null; rest: OutboxEntry<T>[] } {
  const i = list.findIndex((e) => e.id === id);
  if (i < 0) return { entry: null, rest: [...list] };
  return { entry: list[i]!, rest: [...list.slice(0, i), ...list.slice(i + 1)] };
}

/** The composer's one-line hint while lines wait — Claude Code's own copy
 *  ("Press up to edit queued messages"), shortened for a 24px line. */
export function outboxHint(count: number): string {
  if (count <= 0) return "";
  return count === 1
    ? "1 message waiting to send · ↑ to edit it"
    : `${count} messages waiting to send · ↑ to edit the last one`;
}
