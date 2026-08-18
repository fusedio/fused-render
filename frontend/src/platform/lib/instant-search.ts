// What makes a search box that asks the SERVER per query feel like one that
// ranked locally.
//
// Both of the app's search boxes now work that way — the home page's
// (FilesHome/lib/home-search) and the listing's in-folder one
// (listing/useWalkSearch) — and neither can afford to feel hesitant: the
// version each replaced held a corpus in the browser and repainted within a
// frame. A round trip per keystroke is only an improvement if it never reads
// as one, so the same three pieces are used in both places, and they live here
// rather than in either box because two boxes in one app that answer at
// different speeds is something users experience as "it was faster last time".
//
// The rules, which are the reason this file exists at all:
//
//   * the FIRST keystroke after a pause fires immediately (`searchDelay`
//     returns 0 on the leading edge). The debounce coalesces a burst; it does
//     not delay the first request, which answers in ~40-50 ms.
//   * a pending indicator waits `PENDING_INDICATOR_MS`, so the common fast
//     answer never flashes one.
//   * a backspace is answered from memory (`QueryMemo`), because deleting a
//     character walks back through queries that were answered seconds ago.

// How long a burst of keystrokes coalesces into one request. A TRAILING window
// only — see `searchDelay`.
export const INSTANT_DEBOUNCE_MS = 120;

// How long a request may run before the box admits to being busy. Under this,
// the answer arrives before a spinner would have been readable, and painting
// one is a flicker that reads as slower than doing nothing.
export const PENDING_INDICATOR_MS = 200;

// Queries whose answers are remembered for the session. Small on purpose —
// this is a typing trail, not a cache with a coherence story (the index moving
// clears it wholesale).
export const QUERY_MEMO_LIMIT = 20;

/**
 * Milliseconds to wait before issuing the request for a freshly typed query.
 *
 * ZERO on the leading edge, and that is the point. The debounce exists to
 * coalesce fast typing, not to delay the first request: a selective query
 * answers in ~40 ms, and parking that behind a timer is exactly how a box that
 * does less total work ends up feeling more hesitant. So the first keystroke
 * after any pause fires now, and only a burst waits — for the REMAINDER of the
 * window, so a fast typist's requests land one debounce apart rather than one
 * per letter.
 */
export function searchDelay(
  now: number,
  lastIssuedAt: number,
  debounceMs: number = INSTANT_DEBOUNCE_MS,
): number {
  const since = now - lastIssuedAt;
  return since >= debounceMs ? 0 : debounceMs - since;
}

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
