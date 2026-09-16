# SPEC: Spaces in file search act as wildcards

Agreed scope, confirmed with the user. Build exactly this.

## Context

Today a space in a file-search term is a literal character. `hello world` runs a
substring match, so it finds `hello world.txt` but misses `hello-world.txt`,
`hello_world.py`, `helloBigWorld.js`. Users expect a multi-word search to find
files containing those words in order, whatever separator sits between them.

The complication: `*` is not merely a wildcard in this grammar, it is a **mode
switch**. The moment a query contains `*` anywhere, `resolve_query` leaves
"substring" mode and enters "glob" mode, which is a **both-ends-anchored full
match** with **no relevance scoring** and **no result highlighting**. So this
change routes every multi-word search down a materially different path. That is
accepted, but it is why a naive `raw.replace(" ", "*")` is WRONG — see
"Anti-goal" below.

## Anti-goal (read this before writing code)

A literal `" "` -> `"*"` substitution **regresses the motivating case**. Traced
against the real code:

    query "hello world" -> "hello*world" -> pattern "**/hello*world"
                        -> regex ^(?:[^/]*/)*hello[^/]*world$

    hello world.txt         MATCHES TODAY -> would STOP matching (must END in "world")
    hello world extra.txt   MATCHES TODAY -> would STOP matching
    sub/hello world.txt     MATCHES TODAY -> would STOP matching
    HELLO World.txt         MATCHES TODAY -> would STOP matching
    hello-world.txt         the case we WANT -> still does NOT match
    hello.world             gains a match (the only gain)

Net: 4 everyday matches lost, 1 odd one gained, motivating case still broken.
The implied leading/trailing wildcards in §1 are what make this correct. Do not
drop them.

## 1. Query transformation

Applied to the raw typed string before mode resolution.

1. **Trim** leading/trailing whitespace. (A trailing space mid-typing must not
   produce a stray wildcard.)
2. **Collapse** every run of whitespace to a single `*`. `hello  world` (two
   spaces) and `hello world` must behave **identically**. Left unhandled, `**`
   is a *different, cross-directory* wildcard in this grammar and a stray
   keystroke would silently change results — this is the specific footgun the
   collapse exists to prevent.
3. **Imply wildcards on the final path segment**, but only when the query
   actually contained whitespace, and only if that segment has no user-typed
   `*`. This is what delivers "contains, in order":

       hello world           -> *hello*world*     (then resolve_query prefixes **/)
       ~/My Documents/report -> ~/My*Documents/*report*
       *.pdf                 -> *.pdf             (UNCHANGED — no whitespace)
       src/**/*.ts           -> src/**/*.ts       (UNCHANGED)
       hello world.pdf       -> *hello*world.pdf*

   Segments **before** the last get the space->`*` conversion but **no** added
   wrapping.

### Required behavior table

| Query | Must match | Must NOT match |
|---|---|---|
| `hello world` | `hello-world.txt`, `hello world.txt`, `hello_world.py`, `my_hello_big_world.py`, `sub/hello world.txt`, `HELLO World.txt`, `hello world extra.txt` | `world-hello.txt` (order matters) |
| `hello  world` (2 spaces) | identical result set to `hello world` | — |
| `*.pdf` | unchanged from today | — |
| `report` (single word) | unchanged from today, still substring mode, still ranked | — |

## 2. Apply everywhere (user decision)

The rule applies uniformly, including inside leading folder paths. Accepted
consequences, confirmed with the user — do **not** add special-casing to avoid
them:

- `~/My Documents/report` no longer walks into `My Documents` as a literal
  directory; `My*Documents` also loosely matches `MyOldDocuments`. Accepted.
- Note `_walk_from` (`fused_render/index/query.py:547-624`) stops its walk at the
  first segment containing `*`, so the base resolves shallower than today. That
  is the accepted behavior, not a bug to work around.
- **No escape hatch.** Do NOT add quoting or backslash escaping for literal
  spaces. Explicitly out of scope.

