# fused-render — Decision Log & Project Context

Purpose: this file + `SPEC.md` + `ARCHITECTURE.md` carry the *entire* design conversation.
A fresh session should be able to continue the project from these three files alone.

---

## What we are building (one paragraph)

A fully local file explorer: a Python server on `127.0.0.1` + a browser UI to browse the **entire computer's** filesystem and preview files. The core primitive is **renderable HTML**: any `.html` file previews live and gets an injected JS runtime with (a) `fused.runPython(pyPath, params)` — executes the `main()` function of a local Python file and returns its JSON result — and (b) `fused.params` — string key/values synced into the browser URL, so any view's state is bookmarkable/refreshable. **Preview templates** (parquet table, image, text …) are just renderable HTML files shipped inside the app that receive the target file as a reserved `_file` param — the same two primitives are the entire rendering power of the system. Local only, forever; never cloud.

## Project timeline / conversation state

- 2026-07-03: initial spec drafted (SPEC.md), broad v1 with security layer, decorators, worker pools.
- 2026-07-03/04: extended design discussion trimmed it down hard (all decisions below).
- 2026-07-04: M1 scope locked; blueprint written (ARCHITECTURE.md); build delegated to a subagent with the main session architect-reviewing.
- 2026-07-04 (later): M1 shipped + verified; M2 sidebar/bookmarks shipped; dark theme, sortable listing, `_file` URL cleanup, no-cache, shell.js ES-module split (D23–D28). An authoring skill for agents lives in `skills/fused-render-authoring/` with an eval workspace beside it.
- Pre-existing partial code: `fused_render/_child.py`, `fused_render/executor.py`, `fused_render/__init__.py`, `pyproject.toml` (written before discussion finished; kept — they match the final design).

## Decisions (all explicitly made by the project owner)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D1 | Python entry point | Function named `main()` — plain convention, no decorator, no import | Rejected `@render` decorator: convention simpler, file works standalone |
| D2 | Filesystem scope | **Whole computer. No root-scoping concept at all.** `--start-dir` is only the initial UI location | Rejected serve-root model entirely |
| D3 | Security | **None in v1.** Only freebie kept: bind 127.0.0.1. Token auth, Origin/Host checks, sandboxed iframe = documented follow-ups (SPEC §9 keeps the threat note for later) | Owner: "forget about security, make it as simple as possible; all things are follow-ups" |
| D4 | HTML isolation | Originally chose sandboxed iframe + postMessage bridge; **superseded by D3** → plain same-origin iframe, runtime fetches server API directly | Bridge protocol deleted from scope |
| D5 | Python execution | Fresh subprocess per call, 30 s timeout | Rejected warm pool (later upgrade, API-compatible) and in-process (crash risk) |
| D6 | Return types | JSON-native only; DataFrame/bytes → clear error telling user to convert | Arrow/DataFrame encoding = follow-up for data-heavy templates |
| D7 | Param values | Strings only. `set()` rejects non-strings. User JSON-encodes himself | Rejected auto-typing (footguns) and JSON escape hatch (unneeded now) |
| D8 | Param↔URL sync | URL wins on load; `history.replaceState` always; no history entries from param changes | pushState opt-in possible later without API break |
| D9 | Py path resolution | Relative → against the HTML file's dir; absolute → anywhere on disk (consistent with D2) | |
| D10 | Re-run on param change | Manual: author calls `runPython` inside `params.onChange` | Declarative binding = possible later layer |
| D11 | Template registry | **Server-side** (single source of truth). `stat` response carries `template` field; shell obeys | Rejected shell-side JS registry (migration pain once user overrides exist) |
| D12 | Shell dispatch | Exactly three-way: **template > html > fallback**. Shell has zero file-type special cases; image & text are ordinary templates | Owner explicitly moved image/text out of the shell into templates |
| D13 | M1 template set | parquet (paged table via pyarrow) + image + text. All other formats follow up | |
| D14 | Python env | The env the server was launched from. Good enough for v1 | Per-project venv detection = maybe later |
| D15 | Streaming results | Not required. Plain request/response | |
| D16 | Frontend stack | No framework, no build step; plain JS/CSS | Descoped Vite/React from original spec |
| D17 | Error DX | runPython failure the page doesn't catch → red traceback overlay auto-shown in iframe; python `print()` echoed to browser console | Architect default, unobjected |
| D18 | pyarrow | Normal dependency (not extras) | Simplest install UX; revisit if weight bothers |

