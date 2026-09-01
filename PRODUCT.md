# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Developers building custom views** — author `.html` + `.py` interactive pages as mini-apps, using the injected runtime (`fused.runPython()`, URL-synced params, file IO).
- **General power users** — want richer file browsing/preview than Finder/Explorer for their whole machine.
- **General users running fused-apps** — non-authors who use pages others built as small local tools for everyday jobs.

## Product Purpose

A local file explorer for the whole computer, in the browser. Browse any directory, preview files richly, and author interactive views: any `.html` file gets an injected runtime that can call a local Python `main()` and sync state to the URL. Success is fast navigation, format-appropriate previews, and bookmarkable/shareable-with-self URLs that fully reconstruct view state.

## Positioning

Two claims, held equally (user-confirmed):

1. **Renderable HTML system** — any `.html` can call local Python inline and sync its state to the URL; the built-in preview templates are the same primitive, not a separate engine.
2. **Fully local, no cloud** — the entire computer browsable at `127.0.0.1`; no accounts, no tokens, no sandboxing of the user's own trusted code. Publishing delegates entirely to the `fused` CLI run by the user (the app no longer brokers deploy or sign-in).

## Operating Context

- Launched via `fused-render` CLI or packaged desktop app (macOS DMG/cask, Windows, Linux); opens a browser tab at `http://127.0.0.1:1777/`.
- `--start-dir` is a UI convenience only — the whole filesystem stays browsable (FS-3).
- OS integration: Windows Explorer right-click "Open with"; macOS app bundles the `fused` CLI and rclone.
- Governing documents live in-repo: `SPEC.md` (living requirements, numbered FS-*/§ decisions), `ARCHITECTURE.md`, `DECISIONS.md` (D-numbered). Design work must respect decided interaction rules there (e.g. FS-5/D460: plain-press release opens, modified press selects — every row, every listing, every width).

## Capabilities and Constraints

- Explorer shell (React/Vite frontend in `frontend/`), FastAPI/uvicorn Python server, previews run in plain same-origin iframes with injected runtime JS.
- Built-in preview templates for parquet, CSV, images, video, PDF, etc.; template registry maps extension → template HTML.
- In-folder recursive filename search (§22); folder-scoped split preview pane (FS-10/FS-11, D460); sortable listings with sort state in URL.
- Server binds `127.0.0.1` only, never `0.0.0.0`. Protecting against other websites driving the server is in scope; sandboxing the user's own code is not.
- v1 is read/preview oriented — no file editing. No cloud, multi-user, or auth (non-goals).
- Python 3.11+ for the pip path; packaged apps need no Python.

## Brand Commitments

- Name and identity tied to **Fused** (fused.io) — keep naming, voice, and brand association consistent with the parent brand.
- **Fused Lime (#E5FF44) is the binding brand accent** (user-confirmed, 2026-09-01).
- **Standing visual preference: the category standard, played straight** — modern dev-tool canon executed at full craft, benchmarked against Linear, Vercel dashboard, and Raycast (user chose canon over conceptual directions, 2026-09-01). No irony, no smuggled quirk.
- Dark + light themes are contractual: every color a token with a light counterpart (`tests/test_theme.py`). Current shell tokens are evidence, not law.

## Evidence on Hand

- Real product screenshots/GIFs in `docs/screenshots/` (e.g. `open_with_right_click.gif`).
- Shipped, downloadable product: https://render.fused.io and GitHub releases (DMG, wheel, cask).
- No testimonials, case studies, or benchmarks on hand — do not fabricate any.

## Product Principles

1. **Local is the feature** — every design choice should read as "your machine, your code, no cloud"; never imply accounts, sync, or hosted services.
2. **One primitive, everywhere** — previews, templates, and user apps are all renderable HTML; design should expose that uniformity, not hide it behind special cases.
3. **URL is the state** — any view worth looking at is worth bookmarking; interactions that change state should reflect in the URL.
4. **Serve both authors and users** — developer-authors need legible primitives and docs; app-users need the tool to feel like a finished small app, not a dev harness.
5. **Decisions are written down** — SPEC/DECISIONS govern interaction behavior; design work cites and preserves decided rules rather than re-deciding them.
