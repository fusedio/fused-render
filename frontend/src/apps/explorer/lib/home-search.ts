// The explorer home page's instant search: what a keystroke is worth before
// anyone asks a model anything.
//
// The home page used to be an AI-first composer — every query, including
// "invoice", spent a round trip through haiku to learn that "invoice" is a
// filename. The file index already holds the whole home tree, and the in-folder
// search already knows how to rank a corpus of it, so typing here now ranks
// locally and paints as you type; AI search is one row at the bottom of the
// results (see FilesHome), taken only when the user asks for it.
//
// The SERVER now filters and ranks (/api/index/rank, fused_render/index/
// rank.py). The page used to fetch the whole corpus — 19.8 MB and 164k rows on
// the first keystroke, capped at 200k entries so ~71% of a 571k-file home
// could not be found at all — and rank it here. It now asks per query and gets
// a few KB, over the WHOLE index.
//
// The ranking is still not reimplemented anywhere: rank.py is a port of
// listing/search.ts pinned by a cross-language fixture, because two search
// boxes in the same app that order results differently is a bug the user
// experiences as "it found it last time".
//
// Two things must not be papered over:
//
//  * a cold index. The home page has no live-walk fallback (that is the
//    listing's job, one folder at a time), so an uncovered root is reported as
//    "still building", never as "no matches" — blaming the user's files for
//    the app's state is exactly the failure the server's search refuses to
//    make.
//  * the WAIT. Ranking used to be local, so results repainted within a frame
//    and never blanked. A round trip per query can only feel as good if it
//    never blanks the list, never flashes a spinner, fires the first keystroke
//    without a debounce, and answers a backspace from memory — which is what
//    the pieces below are for.
//
// The pieces that make a per-query round trip feel instant — the leading-edge
// debounce, the pending threshold, the backspace memo — are NOT here: they are
// shared with the listing's in-folder box, which is now the same kind of box,
// and they live in platform/lib/instant-search.
import type { IndexRankResult } from "@platform/lib/api";
import { fuzzyMatch } from "@platform/lib/fuzzy";

// Rows rendered at most. Far smaller than the listing's SEARCH_RESULT_CAP, and
// the number is set by what has to stay VISIBLE rather than by how many hits are
// interesting: "Search with AI" is the LAST row of this list, so any cap that
// overflows the viewport hides the one action a user who isn't finding their file
// needs — at 40 it sat two screens down, reachable only by scrolling past the
// results that had already failed them. Ten rows plus the AI row fit a laptop
// screen. Ranking still runs over the whole corpus (the note below owns up to
// what is not shown); past ten rows the useful move is a better query or the AI
// row, not more scrolling.
export const HOME_RESULT_CAP = 10;

// Hits asked of the server per query. The list renders HOME_RESULT_CAP of
// them; the rest are what makes the count note ("Showing top 10 of 137") true
// without a second request. 200 rows is a few KB.
export const RANK_FETCH_LIMIT = 200;

// One rendered result row. `path` is absolute (the index answers with rels
// relative to the searched root), which is what navigation and the icon
// helpers want. `positions` are indices into `rel` — computed here by
// re-running fuzzyMatch, NOT sent by the server, so platform/lib/fuzzy.ts
// stays the single source of truth for what highlights.
export interface HomeHit {
  path: string;
  rel: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
  positions?: number[];
}

/**
 * One answered query: the rows on screen, and what they are an answer TO.
 *
 * Carrying the query is what lets the box never blank. A new query in flight
 * leaves the previous answer rendered (dimmed, because `answer.query !== q`
 * says out loud that these are the old rows) instead of dropping to an empty
 * frame and back — going results → nothing → results is the single most
 * visible way a per-query round trip can feel worse than ranking locally, and
 * the local version never had an empty frame.
 */
export interface HomeAnswer {
  /** The (trimmed) query these hits answer. */
  query: string;
  hits: HomeHit[];
  /** More matched than were returned; the count note owns up to it. */
  truncated: boolean;
  /** Hits the server ranked for this query, capped at RANK_FETCH_LIMIT. */
  total: number;
  /**
   * The index has covered the home root. False is "still building", NOT "no
   * matches": the home page has no live walk to fall back on, so a miss here
   * is a statement about the app, not about the user's files.
   */
  covered: boolean;
}