### M2 decisions (2026-07-04, post-M1)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D19 | Next feature | Left sidebar: Home entry + bookmarks of the current right-side view | Owner-defined scope |
| D20 | Bookmark semantics | Save the **exact URL verbatim** at capture time (incl. all params); click = plain redirect; renamable + deletable | No structured bookmark model — URL is the whole state by design (PR-1/PR-7) |
| D21 | Bookmark storage | Browser localStorage (`fused.bookmarks`) | Owner chose simplest over server-side JSON file; export path trivial if migrated later |
| D22 | Bookmark naming | Default = basename of viewed path; rename covers the rest | Rejected params-in-name (noise) and prompt-at-create (friction) |

### Post-M2 refinements (2026-07-04, all owner-directed unless noted)

| # | Decision | Choice |
|---|---|---|
| D23 | Look | Dark theme everywhere; single palette via CSS vars; "Raw" header action removed (fallback card keeps a Download link) |
| D24 | Bookmark UX | "+ Bookmark" text button (not a star glyph); hover card shows target path + saved params; active bookmark + starred button highlighted (architect) |
| D25 | Listing sort | Sortable columns; sort state in URL params, dirs group first (URL-is-state philosophy) |
| D26 | `_file` plumbing | `_file` rides on the template iframe's own URL, not the shell URL (no path duplication); runtime falls back to shell URL for manual/legacy links |
| D27 | Caching | `Cache-Control: no-cache` on every response — stale JS caused half-old-UI bugs |
| D28 | Frontend structure | shell.js split into ES modules (see ARCHITECTURE §1/§6): one-way deps, pure store/format modules, no build step kept (architect, owner asked) |

### M3 decisions — DMG distribution (2026-07-04)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D29 | Packaging | .app bundles standalone CPython (python-build-standalone); server runs `python3 -m fused_render.app` | Rejected PyInstaller freeze: breaks subprocess executor (sys.executable) + seals env |
| D30 | User py env in app | Bundled interpreter ONLY, no override; pyarrow+pandas preinstalled | Owner chose hermetic-simplest over override-setting; supersedes D14 for packaged app (dev installs keep D14) |
| D31 | Lifecycle | Menu bar icon via rumps: Open / Copy URL / Quit; LSUIElement, no Dock | Rejected fully-headless (invisible process) |
| D32 | Signing | Ad-hoc unsigned for now; signing/notarization hook left in build script | Developer ID ($99/yr) deferred until public distribution |

### M3.5 decisions — packaging rework (2026-07-04, after owner-requested research)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D33 | Bundle builder | **py2app** replaces the hand-rolled shim+tarball assembly | Canonical rumps packager; modern py2app ships a real python interpreter in-bundle so `sys.executable` subprocess executor works; its stub executable gives proper LaunchServices/AppKit process identity — hand-rolled bash-shim launches were the likely cause of flaky NSStatusItem/event behavior under Finder |
| D34 | Dock presence | **Regular app (no LSUIElement)** — Dock icon + menu bar ✦ both | Owner expects Dock presence; Dock right-click → Quit permanently solves lifecycle confusion. Supersedes D31's LSUIElement detail (menu bar item stays) |
| D35 | DMG creation | `dmgbuild` (config-driven) replaces raw hdiutil; **Briefcase external-app mode** = designated future path for sign+notarize+DMG when Developer ID lands | Full Briefcase rejected: its app template breaks sys.executable |

