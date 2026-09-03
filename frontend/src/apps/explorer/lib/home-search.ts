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
import type { IndexRankResult, RankReason } from "@platform/lib/api";
import { fuzzyMatch } from "@platform/lib/fuzzy";

// Rows rendered at most. Far smaller than the listing's SEARCH_RESULT_CAP, and
// the number is set by what has to stay VISIBLE rather than by how many hits are
// interesting: at 40 "Search with AI" — the LAST row of this list, and the one
// action a user who isn't finding their file needs — sat two screens down,
// reachable only by scrolling past results that had already failed them.
// Twenty rows go below the fold on their own, which is why the AI row is now a
// STICKY footer (`.fh-ai-row`, preferences.css — `position: sticky; bottom: 0`
// over a scrolling `.fh-results`) instead of a row that scrolls away with the
// rest of the list: the row is always reachable without scrolling, which is
// the guarantee this constant originally existed to protect, at four times the
// cap it was first sized for. Ranking still runs over the whole corpus (the
// note below owns up to what is not shown); past twenty rows the useful move
// is a better query or the AI row, not more scrolling.
export const HOME_RESULT_CAP = 20;

// Below this many characters, a query is not sent at all. One or two letters
// match almost every file in a home tree — a substring-pass candidate cap gets
// hit on "a" or "e" alone — so the round trip is pure cost: it burns the
// escalation ladder's expensive half on a query that could never narrow
// anything, and it pre-arms "Search with AI" (a paid model call) on a query
// nobody meant to submit yet. Gated on the REQUEST, not on `active`
// (`FilesHome.tsx`'s `q !== ""`): `active` is what gives the search panel the
// page body, and flipping it on the first character would bounce the whole
// page as the user types their second one.
export const MIN_QUERY_CHARS = 2;

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
  /**
   * WHY, when `covered` is false — carried through verbatim from the
   * server's `reason` (platform/lib/api.ts's `RankReason`). FilesHome reads
   * this only to say "indexing is off" instead of the generic "still
   * building" when `reason === "disabled"`; every other value renders the
   * same not-covered message it always has.
   */
  reason: RankReason;
  /**
   * Wall-clock cost of the request this answer came from, `Date.now()` at
   * issue to `Date.now()` when the response was applied — the true
   * end-to-end latency the user felt, not just server time. A memoised
   * answer (the backspace path, `QueryMemo`) keeps the value it was
   * measured with: it is a real measurement of a real request, and
   * re-timing a cache hit would report ~0ms for a query that actually cost
   * a full round trip moments earlier.
   */
  elapsedMs: number;
}

/** A ranked response as an answer: absolutized, capped, and highlighted. */
export function answerFrom(
  res: IndexRankResult,
  query: string,
  home: string,
  elapsedMs: number,
): HomeAnswer {
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
    reason: res.reason,
    elapsedMs,
  };
}

/**
 * The held answer's hits, re-filtered against a NEWER query with no round
 * trip.
 *
 * The common case while a request is in flight is the new query EXTENDING the
 * old one ("read" -> "readm"): re-running `fuzzyMatch` (the same matcher
 * rank.py mirrors) over the hits already in hand and keeping only the ones
 * that still match — with `positions` recomputed for the new query — narrows
 * the list on screen with no round trip and no blank frame, which is strictly
 * better than dimming rows that cannot possibly be answers to what is now
 * typed.
 *
 * Deliberately does NOT re-rank or add rows: it can only ever REMOVE hits from
 * the held answer, which is what makes the result a provable SUBSET of the
 * true answer for `q` — it can never show something the fresh answer
 * wouldn't. A query that is not an extension of the old one (a paste, a
 * select-all retype) narrows to whichever held hits happen to still
 * fuzzy-match `q` directly, which is usually few or none; that emptiness is
 * exactly the signal the staleness deadline (`STALE_CLEAR_MS`,
 * platform/lib/instant-search) uses to decide there is nothing worth holding
 * onto.
 */
export function narrowAnswer(answer: HomeAnswer, q: string): HomeHit[] {
  const out: HomeHit[] = [];
  for (const hit of answer.hits) {
    const m = fuzzyMatch(q, hit.rel);
    if (!m) continue;
    out.push({ ...hit, positions: m.positions });
  }
  return out;
}

