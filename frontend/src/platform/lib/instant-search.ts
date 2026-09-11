// What makes a search box that asks the SERVER per query feel like one that
// ranked locally.
//
// Both of the app's search boxes now work that way — the home page's
// (FilesHome/lib/home-search) and the listing's in-folder one
// (listing/useListingSearch) — and neither can afford to feel hesitant: the
// version each replaced held a corpus in the browser and repainted within a
// frame. A round trip per keystroke is only an improvement if it never reads
// as one, so the same three pieces are used in both places, and they live here
// rather than in either box because two boxes in one app that answer at
// different speeds is something users experience as "it was faster last time".
//
// The rules, which are the reason this file exists at all:
//
//   * every keystroke waits `INSTANT_DEBOUNCE_MS` — a normal TRAILING
//     debounce. This file used to fire the first keystroke after a pause
//     immediately (a leading-edge throttle keyed on when the last request was
//     ISSUED), which sounds responsive but is backwards: the first keystroke
//     after any gap is always the shortest query of the run, which is the
//     broadest and most expensive one to answer — so the request most worth
//     delaying was the one guaranteed to fire with zero delay.
//   * a pending indicator waits `PENDING_INDICATOR_MS`, so the common fast
//     answer never flashes one.
//   * a backspace is answered from memory (`QueryMemo`), because deleting a
//     character walks back through queries that were answered seconds ago.

// How long a burst of keystrokes coalesces into one request, and how long
// EVERY request — including the first after a pause — now waits before
// firing. A plain trailing debounce: each call site's effect already re-runs
// on every query change and its cleanup already clears the pending timer, so
// an unconditional wait of this many ms IS a correct debounce with no
// separate leading-edge case to track. 200, not a shorter value tuned to a
// single selective query's ~40-50ms round trip: with the leading edge gone,
// EVERY keystroke now pays this wait before its request even fires, so it is
// chosen for what a sustained typist feels while holding a key down, not for
// the fastest possible single answer. Bumped 120 -> 300 mid-implementation
// (D705), then brought back down to 200 by direct owner request (D705
// correction, 2026-09-07) — still well above the round trip, tuned lighter
// for a sustained typist than 300 was.
export const INSTANT_DEBOUNCE_MS = 200;

// How long a request may run before the box admits to being busy. Under this,
// the answer arrives before a spinner would have been readable, and painting
// one is a flicker that reads as slower than doing nothing.
export const PENDING_INDICATOR_MS = 200;

// Queries whose answers are remembered for the session. Small on purpose —
// this is a typing trail, not a cache with a coherence story (the index moving
// clears it wholesale).
export const QUERY_MEMO_LIMIT = 20;

// How long a request may run before the rows still on screen (an OLDER
// query's answer, since the list is never blanked) are dropped rather than
// held. Never-blank was written for a ~40 ms local round trip, where showing
// the previous answer for one more frame is free; a server round trip can run
// into seconds, and past this long the rows on screen stop being "a little
// behind" and start being flatly wrong — narrowing them locally (home-search's
// `narrowAnswer`) is the first line of defense, and this is what fires when
// narrowing leaves nothing: no rows and an honest "Searching…" beats rows for
// a query the user has visibly moved past. Longer than PENDING_INDICATOR_MS
// for the same reason a doctor's second opinion takes longer than the first
// glance: this is a decision to throw away information, not just to admit a
// wait is happening.
export const STALE_CLEAR_MS = 600;

/**
 * The last few `query -> answer` pairs, so backspacing is instant.
 *
 * Deleting a character walks back through queries that were answered seconds
 * ago; re-asking the server for those is a round trip the user can feel for
 * rows the page already had. Insertion-ordered and capped — the OLDEST entry
 * goes, which for a typing trail is the query least likely to be typed next.
 *
 * Deliberately not a coherence-managed cache: the index moving (a scan
 * finishing, the store being deleted, this app renaming a file) makes every
 * remembered answer suspect at once, and the caller clears the whole thing on
 * that signal rather than trying to reason per entry.
 */
export class QueryMemo<T> {
  private readonly entries = new Map<string, T>();

  constructor(private readonly limit: number = QUERY_MEMO_LIMIT) {}

  get(query: string): T | undefined {
    return this.entries.get(query);
  }

  put(query: string, answer: T): void {
    this.entries.delete(query);
    this.entries.set(query, answer);
    while (this.entries.size > this.limit) {
      const oldest = this.entries.keys().next();
      if (oldest.done) break;
      this.entries.delete(oldest.value);
    }
  }

  clear(): void {
    this.entries.clear();
  }

  get size(): number {
    return this.entries.size;
  }
}
