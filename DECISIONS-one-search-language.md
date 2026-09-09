# Decisions — one search language for the home and explorer search boxes

## Backend

- `resolve_query(root, raw)` lives in `fused_render/index/query.py`, next to
  `search_ranked` — it is a pure function apart from the `os.path.isdir`
  calls `_walk_from` makes while escaping a box's root, so it needed no new
  module.
- Mode decision (`"glob"` vs `"substring"`) is `"*" in raw` — unconditional,
  independent of base resolution. `?` and `[`/`]` never flip it, per spec.
- Base resolution (walking `~`/`/` segments to the deepest real directory)
  runs for EVERY query that starts with `~` or `/`, not only glob ones —
  `~/nope/x.csv` (no `*` anywhere) still escapes to home and still folds the
  missing `nope/` segment into the pattern; it just stays in substring mode
  once there, `LIKE '%nope/x.csv%'` doing the anchoring implicitly because
  the pattern itself now contains a literal `/`.
- The leading-slash disambiguation is implemented as "try the absolute walk
  first; if it couldn't even consume its first segment, it was never
  absolute" (`_walk_from` returns an `advanced` flag) rather than a separate
  existence check followed by a second walk — one walk answers both
  questions.
- The implicit `**/` prefix is applied to `raw` fresh at the top of
  `resolve_query`, gated on `"/" not in raw` (the untouched raw string) and
  `is_glob`, and only ever prepended to whatever `pattern` the base-splitting
  step produced — never re-derived after the split. This is the ordering the
  spec calls out as the most likely way to get this wrong.
- `search_ranked` gained a `glob: bool = False` parameter rather than a
  separate function. It shares the coverage check, root/prefix resolution,
  the files/dirs UNION ALL branch construction, cancellation, and the
  LIMIT-plus-one truncation trick with the substring/ranked paths — only the
  WHERE clause and ORDER BY differ, and those already lived behind one
  `con.execute(sql)` call with the SQL string chosen just above it, so a
  third SQL-building branch was a smaller diff than a parallel function
  reimplementing all of the above.
- Glob hits carry `score: 0, longest_run: 0, tier: 0` (not real scores) for
  the same reason the unranked substring branch carries fixed placeholders —
  nothing downstream keys off them, and the HTTP layer strips all four
  scoring fields unconditionally regardless of which branch produced them.
- `_rank_reason` (server/routers/index.py) is now called with the
  **resolved base**, not the box's own `root` — a `~`/`/`-escaping query can
  leave the box's root far behind, and mount/package/ignored/scanning are
  all questions about where the search actually ran.
- Response gains `base` and `mode` (both from `resolve_query`), added by
  `_rank_body` before the `_WIRE_DROP` filtering happens; neither is in
  `_WIRE_DROP`, so both reach the wire.

## Dev environment (this worktree)

- The venv's `fused_render` package is installed editable **against the main
  checkout**, not this worktree — `.venv/bin/pytest` run from the worktree
  silently imports `/home/iamsdas/Work/fused-render/fused_render/...`
  instead of the worktree's copy unless `PYTHONPATH` is set to the worktree
  root first. Every backend test command in this file (and every one a
  resuming builder should run) needs:

  ```
  PYTHONPATH=/home/iamsdas/Work/fused-render/.claude/worktrees/one-search-language \
    /home/iamsdas/Work/fused-render/.venv/bin/pytest tests/test_index_query.py -q
  ```