### Editing decisions (2026-07-05)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D36 | Cross-origin POST guard | The two mutating/executing endpoints (`POST /api/run`, `POST /api/fs/write`) require a custom `X-Fused: 1` request header; missing/wrong → 403 | Read endpoints are already safe cross-origin (the browser blocks a foreign page from reading our response), but a POST can be fired blind via a no-cors fetch by any website open in the same browser. A custom header forces a CORS preflight, which fails cross-origin since we send no CORS headers, so only our own same-origin pages get through. **NOT authentication** (D3 stands — no tokens, no user accounts): it only blocks blind cross-origin POSTs; anything that can already run our JS is unaffected. Cheap and removable if the descoped security layer lands. |
| D37 | File editing | `POST /api/fs/write` (atomic temp-file + `os.replace`, preserves mode) with optimistic locking on `st_mtime`; `code_template.html` is now an always-editable CodeMirror buffer (Save / Cmd+S, conflict banner offering Reload/Overwrite) | Supersedes SPEC §1 non-goal "no editing in v1". Optimistic lock (409 on stale/deleted mtime) chosen over no-lock (silent clobber) and over server-side locking (needs state, D-against SV-1). HTML "Source" view reuses the same editable template, so HTML editing came free |
| D38 | Bookmark updating | Opening a bookmark **via sidebar click** arms it (sessionStorage `fused.armedBookmark`); when view params diverge from the saved url on the same page, an "Update bookmark" button appears left of "+ Bookmark" and overwrites the bookmark's url with the current one. Changing pathname disarms **permanently** — only a new sidebar click re-arms (owner-chosen over re-arm-on-return). Shell observes param changes by wrapping `history.replaceState` (the iframe runtime writes params through it; no native event exists) | Keeps D20 verbatim-URL model — update = overwrite url, no structured diffing. sessionStorage so tracking survives refresh but not new tabs |

### Embed mode (2026-07-06)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D39 | Embed mode | `/embed/<path>?params` serves the same shell chrome-free: sidebar/breadcrumb/preview-header hidden via `body.embed` CSS, sidebar never initialized. Router prefix is dynamic (`/embed/` vs `/view/`, fixed at page load), so refresh, in-listing navigation, and param sync stay inside embed. View modules stay embed-unaware — CSS does all hiding | One server route + prefix switch reuses the whole shell; rejected separate embed page (duplication) and query-flag (`?embed=1` would collide with the user param namespace, D7/D8) |
| D40 | HTML view mode param | Rendered/Source toggle driven by reserved shell param `_mode=render\|source` (absent = render, and switching back to render deletes the key). Clicks write it via `history.replaceState` (D8); `?_mode=source` on view/embed/bookmark URLs opens straight into source | URL-is-state (D8/D20/D25): mode was the last piece of view state living only in JS. `_`-prefix already hidden from `fused.params` by the runtime, so no collision with user params |

### M4 — Live editing: autosave + auto-reload (2026-07-06, full spec = SPEC §13)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D41 | Editor autosave | code_template autosaves 250 ms after last edit (debounced), always-on, no toggle. Same optimistic lock as manual save; 409 → conflict banner shows and autosave suspends until resolved — never auto-overwrites. Manual Save/Cmd+S stay and cancel the pending timer | Always-on chosen over a setting: live-preview loop is the point; half-typed states reaching disk are normal (D17 overlay explains broken intermediates). Auto-overwrite rejected — would reduce the lock to decoration |
| D42 | File-change feed + auto-reload | New `GET /api/fs/events?path=A&path=B` SSE endpoint (async 200 ms stat loop, no watchdog dep, no X-Fused — read-only). All reload logic lives in the injected runtime: each rendered page watches its own file + `_file` + every `runPython` file (learned from new `resolved_py` field on `/api/run`, recorded on failures too) and reloads itself on change (300 ms debounce). `fused.autoReload(false)` opts out — the editor uses it; watching starts at DOMContentLoaded so the opt-out is race-free. Directory listings watch their dir and re-render | Full iframe reload over fine-grained re-run: runtime can't replay what a page did with a python result, and URL-is-state (D8/D20/D25) makes reload lossless. Runtime-side dependency tracking over a server-side dep map (server state/staleness for knowledge the page already has) and over static html analysis (misses dynamic paths). SSE over client polling (chatty) and over WebSocket (two-way machinery for one-way events) |

