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
// query.py's `_rank_sql`, one SQL statement — see D708). The page used to
// fetch the whole corpus — 19.8 MB and 164k rows on the first keystroke,
// capped at 200k entries so ~71% of a 571k-file home could not be found at
// all — and rank it here. It now asks per query and gets a few KB, over the
// WHOLE index.
//
// Index-backed search is substring-only (D708 — the deleted `index/rank.py`'s
// fuzzy subsequence escalation is gone, an owner-accepted trade). `narrowAnswer`
// below matches that with `platform/lib/fuzzy.ts`'s `substringMatch` rather
// than `fuzzyMatch`, which still accepts a subsequence and would otherwise
// keep rows the server no longer returns. `fuzzyMatch` (listing/search.ts)
// stays subsequence-based and is still correct there — the live walk it ranks
// has no server to agree with.
//
// Two things must not be papered over:
//
//  * a cold index. The home page has no live-walk fallback (that is the
//    listing's job, one folder at a time), so an uncovered root is reported as
//    the state of the INDEX (`indexGap`, below — building, not built, off, or
//    never coverable), never as "no matches" — blaming the user's files for
//    the app's state is exactly the failure the server's search refuses to
//    make.
//  * the WAIT. Ranking used to be local, so results repainted within a frame
//    and never blanked. A round trip per query can only feel as good if it
//    never blanks the list, never flashes a spinner, and answers a backspace
//    from memory — which is what the pieces below are for.
//
// The pieces that make a per-query round trip feel instant — the trailing
// debounce, the pending threshold, the backspace memo — are NOT here: they are
// shared with the listing's in-folder box, which is now the same kind of box,
// and they live in platform/lib/instant-search.
import type { IndexRankResult, RankReason } from "@platform/lib/api";
import { globMatch, substringMatch } from "@platform/lib/fuzzy";

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
  /**
   * The directory `hits`' `rel`s are relative to — `res.base` from the
   * server, not necessarily the box's own root: a `~`/`/`-leading query
   * walks `resolve_query` (fused_render/index/query.py) out to wherever it
   * escapes to, and every absolute `path` a hit renders has to be rebuilt
   * from THIS, not from the root the box was opened on.
   */
  base: string;
  /** Which matcher produced `hits` — see `IndexRankResult.mode` (api.ts).
   * Both modes highlight against `positions`, computed by different means
   * (see `answerFrom`): a substring hit via `substringMatch` against the
   * query text, a glob hit via `globMatch` against the server's resolved
   * pattern. */
  mode: "substring" | "glob";
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
   * server's `reason` (platform/lib/api.ts's `RankReason`). FilesHome pairs
   * it with the live scan poll to pick which of the four not-covered states
   * the root is in, and only one of them tells the user to wait: see
   * `indexGap`.
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

/**
 * The part of a query the server actually filtered `rel`s against, for
 * re-highlighting/re-narrowing hits client-side.
 *
 * A `~`/`/`-leading query can walk `resolve_query`'s `base`
 * (fused_render/index/query.py) out past the box's own root — `res.base`
 * says where it landed, but a hit's `rel` is relative to THAT, not to the
 * raw query text. Everything up to the last "/" in a query like that named a
 * real directory the server already walked past on its way to `base`; only
 * the trailing segment is what it actually tested each `rel` against. A
 * query with no "/" is already exactly its own pattern — the common case,
 * unchanged from before.
 */
export function patternTail(query: string): string {
  const slash = query.lastIndexOf("/");
  return slash === -1 ? query : query.slice(slash + 1);
}