- The worktree's `fused_render/static/shell-dist/` does not exist until the
  frontend is built once (it's gitignored, same as `.venv`) — every server
  route test (`TestClient(create_app(...))`) raises `RuntimeError: React
  shell not built` until `cd frontend && bun run build` has run at least
  once in this worktree.

## Frontend

- `suppressRank` (FilesHome.tsx) is deleted outright, not narrowed. Search
  now runs for every searchable query regardless of whether it looks like a
  path — the Open row and the ranked file list are independent facts about
  the same query, not alternatives. This collapses most of what used to be
  the staleness-deadline effect's comment block: that effect used to gate on
  `pending || addr.status === "checking" || (suppressRank && addr.status ===
  "exists")` to cover the windows where `pending` never fired because ranking
  itself was suppressed; with ranking never suppressed, `pending` alone is
  always the right signal, and the other two disjuncts (and the two review
  fixes they existed to patch) are gone along with the mechanism they were
  patching.
- `RowModel.fileCount` is no longer zeroed by `showOpenRow` — file rows
  render underneath a resolved Open row, at row index `(openRow ? 1 : 0) +
  i`, rather than being hidden while the row is present. `aiRow` still turns
  off under `showOpenRow`: an address that resolves is never an AI-row
  candidate, independent of whether ranking also ran for it.
- `IndexRankResult` gained `base` (the directory the server actually
  searched — can differ from the box's own root once a query walks out via
  `~`/`/`) and `mode` (`"substring" | "glob"`). `answerFrom`/`narrowAnswer`
  (home-search.ts) build each hit's absolute path by joining `rel` onto
  `res.base`, not the box's own `home` — joining onto the box's root would
  point every hit at a path that does not exist as soon as a query escapes
  the root.
- The pre-existing highlight-drop bug: `answerFrom`/`narrowAnswer` used to
  re-run `substringMatch` client-side against every hit's `rel` and drop
  whatever failed it, which was silently correct only because every hit used
  to BE a substring match. A glob hit (`*.csv` matching `report.csv`) has no
  literal substring relationship to the query text at all, so the same logic
  was dropping real glob hits outright. Fixed by branching on `res.mode`:
  substring-mode hits still get `substringMatch`-derived highlight
  positions; glob-mode hits get `positions: []` (no highlight, but kept).
  `narrowAnswer` on a glob-mode held answer bails to `[]` rather than
  attempting a local narrow — a glob pattern does not narrow monotonically
  the way a growing substring does, so no local approximation of the
  server's `regexp_matches` full-match is safe, and forcing a real round
  trip is the correct fallback.
- Same highlight-drop shape exists at `listing/ranked-hits.ts:38` — fixed the
  same way (see below), independent of the home box's code.

### Listing convergence

- Spec inaccuracy: the spec's claim that `fuzzyMatch` (`platform/lib/fuzzy.ts:111`)
  "is still used by the bookmark search" is false. Its only real caller left
  is `explorer/lib/ai-search.ts:245` (unrelated, untouched). Its other
  caller, `listing/search.ts`, is one of the files this scope deletes.
  `fuzzyMatch` itself is kept — `substringMatch` in the same file is what
  `listing/ranked-hits.ts` calls, and `fuzzyMatch` still has its own live
  caller.
- Deleted, one line each: `listing/useWalkSearch.ts` (superseded by
  `listing/useListingSearch.ts`); `listing/useRankedScan.ts`, `listing/search.ts`,
  `listing/corpus-hold.ts`, `listing/scan-job.ts`, `listing/scan-resume.ts`
  (walk/corpus/scoring machinery with no callers once the walk fallback is
  gone); `platform/lib/search-hold.ts` (walk-only, query-tagged result
  holding — the ranked path's never-blank is just "keep the last answer
  object on screen," no separate hold module needed); `platform/lib/api.ts`'s
  `walkDirStream`/`WalkStreamEnd` (their one caller was `useWalkSearch.ts`).
  Every deletion was verified stranded via `grep -rln` across `src/` before
  removal; the walk/corpus/hold trio had already been deleted in an earlier
  segment of this same unit's work, confirmed here by re-grepping rather than
  re-deleting.
- Open design call: `useListingSearch`'s `searching` is one boolean —
  `q.length >= MIN_QUERY_CHARS` — covering both "swap to flat search view"
  and "worth firing a request." The home box (`FilesHome.tsx`) needs two
  separate flags (`active`/`searchable`) because it also toggles
  bookmarks/recents visibility independently of the request gate; the
  listing has no such third state, so collapsing them was the simpler
  correct choice rather than a forced parallel to the home box's split.
- Along the way, fixed a real bug versus the old hook: `hitsFromRank` is now
  called with `res.mode` threaded through (`hitsFromRank(res.hits, q,
  res.mode)`); the old hook never passed `mode` at all, so a glob answer
  reaching the listing would have hit the same highlight-drop bug documented
  above for the home box.
- `Listing.tsx`'s render logic lost its walk-specific "no matches" nuance:
  the old UI distinguished a truncated walk ("too large to search fully")
  from a complete one. The index answers per-query truncation only when
  there ARE hits (`SEARCH_RANK_LIMIT` on the server), never on an empty
  result, so that branch had nothing left to report — simplified to a plain
  "No matches" once the answer settles empty, and "Searching…" while
  `scanPending` or `searchState.status === "pending"`.
- `source` and `showingHeld` are gone from the hook's return value, not just
  always-false: with the walk deleted there is only one source (the index),
  and never-blank means "the previous answer object is still `answer`, not a
  separately-tracked held copy" — nothing downstream needed to distinguish
  "held" from "not stale" once `staleRows`/`isStale` already carry that.
- Tests: `useListingSearch.render.test.ts` is new (16 tests, TDD — written
  and watched red before the hook existed), covering the MIN_QUERY_CHARS
  gate, one-request-per-query with abort-on-edit, never-blank/stale-while-
  revalidate, the scan/poll flow, an uncoverable folder settling instead of
  polling forever, replies that outlive their query, the ranked-search
  preference, and closing the box mid-scan. `revalidate.test.ts`'s "focus is
  not a boundary" source-guard test now reads `useListingSearch.ts` and
  checks `prefetchIndex`'s body has no `reconcile()` call and never
  references `refresh` (only `pinned`) — the new `prefetchIndex` has no
  walk-request state to smuggle a generation through, so the assertion is
  simpler than the old `setWalkReq(refresh)`/`setWalkReq(pinned)` pair it
  replaces, not just renamed. `fuzzy.test.ts`'s literal path fixture was
  repointed at the new file. Comment-only mentions of `useWalkSearch` /
  `search-hold` / `scan-job` / `search-columns` across `selection.ts`,
  `selection.test.ts`, `column-shedding.test.ts`, `fuzzy.ts`, `FilesHome.tsx`,
  `instant-search.ts`, `instant-search.test.ts`, `ranked-search-pref.ts`,
  `Indexing.render.test.tsx`, and `RepoUpdatesDock.test.tsx` were reworded to
  describe current behavior rather than cite deleted files. All scoped test
  files pass; see the unit's final report for the full list and counts.

## Windows CI fixes (query resolution)

Two distinct causes behind `test-python-windows`'s eight `resolve_query`/
`/api/index/rank` failures, diagnosed against the log without a Windows
machine to reproduce on.

- **Separator mismatch — a test bug, not a code bug.** `resolve_query`
  already returns every base through `norm()` (forward slashes only) via
  `_walk_from`'s `base = norm(start).rstrip("/")` — this was already the
  canonical form the rest of the index stores and compares paths as
  (`ignore.norm`'s own docstring: "canonical path form for everything
  stored, matched, or compared"). The failing tests built their EXPECTED
  values from `str(tmp_path / ...)` and `str(home)` directly, which on
  Windows carry native backslashes, then compared them against the
  already-normalized actual `base` — a mismatch in the test fixture, not in
  what the resolver returns. Fixed by normalizing the expected side with
  `norm()` in both `tests/test_index_query.py` (the `_home` fixture, and
  `test_resolve_absolute_path_walks_the_filesystem`'s `etc` path) and
  `tests/test_index_search.py` (the three HTTP-level tests that compare
  `body["base"]` against a raw `str(...)` path). No production code changed
  for this cause — the resolver was already consistent with how `root`/
  `base` are normalized everywhere else they're consumed (`search_ranked`'s
  own `root = norm(os.path.abspath(...))`, `stats`, `search_under`).
- **Drive-letter absolute paths never recognized.** `resolve_query`'s
  absolute-path branch was `elif raw.startswith("/")` only — a Windows
  absolute path starts `C:\` or `C:/`, so it fell through to the `else`
  branch and the whole raw string became the literal pattern. Fixed by
  adding `_DRIVE_ABS = re.compile(r"^[A-Za-z]:[\\/]")` (query.py) and a new
  branch in `resolve_query`, checked before the POSIX leading-`/` branch:
  a drive-letter match always walks as an escape from the drive root
  (`_walk_from(raw[:2] + "/", raw[3:].replace("\\", "/"))`), unconditionally
  — unlike the POSIX leading-`/` case, there is no ambiguity to fall back
  from (nothing else a search box's raw string can start with looks like
  `C:\`), so `_walk_from`'s `advanced` flag is not consulted the way it is
  for `/`. The rest of the string's backslashes are folded to `/` before
  the walk only for this branch — the drive letter's own separator is
  native, but `_walk_from` and `_glob_to_regex` both only ever split/match
  on `/`, per spec ("a slash is the only thing that limits depth").
  **Deliberately left alone**: the implicit `**/` depth decision still reads
  the RAW string exactly as typed, backslashes and all, so an all-backslash
  Windows query with no forward slash anywhere (`C:\Users\x\*.conf`) widens
  to any depth under the resolved base, same as any other slash-free glob —
  covered by `test_resolve_windows_drive_letter_path_walks_the_filesystem`.
  A query typed with forward slashes past the drive letter
  (`C:/Users/x/*.conf`) anchors normally, covered by
  `test_resolve_windows_drive_letter_path_with_forward_slashes`. Both new
  tests monkeypatch `os.path.isdir` rather than using `tmp_path`, since a
  drive letter has no real counterpart on the POSIX filesystem this suite
  runs on.
- The ninth Windows failure, `test_git_status_listing.py::
  test_parse_porcelain_decodes_a_non_utf8_name_like_scandir_would`, is in a
  file this branch never touched (`git log origin/main..HEAD --stat --
  tests/test_git_status_listing.py` is empty) — left alone, per instruction.