### Bookmark reordering (2026-07-06)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D43 | Bookmark drag-reorder | Native HTML5 drag-and-drop on sidebar rows; localStorage array order **is** display order (no `order` field, no schema change) — `moveBookmark(id, postRemovalIndex)` splices. Whole row draggable (link gets `draggable="false"` so the browser doesn't drag the URL); insert indicator via box-shadow (no layout shift); drag disabled during rename; tooltip suppressed while dragging. `draggedId` reset in `drop` as well as `dragend` — Chrome skips `dragend` when the re-render detaches the source row | Native DnD over vendoring Sortable.js (~40 KB for one short vertical list) and over pointer-event reimplementation (more code, same result). No drag handle — whole row is a bigger target and rows have no competing drag affordance |

### Bookmark folders (2026-07-06)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D44 | One-level bookmark folders | Drop a bookmark on the middle of a top-level bookmark → folder created around both (rename starts immediately). Same localStorage array, folders are `{type:"folder", name, collapsed, children:[bookmarks]}` — old flat data loads unchanged, no migration. Drop zones: 25/50/25 (above/into/below) where combine is possible, half-split otherwise; folders reorder at top level only, never nest; child rows ignore folder drags. Empty folders auto-pruned after every mutation (move, delete, create). Collapse persisted. Folder UI: inline-SVG folder icon in the star-glyph slot (icon + name toggle fold, no chevron), semibold name, child-count pill pinned far right that swaps for the hover actions, children indented behind a 1 px rail; collapsed folder holding the active bookmark gets the active highlight | One level deep over arbitrary nesting: schema already recursive so depth is pure UI work later, but nested drop-target semantics (cycle guards, indent-sensitive zones, collapsed-edge ambiguity) ~2.5× the cost for a rarely-needed depth. `children` array over flat `parentId` (rendering + reorder messier). Chevron removed after review — icon alignment with star glyphs reads better in a narrow sidebar; count pill carries the collapsed signal |

### M5 — Layout mode: split panes (2026-07-06, full spec = SPEC §14)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D45 | Layout mode | `/view/_panel?_layout=<tree>&<merged params>` (also `/embed/_panel` — layout mode works under both prefixes, so a layout can itself be embedded) — sentinel pathname intercepted by the shell `route()` before stat (zero server changes). Pane tree in reserved `_layout` param: `,` = row, `;` = column, `()` nesting; each segment = pane fs path + optional pane-local query, delimiter chars percent-encoded. Panes are `/embed/<path>` iframes (D39) with a pane bar: crumbs, split right/down (duplicates pane), maximize (transient, not in URL), close (last close exits to `<prefix>/<pane path>` in the active prefix). Entry = "Split" button in crumb-actions carrying current path+params. Layout view observes pane URLs (iframe load + pane `fused:urlchange`) and re-encodes `_layout` via replaceState — so bookmarks (D20) and the update-bookmark flow (D38) capture layout+params verbatim with zero bookmark-layer changes. New module `views/panel.js` (imports router only) | Reusing embed pages makes every pane a full navigable mini-explorer for free; rejected panes-as-bare-`/render`-iframes (loses listing/navigation/templates dispatch). Sentinel path over a new top-level route (`/layout`): stays inside the `/view`+`/embed` prefixes the server and router already handle. Sentinel pathname `_panel`, tree param `_layout` (sentinel renamed same-day from `_layout`, no backward compat kept). Reserved `_layout` param over `?layout=`: `_` prefix is already hidden from `fused.params` (PR-6), no user-param collision |
| D46 | Runtime param target | `fused.params` reads/writes the **topmost same-origin ancestor** window's URL (was: direct parent) — in normal view/embed, parent = top, so nothing changes; in layout mode all panes share the layout URL, so param merging and same-key cross-pane sync are structural. Notification: shell wraps pushState too (was: replaceState only) to dispatch `fused:urlchange`; runtime listens on the target window and notifies `onChange` only when the non-reserved param snapshot actually changed (diff guard kills loops/dupes; `set()`'s direct notify removed in favor of the event path) | Chosen over a manual sync layer in the layout module (watch each pane URL, merge up, propagate down, loop-guard — more code, more failure modes). Trade-off accepted: params are global session state by definition; pages wanting pane-private state namespace their own keys (documented, not enforced) |

