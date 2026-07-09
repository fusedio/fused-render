# History view template — design

**Date:** 2026-07-09
**Branch:** `agent/20260709-history-view`
**Status:** approved approach (Option A: single-file JS template), spec pending user review

## Goal

A `history` view template that renders the per-file `.html.json` sidecar (the shared
store written by the claude template, bookmark mirror, session restore, and annotate —
see SPEC §21/LSN, SB-7, PR #64). Reachable two ways:

1. Opening `sine.html` and switching to the `history` mode.
2. Opening `sine.html.json` directly — `history` is the default mode there.

Scope is `.html` targets only for now; other extensions can be added later by
registering more compound keys.

## Non-goals

- No shell/React changes. All logic lives inside the template (claude-template precedent).
- No writes to the sidecar — strictly read-only against the file.
- No comment navigation/re-activation. Annotate's live store is the URL `comments`
  param; we decided not to synthesize URLs for logged comments. Comments render as a
  read-only log.
- No support for non-`.html` sidecars yet.

## Files

| File | Purpose |
|---|---|
| `fused_render/templates/history/template.html` | The whole view: fetch, validate, render, navigate. |
| `fused_render/templates/history/icon.svg` | Mode icon (clock/history glyph), same size conventions as siblings. |
| `fused_render/templates/registry.json` | Two edits (below). |
| `tests/test_history_template.py` | Registry + template presence/shape sanity. |
| `SPEC.md` / `DECISIONS.md` | Document the new binding + template (repo convention). |

No Python file. Convention check: templates ship a `.py` only when the browser can't
parse the format (parquet/sqlite/xlsx readers, tile servers). JSON is browser-parseable;
`tree` (the existing JSON viewer) is pure `template.html` + `fused.readFile`. History
follows `tree`.

## Registry binding

```json
".html.json": ["history", "tree", "code", "annotate"],
".html":      ["_render", "code", "claude", "annotate", "history"]
```

- Compound suffix keys are already supported (server.py CT-3, "`.xyz.json`").
  `.html.json` is more specific than `.json`, so it wins for sidecar files;
  plain `.json` files keep their current `tree`-first binding.
- `.html` keeps `_render` as default; `history` is appended at the end of the
  current list (`["_render", "code", "claude", "annotate"]` at HEAD).

## Data flow (inside template.html)

1. `file = fused.params.get("_file")`.
2. Resolve roles:
   - `file` ends with `.html.json` → `sidecarPath = file`, `targetPath = file` minus `.json`.
   - `file` ends with `.html` → `sidecarPath = file + ".json"`, `targetPath = file`.
3. `text = await fused.readFile(sidecarPath)`.
   - Read fails / file absent → friendly empty state: "No history yet for `<target>`".
4. `JSON.parse(text)`:
   - Whole-file parse failure → single full-page warning with the raw text shown
     (nothing to section — the file is one corrupt blob).
   - Parse OK but not an object → same full-page warning.
5. Per-key validation against the inline schema (below). Each known key renders
   independently:
   - Valid → its section renders.
   - Invalid → **that section only** shows a warning card ("`comments` is corrupted:
     <first validation error>") plus the collapsed raw JSON of that key. Other
     sections are unaffected.
   - Absent → section renders an empty state ("no claude sessions recorded").
6. Keys not in the schema (future writers) → one collapsed "Other keys" section with
   raw JSON. Unknown ≠ corrupt.

## Inline schema

A `const SCHEMA = {...}` object in `template.html` — per-top-level-key JSON-Schema-style
definitions, enforced by a small hand-rolled validator (~50 lines) supporting the subset
we need: `type` (object/array/string/number), `required`, `properties`, `items`.
No vendored validator library.

Shapes (from `agent.py`, `bookmarks.py`, `annotate.py`, `server.py` at HEAD):

```js
claudeSessions:  [{ id: string, preview: string, created_at: number /* s */,
                    last_used: number /* s */, cwd: string }]
bookmarkHistory: [{ id: string, search: string,               // required
                    name?: string, icon?: string,             // absent when None
                    created_at?: number /* ms */,
                    recorded_at: number /* s */, updated_at: number /* s */ }]
lastSession:     { search: string, updated_at: number /* s */ }
comments:        [{ id: string,                               // required
                    content?: string, createdAt?: number /* ms */,
                    resolved?: true, anchorPath?: string, view?: string,
                    recorded_at: number /* s */, updated_at: number /* s */ }]
```

**Timestamp units are intentionally mixed** (documented in `bookmarks.py` /
`annotate.py`: "different units in one file, by design — do not unify"):
`created_at` on bookmarks and `createdAt` on comments are **ms** epoch (JS
`Date.now()`); everything else (`recorded_at`, `updated_at`, claude's
`created_at`/`last_used`) is **seconds** epoch (Python `time.time()`). The
view's time formatter must pick the unit per field, not heuristically.

Only fields the view renders are `required`; extra fields on entries are allowed
(writers may grow their records — additive changes must not read as corruption).

## Sections + interactivity

All navigation uses `window.top.location.href` + the `/view/<encoded-path>?<search>`
shell URL shape (same-origin; claude template already touches `window.top`). Path
encoding must match the shell router codec (`frontend/src/lib/router.ts`), including
Windows drive-letter handling (#68).

| Section | Renders | Click |
|---|---|---|
| Claude sessions | preview text, created/last-used as relative time, cwd; sorted by `last_used` desc | navigate `targetPath` with `_mode=claude&session_id=<id>` (claude's existing resume contract) |
| Bookmark history | name, stored search, updated time | navigate `targetPath` with the stored `search` verbatim |
| Last session | one card: params pretty-printed, updated time | navigate `targetPath` with the stored `search` verbatim |
| Comments | content, created/updated time, resolved badge, annotated view — read-only log | none |
| Other keys | collapsed raw JSON | none |

Empty sidecar (`{}` or all keys absent) → the per-section empty states, not an error.

## Styling

Follow sibling templates (`tree`): dark theme, `ui-monospace` stack, `#E5FF44` accent,
`#131417` background, same status/error patterns. Self-contained CSS in the file.

## Error handling summary

| Failure | Blast radius |
|---|---|
| Sidecar missing | full-page friendly empty state |
| Sidecar unreadable (IO) | full-page warning |
| Whole-file JSON invalid | full-page warning + raw text |
| One key fails schema | that section only: warning + collapsed raw of that key |
| Unknown top-level key | "Other keys" section, no warning |
| Navigation target missing on disk | shell's own not-found handling; template doesn't pre-check |

## Testing

- `tests/test_history_template.py`: registry contains the `.html.json` key with
  `history` first and `history` appended to `.html`; `templates/history/template.html`
  and `icon.svg` exist; template references no `.py` (guards the no-subprocess claim).
- Manual/Playwright pass in the running app (dev-flow gate): open `examples/sine.html`
  → switch to history mode; open `examples/sine.html.json` directly; corrupt one key in
  a scratch sidecar and confirm only that section warns; click a claude session and land
  in the resumed chat.

## Decisions log (to fold into DECISIONS.md)

- History is a template, not shell code — same posture as annotate-v2 (#58).
- `.html` only for now; broaden by adding registry keys later.
- Comments are display-only; no URL synthesis for logged comments.
- Validation schema lives inline in the template; hand-rolled subset validator, no vendor lib.