/** A ranked response as an answer: absolutized, capped, and highlighted. */
export function answerFrom(res: IndexRankResult, query: string, home: string): HomeAnswer {
  return {
    query,
    hits: res.hits.slice(0, HOME_RESULT_CAP).map((h) => ({
      path: home + "/" + h.rel,
      rel: h.rel,
      is_dir: h.is_dir,
      size: h.size,
      mtime: h.mtime,
      // Re-matched HERE rather than sent: fuzzy.ts decides what highlights,
      // full stop, and the server's ranker is a port of it (index/rank.py),
      // so this reproduces the alignment that produced the score.
      positions: fuzzyMatch(query, h.rel)?.positions ?? [],
    })),
    truncated: res.truncated,
    total: res.total,
    covered: res.covered,
  };
}

/**
 * The filesystem path a query is really an address for, or null.
 *
 * A pasted or typed `/…`, `~/…` or `C:\…` is an exact address, and searching
 * for it would be answering a question nobody asked. The caller still has to
 * `statPath` it: a path that does not exist falls back to being a search.
 */
export function pathShortcut(query: string, home: string): string | null {
  const q = query.trim();
  if (!/^(\/|~\/|~$|[A-Za-z]:[\\/])/.test(q)) return null;
  let fsPath = q === "~" || q.startsWith("~/") ? home + q.slice(1) : q;
  // Backslashes are only separators in drive-letter paths (same rule as the
  // shell's path codec) — on POSIX "\" is a legal filename char.
  if (/^[A-Za-z]:[\\/]/.test(fsPath)) fsPath = fsPath.replace(/\\/g, "/");
  // Strip a trailing slash but keep roots whole: "/" stays "/", and a drive
  // root keeps its slash (bare "C:" reads as cwd-relative).
  fsPath = fsPath.replace(/\/+$/, "") || "/";
  if (/^[A-Za-z]:$/.test(fsPath)) fsPath += "/";
  return fsPath;
}

/**
 * The highlight positions for a rendered cell, given positions into the rel.
 *
 * The rows render the rel twice — as a bare name and as a `~/`-prefixed path —
 * and `highlightSegments` wants indices into the string it is given, so the
 * rel's positions have to be rebased into each. Out-of-range positions are
 * dropped rather than clamped: a match on the parent directory has nothing to
 * mark in the name cell, and marking the wrong character is worse than marking
 * none.
 */
export function positionsWithin(positions: number[], from: number, length: number): number[] {
  const out: number[] = [];
  for (const p of positions) {
    if (p >= from && p < from + length) out.push(p - from);
  }
  return out;
}

/** Where the entry's own name starts inside its rel. */
export function nameStart(rel: string): number {
  return rel.lastIndexOf("/") + 1;
}

/**
 * The match count, phrased like the listing's (listing/result-cap) so the two
 * searches sound like one app.
 *
 * `corpusTruncated` is the index's own entry cap — a separate, pre-existing
 * "there was more than this" that the number carries as a `+`. Both truncations
 * can be true at once, and the count stays TRUE either way: reporting the
 * capped number would be a lie about the disk.
 */
export function homeCountNote(total: number, corpusTruncated: boolean): string {
  const suffix = corpusTruncated ? "+" : "";
  const n = total.toLocaleString();
  if (total <= HOME_RESULT_CAP) return `${n}${suffix} match${total === 1 ? "" : "es"}`;
  return `Showing top ${HOME_RESULT_CAP} of ${n}${suffix}`;
}

// -- typing anywhere is typing here ------------------------------------------

/** The parts of a keydown that decide whether the search box should claim it. */
export interface KeyIntent {
  key: string;
  ctrlKey: boolean;
  altKey: boolean;
  metaKey: boolean;
  /** `tagName` of the event target, uppercase as the DOM reports it. */
  tagName: string | undefined;
  isContentEditable: boolean;
  /** The target IS the search box — it already has the caret. */
  isSearchInput: boolean;
}

/**
 * Whether a keystroke aimed at the page should be redirected into the box.
 *
 * This page's whole purpose is to be typed into, so a printable key that no
 * other field wants belongs in the search bar — nobody should have to click a
 * search box on a search page. The exclusions are what keep that from being
 * theft:
 *
 *  * a ctrl/alt/meta chord is a COMMAND, not typing, and swallowing focus from
 *    one would break every app shortcut on the page. Shift is not in that list:
 *    a capital letter is typing.
 *  * another input/textarea/select/contenteditable owns its own keystrokes.
 *  * the box already having the caret has to be a no-op, not a redundant
 *    focus(): re-focusing collapses the selection to the end, which would make
 *    editing the middle of a query impossible.
 *
 * `key.length === 1` is the printable test that doesn't enumerate alphabets (it
 * admits every letter, digit, and symbol in any script while rejecting the named
 * keys — "Enter", "Tab", "ArrowUp", "F5"). Backspace is admitted on top of it so
 * a correction reaches the box rather than the browser's back gesture.
 */