/**
 * The filesystem path a query is really an address for, or null.
 *
 * A pasted or typed `/…`, `~/…` or `C:\…` is an exact address, and searching
 * for it would be answering a question nobody asked. The caller still has to
 * `statPath` it: a path that does not exist falls back to being a search.
 *
 * Real pastes are not as clean as a typed shortcut, so the shape test runs
 * only after stripping, in order: surrounding whitespace/newlines (a paste
 * from a terminal or a chat window often carries one), matching wrapping
 * quotes (`"~/Downloads"`), a `file://` scheme, and a shell backslash-escape
 * before a space (`My\ File` -> `My File`) — that last one applies regardless
 * of platform, unlike the drive-letter de-backslashing below: a `\` followed
 * by a space is overwhelmingly a shell escape, never a real two-character
 * POSIX filename fragment, so unescaping it does not touch the POSIX
 * backslash-is-a-legal-char rule the drive-letter branch exists for.
 */
export function pathShortcut(query: string, home: string): string | null {
  let q = query.trim().replace(/[\r\n]+/g, " ").trim();
  const quoted = q.match(/^(['"])([\s\S]*)\1$/);
  if (quoted) q = quoted[2].trim();
  if (/^file:\/\//i.test(q)) {
    q = q.slice("file://".length);
    // A Windows file:// URI's third slash is the URI's (empty) authority
    // separator, not part of the path — file:///C:/Users/x is "C:/Users/x".
    // Left in, it makes the string start with "/C:/…", which then PASSES the
    // shape guard below via its leading-slash (POSIX) alternative instead of
    // failing the drive-letter one, so a bogus absolute path is returned with
    // full confidence instead of falling back to search. A bare POSIX
    // file:// URL has no such extra slash: file:///home/x really is
    // "/home/x", and is left untouched.
    if (/^\/[A-Za-z]:[\\/]/.test(q)) q = q.slice(1);
  }
  q = q.replace(/\\ /g, " ");
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

/**
 * `elapsedMs` as a short latency readout next to the count note: `"42 ms"`
 * under a second (rounded — the readout is a feel, not a profiler), one
 * decimal place in seconds at or above it (`"1.2 s"`, never `"1234 ms"`).
 */
export function formatElapsed(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/**
 * Which answer the result note should read from: the live one once ranking
 * has settled for the current query, otherwise whatever the LAST settled
 * answer was — never a value recomputed from the narrowed hits in between.
 *
 * This is the fix for the note rewriting itself 2-3 times per keystroke: the
 * caller renders `held`'s total/truncated/elapsedMs verbatim rather than
 * reaching for `answer.total` (the previous query's number, wrong the
 * instant `q` changes) or `hits.length` (a shrinking lower bound that hits
 * zero the moment narrowing empties the held hits, which used to force a
 * "Searching…" flash). Staleness is still communicated — the rows dim while
 * behind, and the `slow`-gated "· Searching…" suffix covers the in-flight
 * case — so the note itself is free to just hold still.
 */
export function noteAnswer(
  answer: HomeAnswer | null,
  settled: boolean,
  held: HomeAnswer | null,
): HomeAnswer | null {
  // `settled` is true on a failed request even when `answer` is null (a
  // later query's request failed while an earlier, unrelated query's held
  // answer got cleared by the stale-clear effect — see FilesHome.tsx). A
  // null `answer` here is never itself something to show; falling back to
  // `held` is what keeps that render reporting the last real result instead
  // of flashing "Searching…" over one it already showed.
  return settled && answer !== null ? answer : held;
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
// The list used to be a fixed shape — `fileCount` file rows followed by
// exactly ONE action row — which let the AI row's index be plain arithmetic
// (`fileCount`). Section 7 adds a second, EARLIER action row (an "Open" row
// for a resolving path address), which arithmetic cannot express: `fileCount`
// alone no longer says where anything is once a row can also come BEFORE the
// files. `RowModel` replaces the arithmetic with a small descriptor every
// other row-model function derives from, so ↑/↓ is still a single wrap-around
// step over a heterogeneous list, however many of its three parts are present.

/** The shape of the rendered list: at most one open row, then files, then at
 * most one AI row — any of the three may be absent. */
export interface RowModel {
  /** A resolving path address is offered as row 0, ahead of any file rows. */
  openRow: boolean;
  /** File hits, in rendered order — between the open row (if any) and the AI
   * row (if any). */
  fileCount: number;
  /** "Search with AI" as the LAST row. */
  aiRow: boolean;
}

function totalRows(m: RowModel): number {
  return (m.openRow ? 1 : 0) + m.fileCount + (m.aiRow ? 1 : 0);
}

/** Whether row `index` is the leading "Open" row. */
export function isOpenRow(index: number, m: RowModel): boolean {
  return m.openRow && index === 0;
}

/** Whether row `index` is the AI action row rather than a file. */
export function isAiRow(index: number, m: RowModel): boolean {
  return m.aiRow && index === totalRows(m) - 1;
}

/**
 * Move the highlight by one row, wrapping, entering the list from either end.
 *
 * Null on a genuinely empty model (no open row, no files, no AI row — reachable
 * when a path-shaped query does not resolve: ranking runs and comes back with
 * zero hits, but the AI row stays suppressed because the query is still
 * shaped like a path). There is no row 0 to land the arrow key on; returning
 * 0 here used to hand `activeRow` a position to clamp into -1 instead.
 */
export function stepHighlight(current: number | null, m: RowModel, delta: 1 | -1): number | null {
  const n = totalRows(m);
  if (n === 0) return null;
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
 * really is the only content left. Two conditions on that, and they are
 * different conditions:
 *
 *  * only while nothing is in flight. `pending` is checked FIRST, because a
 *    request that is still out may yet answer, and reading the previous
 *    failure as this query's verdict is how a single transient failure turned
 *    every later keystroke into an armed AI row.
 *  * only if the rows on screen are not answering some OTHER query. The list
 *    is deliberately never blanked, so a failure typically arrives over the
 *    previous query's hits — and "settled" would then license `submitRow`'s
 *    top-hit fallthrough to open one of them. Type "read", type "readme",
 *    have that request fail, press Enter: you get "read"'s best match. The
 *    pending check cannot see this one; nothing is in flight and the failure
 *    is real. With no rows at all the AI row is still armed, because that is
 *    the case the paragraph above is about.
 */
export function rankingSettled(
  answer: HomeAnswer | null,
  query: string,
  pending: boolean,
  failed: boolean,
): boolean {
  if (pending) return false;
  if (failed) return answer === null || answer.query === query;
  return answer !== null && answer.query === query;
}

/**
 * The row the highlight is ON — the explicit choice, clamped into the list —
 * and, with no explicit choice, the row Enter would commit. Those used to be
 * two different answers: this function pre-selected nothing over file hits,
 * while `submitRow` (below) still opened the top one on a bare Enter. The
 * user saw an unhighlighted list and pressed Enter anyway, because Enter is
 * the obvious gesture in a search box — and got a row they were never shown
 * as selected. One rule now: the row that visually pre-selects IS the row
 * Enter commits, always.
 *
 * With no highlight there are three defaults, checked in this order:
 *
 *  * an open row pre-selects UNCONDITIONALLY — unlike the AI row, resolving an
 *    address costs nothing to arm (it navigates, it does not call a model),
 *    and by the time `RowModel.openRow` is true the stat has already settled
 *    on "this address exists", so there is no in-flight ambiguity to gate on.
 *    It is also, by construction, the only content on screen: an open row
 *    implies zero file rows (the request is skipped entirely once an address
 *    resolves — see FilesHome).
 *  * failing that, with file hits on screen, the TOP hit pre-selects — gated
 *    on `settled` for the reason that gate exists everywhere else in this
 *    file: the list is deliberately never blanked, so rows for the PREVIOUS
 *    query are on screen while this one is in flight (or its answer failed),
 *    and "the top hit" then means the top hit for something the user has
 *    already finished typing over. Type "read", get ten rows, type "readme",
 *    have that request fail before Enter — `settled` is false, so there is no
 *    highlight AND Enter does nothing, rather than opening "read"'s best
 *    match. An explicit highlight still commits regardless — the user
 *    pointed at a row they can actually see.
 *  * failing THAT, the settled zero-hit case: the AI row is then the only
 *    content, so IT pre-selects. Gated on `settled` for the same reason —
 *    offering it while the scan is still running spends a model call on a
 *    query that was about to answer itself.
 */
export function activeRow(
  highlight: number | null,
  m: RowModel,
  settled: boolean,
): number | null {
  // A genuinely empty model — no open row, no files, no AI row — has no row
  // to pre-select AND no row an explicit `highlight` could have meant, so
  // this returns null unconditionally before consulting `highlight` at all.
  // Falling through to `Math.min(highlight, totalRows(m) - 1)` below used to
  // clamp any non-null highlight to -1 here (`totalRows(m) - 1` === -1),
  // which `activateRow` (FilesHome.tsx) then dereferenced as `hits[-1]`.
  if (totalRows(m) === 0) return null;
  if (highlight === null) {
    if (m.openRow) return 0;
    if (!settled) return null;
    if (m.fileCount > 0) return 0;
    return m.aiRow ? totalRows(m) - 1 : null;
  }
  return Math.min(highlight, totalRows(m) - 1);
}

/** The row Enter commits. Now just `activeRow` — see its doc comment — kept
 * as its own name because "what Enter commits" and "what is highlighted" are
 * different QUESTIONS even though they now always share one answer. */
export function submitRow(
  highlight: number | null,
  m: RowModel,
  settled: boolean,
): number | null {
  return activeRow(highlight, m, settled);
}
