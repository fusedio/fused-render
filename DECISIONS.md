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

## Descoped / follow-up list (recorded, not built)

Security layer (token, Origin/Host validation, sandboxed bridge — see threat note SPEC §9; X-Fused preflight guard shipped, D36) · user template overrides (`~/.fused-render/templates/` checked before builtins) · per-directory templates · warm worker pool · DataFrame/Arrow returns · WebSocket/SSE push · exec console + structured logging · caching · search/sort/tree/keyboard-nav/hidden-file toggle · editing beyond code_template (text/markdown buffers, new-file/rename/delete APIs) · pushState opt-in · declarative param binding. *(Built since this list was written: M2 template set, file editing — D37.)*

## Open items (small, non-blocking)

- Shell visual design: v1 = clean minimal; real design pass later.
- `fused.ready()` promise: not needed in v1 (params readable synchronously from parent URL).

## How to continue this project

1. Read `SPEC.md` (what & why), `ARCHITECTURE.md` (exact contracts), this file (decisions).
2. M1 definition-of-done = ARCHITECTURE §9 verification checklist.
3. Don't re-litigate decisions D1–D18 without the owner asking.
4. After M1: pick from follow-up list with the owner.