/**
 * A client-side mirror of `expand_whitespace_query`
 * (`fused_render/index/query.py`) — the ONE shared transform both
 * `resolve_query` and `search_under` run a raw typed string through before
 * deciding `mode`. Kept here, not re-derived at each call site, so every
 * caller that needs to know whether a query WOULD settle in glob mode
 * before a response has come back agrees with the server by construction.
 *
 * Byte-equivalent with `expand_whitespace_query` (`fused_render/index/
 * query.py`) — a follow-up to the original whitespace-as-wildcard rule that
 * fixes three disagreements (DECISIONS.md, worktree-search-trailing-space):
 *
 *  1. A TRAILING space used to be trimmed away (`*.js ` searched for `*.js`,
 *     silently dropping the space the user just typed). Trimming is gone:
 *     leading/trailing whitespace is now as meaningful as any other run.
 *  2. `icon copy` (found `icon copy.png`) and `icon*copy` (found nothing —
 *     the typed `*` produced an anchored pattern requiring the name to END
 *     in "copy") used to disagree; a since-reversed fix made them agree
 *     (DECISIONS.md, worktree-search-trailing-space). They are DELIBERATELY
 *     back to disagreeing again: a mid-segment user-typed `*` now
 *     suppresses the trailing wrap (see rule 5), so `icon*copy` stays
 *     anchored and `icon copy` does not. Reversed because results are
 *     capped (top ~20 of 200+) — a loose filter does not just rank a
 *     correct answer lower, it consumes a slot and can push the correct
 *     answer out of the window entirely, and ranking cannot enlarge the
 *     window. `icon*copy*` (a user-typed trailing `*`) still opts back into
 *     the old, loose behavior.
 *  3. A trailing space used to NARROW rather than widen: the wildcard it
 *     inserted was a single-segment `*`, which cannot cross a `/`, so
 *     `"src "` lost `srcdir/file.txt` even though `"src"` (substring mode)
 *     matched it. Every wildcard THIS function inserts — the whitespace
 *     collapse and the final-segment end wrap alike — is now the
 *     cross-directory `**` token, never a bare `*`: a space only ever
 *     widens. A whitespace-only query (`"   "`) has no literal character
 *     left to search for at all, so it resolves to `""`, matching how an
 *     empty query already behaves (it used to become the "match anything"
 *     glob `"*"`).
 *
 * One documented exception to "byte-equivalent": "whitespace" here means
 * whatever `expand_whitespace_query`'s Python `\s` matches, NOT whatever
 * JS's native `\s`/`String.trim()` matches — the two disagree in two
 * places. First, U+FEFF (ZERO WIDTH NO-BREAK SPACE, a leading BOM some
 * editors/OSes prepend): Python's `\s` does not treat it as whitespace
 * (Unicode category Cf, not a whitespace category) but JS's does. Second
 * (code review finding 7(b)), the four C0 "information separator" control
 * characters U+001C-U+001F and U+0085 (NEL): Python's `\s` DOES treat these
 * as whitespace (via CPython's Unicode bidirectional-class tables) but JS's
 * does not (JS follows the `White_Space` property, which excludes all
 * five). Every place below that would naively reach for `\s` uses
 * `NON_BOM_WS` instead, which is defined to match exactly what Python's
 * `\s` matches on both counts, so a BOM-prefixed or C0-separator-containing
 * literal takes the same path on both sides of the wire (pinned by
 * `test_expand_whitespace_query_does_not_treat_a_bom_as_whitespace` in
 * tests/test_index_query.py and this file's own "does not treat a leading
 * BOM" and "treats the C0 separators" tests) rather than silently
 * resolving to different modes in the two languages.
 *
 * The rule:
 *  1. Whitespace-only (`raw` made ONLY of `NON_BOM_WS` characters,
 *     including the truly empty string) resolves to `""` — no wildcard,
 *     nothing to search for.
 *  2. Otherwise, no trimming.
 *  3. The ONE no-op besides rule 1: a string with NEITHER whitespace NOR
 *     `*` anywhere (`report`) is returned unchanged — still substring mode,
 *     still ranked.
 *  4. Collapse every whitespace run to `**` — dropped, not replaced,
 *     whenever a run directly borders a literal `*` the user already typed
 *     (`report *.pdf` must not stack a THIRD star beside it).
 *  5. On the FINAL `/`-separated segment only, wrap each END independently,
 *     with DIFFERENT tokens (code review finding — an earlier version used
 *     `**` on both ends, which let a folder-anchored query like `/*.pdf`
 *     leak into a differently-named subtree, e.g. matching
 *     `x.pdf/inner/deep.bin`): prepend `**` unless it already starts with
 *     `*` (crosses directories, same as the whitespace collapse, and this
 *     leading half is UNCONDITIONAL otherwise — an earlier segment's `*`
 *     never suppresses it); append a single `*` unless it already ends with
 *     `*` OR the final segment contains a user-typed `*` ANYWHERE in it
 *     (confined to one segment — its only job is letting an unanchored
 *     fragment also match a longer name in the SAME folder, which one `*`
 *     already gives in full). REVERSED (DECISIONS.md,
 *     worktree-search-trailing-space): a mid-segment `*` used to still get
 *     the trailing wrap (`icon*copy` -> `**icon*copy*`, agreeing with
 *     `icon copy` -> `**icon**copy*`); it no longer does — a user-typed `*`
 *     anywhere in the final segment is now read as an opt-in to precision,
 *     so `icon*copy` -> `**icon*copy` (anchored, does not match `icon
 *     copy.png`) and `*.parquet` -> `*.parquet` (anchored, does not match
 *     `report.parquet.bak`). Earlier segments get the whitespace collapse
 *     but no wrap.
 *
 * The invariant that survives (A4): the function never manufactures a run
 * of THREE OR MORE consecutive `*` unless the input already had one — it is
 * allowed to concatenate two adjacent user-typed single stars into `**`
 * once the whitespace between them is dropped (`"* *"` -> `"**"`), which is
 * accepted, not invented.
 *
 * Known, accepted consequence (do not special-case around it): a
 * whitespace-free glob like `*.pdf` still gains the leading `**` (it
 * already starts with `*`, so that guard is moot, but the rule is
 * unconditional otherwise) and now ALSO keeps no trailing wrap — it already
 * carries a `*`, so it means exactly "ends with .pdf" and no longer matches
 * `notes.pdfx` or `report.pdf.bak`. See DECISIONS.md.
 */