### Post-M6 audit cleanup (2026-07-06)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D49 | Layout-family naming + codec ownership | Vocabulary line drawn: **"layout" = the family** (the `_layout` param, `views/layout-codec.js`), **modes name themselves** (`renderPanel`/`stopPanel`/`panelUrl`/`.panel-*`/"Panel" label; tabs already consistent). Codec owns everything both modes share: the `_layout` codec, embed URL helpers, and the `fused:urlchange` hook (`attachEmbedUrlChange`/`detachEmbedUrlChange`, one expando `_fusedUrlHooked`); it imports `router.js` only (for the single `EMBED_PREFIX` — its duplicate + dead export deleted). Breadcrumb imports the segment encoder from the codec directly (panel.js re-export removed). Panel pane DOM now built via `createElement` like tabs (drops the escapeHtml/innerHTML template) | Audit findings (architect + reviewer, both flagged the re-export and the duplicated hook machinery). Rejected from the same audit: pane registry Map over DOM `data-id` lookups (vanilla idiom, indirection for nothing); centralizing the `_fusedParamBoundary` flag outside tabs.js (would leak tab knowledge into main.js; contract already documented in runtime/tabs/ARCHITECTURE) |

### M6 — Tab mode + folders-as-tabs (2026-07-06, full spec = SPEC §15)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D47 | Tab mode | `/view/_tab` + `/embed/_tab` sentinel (same interception as `_panel`, zero server changes); tab list = flat top-level `,` row of the same `_layout` codec (nested structure defensively flattened to leaves on parse). Tabs are `/embed/<path>` iframes, **lazy-mounted on first activation, kept alive hidden** — state survives switching, hidden tabs stay param-synced (runtime listens on top window, D46). Params are **tab-independent — no merged pool** (inverts D45's pooling for this mode): the tab shell sets `window._fusedParamBoundary = true` and the runtime climb (D46) stops below a boundary-marked ancestor, so each tab's pages target their own embed URL; the full pane query (user params included) is captured segment-local by the ordinary `_layout` sync. A segment path may itself be `_panel`/`_tab` — nested layouts flow through the ordinary embed pipeline; a nested panel keeps merged-pool semantics among its own panes (climb stops at the panel shell) while isolated from other tabs. Tab bar: label = basename of live path, per-tab ×, trailing `+` (new tab at start dir); **active tab NOT in URL** (refresh/bookmark restores first tab). Last tab close exits to plain view. Codec extracted to shared `views/layout-codec.js` (panel.js, new tabs.js, breadcrumb.js import it) | Keep-alive over recreate-on-switch: preserves scroll/editor state; hidden tabs stay live for free. Tab-independent params (owner call, reversing the initial merged design): a folder of bookmarks must reproduce each bookmark exactly — pooling would let the last child clobber same-key siblings; the boundary flag keeps the isolation structural (one runtime `if`, no sync layer). Active tab left out of URL: encoding it would flash "Update bookmark" on every switch (churn > value). Reusing the `_layout` param + codec over a new `_tabs` param: one codec, one escaping story, and panel↔tab URLs stay hand-convertible |
| D48 | Folder opens as tabs | Clicking a bookmark folder's **name/row** composes and opens `/view/_tab?_layout=<children>`: each child's **entire saved query stays segment-local** (D47 — every bookmark keeps exactly its own params, no hoisting, no collisions). Opening also expands a collapsed folder (sidebar mirrors the tabs); the **folder glyph** keeps the plain collapse/expand toggle (was: name and glyph both toggled). Folder click arms nothing (a folder isn't a bookmark); ★ Bookmark saves the composed URL as a normal bookmark, which then gets the full D38 update flow | Folder-as-dashboard was the original ask behind tab mode; a tab must reproduce its bookmark verbatim. Rejected: breadcrumb "Tabs" entry button (folder-only entry, owner call); encoding folder identity in the URL (a tab layout is a value, not a live link to the folder — later folder edits don't retro-sync, by design) |

