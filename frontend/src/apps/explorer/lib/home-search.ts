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
// The RANKING is deliberately not reimplemented: scoring, the substring-beats-
// fuzzy invariant, hidden-entry intent and the comparator all live in
// listing/search.ts, and two search boxes in the same app that order results
// differently is a bug the user experiences as "it found it last time".
//
// The one thing that must not be papered over is a cold index. The home page
// has no live-walk fallback (that is the listing's job, one folder at a time),
// so an uncovered root is reported as "still building", never as "no matches" —
// blaming the user's files for the app's state is exactly the failure the
// server's search.py refuses to make.
import type { IndexSearchResult, WalkEntry } from "@platform/lib/api";
import { indexCorpusFrom } from "@apps/explorer/listing/index-corpus";
import type { SearchHit } from "@apps/explorer/listing/types";

// Rows rendered at most. Smaller than the listing's SEARCH_RESULT_CAP: this
// list sits under a hero on a launcher page, not in a scrollable file table,
// and past a screenful the useful move is a better query. Ranking still runs
// over the whole corpus — the note below owns up to what is not shown.
export const HOME_RESULT_CAP = 40;

// How still the query must be before the corpus is re-scanned. Short enough to
// read as "while typing", long enough that a burst of keystrokes costs one scan
// instead of one each. The corpus itself is fetched once and reused, so this
// debounces scoring, not the network.
export const INSTANT_DEBOUNCE_MS = 120;

// One rendered result row. `path` is absolute (the index's corpus is relative
// to the searched root), which is what navigation and the icon helpers want.
export interface HomeHit {
  path: string;
  rel: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
}

// The corpus behind the instant search, or why there isn't one.
export type CorpusState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ok"; entries: WalkEntry[]; truncated: boolean }
  // The index has not covered the home root yet — no answer, not zero answers.
  | { status: "cold" }
  | { status: "error"; message: string };

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

/** The rows to render, in rank order: the top of the ranking, absolutized. */
export function homeHitsFrom(hits: SearchHit[], home: string): HomeHit[] {
  return hits.slice(0, HOME_RESULT_CAP).map((h) => ({
    path: home + "/" + h.entry.rel,
    rel: h.entry.rel,
    is_dir: h.entry.is_dir,
    size: h.entry.size,
    mtime: h.entry.mtime,
  }));
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

/** An index response as a corpus, or `cold` when it cannot answer for the root. */
export function corpusFrom(res: IndexSearchResult): CorpusState {
  const corpus = indexCorpusFrom(res);
  if (corpus === null) return { status: "cold" };
  return { status: "ok", entries: corpus.entries, truncated: corpus.truncated };
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
 * The row Enter activates: the explicit highlight, clamped into the list.
 *
 * With no highlight there is only one sensible default, and it is the zero-hit
 * case: the AI row is then the only content on screen, so it is pre-selected
 * and Enter runs it. With file hits showing, Enter waits for a choice — firing
 * an AI search because someone pressed Enter out of habit would spend a model
 * call on results they were already reading.
 */
export function activeRow(highlight: number | null, fileCount: number): number | null {
  if (highlight === null) return fileCount === 0 ? 0 : null;
  return Math.min(highlight, fileCount);
}