// "Whitespace, but not a BOM": JS's `\s` (and therefore `String.trim()`)
// treats U+FEFF (ZERO WIDTH NO-BREAK SPACE, a leading BOM some editors/OSes
// prepend) as whitespace; Python's `\s` (and `str.strip()`) does not — it is
// Unicode category Cf (format), not a whitespace category. `[^\S\uFEFF]` is
// the standard JS idiom for this: inside a negated class, `\S` and `\uFEFF`
// are unioned before the negation, so the class matches exactly the
// characters that are whitespace AND not U+FEFF — i.e. Python's `\s`. Used
// everywhere this function would otherwise reach for a bare `\s`, so a
// BOM-prefixed literal takes the same no-op path `expand_whitespace_query`
// (query.py) takes for it, keeping the documented byte-equivalence between
// the two (see `test_expand_whitespace_query_...bom...` in both test files).
//
// Gap 2 (the other direction \u2014 code review finding 7(b)): Python's `\s`
// (and `str.isspace()`) ALSO matches five characters JS's `\s` does not:
// the four C0 "information separator" control characters U+001C-U+001F
// (FS/GS/RS/US) and U+0085 (NEL, NEXT LINE). This is because CPython's
// Unicode tables classify these by bidirectional class (a paragraph/segment
// separator), not by the `White_Space` property JS's `\s` follows exactly \u2014
// `White_Space` excludes all five. `[-\u0085]` adds them back
// in, alternated alongside the BOM-excluding class above, so this fragment
// matches exactly what Python's `\s` matches: JS-whitespace-minus-BOM, plus
// the five characters Python additionally treats as whitespace that JS does
// not (pinned by this file's "treats the C0 separators" test).
const NON_BOM_WS = /(?:[^\S\uFEFF]|[-\u0085])/;

const ALL_NON_BOM_WS = new RegExp(`^${NON_BOM_WS.source}*$`);