export function redirectsToSearch(e: KeyIntent): boolean {
  if (e.ctrlKey || e.altKey || e.metaKey) return false;
  if (e.key.length !== 1 && e.key !== "Backspace") return false;
  if (e.isSearchInput) return false;
  if (e.isContentEditable) return false;
  return e.tagName !== "INPUT" && e.tagName !== "TEXTAREA" && e.tagName !== "SELECT";
}

// -- the row model the keyboard walks ----------------------------------------
//
// The rendered list is `fileCount` file rows followed by exactly ONE action row
// (Search with AI), so the AI row's index is always `fileCount`. Keeping that
// as arithmetic rather than a flag is what makes ↑/↓ a single wrap-around step
// over a heterogeneous list.

/** Whether row `index` is the AI action row rather than a file. */
export function isAiRow(index: number, fileCount: number): boolean {
  return index >= fileCount;
}

/** Move the highlight by one row, wrapping, entering the list from either end. */
export function stepHighlight(
  current: number | null,
  fileCount: number,
  delta: 1 | -1,
): number {
  const n = fileCount + 1;
  if (current === null) return delta === 1 ? 0 : n - 1;
  return (current + delta + n) % n;
}

/**
 * Whether the instant results are a FINISHED answer for the CURRENT query.
 *
 * `hits.length === 0` alone cannot tell "not answered yet" from "nothing
 * matches", and the difference is a paid model call: while the request is in
 * flight (or the previous query's rows are still on screen) a query with
 * plenty of instant matches shows none of them, and pre-arming the AI row
 * there spends a call on a query that was about to answer itself.
 *
 * A failed request IS settled — no answer is coming for it, so the AI row
 * really is the only content left. But only while nothing is in flight:
 * `pending` is checked FIRST, because a request that is still out may yet
 * answer, and reading the previous failure as this query's verdict is how a
 * single transient failure turned every later keystroke into an armed AI row.
 */
export function rankingSettled(
  answer: HomeAnswer | null,
  query: string,
  pending: boolean,
  failed: boolean,
): boolean {
  if (pending) return false;
  if (failed) return true;
  return answer !== null && answer.query === query;
}

/**
 * The row the highlight is ON: the explicit choice, clamped into the list.
 *
 * With no highlight there is one default, and it is the settled zero-hit case:
 * the AI row is then the only content on screen, so it is pre-selected. That
 * pre-selection is gated on `settled` because it ARMS Enter — offering it while
 * the scan is still running spends a model call on a query that was about to
 * answer itself. With file hits showing there is no highlight until the user
 * picks one; `submitRow` is what Enter consults.
 */
export function activeRow(
  highlight: number | null,
  fileCount: number,
  settled: boolean,
): number | null {
  if (highlight === null) return fileCount === 0 && settled ? 0 : null;
  return Math.min(highlight, fileCount);
}

/**
 * The row Enter commits, which is not always the highlighted one.
 *
 * With hits on screen and no arrow-key choice, Enter opens the TOP hit. It used
 * to resolve to null and do nothing at all — a silent no-op, in the one box in
 * this app where Enter is the obvious gesture. It still never falls through to
 * the AI row that way: reaching a paid action takes either zero settled hits or
 * an explicit highlight.
 *
 * That fallthrough is gated on `settled` for the same reason the AI row is, and
 * the reason arrived with server-side ranking: the list is deliberately never
 * blanked, so rows for the PREVIOUS query are on screen while this one is in
 * flight, and "the top hit" then means the top hit for something the user has
 * already finished typing over. Typing "read", then "readme", then Enter
 * navigated to "read"'s best match. An explicit highlight still commits —
 * the user pointed at a row they can actually see.
 */
export function submitRow(
  highlight: number | null,
  fileCount: number,
  settled: boolean,
): number | null {
  const row = activeRow(highlight, fileCount, settled);
  if (row !== null) return row;
  return settled && fileCount > 0 ? 0 : null;
}