## 3. Public API also changes (user decision)

`fused.fileIndex.search` (the JS bridge in `fused_render/static/runtime.js`
around lines 5096-5114, backed by `search_under` in
`fused_render/index/query.py:395-515`) gets the **same** semantics. This is a
**breaking change** to an API user-written pages already call — that is
accepted and intended.

Note `search_under` is a separate, simpler path: plain `path ILIKE '%q%'`, no
`resolve_query`, no glob mode. Getting the new semantics there is real work, not
a one-liner. Factor the transformation so both paths share one implementation
rather than duplicating the rule.

## 4. Highlighting for glob results

Glob hits render **completely plain** today. There is an explicit branch keyed
on `mode` that skips position computation:

- `frontend/src/apps/explorer/listing/ranked-hits.ts:44-69` (`hitsFromRank`) —
  `if (mode === "substring")` guard; glob leaves `positions` as `[]`.
- `frontend/src/apps/explorer/FilesHome.tsx:190-203` — same thing inline:
  `res.mode === "substring" ? substringMatch(...) : []`.

**Required:** all glob-mode results get highlighting — both space-derived
queries and explicitly typed globs like `*.pdf` and `src/**/*.ts` (user chose
"all glob queries", not just space-derived).

**What to mark — each literal piece separately**, wildcard-filled gaps left
unmarked:

    query:  hello world     result: my_[hello]_big_[world].py
    query:  *.pdf           result: Q3 report[.pdf]
    query:  src/**/*.ts     result: [src]/lib/util[.ts]

(Square brackets = marked. This was assumed, not explicitly confirmed — the user
did not answer within the timeout, and it follows their stated "similar to text
search" intent. Flag it in the PR body so they can flip it to a single
continuous span if they prefer.)

Constraints on highlighting:
- Marks spanning a `/` stay continuous (existing behavior, pinned by
  `frontend/src/apps/explorer/listing/highlight-path.test.tsx`).
- Rendered text must still reconstruct **byte-exact** to the original string —
  copy-to-clipboard correctness depends on it.
- The server sends **no** match positions on the wire and must continue not to;
  positions are re-derived client-side. See the doc comment at
  `frontend/src/platform/lib/api.ts:605-608` establishing
  `platform/lib/fuzzy.ts` as the single source of truth for what highlights.
- Two call sites compute positions independently (`ranked-hits.ts` for the
  in-folder listing, `home-search.ts`/`FilesHome.tsx` inline for the home page)
  but share the render layer (`renderHighlight`/`renderHighlightPath` in
  `bits.tsx`, `highlightSegments` in `fuzzy.ts`). Prefer adding one shared
  position-computing helper over duplicating the glob logic twice.

## 5. Tooltip copy

