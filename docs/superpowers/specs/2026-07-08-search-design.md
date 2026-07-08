# Search: bookmarks + file explorer — design

Date: 2026-07-08. Branch `agent/20260708-search`.

Two independent searches, approved by Akshil:

## Shared: fuzzy scorer — `frontend/src/lib/fuzzy.ts`

- Dep-free case-insensitive subsequence matcher: `fuzzyMatch(query, text): { score: number; positions: number[] } | null`.
- Null when query chars don't appear in order. Score bonuses: consecutive matches, segment-start chars (after `/`, `.`, `-`, `_`, space, camelCase boundary). Positions drive highlight rendering.
- Shared `highlight(text, positions)` helper (or inline) renders matched chars in `<mark>`-style span.

## Explorer search (recursive, current folder subtree)

**Server** — `GET /api/fs/walk?path=<abs dir>` in `fused_render/server.py`:
- `os.walk(..., followlinks=False)`, prune hidden entries (leading `.`) and `node_modules`, `__pycache__`, `venv` from descent; skip hidden files too.
- Cap: stop after 20,000 entries collected; response `{path, entries: [{rel, is_dir, size, mtime}], truncated: bool}`. `rel` is posix-style relative to `path`. Unreadable entries skipped silently (match `/api/fs/list`).
- 400 if not a directory. pytest coverage in `tests/` (tmp_path tree: nesting, hidden/ignored pruned, truncation flag).

**Client** — `Listing.tsx` + `lib/api.ts` `walkDir()`:
- Always-visible slim search input above the table, placeholder “Search this folder…”. Focus prefetches walk; cached per `fsPath`, invalidated when the SSE dir watch fires.
- Empty query → existing listing untouched. Non-empty → flat result rows (same 3 columns), name cell = relative path with match highlight, dir/file icon kept, click navigates, sorting header hidden or inert while searching (results are rank-ordered).
- Rank: fuzzy score desc → fewer path segments → alpha.
- `q` synced to URL via `history.replaceState` (same pattern as `sort`). Esc clears + blurs.
- Footer line: “N results” + “ — truncated at 20,000 entries” when `truncated`. No matches → “No matches”.
- Walk error → inline error message; input stays usable.

## Bookmarks search (sidebar)

- Small search input atop bookmarks section in `Sidebar.tsx` (render only when bookmarks exist). Pure client over localStorage data.
- Match against bookmark display name AND target path; folder-name match includes its child bookmarks.
- Non-empty query → flat list of matching bookmark rows (highlighted), folder chrome hidden. Clear/Esc restores tree. “No matches” empty state.
- Existing row actions (open, rename, delete, context menu) keep working on filtered rows.

## Non-goals

- No vitest (repo has no frontend test runner). No recursive search from bookmarks. No content/grep search. No hidden-files toggle (later if wanted).

## Verification

- `npm run build` (tsc gate) green, pytest green, feature exercised in running app on port 8798 via Playwright.