### M7 — Custom template overrides (2026-07-06, full spec = SPEC §16)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D50 | User template registry | Self-contained template folders `~/.fused-render/<name>/template.html` (+ sibling `.py` readers/assets; name = arbitrary label) bound to extensions by `~/.fused-render/registry.json`: dotted keys → folder name or `null` (`{".parquet": "geo", ".tar.gz": "archive", ".png": null}`). `null` disables the built-in (shell raw/metadata fallback). Matching: longest-suffix, case-insensitive (dotted keys give compound-extension support the built-in `splitext` table can't express); precedence registry > built-in; `.html`/`.htm` exempt (renderable HTML stays the core semantic). Registry read per resolution (no restart); missing dir/file = no-op; invalid entry (bad JSON, unsafe name, missing template.html) → built-in fallback + `template_error` on stat. Unregistered folders inert (drafts). Server-only change — shell already obeys stat's `template` path, `/render` renders any path, M4 auto-reload gives the live template dev loop for free. Companion repo skill `fused-render-custom-templates` documents layout+registration and delegates authoring to `fused-render-authoring` | Hybrid chosen over pure convention dir (folder-name-as-extension dead-ends on many-ext-to-one and edit-in-place) and over manifest of absolute paths (loses the self-contained folder unit). Folder names as binding (`mp4.mov.webm/`) rejected: renames to change mappings, conflict rules. Convention fallback (unbound folder named like an ext auto-binds) rejected: two sources of truth, deleting a registry line wouldn't unregister. Name validation is correctness, not auth (D3 stands) |

### Layout URL grammar — parenthesized `_layout` (2026-07-06, spec = SPEC §14.1 LM-2)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D51 | `_layout` scope grammar | The entire `_layout` value is **parenthesized and emitted last**: `?global=1&_layout=(/a.html?x=1&y=2,/b.html?z=3)`. Parens delimit param scope visually (inside = iframe-local, outside = global) and structurally: **`&` is literal inside them** — segment queries read exactly as written, no `%26` soup. Wrap is codec-transparent (`parseLayout` already reads `(A,B)` as `A,B`); only the URL layer changed. All shell-query reads go through `layout-codec.js` `splitShellSearch` (balanced-paren scan; safe — literal parens inside segments are codec-escaped, so span parens are structural and balanced): panel.js, tabs.js, breadcrumb's bookmark-equality `sameSearch`. The runtime (standalone, no imports) duplicates the scan as `splitSearch`: get/getAll parse only the non-layout remainder; `set()` reinserts the raw span untouched and last — also fixes the old wart where `URLSearchParams.toString()` percent-mangled a readable `_layout` on the first `fused.params.set()`. **Strict read** (owner call): unwrapped `_layout` = not this grammar, reads as absent, key dropped on next sync — old-format bookmarks fall back to a single start-dir pane/tab. **Paste breakage accepted** (owner call): auto-linkers may eat the trailing `)`; unbalanced span = invalid layout (span still excluded from params), missing-layout fallback | Chosen over "enforce `_layout` last as position-only convention" (look without guarantee; `&` stays escaped) and over "raw tail after `_layout=`" (position-dependent; trailing hand-added `&debug=1` silently swallowed into last segment — parens make position a convention and the boundary explicit, params after `)` stay global). Lenient read of old grammar rejected (owner call): one grammar, no two-format parse paths. Auto-closing missing trailing parens rejected with it: same leniency, masks truncation |