`frontend/src/apps/explorer/SearchField.tsx:96-100` holds `SEARCH_GRAMMAR_HINT`,
the only place search syntax is shown to users:

    report — names containing it, at any depth below
    *.pdf — a pattern, at any depth below
    /*.pdf — a pattern, in this folder only
    ~/Work/*.md — start from another folder

Add a line documenting space behavior, e.g.
`hello world — names with both, in order`. Match the existing line style.

The home box's placeholder documents no syntax today and gains none.

## Explicitly NOT in scope

- Any escape/quoting syntax for literal spaces.
- Restoring relevance ranking to multi-word searches. Glob mode's
  depth-then-alphabetical ordering and its 5000-row unsliced ceiling are
  accepted as-is. **Do not** add scoring to `_glob_sql`.
- Out-of-order matching (`world hello` must not find `hello-world.txt`).
- Full-text search inside file contents.
- The AI natural-language search (`frontend/src/apps/explorer/lib/ai-search.ts`).
- The guarded SQL / "Ask" surface (`frontend/src/shell/Indexing.tsx`
  `QuerySection`) — spaces are legitimate SQL syntax there.
- The home search box placeholder text.

## Constraints

- A multi-word search must not return **fewer** everyday matches than today.
- Case-insensitive matching preserved.
- Single-word searches completely unchanged — same matching, same relevance
  ranking, same highlighting.
- A space-derived wildcard must not cross folder boundaries (`[^/]*`, not `.*`).
- No stored-data migration: search terms are never persisted to URL params,
  history, or saved searches.

## Key files

Server:
- `fused_render/index/query.py` — `resolve_query` (627-753), `_walk_from`
  (547-624), `_glob_to_regex` (766-782), `_rank_sql` (805-951), `_glob_sql`
  (954-975), `search_ranked` (978-1284), `search_under` (395-515).
- `fused_render/server/routers/index.py` — `_rank_body` (485-543),
  `api_index_rank` (1787), `api_index_search` (1741).
- `fused_render/index/specs/query.md` and `specs/server-api.md` — authoritative
  specs. **Update them** to match the new grammar.

Frontend:
- `frontend/src/platform/lib/fuzzy.ts` — `substringMatch` (96-110),
  `highlightSegments` (165-183).
- `frontend/src/apps/explorer/listing/ranked-hits.ts` — `hitsFromRank` (44-69).
- `frontend/src/apps/explorer/listing/bits.tsx` — `renderHighlight` (66-76),
  `renderHighlightPath` (104-114).
- `frontend/src/apps/explorer/lib/home-search.ts` — `positionsWithin` (321-337).
- `frontend/src/apps/explorer/FilesHome.tsx` (~160-205),
  `frontend/src/apps/explorer/Listing.tsx` (~1416-1462).
- `frontend/src/apps/explorer/SearchField.tsx` — `SEARCH_GRAMMAR_HINT` (96-100).
- `frontend/src/apps/explorer/listing/glob-broaden.ts` — the zero-result "widen"
  offer. It bails when a query has no `*`; multi-word queries will now carry one,
  so this ladder starts firing for them. Check it still makes sense and does not
  offer nonsense suggestions for space-derived patterns.

Existing tests that pin current behavior (expect to update some):
- `tests/test_index_query.py:88-105` — pins `?` and `[` as literal in globs.
- `tests/test_index_rank.py` — `_rank_sql` scoring, glob limit clamp.
- `frontend/src/apps/explorer/listing/ranked-hits.test.ts`
- `frontend/src/apps/explorer/listing/highlight-path.test.tsx`
- `frontend/src/platform/lib/fuzzy.test.ts`
- `frontend/src/apps/explorer/lib/home-search.test.ts`
- `frontend/src/apps/explorer/FilesHome.render.test.tsx`
- `frontend/src/apps/explorer/listing/rank-parity.test.ts` +
  `tests/fixtures/rank-parity.json` — cross-language SQL-vs-`fuzzy.ts` parity
  fixture. Keep the two rankers in sync.

## Traps (learned the hard way in this repo)

- **`bun test` green does not mean pytest green.** Some Python tests in `tests/`
  assert against **literal frontend source lines**. If you delete or rename a TS
  symbol, grep `tests/` for it before assuming you are done.
- **`bun mock.module` is process-wide.** A stub in one test file can break
  unrelated files in a full `bun test` run; a filtered run is structurally blind
  to it. If you add module mocks, run the full `bun test` at least once.
- Do not start a dev server. If you need to check something live, say so in your
  report instead — the orchestrator handles it.

## Build instructions

- TDD: write the failing test first, watch it fail, implement, watch it pass.
- **Scoped tests only in your inner loop.** Never run the full suite per commit.
  Use `pytest -k` / path args and `bun test <path>`. The orchestrator runs the
  full suite once at the end.
- Commit per logical unit with clear messages. Do not squash.
- Record decisions, dead ends, and anything this spec got wrong in
  `DECISIONS.md` in the worktree root, so a later builder resumes from disk.
- Self-review each diff before committing.
- Report: what was built, deviations from this spec and why, and test state.