export function expandWhitespaceQuery(raw: string): string {
  const value = raw ?? "";
  if (ALL_NON_BOM_WS.test(value)) return "";
  if (!NON_BOM_WS.test(value) && !value.includes("*")) return value;
  const segments = value.split("/");
  const last = segments.length - 1;
  // Captured BEFORE whitespace collapse, same as query.py: the collapse
  // step below only ever inserts `**`, never a bare `*`, so a `*` found
  // here is always one the user typed themselves. Scope is the FINAL
  // segment only — an earlier segment's `*` is a directory wildcard and
  // says nothing about the filename (`src/*/index` must still trailing-wrap
  // `index`).
  const finalHasUserStar = segments[last]!.includes("*");
  const wsRun = new RegExp(NON_BOM_WS.source + "+", "g");
  const collapseWs = (segment: string): string =>
    segment.replace(wsRun, (match, offset: number) => {
      const before = offset > 0 && segment[offset - 1] === "*";
      const after =
        offset + match.length < segment.length && segment[offset + match.length] === "*";
      return before || after ? "" : "**";
    });
  const collapsed = segments.map(collapseWs);
  let final = collapsed[last]!;
  if (!final.startsWith("*")) final = `**${final}`;
  // Reversal (DECISIONS.md, worktree-search-trailing-space): the trailing
  // append is now ALSO suppressed when the final segment carries a
  // user-typed `*` anywhere in it, not only at its very end. `icon*copy` no
  // longer agrees with `icon copy` — a mid-segment `*` is read as an opt-in
  // to precision, and `icon*copy*` (a user-typed trailing `*`) still
  // reproduces the old behavior.
  if (!(final.endsWith("*") || finalHasUserStar)) final = `${final}*`;
  collapsed[last] = final;
  return collapsed.join("/");
}

/**
 * Whether `raw`, run through the same transform `resolve_query` applies,
 * would settle in `mode: "glob"` server-side — `"*" in raw` after
 * `expand_whitespace_query`, exactly mirroring `resolve_query`'s own rule
 * (fused_render/index/query.py). Needed wherever a caller has to pick a
 * behavior BEFORE a response comes back and says which mode actually ran
 * (e.g. how many rows are worth asking for) — see `useListingSearch.ts`'s
 * request `limit`.
 */
export function willResolveToGlobMode(raw: string): boolean {
  return expandWhitespaceQuery(raw).includes("*");
}

/**
 * A ranked response as an answer: absolutized, capped, and highlighted.
 *
 * A hit's `path` is built from `res.base`, the directory the server actually
 * searched: a plain query never moves it off the box's own root, but a
 * `~`/`/`-leading one can walk it anywhere `resolve_query`
 * (fused_render/index/query.py) resolves to, and joining `h.rel` onto any
 * other directory would point every hit at a path that does not exist.
 */