### Scientific-file templates + directory templates (2026-07-06)

| # | Decision | Choice | Rationale / rejected alternatives |
|---|---|---|---|
| D52 | GeoTIFF / NetCDF / Zarr templates + directory-template routing | Three new preview templates adapted from the app's battle-tested standalone sci viewers (geotiff/netcdf/zarr), restyled to the shell palette; input is the reserved `_file` param (drag-drop/URL/help chrome dropped). Decoders vendored as single self-contained ESM bundles under `templates/vendor/` via `scripts/vendor-sci/build.sh` (Node 22 + esbuild, `--format=esm --minify`, **no `--splitting`** so one file per lib; zarrita inlines numcodecs WASM as base64) — no CDN at runtime (D3). `.tif/.tiff → geotiff`, `.nc/.nc4/.cdf → netcdf` join `TEMPLATES`. A **Zarr store is a directory**, so a new `DIR_TEMPLATES = {".zarr": …}` registry is checked by `_template_for` when `is_dir` (basename extension picks the template); `stat` then carries a `template` for the dir, and the shell dispatches on `stat.template` (not `is_dir`) so the store previews like a file. A "Browse contents" action navigates to `?listing=1`, which `route()` honors to force the plain listing so the store's raw members stay browsable. GeoTIFF full-fetches small files and range-requests (`geotiff.fromUrl`) over the raw endpoint for >32 MiB (Starlette FileResponse honors Range); Zarr members are fetched per-key (404 = missing chunk → zero-fill). Directory templates are **package-only** — the M7 user registry (D50) is a per-file suffix match and doesn't extend to directories yet (documented in server.py `_template_for`). | Adapt the standalone viewers verbatim (7 rounds of browser QA) over rewriting decode/render. Separate `DIR_TEMPLATES` over overloading `TEMPLATES` with a dir flag: the file case (the common one) stays a plain dict lookup and the directory case is explicit. Dispatch on `template` not `is_dir` so no per-format logic leaks into the shell (D12 holds). `?listing=1` override kept minimal — one `route()` check + a `navigateUrl` link, zero new server routes |

## Descoped / follow-up list (recorded, not built)

Security layer (token, Origin/Host validation, sandboxed bridge — see threat note SPEC §9; X-Fused preflight guard shipped, D36) · warm worker pool · DataFrame/Arrow returns · WebSocket/SSE push · exec console + structured logging · caching · search/sort/tree/keyboard-nav/hidden-file toggle · editing beyond code_template (text/markdown buffers, new-file/rename/delete APIs) · pushState opt-in · declarative param binding. *(Built since this list was written: M2 template set, file editing — D37; custom template overrides — D50; per-directory templates + sci templates — D52.)*

## Open items (small, non-blocking)

- Shell visual design: v1 = clean minimal; real design pass later.
- `fused.ready()` promise: not needed in v1 (params readable synchronously from parent URL).

## How to continue this project

1. Read `SPEC.md` (what & why), `ARCHITECTURE.md` (exact contracts), this file (decisions).
2. M1 definition-of-done = ARCHITECTURE §9 verification checklist.
3. Don't re-litigate decisions D1–D18 without the owner asking.
4. After M1: pick from follow-up list with the owner.
