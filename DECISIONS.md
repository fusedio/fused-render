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

## Descoped / follow-up list (recorded, not built)

Security layer (token, Origin/Host validation, sandboxed bridge — see threat note SPEC §9) · remaining templates (csv, json, markdown, media, pdf, syntax-highlighted code) · user template overrides (`~/.fused-render/templates/` checked before builtins) · per-directory templates · warm worker pool · DataFrame/Arrow returns · WebSocket/SSE push · exec console + structured logging · caching · search/sort/tree/keyboard-nav/hidden-file toggle · file editing · pushState opt-in · declarative param binding.

## Open items (small, non-blocking)

- Shell visual design: v1 = clean minimal; real design pass later.
- `fused.ready()` promise: not needed in v1 (params readable synchronously from parent URL).

## How to continue this project

1. Read `SPEC.md` (what & why), `ARCHITECTURE.md` (exact contracts), this file (decisions).
2. M1 definition-of-done = ARCHITECTURE §9 verification checklist.
3. Don't re-litigate decisions D1–D18 without the owner asking.
4. After M1: pick from follow-up list with the owner.