export function answerFrom(
  res: IndexRankResult,
  query: string,
  elapsedMs: number,
): HomeAnswer {
  const pattern = patternTail(query);
  return {
    query,
    base: res.base,
    mode: res.mode,
    hits: res.hits.slice(0, HOME_RESULT_CAP).map((h) => ({
      path: res.base + "/" + h.rel,
      rel: h.rel,
      is_dir: h.is_dir,
      size: h.size,
      mtime: h.mtime,
      // Substring hits are re-matched HERE rather than sent: fuzzy.ts
      // decides what highlights, full stop, and `substringMatch` is the
      // exact test the server's substring mode filtered on, so the
      // guarantee that this finds something is EXPLICIT rather than
      // incidental — against `pattern`, not the raw query text, since a
      // `~`/`/`-leading query's `rel`s are relative to `res.base`, not to
      // whatever came before the last "/" in what was typed.
      //
      // A glob hit is matched against `res.pattern` (`IndexRankResult.
      // pattern` — the server's resolved, base-peeled, whitespace-expanded
      // pattern) via `globMatch` instead: a glob match is not necessarily a
      // substring of the query text at all (`*.csv` matching `report.csv`
      // has no literal `"*.csv"` anywhere in `report.csv`), so
      // `substringMatch` cannot even be asked here (SPEC-search-space-
      // wildcard.md §4). A glob hit `globMatch` itself does not confirm
      // (should not happen for a hit the server already matched) renders
      // unhighlighted rather than dropped — the same honest-over-lucky
      // posture the substring branch always had.
      positions:
        res.mode === "substring"
          ? substringMatch(pattern, h.rel)?.positions ?? []
          : res.pattern
            ? globMatch(res.pattern, h.rel)?.positions ?? []
            : [],
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
 * old one ("read" -> "readm"): re-running `substringMatch` over the hits
 * already in hand and keeping only the ones that still match — with
 * `positions` recomputed for the new query — narrows the list on screen with
 * no round trip and no blank frame, which is strictly better than dimming
 * rows that cannot possibly be answers to what is now typed.
 *
 * `substringMatch` (platform/lib/fuzzy.ts), NOT `fuzzyMatch` (D708 correction
 * — review finding): the index-backed server is substring-only, so narrowing
 * with `fuzzyMatch`'s looser subsequence test could KEEP a row the server
 * would no longer return (`"rdme"` is a subsequence of `"readme.md"` but
 * never a substring of it) — painting a hit for a query, then watching it
 * vanish when the real answer lands empty. `substringMatch` is the exact test
 * `_rank_sql`'s `WHERE lower(rel) LIKE '%q%'` (fused_render/index/query.py)
 * filters on server-side, reproduced here so this agrees with it with no
 * round trip. `fuzzyMatch` stays correct for the LIVE-WALK path
 * (`listing/search.ts`), which has no server-side filter to disagree with.
 *
 * Deliberately does NOT re-rank or add rows: it can only ever REMOVE hits from
 * the held answer, which is what makes the result a provable SUBSET of the
 * true answer for `q` — it can never show something the fresh answer
 * wouldn't, now that both agree on what counts as a match. A query that is
 * not an extension of the old one (a paste, a select-all retype) narrows to
 * whichever held hits happen to still contain `q` as a substring, which is
 * usually few or none; that emptiness is exactly the signal the staleness
 * deadline (`STALE_CLEAR_MS`, platform/lib/instant-search) uses to decide
 * there is nothing worth holding onto.
 *
 * A GLOB-mode held answer (`answer.mode === "glob"`) is narrowed ONLY when
 * `q` is still a PURE whitespace-derived query — no literal `*` typed
 * anywhere, and no `/` (a path-shaped query walks `resolve_query`'s base
 * off `q` itself; reproducing that walk locally is exactly the "no local
 * test can reproduce the server's semantics" case below, so it still
 * bails). For that narrow shape, `expand_whitespace_query`'s own transform
 * (fused_render/index/query.py) is fully reproducible client-side: no
 * trimming, collapse whitespace runs to `**`, then wrap the whole (single-
 * segment) query in a leading/trailing `**`. Every multi-word home query
 * hits this path on every keystroke while the user is still typing inside
 * or adding a word — SPEC-search-space-wildcard.md's whole motivating case
 * — so bailing to `[]` here blanked the result list between keystrokes for
 * exactly the queries this feature exists for (code review finding).
 * `globMatch` against the rebuilt pattern is the SAME test `_glob_sql`
 * filtered the held hits with originally, just re-evaluated for `q`, so a
 * held hit that still matches stays a provable subset of what a fresh
 * round trip for `q` would answer.
 *
 * A literal `*` the user typed themselves has no such guarantee — one more
 * wildcard character can match an entirely different set of paths (`*.csv`
 * -> `*.csv?` — SPEC's own example) — so that case (and the path-shaped
 * case above) still bails to `[]`, the same "nothing worth holding onto"
 * signal a paste produces, and lets the real round trip already in flight
 * for `q` supply the answer instead. `substringMatch` is not a weaker
 * version of glob matching whose survivors are safely a subset of it, so it
 * is never used as a stand-in here even in the narrowed case above —
 * `globMatch` against the rebuilt pattern is the only test that agrees with
 * the server for glob mode.
 *
 * A SUBSTRING-mode held answer (`answer.mode === "substring"`) whose query,
 * extended by THIS keystroke, would now resolve to glob mode server-side
 * (`willResolveToGlobMode(q)`) takes the SAME glob-narrowing path above,
 * not the plain substring branch below (code review finding). The classic
 * case is the space that turns "report" into "report ": every held hit
 * already satisfies "contains 'report' as a substring", and
 * `expandWhitespaceQuery` wraps that same literal text in a leading/
 * trailing `**` with nothing else changed, so `globMatch` against the
 * rebuilt pattern reduces to the exact same "contains 'report'" test —
 * every held hit that matched still matches, none blank out. Running the
 * substring branch instead (`substringMatch` against a query that now
 * literally ends in a space) matched nothing, since no `rel` ends with a
 * space character, and blanked the whole list for a full debounce + round
 * trip on the very keystroke `narrowAnswer` exists to smooth over. The
 * reverse direction (a glob-mode held answer whose query stops being
 * multi-word) is NOT symmetric and does not get this treatment: that case
 * stays inside the `answer.mode === "glob"` branch above and bails to `[]`
 * (a glob answer's hits were matched by a wildcard pattern, not a plain
 * substring test, so they are not provably a subset of a fresh substring
 * answer without a round trip).
 */
export function narrowAnswer(answer: HomeAnswer, q: string): HomeHit[] {
  if (answer.mode === "glob" || willResolveToGlobMode(q)) {
    // A path-shaped query walks `resolve_query`'s base off `q` itself — out
    // of scope, same as a literal "*" (see the doc comment above). Both
    // rule this out before the pattern is even built.
    if (q.includes("*") || q.includes("/")) return [];
    const pattern = expandWhitespaceQuery(q);
    // `q` already has no `*` of its own (ruled out above), so the only way
    // `expandWhitespaceQuery` can still produce a `*`-free `pattern` here is
    // the one no-op case: no whitespace anywhere in `q` either. That means
    // the server would resolve `q` back to SUBSTRING mode, not glob
    // (`willResolveToGlobMode` would be false) — not a glob pattern this
    // branch can safely match with.
    if (!pattern.includes("*")) return [];
    const out: HomeHit[] = [];
    for (const hit of answer.hits) {
      const m = globMatch(pattern, hit.rel);
      if (!m) continue;
      out.push({ ...hit, positions: m.positions });
    }
    return out;
  }
  // Same reasoning as `answerFrom`: `hit.rel` is relative to `answer.base`,
  // which a `~`/`/`-leading query can have walked past the box's own root —
  // matching the raw `q` against a base-relative `rel` fails on every row
  // for a query shaped like that. `patternTail` is the same trailing-segment
  // stand-in `answerFrom` re-highlights with.
  const pattern = patternTail(q);
  const out: HomeHit[] = [];
  for (const hit of answer.hits) {
    const m = substringMatch(pattern, hit.rel);
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

// -- why there is no index answer --------------------------------------------

/**
 * The four states a not-covered answer can be in. They differ in what the
 * user can do about it, which is the only reason to tell them apart.
 *
 *  * `scanning` — a scan is running, so waiting really is the advice.
 *  * `buildable` — the root is not in the index and NOTHING is running.
 *    Waiting fixes nothing; only a scan does.
 *  * `disabled` — the indexing pref is off, so no scan can start until it is
 *    back on.
 *  * `fda` — the packaged mac app has no Full Disk Access, so no scan can
 *    start until it is granted (and the app relaunched). The page offers the
 *    grant, not a scan.
 *  * `unavailable` — mount-backed, inside a package, or pruned by the ignore
 *    rules: no scan will ever cover it (see `RankReason`).
 */
export type IndexGap = "scanning" | "buildable" | "disabled" | "fda" | "unavailable";

/**
 * Which of those an uncovered root is in.
 *
 * The page used to render one message — "the file index is still building" —
 * for every `reason`, and that message is a promise that something is coming.
 * For `uncovered` nothing is: this page never asks for a scan (that is the
 * in-folder box's `requestFolderScan`) and the startup scheduler runs once per
 * boot, so a first scan that was refused, debounce-skipped, or died with its
 * worker leaves the page promising a build that no longer exists — for as long
 * as the user is willing to stare at it. `buildable` is that case as its own
 * state, so the page can offer the scan instead of describing one.
 *
 * `scanning` is read from the LIVE status poll as well as from `reason`, and
 * the poll wins in BOTH directions — which is why it is tri-state (`null` is
 * "the poll has not answered yet", not "idle"):
 *
 *  * `reason` was fixed when the answer was ranked, so a scan started since
 *    the last keystroke (by the user's own button, or by anything else) shows
 *    up in the poll a second later and in `reason` only on the next query.
 *  * a definite `false` DEMOTES `reason === "scanning"` to `buildable`. A rank
 *    that landed while the startup scan was alive says "scanning" forever
 *    after: if that worker is then killed, `/api/index/status` reports idle
 *    (runner._with_liveness, after ABANDONED_RUN_S) but `last_completed_at`
 *    never moves, so no lifecycle event fires and nothing re-ranks. Trusting
 *    the frozen `reason` there is the twenty-minute wedge again, wearing the
 *    other reason's clothes — and with a frozen file count to make it
 *    convincing.
 *
 * `disabled` outranks the poll because nothing can be in flight while the pref
 * is off — every trigger is gated on it, and a scan running at the moment of
 * toggle-off is cancelled outright (routers/index.cancel_all_scans).
 */
export function indexGap(reason: RankReason, scanning: boolean | null): IndexGap {
  if (reason === "disabled") return "disabled";
  // Same argument as `disabled`: every trigger is gated on the grant, so a
  // lagging `scanning` from the poll cannot be a scan that will finish.
  if (reason === "fda") return "fda";
  if (scanning === true) return "scanning";
  if (reason === "scanning" && scanning === null) return "scanning";
  if (reason === "mount" || reason === "package" || reason === "ignored") {
    return "unavailable";
  }
  return "buildable";
}

/**
 * Whether AI search is worth offering.
 *
 * AI search is not a second engine. The spec the model produces is executed
 * against the SAME file index — `routers/search._search_index`, whose
 * docstring says "the only engine" — so with nothing built it raises
 * `IndexUnavailable` and reports "the file index has not been built yet". That
 * is the message the note is standing next to, which makes "AI search can
 * answer in the meantime" false in exactly the state that printed it, and the
 * "Search with AI" row a click into the same wall.
 *
 * A root the index deliberately does not cover (mount / package / ignored) is
 * a different case: the index is built, so AI search does answer — just not
 * about that root's files. `has_index` is the right question either way.
 *
 * `null` — no poll answer yet — offers it. `has_index` is false on a cold
 * poll, and hiding the row for the first second of every page load would be
 * its own wrong answer.
 */
export function aiSearchUsable(status: { has_index: boolean } | null): boolean {
  return status === null || status.has_index;
}

/**
 * A scan this page asked for that the status poll has not accounted for yet.
 *
 * `at` is `Date.now()` when the request went out, `completedAt` the
 * `last_completed_at` it went out under.
 */
export interface PendingScan {
  at: number;
  completedAt: number | null;
}

/**
 * How long a requested scan may stay unaccounted for before the note stops
 * claiming it is starting. Two idle poll intervals
 * (`INDEX_IDLE_POLL_MS`) — long enough that the poll has certainly had a turn,
 * short enough that a scan which died between two polls without moving
 * `last_completed_at` cannot leave "Starting…" on screen indefinitely. That
 * failure mode is the one this whole file is about; it does not get to come
 * back as the spinner for its own fix.
 */
export const SCAN_START_GRACE_MS = 20_000;

/**
 * Whether to say a requested scan is starting rather than re-offer the button.
 *
 * The POST returning is not the scan appearing. While idle the shared poll is
 * on a ten-second beat, so between "started" and "scanning" the answer's
 * `reason` is still `uncovered` and the note would go back to offering "Index
 * them now" — a click that reads as a no-op on the one screen whose whole
 * point is that waiting is futile, and whose obvious response is to click
 * again. The claim is held until the poll either shows the scan running or
 * shows it already over (`last_completed_at` moved).
 */
export function scanStarting(
  pending: PendingScan | null,
  scanning: boolean | null,
  completedAt: number | null,
  now: number,
): boolean {
  if (pending === null) return false;
  if (now - pending.at >= SCAN_START_GRACE_MS) return false;
  if (scanning === null) return true;
  if (scanning) return false;
  return completedAt === pending.completedAt;
}
