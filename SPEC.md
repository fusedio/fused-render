# fused-render — Requirements Specification

**Status:** Living specification, maintained alongside the shipped product.
**Scope:** A fully local, single-user, single-machine app — it runs no cloud service of its own. Publishing a page to a hosted URL delegates to the `fused` CLI (bundled in the macOS app, a pip extra otherwise — §19, §27); see Non-Goals.

---

## 1. Overview

fused-render is a local file explorer consisting of:

1. **A local server** that runs on the user's machine, exposes the file system, executes user-authored Python, and serves the browser UI.
2. **A browser UI** where the user browses their computer's files and views rich previews of them.

The differentiating feature is the **renderable HTML** system: HTML files can call Python functions inline for data, and can sync their internal state to the browser URL via **params**. Built-in **preview templates** for known file formats (parquet, CSV, images, …) are themselves just renderable HTML files that ship with the application — the same primitives (params + Python execution) are the entire rendering power of the system.

### Goals

- Browse the local file system in the browser with fast navigation.
- Preview any supported file format with a rich, format-appropriate UI.
- Let users author their own interactive HTML views backed by local Python code.
- Make preview URLs shareable-with-self / bookmarkable: URL fully reconstructs the view state.

### Non-Goals

- Cloud or remote deployment, multi-user access, authentication/user accounts.
  (Unchanged by §19/§27: deploying delegates to the fused CLI — bundled in the
  packaged macOS app, a pip extra for source/pip installs — and the §27 "Fused
  account" surface manages the *fused CLI's own*
  credentials for those deploys — fused-render itself still has no accounts,
  no tokens, and no server-side users.)
- File editing (v1 is read/preview oriented; editing is a possible v2).
- Sandboxing Python for safety against the *user's own* code — the user's code is trusted. (Protecting against *other websites* driving the server is in scope; see §9.)

---

## 2. Architecture

```
┌────────────────────────────── Browser ──────────────────────────────┐
│  Explorer Shell (app UI)                                            │
│  ├── File tree / directory listing                                  │
│  ├── URL routing  (/view/<path>?params…)                            │
│  └── Preview pane                                                   │
│        └── plain same-origin <iframe> ← renderable HTML runs here   │
│              • injected runtime JS: runPython(), params API         │
│              • talks to server directly via fetch                   │
└───────────────┬─────────────────────────────────────────────────────┘
                │ plain HTTP (localhost)
┌───────────────┴─────────────────────────────────────────────────────┐
│  Local Server (Python)                                              │
│  ├── Static: explorer shell app, injected runtime JS                │
│  ├── FS API: list / stat / raw file streaming                       │
│  ├── Python Executor: runs main() of a .py file in worker proc  │
│  └── Template Registry: extension → preview template HTML           │
└──────────────────────────────────────────────────────────────────────┘
```

- **Server language:** Python (natural fit — it must import and execute user Python). Suggested: FastAPI + uvicorn.
- **Binding:** `127.0.0.1` on a configurable port (default e.g. 1777). Never `0.0.0.0`.
- **Startup:** single CLI command, `fused-render [--start-dir DIR] [--port N]`, opens the browser. Start dir is a UI convenience only — the whole filesystem is accessible.

---

## 3. File Explorer (Shell UI)

### Requirements

- **FS-1** Directory listing with name, size, modified time, type; sortable columns (sort state in URL params `sort`/`order`; **name** sorts purely alphabetically with dirs and files interleaved, **size**/**mtime** group dirs first since a dir has neither meaningfully; dot-entries always last).
- **FS-2** Breadcrumb navigation. *(tree pane, keyboard nav: follow-up)*
- **FS-3** **DECIDED:** the explorer browses the **entire computer** — there is no root-scoping concept. The CLI may take a *start directory* (`--start-dir`, default home) but it is only the initial UI location, not a restriction.
- **FS-4** v1 shows all files including dotfiles. *(hide/toggle: follow-up)*
- **FS-5** Selecting a file opens its preview (§5). Selecting a directory navigates into it. **With the split preview pane on this changes** — the plain single click selects and drives the pane instead of opening, and double-click opens; see FS-15.
- **FS-6** The current directory/file is reflected in the URL path so browser back/forward and refresh work: `http://localhost:1777/view/<url-encoded-path>`.
- **FS-7** **DONE (M14):** in-folder filename search over a streamed recursive walk — see §22.
- **FS-8** "Open raw" escape hatch for any file: streams bytes with correct MIME type (used for download and by templates for images/video/pdf).

### Split preview pane (D185)

The listing may show the selected entry's preview beside the list — Finder's list view. This is the one feature the deleted `preview` directory template had and the shell did not (D185); it lives here so it inherits the listing's watch (LS-1), file operations (§24), multi-select, streaming search (§22) and theming (§30) instead of re-implementing them.

- **FS-9** **Default OFF.** With the pane hidden the listing is exactly the listing FS-1..FS-8 describes — no layout change, no extra fetch, no selection semantics change. The pane is opt-in per folder, so nothing about a plain folder view moves for a user who never asks for it.
- **FS-10** **Toggle — one affordance, two places, chosen by state.** *Closing* the pane is a control **on the pane**: the **first** thing in its own header strip, an icon-only "Hide preview" at the pane's left edge, separated from the previewed row's identity by the same `.bar-rule` zone divider the top bars use — left of the rule acts on the **pane**, right of it is about the **row** — on the seam between list and pane, the divider the user drags and the edge the pane collapses toward. Collapsing is a spatial act, so the control sits on the boundary the action happens at (not in the far corner opposite it) and needs no label to explain itself; its glyph points *into* the right edge, which is where the pane goes. *Opening* it cannot live there — a closed pane hosts nothing — so that half stays in the listing's **search row** (beside the in-folder search input, FS-7/§22.3) and stays **labelled**: glyph + "Preview". An icon-only square in that corner would read as one more of the layout glyphs the title bar carries a few pixels above it, and "there is a pane you are not seeing" is exactly the state a user needs told rather than left to infer. The two never coexist — the search-row button unmounts when the pane opens, and the search box (`flex: 1 1 auto`) simply takes the width back. **The pane's header strip is therefore present in EVERY pane state** — loading skeleton, error, metadata card, multi-selection placeholder and the self target included — because while the pane is open that strip is the only thing carrying a way to close it. It is the whole affordance: there is no separate mode, no `_mode` value, and no registry entry for "listing with a pane" — the pane is a property of *how this folder is being viewed*, not a different view of it (which is precisely the distinction the `preview` template got wrong).
- **FS-11** **Selection-driven content.** The pane renders whatever the currently selected row is:
  - a **file** → its default template mode (PT-8/PT-9) in an **iframe**, `/render?path=<template>&_file=<file>`, exactly as the preview view builds it (PT-2) — one code path, so a file previews identically in the pane and full-screen. No mode switcher in the pane; the pane shows the default, and full-screen is a double-click on the row (FS-15). "Default" means PT-9's rule in full, including its tail: the first **unconditional** entry renders immediately (CT-12 — no waiting on a gate), and an **all-conditional** list resolves its gates and shows the first **allowed** one, so the pane never claims "no preview" for a file that opens fine full-screen. A file with no usable entry at all (empty list, or every gate denied/broken — fail closed) gets the metadata card below. **The pane never edits the mode list on account of its own width** (PT-15): it is a narrow host — 220 px at the floor, half the container by default (FS-12) — and a template whose layout needs more than that collapses itself at its own breakpoint, which is why nothing here knows how wide any template wants to be.
  - a **directory** → a **folder peek**, which is *not* a recursive listing-with-a-pane: a folder holding exactly one app entry (D124's `isAppEntry`) embeds that app, and any other folder shows a **read-only mini child list** (names + icons, no sort, no file ops, no search, not navigable). Read-only is deliberate — two live listings on screen means two watch sockets, two selection models and an ambiguous target for a delete, and the pane exists to answer "what is in here" before you commit to going in.
  - **nothing selected** → the **self target**: the pane's subject is the folder *already open on the left*, and it has **no preview at all**. The header carries the folder's icon and its name — no actions: the folder's "Open as app" used to be handed down into this slot, but a folder that HAS an app is not empty, so the auto-select of FS-16 means this row is barely ever on screen, and the button lives in the title bar whether the pane is open or not. The body is the neutral hint `Select a file to preview.` (the `.pane-hint` the empty pane already had). **There is NO mode picker**, and therefore no mode: the self target never builds a mode list, resolves no default, and issues no stat or lone-app probe. The `/` key's peers are heavyweight opt-ins (D235/D237 put the chat on every folder), so a picker here offered a chat on the folder from a header that otherwise said "select something" — a `Choose view` chip pointing at a view nobody came for. The folder's own modes remain one click away on the **left** half (the browse chip / the folder's own view), and every entry in the folder previews on selection; since opening a folder now auto-selects its first entry (FS-16) and clicking the listing background no longer deselects (FS-15), this state is reached essentially only by an empty folder or by a deliberate **Escape**. *There used to be an elaborate rule here instead — drop `_listing`, offer the peers, but land on no mode unless the folder had a lone app of its own, with a pane-only `_none` entry before that. Hiding the picker deletes the question those were answering.*
- **FS-12** **Draggable divider** between list and pane. **The split is a FRACTION of the split container, not a pixel width** — state is `{ on, frac }`, rendered as a percentage `flex-basis`, defaulting to `PANE_DEFAULT_FRAC = 0.5` (half and half). A proportion is what the user actually chose ("about a third of the window to the list"), and it is the only form of the answer that stays right when the window resizes: the previous model resolved a measured half into pixels at mount and then never rescaled, so a pane sized on a wide monitor became the whole view on a laptop. A percentage also needs **no measurement** — it is correct before the first paint, whatever the container turns out to be — so the measuring layout effect and its unmeasurable-container fallback (`PANE_FALLBACK_W = 420`) are both gone. **The pixel floors remain, and they are where the pixels live now:** the drag runs the cursor's distance from the container's right edge through `clampPaneWidth` (pane ≥ 220 px, list ≥ 60 px, the pane's floor applied last so a container too small for both keeps the pane and scrolls the list) and stores `clamped ÷ container width`. **A container too narrow to hold both floors (< 280 px) expresses no split at all** — the clamp returns the pane's floor whatever the cursor does, so the fraction would describe the container rather than a choice (exactly `1.0` at 220 px), and one drag in a narrow pane would leave the list at its 60 px sliver on every window thereafter. There the drag moves nothing and records nothing, though dragging to the edge still closes the pane; above 280 px the ceiling is `(W − 60) ÷ W`, so a real drag can never reach 1. `panew` values at or above 1 are rejected on read for the same reason; `.listing-pane-slot` and `.listing-main` carry the same two floors as CSS `min-width`, which is what holds them when the *window* — not the divider — is what changed. Only a **dragged** fraction persists (FS-13); the default is never written to `panew`.
- **FS-13** **Visibility rides the URL; width does not.** The two halves of pane state are persisted differently on purpose, because they answer different questions.
  - **Visibility → `?preview=true` on the shell URL** (that literal spelling), *plus* the per-folder view state. Toggling writes the param (`replaceSearch`, D8 mechanics); a pane restored from a folder's view state **reflects itself into the URL** so the two never disagree on screen; and on mount the **URL wins** — `?preview` present is authoritative, view state is consulted only in its absence. So the pane is shareable and bookmarkable (SB-2 captures the URL verbatim, and now captures the pane with it) and survives a refresh or a Back into that entry.
  - **Width → per-folder view state only** (`lib/viewstate.ts`, alongside the folder's sort `?sort`/`&order`), never the URL: a bookmark should reproduce *that there is a pane*, not one machine's drag on one window. Two sibling folders keep independent splits. `panew` holds the **fraction** (FS-12) at three decimals — `"0.42"` — and only a **dragged** one is stored; an untouched pane persists nothing. **Legacy pixel values are ignored, not translated:** anything `>= 1` in `panew` was written by the pre-fraction model against a container this window may not have, so it reads as absent and the folder opens at the default until it is dragged again. **`pane` and `panew` are independent keys:** turning the pane off clears `pane` but *keeps* `panew`, so closing the pane and then leaving the folder (or refreshing) does not throw away a split the user dragged.

  **The param is sticky, and only across directories.** `navigate()` carries `preview` onto **directory** targets so moving between folders never silently opens or closes the pane; it is otherwise never changed automatically, and the breadcrumb's `_mode`-preserving branch carries it identically (a hop out of a `graph`-moded folder must not drop the pane). It is deliberately **not** carried onto **file** targets: `preview` is an *unreserved* param name and the runtime's ancestor-climb (D72) exposes shell-URL params to a template iframe as global fallbacks, so a file URL carrying `preview` would shadow a user template's own param of that name. **Residual caveat, stated rather than fixed:** while the pane itself is open the shell listing URL *does* carry `preview`, so a template rendered **inside the pane** (FS-11) sees it through that same climb — a pane-hosted template using `preview` as its own param name is the one live collision, of a piece with PT-9's documented "template params can collide" quirk.

  View state stays best-effort `localStorage`, silent on failure, same posture as the rest of viewstate and AP-1.
- **FS-14** **Skeleton shimmer** while the pane's content loads (the iframe's first paint, a peek's child fetch) — the pane occupies real width the moment it opens, so an empty rectangle would read as "this folder has no preview" rather than "loading". The list never blocks on the pane: a slow or failing pane leaves the listing fully interactive.
- **FS-15** **The pane owns the single click.** A row's plain click means one of two things, decided by the pane:
  - **pane OFF** (the default) — **select and open**, unchanged from FS-5 and from every version of this explorer. Nothing about the classic click model moves for a user who never opens the pane.
  - **pane ON** — **select only**, for **files and directories alike**; the click's whole job is to drive the pane's preview (FS-11), and **double-click** is what opens (navigates into a folder, opens a file full-screen). A folder is deliberately not special-cased: the pane can peek into it, so a single click that navigated would make the peek unreachable for exactly the rows it is most useful on.

  **Clicking the listing BACKGROUND — the empty area below or beside the rows — does nothing to the selection.** Finder deselects there and this listing used to copy it, but the pane changed what that click costs: a stray click in the whitespace of a short listing threw away the row the user was reading and blanked the preview beside it, with no gesture to get it back other than finding the row again. **Escape** remains the deliberate clear, and the right-click background menu is unaffected.

  **`Enter` opens either way** — the keyboard model does not change with the pane, so there is always one binding that opens regardless of pane state. **Shift+click** (contiguous range) and **Mod+click** (toggle + re-anchor) are untouched by all of this: they build a selection and have never navigated.

  **No single/double-click delay timer.** Distinguishing the two clicks by waiting would put a deliberate ~250 ms lag on the pane preview — the one interaction the pane exists to make fast. Instead the first click of a double-click just selects, which is harmless: the pane fetch it starts is superseded (the row's navigation unmounts the listing), so the cost of the extra click is a request that nobody reads, not a wrong view or a flash of one.
- **FS-16** **Opening a folder auto-selects its first entry**, so the pane opens showing something. With the pane on and nothing else claiming the selection, the listing selects the **first row in the rendered order** — file or directory (the active sort's order — the row the eye is already on) the first time the folder's listing settles. Otherwise the pane opened on the folder's own **self target** (FS-11), whose default state is the `Select a file to preview.` hint: a pane that opens empty asks the user to do the obvious thing before it will do anything at all, on a view they opened precisely to see what is inside. **A directory is previewed like any other row** (D240, overturning the original skip-directories rule): selecting one shows the pane's directory PEEK (FS-11), not a navigation, so landing on it is as harmless as landing on a file — and with dirs-first sorts it is the row the eye actually lands on. Only an **empty folder** keeps the self target and its hint, unchanged.

  **It is ONE SHOT PER FOLDER NAVIGATION** (the listing component remounts per path), and it takes **no reading of the selection at all** — the rendered rows are its only input. There used to be a claim it deferred to, the **`?sel` URL param**, which mirrored the lead row into the address bar and seeded it back on load. That param is **gone**: it wrote to the URL on every arrow-key press (against the ~100-writes/30s `history.replaceState` cap the listing's own `types.ts` documents), and what it bought — a shareable link to a *highlighted row* — is not what a folder URL is for. With no claim to defer to, every folder open lands on its first entry. The selection now lives in component state plus the cross-remount recall store, which still carries a click across the provisional→resolved swap. Every other explorer param is untouched (`sort`/`order`, `q`, `preview`, `_panelMode`, `_mode`).

  Three conditions **hold** the shot rather than spending it, because each can still turn into a folder the user is looking at: the pane being **off** (toggling it on later still lands on the first file), **search** mode (the rendered rows are a query's answer, not the folder's, so clearing the query still lands), and a listing that is not **OK** — not merely "not still loading": a failed first fetch settles with zero rows and the dir-watch refetch that succeeds afterwards does not pass back through the loading state, so spending the shot on an error meant a folder whose first request blipped never auto-selected for the whole mount. Two listings never fire at all: an **embedded** one (the pane's own `_listing` mode — no pane to fill), and the **provisional** pre-stat scaffold, whose auto-selection would mount a preview iframe that the swap to the resolved listing tears down and re-issues a beat later. The scaffold holds the layout's shape, not its work; a click the user makes there still previews, and still carries across the swap. Because it is one shot, a dir-watch refresh never re-fires it and a deliberately cleared selection stays cleared — the auto-selection is an opening move, not a rule that the selection must be non-empty.

### Sidebar & Bookmarks (M2)

Left sidebar in the shell, always visible:

- **SB-1** Fixed left column. Top entry **Home**: navigates to `/view/<home dir>` (the user's real `~`, independent of `--start-dir`). `GET /api/config` gains a `home` field.
- **SB-2** **Bookmarks section** below Home. A bookmark captures *whatever the right side currently shows* — directory listing or any preview — as the **exact current URL verbatim** (`/view/…?freq=2.4&_file=…`). Clicking a bookmark is a plain browser redirect (`location.href = url`); the sidebar never interprets bookmark contents, so bookmarks survive future param/dispatch changes.
- **SB-3** Capture UI: a bookmark button in the shell header area, one click, no prompt. Default name = basename of the viewed path (file or dir name). A new bookmark is appended to the end of the top-level list, so the sidebar scrolls the new row into view (`block: "nearest"` — minimum scroll of the sidebar's own overflow container) once it renders; the just-created row is never left below the fold. Scroll fires only for the create that just committed (a one-shot id handed off by the store), not on unrelated later mutations.
- **SB-4** Bookmarks are renamable inline (edit affordance on hover → input → Enter/blur commits) and deletable. No confirm on delete (re-bookmarking is one click). While a row's rename input is open its whole hover-action cluster is hidden (**Save to disk**, **Rename**, **Delete** — folder rows likewise) — the input wants the row's full width, and every one of them fights the edit in progress: save would snapshot the pre-edit name, rename is what's already happening, and delete would destroy the row being named. Commit (Enter/blur) or cancel (Escape) first.
- **SB-5** **DECIDED: persistence = server-side file** `~/.fused-render/bookmarks.json` (D75; superseded the original localStorage store). JSON array `{id, name, url, created_at}` (+ folders, D44); `id = crypto.randomUUID()`. Served by `GET /api/bookmarks` → `{exists, bookmarks, missing}` and `PUT /api/bookmarks` (whole-tree, atomic, last-write-wins); server code lives in `fused_render/shell/`. Frontend reads a synchronous in-memory cache hydrated at boot; mutations await the PUT (no optimistic update); a 30 s poll re-reads the server so another tab's edits converge (D77, eventual ≤30 s, still last-write-wins). **(D104):** the one-time legacy localStorage import has been removed — every pre-D75 install has long since migrated. **(D127):** `missing` is a bookmark-id side-channel — ids whose target is confirmed gone from disk, recomputed fresh on every GET (bounded, concurrent, mount-safe, fail-open) and never persisted/round-tripped through PUT. The sidebar keeps a flagged row's name at its normal color and shows a warning glyph + hover-card note (owner call: the icon alone carries the flag); nothing is auto-removed. The bookmark poll also fires immediately on window focus (in-flight guarded), not just every 30s. Contrast with Recents (§29, D115), which stays hidden-when-missing by deliberate owner choice.
- **SB-6** Duplicate URLs allowed; **names are globally unique, case-insensitive** (D97 — names become `<name>.bookmark` filenames): a colliding create/rename auto-suffixes `-1`, `-2`, ... instead of rejecting, existing duplicates migrate once on GET (oldest by `created_at` keeps its name). Folder names are a separate namespace. List ordered by creation time. *(drag reorder, active-bookmark highlight: polish, later)*
- **SB-7** **DECIDED: bookmark create/update is mirrored into the target file's `.html.json` sidecar** (D83) as `bookmarkHistory` — the same per-file sidecar the `claude` chat template owns via `claudeSessions` (§7). `POST /api/bookmarks/history` upserts an entry by bookmark `id`; the frontend calls it fire-and-forget right after `addBookmark`/`updateBookmarkUrl` commit. A bookmark targeting a layout/tab sentinel or a path no longer on disk records nothing. **Delete never touches the sidecar** — history is permanent, independent of the bookmark's current lifetime.
- **SB-8** **Save to disk**: a per-bookmark button writes a portable `<name>.bookmark` JSON file (format v1: `{version, name, icon?, kind: single|panel|tab, path?, search}`, D98) next to the file(s) the bookmark points at — a single bookmark into its target's own directory (`path` relative to it), a panel/tab bookmark into the deepest common ancestor directory of all `_layout` leaves, each leaf path rewritten relative to that dir (grammar, nesting, per-leaf queries and global params untouched). The button's hover title shows the exact destination path before the click; it is disabled (greyed, explanatory title) when no save target exists — a leaf without an absolute fs path, or no common root. Frontend computes `{dir, filename, content}` (`lib/bookmark-file.ts`); `POST /api/bookmarks/export` validates and writes, overwrite allowed (a re-save refreshes the snapshot).
- **SB-9** **Double-click open** (macOS): the packaged app registers `.bookmark` as an Owner document type (D99); Finder-opening one routes to the `/view/_bookmark?file=<abs path>` sentinel, which reads the file (`GET /api/bookmark-file`), resolves its relative paths against the file's own directory (`lib/bookmark-file.ts` `bookmarkOpenUrl`, the inverse of SB-8's relativize) and `location.replace()`s to the described view — single, panel or tab. Browsing to a `.bookmark` file in the explorer opens it the same way (never a preview). Malformed / unsupported-version files render a readable error, no redirect.

### OS clipboard interop (D203)

⌘/Ctrl+C in the explorer and in the native file manager mean the same thing. The bridge is the **local Python backend** — the server always binds `127.0.0.1` (D2/D3), so it is necessarily on the user's own machine — reached over `GET`/`POST /api/clipboard/files` (`server/routers/clipboard.py`) and implemented behind a platform-agnostic contract in `fused_render/shell/pasteboard/`: `read_files() -> (paths, token, supported)`, `write_files(paths) -> (token, supported)`. Only the three leaf backends know what OS they are on.

- **CB-1** **Copy out.** An in-app **copy** also publishes its absolute paths to the system clipboard as real *file references*, so ⌘V in Finder / Explorer / Nautilus / Dolphin pastes the actual files while a paste into a terminal or editor yields the paths. One hook in `setClipboard` (`lib/fs-clipboard.ts`) covers every Copy call site. It is **fire-and-forget**: the in-app copy has already happened, and a failed or unsupported write must leave behaviour exactly as it was rather than break the gesture (same posture as `copyToClipboard`).
- **CB-2** **Paste in.** Files copied in the native file manager become the app's clipboard on the next **return to the app** (`useRefreshOnReturn`, plus one read on mount since that hook skips mount), so Paste drops them into the current directory. The adopted clipboard is written into the **existing module clipboard store** as a second writer — every Paste site, `disabled: !clipboard` check and cut-dimming keeps working untouched — and the paste itself is the unchanged server-side `/api/fs/copy`: no bytes cross the browser, folders included, instant at any file size. A copy made while the app is already focused is not seen until the next focus change.
- **CB-3** **The token is a content fingerprint** of the ordered path list (sha256 of the NUL-joined paths), identical on all three platforms — macOS has `changeCount` and Windows `GetClipboardSequenceNumber`, but Linux has no analog. The frontend tracks it as **last seen**, not last written, so an untouched system clipboard never clobbers a pending in-app **cut**. Re-copying an identical selection in the file manager is therefore not detected as a new event, which is harmless (adopting the same paths is idempotent). Adoption never echoes back out to the OS, and neither does **bookkeeping** — `clearClipboardIfDeleted` and `remapClipboardPath` repair the app's own reference after a delete or a rename and keep `op: "copy"`, but the system clipboard belongs to whoever last wrote it, so an unrelated file operation must not rewrite it. Both places that touch the clipboard across an `await` (the reconcile's read, the mirror-write's response) capture a **clipboard epoch** first and discard their result if it moved — otherwise a slow read overwrites a copy or cut made while it was in flight, and a late write restores a token the next reconcile then reads as unchanged and skips.
- **CB-4** **Cut is in-app only, both directions.** No platform exposes a reliable cut-vs-copy flag on read, so honouring one would mean deleting source files on a guess; the GNOME format's `cut` verb is parsed and ignored, and an in-app cut is never published.
- **CB-5** **Per-platform mechanism.** macOS: `public.file-url` on the general `NSPasteboard` via pyobjc, written alongside the newline-joined plain-text paths. Windows: `CF_HDROP` via `ctypes` (a wide-char `DROPFILES` block on globally-allocated memory), plus `CF_UNICODETEXT`; paths are backslashed for the OS and returned in the shell's canonical forward-slash form. Linux: `wl-copy`/`wl-paste`, else `xclip`. No new dependencies — pyobjc already ships in the `[app]` extra, `ctypes` is stdlib, and Linux shells out to tools it detects rather than requiring.
- **CB-6** **Documented Linux limitation.** X11/Wayland have no OS-owned clipboard: a live process must own the selection, GNOME (`x-special/gnome-copied-files`) and KDE (`text/uri-list`) disagree on the format, and `xclip`/`wl-copy` publish only one target per invocation. **Reading tries both**, so pasting *into* fused-render works on either desktop; **writing serves whichever family `XDG_CURRENT_DESKTOP` reports**. A resident GTK/Qt owner process offering both targets at once is the documented upgrade.
- **CB-7** **Everything degrades identically.** Missing pyobjc, a hardened sandbox, no `xclip`/`wl-clipboard`, or a failed request all surface as `supported: false` — a normal 200, not an error — and the app keeps today's in-app-only clipboard.

### Quiet control chrome is a TOKEN, not an alpha

The bars' two bordered controls — the labelled mode trigger and the "Open as app" pill — take their border and fill from `--ctl-quiet-*` / `--ctl-plate-bg` (`styles/tokens.css`) rather than from an alpha spelled into the component rule. **The same alpha is not the same contrast in both themes:** `rgba(var(--fg-muted-rgb), 0.35)` over the light bar resolves a hairline slightly darker than `--border`, and over the dark bar it resolves ~30 luminance steps *lighter* than `--border` — because `--fg-muted` is the light ink there. Both controls read as lit pills on the dark toolbar while looking correct on the light one. In dark the border now sits a step above the app's hairline and the mode trigger's fill is transparent (a `--bg` plate under a `--bg-alt` bar was a darker well inside a lighter ring). **The pill is painted by ONE rule for both of its hosts**, the explorer's portaled actions slot and the preview header: the slot's generic `button` rule excludes it by name (`:not(.open-as-app-btn)`) rather than being out-specified, because the re-assert that tried to outrank it — `.preview-actions .open-as-app-btn` at (0,2,0) against the generic (0,3,1) — never applied, so in the explorer bar the button had always drawn as generic chrome while drawing correctly in the header. Excluding beats re-asserting: it removes the fight instead of winning it.

### Keyboard chords match EXACT modifiers

The listing's file-op chords (⌘/Ctrl+C/X/V/D, the ⌘/Ctrl+arrow and bracket navigation, F2, Delete, Backspace) are matched on the modifiers they carry **and the ones they do not**. "At least the primary modifier" is not a match: the handler once tested a lowercased `e.key` against `mod && key === "c"`, which also matched **Ctrl+Shift+C** — devtools' inspect-element on Linux/Windows, and a terminal copy chord besides — and called `preventDefault` on it, so the browser's own binding never fired. The same over-match covered ⌘⇧[ / ⌘⇧] (macOS tab switching), Shift+Delete (Windows Explorer's *delete permanently*) and Alt+F2 (a desktop run dialog on Linux).

**An unmatched chord is left completely alone** — no `preventDefault`, no state change — so whatever else owns it still works. A matched chord with nothing to act on (an empty selection, an empty clipboard) is likewise left alone rather than swallowed. The only chord that *wants* a secondary modifier is ⌘/Ctrl+Shift+N (new folder), and it takes Shift and nothing else. The table is pure and tested (`listing/shortcut-chord.ts`); the hook around it only wires it to the document and asks whether there is anything to act on.

### Server FS API (shape, not final contract)

| Endpoint | Purpose |
|---|---|
| `GET /api/fs/list?path=` | entries with metadata |
| `GET /api/fs/stat?path=` | single-entry metadata |
| `GET /api/fs/raw?path=` | streamed bytes, `Range` support (video/audio seek), correct `Content-Type` |

---

## 4. Renderable HTML

Any `.html` file on disk, when previewed, is **rendered live** (not shown as source) inside a sandboxed iframe. A "view source" toggle shows the raw text instead.

### 4.1 Runtime injection

The server serves the HTML with a small runtime `<script>` injected (or the iframe loads a bootstrap that provides it). The runtime exposes a global API (working name `fused`):

```js
// Execute main() of a Python file
const result = await fused.runPython(pathToPy, paramsObject);          // stale calls to this file auto-cancel (RH-9)
const result = await fused.runPython(pathToPy, paramsObject, { key: null }); // opt out: run fully concurrent

// Read-only file access. `path` is relative to the HTML file's own location or
// absolute (RH-1). A relative path resolves page-relative in BOTH runtimes: locally the
// runtime passes the page's own path as `base` to /api/fs/raw (mirroring runPython's
// `html`); when hosted, the same key hits the bundle's `_asset` route (§18). So one
// `fused.rawUrl("data/" + name)` works everywhere — no local/hosted branch.
const url  = fused.rawUrl(path);        // synchronous URL of the raw bytes (for <img> src, links)
const text = await fused.readFile(path); // fetch the file's text (via rawUrl)

// Params (see §6)
fused.params.get(name)
fused.params.set(name, value)          // strings only; always replaceState
fused.params.getAll()
fused.params.onChange(callback)   // fires whenever params change; author re-runs Python here

// Runtime identity — "local" here, "hosted" on a deployed artifact (§18, RH-10)
fused.env

// Ask an AI model via the local claude (Claude Code) CLI (RH-11). Local-only.
const { text, model, usage } = await fused.ai(prompt, {
  systemPrompt,               // optional system message
  model,                      // optional model id (default claude-haiku-4-5-20251001)
  effort,                     // optional "low" | "medium" | "high" | "xhigh" (default low: no thinking)
  onChunk,                    // optional (text) => {} — streams deltas as they arrive
});
```

- **RH-10** `fused.env` is the **runtime identity**: `"local"` in the fused-render app,
  `"hosted"` on a deployed/exported artifact (set by the fused wheel's serve runtime,
  §18). It lets a page branch on where it runs — gating any local-only behaviour when
  `fused.env === "local"` and degrading gracefully when `"hosted"`. Both runtimes expose
  it, so the check is a positive signal, not the absence of an API.
- **RH-11** `fused.ai(prompt, opts?)` asks an AI model through the shell: the server's
  `/api/ai` runs one completion through the **`claude` (Claude Code) CLI** — the user's
  existing Claude Code login is the credential; no API key or proxy to configure
  (`FUSED_RENDER_CLAUDE_BIN` overrides the binary; default is `claude` on PATH). The
  CLI runs as a pure one-shot completion (no tools, no settings/CLAUDE.md, no session
  persistence, one turn). Resolves with exactly this shape — the server normalizes it,
  so a page may read the fields without guarding:

  ```json
  {
    "text": "the completion",
    "model": "claude-haiku-4-5-20251001",
    "usage": { "input_tokens": 544, "output_tokens": 73 }
  }
  ```

  `text` string; `model` the **full model id that ran** (an alias request like
  `"sonnet"` echoes the resolved id); `usage` either `null` or exactly
  `{input_tokens, output_tokens}` (integers, **Anthropic-style names** — NOT OpenAI's
  `prompt_tokens`/`completion_tokens`). Rejects with
  a structured error carrying `.type` — `"bad_request"` (empty prompt / bad options),
  `"ai_unavailable"` (claude binary not found or not runnable — the message names what
  to install/set), `"ai_error"` (the CLI exited nonzero, reported an error, or returned
  an unexpected shape), or `"timeout"` (no answer within 600 s). `opts.effort`
  (`"low" | "medium" | "high" | "xhigh"`, **default `low`**): `low` — and an
  omitted effort — means **no extended thinking**, enforced with a thinking-budget
  clamp that works on every model; `medium`/`high`/`xhigh` pass through to
  **Claude Code's own effort semantics** (the same setting as the interactive
  `/effort` command) — effort-capable models (sonnet/opus class) honor it, while
  haiku (the default model) ignores effortLevel, which is exactly why the low
  path uses the budget clamp instead. Calls are accepted concurrently but
  **serialized** through one shared CLI process — a second simultaneous call
  waits for the first (a local single-user app; calls complete in seconds). No
  latest-wins channel (an AI call is never a slider scrub).
  **Streaming**: `opts.onChunk(text)` fires per text delta as the model produces it
  (the server relays `{"stream": true}` NDJSON chunks); the promise still resolves
  with the same `{text, model, usage}` at the end, so streaming only changes when
  the text arrives, not what the call returns. Errors after the first chunk reject
  the promise with the same `.type` values. **Warm process** (D168/D169): the
  server keeps ONE persistent claude CLI process and resets it between calls
  (`/clear` wipes the conversation; model/system-prompt swaps ride a control
  request) — its ~2s Node startup is paid once, so every call is warm, and each
  call still sees an empty context: the reset is what carries the isolation the
  old process-per-call design bought.
  **Local-only**: the CLI lives on the author's machine, so the exporter rejects a
  page that calls it (§18.2) — gate with `fused.env === "local"` instead.

### 4.2 `runPython(path, params)`

- **RH-1** **DECIDED:** `path` may be **relative to the HTML file's own location** or **absolute** (anywhere on the machine — whole filesystem is in scope, consistent with FS-3).
- **RH-2** `params` is a flat JSON object; keys map to the Python function's keyword arguments (§5.2).
- **RH-3** Returns a Promise. Resolves with the deserialized return value; rejects with a structured error `{ type, message, traceback }` on Python exception, missing file, missing `main` function, or timeout.
- **RH-4** Concurrent calls to **different** `.py` files are independent (e.g. a page fires 3 data fetches on load); the server may queue or parallelize, and ordering is not guaranteed. Concurrent calls to the **same** file are, by default, a latest-wins channel (RH-9): the newer supersedes the older. A caller that needs several concurrent calls to one file to all complete opts out with `opts.key: null`.
- **RH-5** Calls have a configurable timeout (default e.g. 30 s), after which the worker is killed and the promise rejects.
- **RH-9** **DECIDED (D114, supersedes D113's opt-in):** stale-request cancellation is **on by default**. Every call belongs to a **latest-wins channel**; the default channel key is the **`.py` path**, so firing a new `runPython` for a file **aborts the prior in-flight call for that same file** — a slider scrubbed through many values leaves only the last value's request alive (superseded fetches are cancelled: browser connection freed, and the server drops the now-irrelevant subprocess when it sees the closed socket). The optional third argument `opts` tunes this: `opts.key` (a string) **regroups** the channel (e.g. share one channel across several files, or split one file into several); `opts.key: null` **opts out** entirely (fully concurrent — required for same-file polling loops, per-tile fetches, and writes that must finish); `opts.signal` (a standard `AbortSignal`) **composes** with the channel, aborting the fetch on whichever fires first. A call **superseded** by a newer same-channel call **never settles** — its promise neither resolves nor rejects, so the caller's stale continuation (its `await`/`.then`, even inside a `try/catch`) simply stops and draws nothing; this keeps a scrub silent for every page shape, with no `AbortError` flashing through the page's own error handling while the latest value is still computing. An abort from the caller's **own** `opts.signal` instead rejects with a standard **AbortError** (`DOMException`, `name === "AbortError"`), which the runtime's unhandledrejection handler treats as benign (no overlay per RH-3/D17, no console noise). Applies identically to the hosted/exported runtime (§18).

### 4.3 Isolation — DESCOPED (v1)

- **RH-6** v1 uses a **plain same-origin iframe**; the injected runtime calls the server API directly with `fetch`. No sandbox, no postMessage bridge, no token. Previewed HTML is fully trusted.
- **RH-7** *(follow-up)* Sandboxed iframe + postMessage bridge if/when untrusted-HTML protection is wanted.
- **RH-8** Network access from inside the iframe to the outside internet: allowed.

---

## 5. Python Execution

### 5.1 Authoring model

**Convention over annotation:** a user Python file exposes a function named **`main`**. No decorator, no import required — a plain `.py` file works as-is:

```python
def main(city: str = "oslo", limit: int = 100):
    import pandas as pd
    df = pd.read_parquet(f"./data/{city}.parquet").head(limit)
    return df
```

- **PY-1** When called from HTML, the executor imports the module and calls its `main` with the params. Missing or non-callable `main` → structured error.
- **PY-2** Module top-level code runs on import (normal Python semantics); side effects there are the user's responsibility.

### 5.2 Parameter binding

- **PY-3** The JS `params` object maps to keyword arguments by name.
- **PY-4** Values arrive as JSON types. If the function has type annotations, the executor coerces (`"100"` → `int 100`, `"true"` → `bool`) since URL-derived params are strings. Unannotated args receive the raw JSON value. String annotations count as annotations: they are resolved first (`from __future__ import annotations` / PEP 563 must not turn coercion off), and an annotation that cannot be resolved degrades to "no coercion" rather than failing the run. **One implementation** serves both engines — `fused_render/_binding.py`; the fused engine embeds that file's source into its generated wrapper (its child cannot import the package) rather than restating the rules, so the two can't disagree on the bound values or on the `error.type` a bad param surfaces.
- **PY-5** Extra params not in the signature: ignored unless the function has `**kwargs`. Missing required args → structured error naming the missing arg.

### 5.3 Execution environment

- **PY-6** **DECIDED (v1):** **user** code executes in a **fresh subprocess per call** — always-fresh code, zero stale state, trivial timeout/kill; a crash or `sys.exit` cannot take down the server. Cost: interpreter + import time on every call. A warm worker pool is the designated v2 upgrade if interactivity demands it (API unchanged). **Exception (D72):** an explicit allowlist of first-party helpers (`executor.INPROCESS_HELPERS` — the `duckdb`/`structure`/`xlsx`/`sqlite` readers and the `api` inspector) run **in the server process**, not a subprocess — they are trusted, fast, bounded, and never import/exec user code, and running them in-process means the protected-folder file access they perform reuses the app's macOS TCC grant instead of re-prompting on every call. Everything else stays subprocess-isolated: user code (the `api` Run button, user-authored template readers) **and every other shipped `templates/` helper** (e.g. the `claude/` chat agent, the geo tile servers/browsers), which can be slow/long-running and so must keep the subprocess timeout.
- **PY-6a** **RETIRED (D166).** The subprocess worker used to bootstrap the
  package onto its own `sys.path` (`_child.py` appending the package's parent,
  `executor._child_env()` appending the same value to `PYTHONPATH`). It is
  spawned as a standalone script, so `sys.path[0]` is the package directory
  rather than its parent and `import fused_render` resolves only when the package
  is pip-installed into that interpreter — which is why a first-party helper that
  delegated to the package (the call-log reader read the store through
  `fused_render.calls`) failed there with *No module named 'fused_render'* while a
  stdlib-only helper worked. **Both halves are gone.** No `templates/` file
  imports the package any more (PY-15), and the injection could not be relied on
  regardless: the fused engine's local backend strips
  `PYTHONPATH`/`PYTHONHOME`/`VIRTUAL_ENV` from its children for venv hermeticity,
  so a template leaning on it worked under the built-in executor and silently took
  its fallback branch under the other engine. The worker now inherits `os.environ`
  untouched; a user `.py` that imports the package is reported with the
  interpreter, `PYTHONPATH` and `sys.path[:3]` named, rather than a bare
  ImportError.
- **PY-6b** The allowlist covers **both** the staged core-templates copy and the
  bundled original of each helper. They are the same first-party file; listing
  only one made a run served from the other fall to the subprocess path
  silently — a per-poll spawn for the readers, and (while PY-6a's bootstrap was
  the only way a child could see the package) an outright failure for one that
  imports the package.
- **PY-7** The worker's Python interpreter/venv is configurable; default is the environment the server was launched from. (User installs pandas etc. there.)
- **PY-8** Working directory of execution = the Python file's directory, so relative data paths in user code behave intuitively.
- **PY-9** Module reload: automatic — every call is a fresh process, so edits to the .py file take effect on the next call.
- **PY-15** **A template learns about its environment from the ENV, never by
  importing `fused_render` (D166).** The server exports, **before it starts
  serving** so every child inherits them:
  `FUSED_RENDER_HOME_DIR` (the shell home, **already branch-resolved**),
  `FUSED_RENDER_MOUNTS_DIR` (the mounts root, normalized),
  `FUSED_RENDER_RO_MOUNTS` (`os.pathsep`-joined absolute mountpoints of mounts
  whose remote rejects writes, **re-exported on every mount-store write** so it
  tracks attach/detach/create/delete) and `FUSED_RENDER_ORIGIN` (the origin the
  server is ACTUALLY bound to — see the `--port` hazard in §26) and
  `FUSED_RENDER_SKILL_PLUGIN_DIR` (the Claude Code plugin root holding the
  canonical skills, to hand a spawned session as `--plugin-dir`; **absent** when
  there is none to hand — nothing synced, or a sync that failed — D216).
  `templates/shared/appenv.py` is the **only** sanctioned reader: stdlib-only, no
  `fused_render` import, every value resolved **per call** (a long-lived template
  daemon must see the read-only set change under it). Templates reach it the
  established way, `sys.path.insert(0, <template dir>/../shared)`.
  Only DERIVED ANSWERS cross the boundary — never `mounts.json`, whose schema and
  whose mount POLICY stay in `shell/mounts.py`, so this does not weaken the
  mount-agnostic rule (§26/MD-11): a template may ASK a fact, never branch on how
  mounts work. A site that cannot reach `appenv` keeps whatever fail-closed or
  degrade rule it documents (`_refuse_mounts` refuses; `_sidecar_writable` falls
  back to `os.access`) — and because that is now the primary path when no server
  exported the vars, each one is tested directly. Pinned by
  `tests/test_templates_decoupled.py`, which asserts **zero** `fused_render`
  imports under `fused_render/templates/` (AST, not grep — the word appears
  throughout the prose) with one documented, in-server-only exception
  (`reader/condition.py`'s prefs read).

### 5.4 Return value serialization

**DECIDED (v1): JSON only.** `main` must return JSON-native values (dict / list / str / num / bool / None). Anything else — including DataFrames and bytes — is a structured "return type not serializable" error; the user converts it themselves (e.g. `df.to_dict("records")`).

Deferred to later milestones (needed for data templates):

| Return type | Wire encoding (future) |
|---|---|
| `pandas.DataFrame` / Arrow table | Arrow IPC or `{columns, records}` JSON |
| `bytes` | binary response with declared content type |

- **PY-10** Large results: responses stream; a configurable size cap (default e.g. 100 MB) protects the browser.

### 5.5 Caching — follow-up, not in v1

- **PY-11** Optional per-call cache keyed by `(resolved py path, file mtime, params)`. Opt-in via config (per-directory or global). Keeps re-renders during param tweaking snappy.

### 5.6 Optional fused engine (D69)

- **PY-12** `/api/run` executes the built-in executor **by default**, regardless of whether the `fused` package is importable. `FUSED_RENDER_ENGINE=auto` opts in to running code through its local compute backend (`engine.py`) instead — fresh subprocess per call in a temp exec dir (PY-6 semantics preserved), a script whose folder declares no `pyproject.toml` running on the app's own interpreter (PY-17) and one whose folder does getting that folder's cached venv, built from exactly what it declares (PY-16), params delivered via `_params.json` — falling back to the built-in executor if `fused` isn't importable; `FUSED_RENDER_ENGINE=fused` requires it (startup error if missing); `=builtin` (or unset) always uses the built-in executor (D70). The active engine is reported in `GET /api/config` (`engine`) and logged at startup — the choice changes the code contract, so it is never silent.
- **PY-13** **Code contract under the fused engine:** a function decorated with **`@fused.udf`** — any name, the last decorated one is the entrypoint — receiving params as **raw JSON values** (no annotation coercion; the calling JS owns types); or a plain script assigning **`result = ...`**. A bare **`main()`** remains supported as a compat bridge with PY-4 coercion and PY-8 cwd semantics, so pages and the built-in templates behave identically under either engine. A file with none of the three → the PY-1 structured error, extended to name the alternatives.
- **PY-14** Both engines return **one wire shape** — `{ok, result, error: {type, message, traceback}, stdout}` (the fused engine adds `stderr`/`duration_ms`) — so `runtime.js` and templates never see which ran. Tracebacks under the fused engine point at the user's real file (the source is compiled as its own unit under its own filename); backend/wrapper plumbing frames are stripped.
- **PY-16** A `.py`'s environment is decided by the **folder** it belongs to, never by anything written in the file. The project root is resolved first — the app folder (`<fused_dir()>/<tag>/<name>`), an immediate child of a template root, else the **topmost** ancestor holding a `pyproject.toml` — and that root's `pyproject.toml` `[project].dependencies` is the whole declaration. A manifest that declares **no** dependencies that apply on this platform is not an environment at all and falls through to PY-17 — a bare `uv init` scaffold must not put a script into an empty venv without the bundled stack. Every `.py` under the root shares one venv, however deep it sits; a `pyproject.toml` in a subfolder is **inert** and is surfaced as such (an inert file that looks correct is the failure this rule exists to prevent), and a `# /// script` header is **not read at all** — a leftover block is an ordinary comment, neither honored, merged, nor reported, and a file carrying one runs exactly as it would without it. There is deliberately **no migration tooling and no detection**: this is a clean break in a pre-release product (D233). The venv contains **exactly** what the manifest declares and nothing else — no baseline is unioned in, so it does not contain the rest of the `[bundled]` extra (DM-2), which only the app's own interpreter ships (PY-17). It is built by `uv sync` and stored **centrally** at `<home_dir()>/venvs/<sha256 of the root's absolute path>[:16]`, never inside the user's folder: the folder gains only `pyproject.toml` and `uv.lock`, both source, both git-tracked (MD-7). The path is hashed **as given**, not canonicalised, so moving or renaming a folder yields a fresh environment by design and the orphan is reclaimed by garbage collection at server startup. Staleness is a **digest of `pyproject.toml`** recorded in a `.fused-source.json` sidecar inside the venv — the manifest only, since `uv.lock` is an output of `uv sync` and not an input to it — never an mtime chain — core templates are re-staged with `copy2` on every release, which would make an mtime rule resync byte-identical dependencies at every upgrade. A template that manages its own venv for a daemon declares its dependencies there, not in the folder manifest — that is the only form the built-in engine can honor too (D174). A core template may declare an environment **only if** it is **necessary** (it names something the platform's app interpreter genuinely lacks — judged against the macOS bundle's real contents, not `[bundled]`'s promises, D176), it is **complete** (it covers every such distribution imported by any `.py` under the folder), it has something a `runPython` call site can actually reach, and it ships a committed `uv.lock` so a released build never resolves against PyPI on first render. All of these are enforced by `tests/test_engine_requirements.py`, `tests/test_bundle_contents.py` and `tests/test_template_locks.py`, which derive entry points from the source (`_runpython_targets`/`_module_refs`) rather than from a maintained list (D172, D177; supersedes the per-file header rule).
- **PY-17** A script whose project root declares **no** `pyproject.toml` (or one with no `[project]` table) runs on **the app's own interpreter** and gets no venv at all: the app ships `[bundled]` + its core `dependencies`, so numpy/pandas/duckdb/rasterio/… are available with no download and no first-run wait. The interpreter is **verified, not assumed** — it is run once per server process and must report this app's own `sys.prefix`, probed under the child's stripped environment (the backend removes PYTHONHOME/PYTHONPATH, which a packaged interpreter may need to locate its stdlib). An autodetected candidate whose basename is not python-shaped is rejected without being spawned. If the direct candidate fails, the app generates a **wrapper script** that restores the `PYTHONHOME` this process depends on and `exec`s the real interpreter, then verifies THAT the same way. This is the packaged-macOS path, not an edge case: measured on a real DMG, the bundled interpreter stripped of `PYTHONHOME` reports the *build machine's* Homebrew framework as its prefix, and the bundle ships no `venv` module, so a venv-based rescue is impossible there. The wrapper sets the child's `sys.executable` to itself (`exec -a`), so a daemon re-spawned as `[sys.executable, …]` — geotiff, zarr_aoi, usd — keeps working even though those templates scrub `PYTHONHOME` from the environments they spawn into. Wrappers are POSIX-only and generated **only** when this process actually needs `PYTHONHOME`; Windows and the Linux AppImage self-locate and stay on the direct candidate. If no interpreter can be verified, such a script **fails with a configuration error** naming `FUSED_RENDER_APP_PYTHON` — it is never silently degraded to a venv, because with no baseline requirements that venv has no data stack and would fail on the first import, and because a core template that declares nothing must never reach the network. Nothing in this resolution installs anything. `FUSED_RENDER_APP_PYTHON` overrides the candidate (still probed) (D172, D175).
- **PY-18** A script whose **project** declares something not installed yet gets an **explicit install flow**, never a blocking download inside `/api/run`: the endpoint answers `needs_install` (venv key + the project root, its display name and its declared requirements, alongside a normal `error` object), `POST /api/env/install` spawns a detached worker that runs `uv sync` and writes `{stage, pct, detail, done, error, pid, ts}` to `progress.json`, `GET /api/env/progress?key=` polls it, and `POST /api/env/cancel` stops it by the recorded pid. `runtime.js` shows the loader and retries the run **once**, so every template gets this without its own code; concurrent callers resolving to one project share a single POST, poller and progress row. Installer failures reach the user **verbatim** — uv's own message ("no matching distribution / no wheels with a matching platform tag") is the answer, never a generic engine error. Progress is deliberately coarse (`uv sync` captures its output, so per-package progress is unavailable) and reports only stages it can observe. Scope is **per-folder** (PY-16): one venv per project root, shared by every script in it — the sharing D173 deferred. Once the venv exists the run is handed its interpreter directly, so the environment can live under the app's home dir rather than in the backend's store.

---

## 6. Params & URL Sync

The core state-sharing mechanism between an HTML view and the browser URL.

- **PR-1** The **shell URL** is the single source of truth: `http://localhost:1777/view/path/to/sample.html?city=oslo&limit=50`.
- **PR-2** On load, the runtime hydrates `fused.params` from the shell URL's query string.
- **PR-3** **DECIDED (v1):** `fused.params.set(k, v)` updates iframe-local state and messages the shell, which updates the URL via `history.replaceState` — always. Param changes never create history entries; refresh/bookmark still reproduce state. (`pushState` opt-in is a possible later addition; API shape allows it without breakage.)
- **PR-4** Views must treat params as reactive inputs: `onChange` fires on every applied change (today: `set()` and shell-initiated updates; back/forward too if pushState ever lands).
- **PR-5** **DECIDED (v1): strings only.** Param values are strings, period — `set()` rejects non-strings, `get()` returns strings. Users JSON-encode themselves if they need structure. Zero magic.
- **PR-6** **Reserved namespace:** param keys beginning with `_` belong to the app shell (e.g. `_file`, `_raw`). User HTML cannot set them; the runtime rejects the call.
- **PR-7** Full page refresh reproduces the exact view: same file, same params, same rendered state (assuming user code is deterministic in its params).
- **PR-8** History writes are coalesced (D99): a `set()` takes effect immediately for all readers via a pending-search overlay, but the underlying `replaceState` lands at most once per 400 ms (trailing flush; flushed on pagehide). WebKit throttles history writes to 100/30 s and throws past the cap — scrub-speed param churn in the popover's WKWebView (§25) must never hit it, and a throttle error is caught, never propagated into the calling view.

---

## 7. Preview Templates

Built-in renderable-HTML files that ship **inside the application code**. They are ordinary renderable HTML — same runtime, same `runPython`, same params — proving the primitive is sufficient. Since M8 (template modes) an extension maps to an **ordered list** of templates; each list entry is a **mode** the user can switch between.

### 7.1 Dispatch

- **PT-1** **DECIDED: the registry is server-side** — single source of truth. The extension → template mapping lives in the server; `GET /api/fs/stat` carries the resolved result and the shell simply obeys. *(Originally a single `template: <abs path>|null` field; since M8 the field is the `templates` array of PT-8 — clean break, no compat alias, shell is same repo.)*
- **PT-2** When the user opens `data/trips.parquet`, the shell renders the returned template in the preview iframe and passes the target file as `_file=<path>` **on the iframe's own URL** (not the shell URL — its pathname already names the file, so no duplication like `/view/x.parquet?_file=/x.parquet`). Reserved `_` params are readable by the template, not settable by page code.
- **PT-3** Every template — built-in or user — is a **self-contained folder** named after the template: `fused_render/templates/<name>/` (built-ins) or `~/.fused-render/templates/<name>/` (user, §16), holding `template.html` (required), any sibling helper files (`reader.py`, css, assets), and optionally `icon.svg` (PT-11). Templates render from their real path, so plain **relative** `runPython` paths work unchanged — no virtual-path mechanism needed:

```js
const page = await fused.runPython("./reader.py",
                                   { file: fused.params.get("_file"),
                                     offset: "0", limit: "500" });
```

- **PT-4** Template UI state (current page, selected columns, sort) uses normal params → survives refresh, e.g. `?_file=…&offset=500&sort=fare`.
- **PT-6** **One name-resolution rule everywhere:** a template name resolves to `~/.fused-render/templates/<name>/template.html` if that exists, else `fused_render/templates/<name>/template.html`, else it is unusable (error). A user folder **shadows** a built-in of the same name — the deliberate override channel. The template **name is public stable API**: it is the registry reference, the `_mode` URL value, and the switcher tooltip label. (`fused_render/templates/vendor/` has no `template.html`, so it can never resolve as a template name — the `/template-assets` mount is unchanged.)

### 7.2 Template set — modes per extension

**Shell dispatch is exactly two-way: `templates` non-empty > fallback.** No file-type special-casing in the shell — image, text, and (via the `_render` sentinel, PT-12) HTML handling all arrive through the `templates` list like any other mode. Directories dispatch the same way: every directory resolves through the registry too, so the built-in listing is itself a mode — the `_listing` sentinel (PT-12), default of the universal `/` directory key (D81). A `.zarr` store previews via its `templates` (`["zarr_aoi", "_listing"]`); the map (`zarr_aoi`) is a `condition.py`-gated mode (CT-12), so the built-in listing is the immediate default and the map joins as a peer once its background gate confirms the store (PT-13).

- **PT-7** The built-in bindings live in **`fused_render/templates/registry.json`** (D73) — data, not code, in **exactly the user-registry format** (§16): dot-anchored suffix-pattern keys (compound `.xyz.json`, wildcard `.*.json`, trailing-`/` directory keys — CT-3) mapping to an **ordered list of template names**. Each entry is a **mode**; the **first entry is the default**. One matcher and one value grammar serve both registries; the only asymmetry is precedence (user match wins, CT-3). Rule of thumb: `code` (the editable CodeMirror buffer) appears as a secondary mode only for text formats where raw text is meaningful — never for binary formats (a code view of `.parquet` is garbage).

| Extension(s) | Modes (first = default) | Notes |
|---|---|---|
| `.parquet` | `duckdb`, `structure`, `h3`, `claude`, `versions`, `history`, `geometry_editor` | paged grid + SQL over the file; binary — no `code` mode. `claude` + `versions` is the authored-file pair of PT-14 |
| `.csv .tsv` | `duckdb`, `excel` (`.csv` only), `code`, `claude`, `versions`, `reader` | paged table + SQL over the file |
| `.xlsx` | `xlsx`, `excel`, `reader` | sheet select + paged table. No authored-file pair (PT-14): a spreadsheet is not authored text |
| `.json` | `tree`, `code`, `duckdb`, `claude`, `versions`, `reader` | collapsible tree; the dominant hand-authored config format, so it carries the authored-file pair (PT-14) |
| `.geojson` | `vector`, `map`, `tree`, `code`, `claude`, `versions`, `geometry_editor` | map + tree over the same bytes |
| `.md` | `markdown`, `code`, `claude`, `versions`, `reader` | notes editor (§32) + raw source + the authored-file pair (PT-14): chat about the note with the note itself in the left pane, and this note's own history |
| `.svg` | `image`, `code`, `claude`, `versions` | `<img>` via raw endpoint; svg source is text, so it is authored and carries the pair (PT-14) |
| `.png .jpg .jpeg` | `image`, `photos`, `pano`, `claude`, `versions` | `<img>` via raw endpoint; an image asset is committed and discussed like any other authored file (PT-14) |
| `.gif .webp` | `image`, `photos` (`.webp` also `pano`) | `<img>` via raw endpoint |
| `.pdf` | `pdf`, `pdf_studio`, `reader` | browser-native embed. No authored-file pair (PT-14): a PDF is a published artefact, not authored source |
| `.mp4 .mov .m4v .webm .mp3 .wav .m4a .ogg .flac` | `media` | raw endpoint w/ Range |
| `.py` | `code`, `api`, `claude`, `versions`, `reader` | editable CodeMirror; `api` = swagger-style run form over the `main()` entry point (D63) |
| `.js .ts .tsx .jsx .cjs .mjs .cts .mts .sh .zsh .fish .ps1 .csh .zsh-theme .vim .yaml .yml .toml .ini .cfg .conf .tf .hcl .css .plist` | `code`, `claude`, `versions`, `reader` | editable CodeMirror. `.toml` leads with `canvas` (§28) and `.plist` with `plist`, then the same tail |
| `.txt .log` | `code`, `text`, `claude`, `versions`, `reader` | editable CodeMirror, with the plain `<pre>` view a click behind it; `.log` leads with `log_studio`. `code` outranks `text` on every key that offers both: they render the same bytes, and `code` renders them better |
| `.jsonl .ndjson` | `code`, `duckdb`, `claude`, `versions`, `git`, `reader` | append-only record streams. They carry the authored-file trio like any other text key: PT-14's question is "is this authored", not "does this diff well". The old `git` exclusion here argued that a scoped commit LOG over a stream says nothing a diff can render — an argument about history, and `git` no longer draws history (GT-2) |
| `.tif .tiff` | `geotiff` | GeoTIFF/COG via vendored geotiff (in-browser decode, no reader.py); full metadata + dump, photometric routing (RGB/palette/YCbCr), band select + RGB stretch + colormaps, histogram, hover. Small files full-fetched; >32 MiB range-request `fromUrl` |
| `.nc .nc4 .cdf` | `netcdf` | NetCDF-3 via vendored netcdfjs (HDF5/NetCDF-4 → graceful card); leading-dim sliders, colormaps + stretch, histogram, hover |
| `.zarr/` (directory) | `zarr_aoi`, `_listing` | Zarr v2/v3 store — a *directory*, bound by the trailing-`/` directory key (PT-13). `zarr_aoi` is the server-side AOI tile-streaming map viewer (opened via zarr-python, tiles streamed as PNG); it ships a `condition.py` store-detection gate (CT-12), so it is a conditional peer rather than the immediate default — the built-in `_listing` (PT-12) shows first and the map joins the switcher when the background gate confirms the store. `_listing` also stays reachable as the raw member listing, replacing the old "Browse contents" escape hatch (D81) |
| `/` (any directory) | `_listing`, `app`, `claude`, `versions`, `git`, `graph`, `zarr_aoi` | The **universal directory key** (CT-3) — the built-in default for *every* folder. `_listing` is a sentinel (PT-12), not a template folder: the shell's built-in directory listing (sortable columns, in-folder search, file ops, and the optional split preview pane — FS-1, FS-9..FS-15). Zero segments, so any dot-anchored directory key (`.zarr/`) beats it (D81). Every entry but `_listing` is a `condition.py`-gated peer (CT-12) — the AOI map here is the same viewer the `.zarr/` row describes, offered to *any* folder its store-detection gate confirms — so `_listing` stays the immediate default and each gated peer joins the switcher only where its background gate allows. **There is exactly one chat entry, and as of D237 it is `claude` for both kinds of target** (this key used to carry both `claude_split` and `claude`, the second of which is deleted). Its gate is the *weakest* on this row — any existing directory passes — but it is a real gate and not the "no gate at all" D235 specified: it refuses a **mount-backed** path, because an agent turned loose on an rclone/NFS mount walks and rewrites the tree through FUSE (PT-16, MD-11). The order is deliberate and reads left to right: `app`, `claude` and `versions` are the **app-builder** trio (App.tsx `APP_MODES`) — `app` first because opening an app is landing on the app; then the chat, which is where an app is built; then `versions` ahead of `git`, because for an app folder the version timeline is the answer and the raw commit log is one click further (#361). `app` and `versions` are still narrowed by their gates to a workspace app folder or a registered linked app, so what the explorer offers for an *app* folder is exactly what the app view offers for it; on an **ordinary** folder those two drop out and the chat stays, which is the D237 asymmetry stated plainly (and as of D239 that chat is **full width** on such a folder — it is the one target of this mode with no left pane, PT-16) — a folder's history answer is `git` (the repo-wide Source Control view, §33, directory-only as of D235/GT-2), and `versions` is the single-file timeline. `graph` is MD-2's link graph. The `preview` folder-preview template that also sat here is **deleted** — its split pane is now `_listing`'s (D185) |
| `.html .htm` | `_render`, `code`, `claude`, `versions`, `reader`, `history` (`.html`; `.htm` stops at `reader`) | defaults shipped in the built-in registry like any other key — user-rebindable since D73 (CT-4 revised); `_render` is a shell sentinel (PT-12) rendering the file itself live (§4). A page is authored, so it carries the authored-file pair (PT-14): the chat's left pane renders the page itself and the chat edits it. This is also the key where a `?_mode=claude` link written before D235 works again for free, because D237's rename put the chat back on the file keys (`examples_seed/tutorial/`) |
| unknown | shell fallback | metadata + raw/download link (built into shell, not a template) |

- **PT-14** **ONE chat template serves both kinds of target; only the HISTORY half splits by kind (D235, chat half overturned by D237).** *Original (D235) form: four companion modes split by target kind — a directory offered `claude` + `git`, a file offered `claude_split` + `versions`, with two separate chat templates. **The two-chat premise is void.*** There is now a single chat template, **`claude`** (`claude_split` renamed after the plain full-width `claude` was deleted, D237), and it is bound to **both** the universal `/` directory key and all 47 authored-file keys — the same 48 keys `versions` holds. What still splits by kind is what each history view can DO, not which one you get: `versions` is the history of **any** target in a git work tree — a file, an ordinary folder, or an app — and `git` (the repo-wide Source Control view, §33, GT-2) is the WORKING TREE beside it, so the two are bound as a pair rather than as alternatives (GT-2 overturned the exclusion: `git` draws no commit log any more, so there is no second story to collide with). Two rules, not 47 table rows: the per-extension lists above simply say *which* extensions count as authored files. The **authored-file set** — source, config, prose, notebooks, record streams, tabular data, geo data and image assets, 47 keys — is deliberately withheld from spreadsheets, PDFs, media, archives, 3D and generated tool files: chat and a version timeline are for bytes a human authors or analyses, and those lists are left alone rather than churned. The `/` key's gating asymmetry is the visible consequence of D237: `app` stays narrowed to an app folder or a registered linked app, keeping the app-builder view (App.tsx `APP_MODES`) identical to what the explorer shows for that same folder, while the chat's gate accepts **any** directory and `versions`' accepts any path in a work tree — so an ordinary folder in a repository offers a chat, its history and `git`, and an app folder offers the full trio. The chat's own contract (its gate, its left pane's three shapes, and the system prompt that must agree with the pane) is **PT-16**; what remains here is the `versions` target rule, which is **three kinds**, resolved once per call by `versions.py::_resolve_target` and asked in this order. **App** first — any path inside a fused app or a git-backed linked app — so a file or folder inside an app keeps the app's own timeline (the one the auto-commits actually produced) rather than being demoted to its own log; it is the only writable kind. **The GATE and the MODULE answer different questions here, and that is the mechanism, not a gap.** `condition.py` decides what is OFFERED: a **file** in any work tree, and a **folder** only when it is in a work tree AND has a top-level page by the shared entry rule (`shared/app_entry.entry_html`) — i.e. a folder this app can RENDER. `versions.py` decides what is ANSWERED, and still answers for ANY folder in a work tree, so a hand-written `?_mode=versions` on a page-less folder works and shows its history (MD-11: the gate is the UX, the module is the guarantee). It has to keep answering anyway — an older revision of an html-bearing folder may PREDATE its html, and that commit's snapshot is a browsable tree. The gate briefly offered every folder in a work tree; that put a history mode in the switcher of every directory of every repository the user opens, for a preview that is a listing of a frozen tree — worth having by URL, not worth a mode everywhere. The rule is deliberately the SAME predicate the `app` view and the chat's pane resolve their page with, so "this folder is something fused-render renders" has one answer across the app rather than one per surface. This is also the one place the `git`/`versions` pair stops being symmetric: `git` is the repository's working tree, which every folder in a work tree has, so its gate takes them all; the registry still binds the pair (GT-2), and only the gates differ. Then, for anything else, membership is asked of **git itself** (`rev-parse --show-toplevel`), never of workspace-relative path arithmetic — the discipline `git/log.py` follows, and the only one that answers correctly for nested repos, worktrees and submodules. A **file** scopes its log to that one path (`-- :(literal)<basename>`) and its snapshot materialises just that file. A **directory** — the kind the widened gate created and the module at first had no answer for, so the mode was offered on every folder in a repo and every one of them answered "not inside a fused app folder" — scopes to its own subtree, archived with the **same pathspec-free call an app uses** (`-C <the dir>`; the directory IS the scope, and a workspace app at its repo root is that same call degenerately). **Every kind previews, so the split is the one layout** — there is deliberately no "can this preview" flag and no single-column shape. What differs is HOW a snapshot is framed, and the SNAPSHOT payload names it rather than the page inferring it from the target's kind — because it differs per COMMIT, not per target (an app's entry page can arrive mid-history): `entry` is a page `/render` serves directly (apps only, by `shared/app_entry.entry_html`), `file` is a file target's one materialised file, and **`browse` is the extracted tree**, framed through **`/explorer/embed/<path>`** — the shell's own chrome-free directory listing — so the user browses the folder as it was. A directory always gets `browse` (a folder is not a document, and the explorer's answer for a folder is a listing, not a guess at which page inside it is "the" one), and **so does an app whose tree holds no page**: that case used to answer `entry: None` and draw "this revision has no entry page — nothing to render" over a tree full of perfectly viewable files. The extraction cost for a directory is real — outside an app this is the user's own repository — and is **accepted rather than refused**: it is paid lazily, only for a commit actually clicked, and at most once per commit (a commit is immutable, so the `.fused-snapshot-complete` marker makes every later click a no-op). No size cap, deliberately: app snapshots have never had one, and a limit invented here would mean a folder whose history silently stops previewing at a threshold nobody can see. A commit with **nothing at all** at the path is refused with a sentence, and the check is `ls-tree` BEFORE the archive: an archive of nothing is not an empty tar this code can extract — it is a lone `pax_global_header` plus tar's EOF blocks, which `tarfile.open` rejects with `ReadError`, i.e. the red traceback overlay. That was latent for as long as only apps had snapshots (an app's folder exists in every commit that built it). Both non-app kinds are **read-only**: `versions.py` refuses `revert` there, because a revert commit carries the `Fused <apps@fused.io>` identity and resets the working tree, which is never done to the user's own repository — the same rule linked apps already had (`fused_render/linked_apps.py`). And every kind refuses a **mount-backed** path before it stats anything, matching the gate: git over an rclone-NFS mount stats and lists its way through the work tree, the pattern that wedges a flat million-key S3 prefix. **The snapshot tree itself is read-only, and that is enforced at the mutation boundary, not by the framing template.** A materialised revision (`~/.fused-render/app-versions/<key>/<sha>/`) is `git archive` output — machine-generated history — and `_snapshot` reuses an extracted tree whenever `.fused-snapshot-complete` exists, so a write that lands there is served back as that revision's content from then on. This only became reachable when `versions` grew file targets: an app snapshot framed the app's entry *page*, while a file snapshot is framed through the file's own default view, which for these extensions is `code` or `markdown` — both of which call `fused.writeFile`. So `server/mount.py::_is_under_snapshot_root` makes every `/api/fs` mutation handler refuse a path under that root with the existing `readonly` contract (403 + `{"error": "readonly"}`, which runtime.js and the explorer already render as a refusal), and makes `_writable` report `false` there so the framed editor draws read-only mode up front instead of only failing at Cmd+S. A **copy out** of a snapshot is still allowed — read-only, not sealed. **The framing of a browsable snapshot is `?preview=false&snapshot=1`** on the embed URL. `preview=false` is the listing's own split pane, which nested inside a preview column would be two previews deep and neither readable; `snapshot=1` (`router.ts` `IS_SNAPSHOT` → `body.snapshot`) says FROZEN TREE, NOT A LIVE FOLDER and suppresses the three pieces of chrome that would act on it as one — the **breadcrumb**, whose crumbs walk up out of the framed directory and into the snapshot cache's own internals (`~/.fused-render/branches/<branch>/app-versions/<key>/<sha>`), a path the user never chose and cannot act on; the **"Browse contents" mode chip**, which over a snapshot dir offers the folder's counterpart mode — a Claude chat ON THE EXTRACTED COPY; and the **"Open as app" chip**, same argument. One flag for three symptoms of one cause, a param rather than a third route because it is the same view of the same path with different chrome, and the predecessor is named in the record: `modechip=false` was exactly this until D237's only producer went away, with the note that the opt-out "comes back with that caller". **The completion marker moved OUT of the extracted tree** for the same reason — `<sha>.complete` beside the directory rather than `.fused-snapshot-complete` inside it, because everything inside is content the snapshot's listing now shows, and a marker row in a browsable historical tree is a file the user never wrote and cannot explain. Both locations are READ (only the new one written), so the change is not a silent wipe of every snapshot already on disk. Fixing this in the framing template was rejected for the reason `versions.py` already refuses `revert` by kind rather than trusting its gate to hide the button: a guard that only one caller honours is not a guarantee. What is **still rejected**: (a) keeping `git` on file keys next to `versions` — two commit-log modes for one story, as above; (b) keeping `annotate` as a standalone mode — its tools live in the chat's pane, so the mode was deregistered from every core key rather than left as a second, staler way in (§17), and its comment handoff is now doubly unreachable (no binding, no receiver). What is **no longer** rejected, and is the reversal itself: D235 rejected "binding ONE chat template to both kinds" on the grounds that the split pane renders a target and an ordinary folder has no app entry to render, so one template would have to branch on kind and carry a dead pane for half its bindings. It does branch on kind, in two places — the pane and the prompt — and D239 has since conceded half of D235's premise while leaving its conclusion overturned: an ordinary folder really does have nothing to render, so it gets **no pane**, not a substitute for one (PT-16). What that does not follow is that the template must therefore fork. A no-pane target is a *layout* the one template resolves — the pane is removed and the conversation takes the width — and everything the two kinds actually SHARE is the part that costs something to duplicate: the transcript, the composer, the approval cards, the permission modes, the run/resume/stop machinery, the session sidecar and the history restore. D235's own evidence is the argument here: the second chat template WAS that fork, and it drifted into the feature-poor twin (8 mentions of the annotation machinery against 277) precisely because a fork's two halves are maintained by whoever happens to be editing one of them. So the branch is one predicate read in two places, and the cost of the case that has no pane is a flag and a removal — not a second copy of a chat.
- **PT-15** **A template whose layout needs width is responsible for collapsing itself; the shell offers modes by *binding and gate* only — never by how much room a host happens to have (D236).** The set of modes a target gets is decided by the registry (PT-7/CT-3) and, for a gated folder, by its `condition.py` verdict (CT-12): those two inputs and nothing else. A **split-layout** template — two panes and a divider, like `claude` (the chat, PT-16) or `versions` — therefore has to survive every host the shell renders it in, and three of them are narrow by design: the listing's **preview pane** (floor 220 px, default width *half* its split container — FS-12), a **Panel pane** dragged freely (§14), and **`/embed`** in a small window. The rule: the template ships a **media query** at the width its own layout stops being **useful** — the sum of its panes' minimum *useful* widths, rounded up, which is **not** the width at which they merely stop overflowing — and below it shows **one view at a time with a toggle**, the idiom `log_studio` (780 px), `map` (650), `duckdb`/`sqlite` (560) and `bundle` (640) already use. `claude` and `versions` both collapse at **880 px** (`claude`: `#left` 420 + divider 4 + `#chat` 440 = 864; `versions`: `#side` 200 + divider 4 + a 640 px preview frame = 844 — both rounded up to the same figure, so the two collapse together). The arithmetic **scopes to the targets that have two panes**, which since D239 is `claude`'s file and app-folder shapes only: an ordinary folder has no `#left`, so there is no sum to satisfy, no collapse to perform and no toggle to offer — a single-column layout is already the thing the breakpoint exists to produce, at every width. This is not an exemption from the rule; it is the rule having nothing to do, and it is why the collapse logic is short-circuited outright for that target rather than left to run against a column that is not in the document. The number is **880 and not the ~560 the overflow floors give** because the listing preview pane defaults to *half* its split container — ~700 px on a 1700 px window — so a breakpoint set at the overflow floor engaged the split in every host that could hold it without breaking and none that could hold it usefully; the arithmetic is written down beside the query, so the figure is checkable rather than a taste call. Three sub-rules the two built-ins establish, because getting them wrong is silent: **(a) park the hidden half, do not `display: none` it** — an iframe with no layout box gives its document a 0×0 viewport, so a screenshot of it rasterises 1×1 and every element rect an annotation pin is anchored to collapses (§17); out of flow + `visibility: hidden` + `pointer-events: none` keeps a real viewport and shows nothing. **(b) An inline width written by the divider's own JS outranks the media query**, so the collapse must neutralise it — either from CSS (`!important`) or by having the apply function skip the inline write while narrow; the split *ratio* param is never touched either way, so crossing back restores the user's width with no reload. **(c) A control that acts on the hidden half is absent, not disabled**, and any **armed** state it owns is reset on the flip — a disabled control still asserts the feature exists, and an armed control over an invisible document swallows input or attaches something the user cannot see. Only the view toggle itself (navigation, not a feature) and content the user has already authored stay reachable from both views. The toggle **names what its destination is FOR**, not merely where it goes: `claude`'s reads **"Annotate preview"** outbound and **"Back to chat"** on the return (D239), because the preview column is where the annotation tools live and that is the only reason a person leaves the conversation for it — "Preview"/"Chat" named the two halves and said nothing about why one would move. It stays navigation and is **not merged** with the annotate switch (§17), which arms the mode once you are there: one control moves the view, the other changes what a click in the frame does, and one button doing both would arm a mode in the same gesture that reveals the surface. The label and the `aria-label` are **one string**, since a second wording is a second thing to keep in step; the longer labels are what `#viewbtn`'s `flex-shrink: 0` and the annotate switch's own ellipsis exist for, so a 220px host truncates the mode name rather than overflowing the row or half-hiding the only way back. Which view **leads** is the mode's subject, not the wider pane: the chat opens on the chat, `versions` on the commit list (a snapshot must be picked before there is a preview). **Pane-local params** are the persistence channel and stay pane-local under D72's boundary: `claude` carries `split` (the ratio), `annotations` + `annmode` (§17's notes and armed mode), **`leftmode`** (which of the offerable stat entries the left pane frames, PT-16 — a listbox picker at the RIGHT-HAND end of the pane's own bar, showing each template's `icon.svg` beside its name, hidden below two choices, an unknown value falling back to the default silently as in PT-9) and **`paneview`** (`chat`|`preview`, which of the two the narrow layout shows, chat by default) — all four of which an ordinary folder's chat **ignores silently** rather than strips (PT-16), since they describe a layout that target does not have; `versions` keeps its narrow view in a **body class only**, deliberately not a param, since which half a temporarily-narrow host shows is not state a bookmark should reproduce. What was **rejected**: having the **shell filter split-layout modes out of narrow hosts**. A pane's width is *dynamic* — the listing pane defaults to half its container, so on a wide window the split fits and the mode should be offered — which makes a host-based ban wrong in the one place it was aimed at; a width-based filter makes modes appear and disappear from the switcher (PT-10) mid-divider-drag and can yank the **active** mode out from under the user; it needs per-template width knowledge in the shell, i.e. a new `registry.json` field plus a new field on stat's template entries (which carry only `mode`/`path`/`icon`/`conditional`, PT-8), applied separately in three hosts (`ListingPreviewPane.tsx`, `PaneModeMenu.tsx`, `/embed`); and **user templates (§16) would never inherit it**, whereas a media query in the template is something a user template gets for free.
- **PT-16** **The chat template's contract: one gate, TWO pane shapes plus a no-pane case, and a system prompt that cannot disagree with the pane (D237, revised by D239).** `templates/claude/` is the single chat mode (PT-14). Because it is bound to two kinds of target it branches on kind in exactly two places — the left pane and the prompt — and both read the **same** predicate, `shared/app_entry.entry_html`, so what the prompt claims is beside the chat is what is beside the chat. *This clause said "three pane shapes" until D239: the third shape — fused-render's own file browser framed for a folder with no app entry — is **removed**, and an ordinary folder now gets a full-width chat with no pane at all. The predicate and the two-places rule are unchanged; what changed is that one of the two answers is "there is nothing beside the chat", and the prompt says nothing about a pane there because there is none.*
  - **The gate** (`claude/condition.py`, CT-12) accepts **any existing regular file and any existing directory**, and nothing else: `os.path.isfile` / `os.path.isdir`, never `not isdir` (the loose form also swallows every path that does not exist, and "cannot tell" must read as "refuse"), and it never lists, walks, globs or resolves symlinks, because it runs for every path the explorer stats. That reduces to "the path exists", which the shell already knows — so the gate exists for **one** refusal: a **mount-backed** path (`shared/appenv.is_mount_backed`). The bytes under the mounts dir arrive over FUSE and an agent turned loose there rewrites the remote tree, the same reason every peer gate refuses those paths (MD-11). This is a **capability deliberately removed** relative to the deleted plain chat template, which shipped no `condition.py` at all and therefore did offer a chat over an rclone/NFS mount. **Rejected:** deleting the gate outright now that everything else about it is always-true — an always-true gate would be worth removing, a gate that still says no to remote mounts is not.
  - **The left pane, TWO shapes and a no-pane case (D239).** A **file** → the file in its OWN default template: `GET /api/fs/stat` for the target, drop `conditional` entries (their verdict lives behind `/api/fs/conditions` and is deliberately not fetched — an unresolved gate reads as "not offered") and drop the chat mode itself (a pane framing the chat again is a mirror, not a preview), then frame `/render?path=<that template>&_file=<file>` — or the file itself when the entry is the `_render` sentinel. That is the shell's own `defaultTemplate` rule (PT-8) reused rather than a per-extension table inside the chat, which would drift from the registry on the next rebinding and ignore a user override (§16); and it is a **default, not a lock** — the pane-local `leftmode` param (PT-15) selects any other offerable entry from that same stat payload, unknown values falling back silently as in PT-9. **The picker sits on the pane it controls, at the RIGHT-HAND end of it:** in the split layout it is a row across the top of the LEFT column, not a control in the chat pane's strip across the divider — and it is pushed to that row's far end (`margin-left: auto`, scoped to `#leftbar`), because the bar exists to carry this one control and a lone control hard against the left edge reads as a LABEL for the pane rather than as a switch on it. It is a **listbox, not a `<select>`**, and the reason is the ICON: its rows show each template's own `icon.svg` beside the mode name, exactly as the shell's mode menu does (`templateModeIcon`), and an `<option>` renders text in every engine. The icon needs no new server plumbing — stat's `templates` entries already carry the icon's absolute path (PT-11), `/api/fs/raw` serves it, and it is drawn as a mask filled with `currentColor` so one flat glyph follows the row's ink in both themes; a template with no `icon.svg` (and the `_render` sentinel) falls back to the shell's own lettered box. The rows are real `<button>`s inside a `role="listbox"` popup under an `aria-haspopup` trigger — the idiom the `reader` template's voice menu already uses here — so focus, Enter and Space stay the platform's job and only the arrows and Escape are the template's — the same grammar the explorer's own preview pane follows (FS-10). Below the 880px breakpoint there is no persistent left column to hang a bar on, so the *same* element moves back into the shared `#anntools` strip, the one row both narrow views keep; crossing the breakpoint relocates it live, with no reload and no effect on `leftmode` itself. It is hidden entirely when the target offers fewer than two views. An **app folder** (an entry page resolves) → that entry page, via `/render`. **The entry rule is `index.html`, else the FIRST top-level `.html` in name order** (`shared/app_entry.entry_html`, `sorted` so two consumers cannot land on different pages). It used to call several pages without an `index.html` *ambiguous* and resolve to None, which meant every consumer dead-ended on such a folder — this pane drew nothing, the `app` mode drew "no entry page", and a `versions` snapshot of one showed that notice instead of the app at that commit. Owner call on the user's own wording ("for multiple html files, just pick the first one"): a deterministic first page is one click from any of the others once the folder is open, and None was one click from nowhere. The consequence here is that a folder with several pages and no index now HAS a pane (and the `app_state` tool with it) where it previously had none. Everything the pane implies rides on those two and nothing else: the annotation layer (§17), the pane screenshot, the 880px collapse (PT-15) and the `app_state` tool (below). A folder with **no** app entry → **no pane at all**: no `#leftframe`, no `#divider`, no view toggle, no pane-shot pill, no annotate affordance, and the conversation owns the full width. *What this overturns.* D237 framed **`/explorer/embed/<dir>?preview=false&modechip=false`** there — the chrome-free navigable shell (LM-4/D39), a real file browser beside the chat — and it was chosen as the fix for code that used to `throw` (`no app entry…`, a permanent error panel beside a working chat). It fixed the throw and left the real problem untouched: **nothing flowed back from that pane.** The template has no `postMessage` and no message listener, so selecting a file in the browser attached nothing, fed nothing to the composer and changed no agent context; annotate was hard-disabled over it by construction (no element of a file listing is a thing a pin could mean anything about); and the `leftmode` picker was inert for it, since neither directory branch populates `paneEntries`. So it was half the width of a folder chat spent on a view that reported to nobody, for a question the agent's ordinary file tools already answer. **Deliberately given up:** the `state.url` backchannel — embed navigation rewrote the iframe's path, so `app_state` could tell the agent which folder or file the user had walked into. It was the one signal that did flow back, and it goes with the pane. **Both embed params go too, and they go differently:** `modechip=false` loses its only producer in the codebase, so its plumbing is **removed from its consumer** as well (`Preview.tsx` no longer reads it and the corner chip has no opt-out) — a URL param no caller can produce is a branch nothing can test, and if another template ever frames an embed of its own counterpart's target the opt-out returns with that caller; `preview=false` **stays**, because the listing writes it for itself when the user closes the pane (`listing/pane.ts`) and it still means something. With the folder embed gone, `/embed` is **no longer used as a pane by any template**, so D235's rejection of it for a FILE target stands unqualified. *The no-pane case is a designed ABSENCE, not a missing element, and the difference is load-bearing.* Shipping the markup without `#leftframe` cannot work: the frame's `load` hook is wired at top level, so with no element to wire that statement throws a `TypeError` and aborts **every declaration after it** — the agent poll loop, the pane-shot toggle, the composer wiring — and the boot `catch` cannot report it either, because its own first statement removes that same missing element, so the throw lands inside the catch and neither the error panel nor `pushAppLog` runs. A blank page with a working-looking composer. So the markup ships the column exactly as it does for a file, every declaration initialises against it, and `enterNoPane()` takes it away **afterwards** — ordering that is guaranteed rather than hoped for, since the template is one `<script>` and the loader reaches that branch only after `await`ing a fetch. Three subtrees are removed (`#left`, `#divider`, and `#anntools` — which is a child of `#chat`, so it survives removing the column and would otherwise sit there as an empty bordered row of controls for a pane that is not there), plus both copies of the pane-shot pill. One `noPane` flag then short-circuits `applySplit`, `applyNarrowView`, `renderAnn` and `annSetMode`, so nothing writes to a detached node or to a param describing a layout this target does not have. **Stale params are ignored SILENTLY and never stripped:** `split`, `paneview`, `leftmode`, `annmode` and `annotations` left on a folder URL by an old bookmark open a full-screen chat with no error — the same forgiving posture PT-9 takes for an unknown `_mode` — and rewriting them would break that bookmark's round trip for the day the folder grows an `index.html` and gets its pane back. **Also enforced, not documented:** `appEntry` (the only field in the app-state payload that distinguishes the user's real app from our own UI) is never set on this path, and the "pane unreadable" sentence no longer names "no app entry" among its causes, because that condition now produces no pane and therefore no tool to ask. **Rejected:** the `throw` (an error panel for the ordinary case of a folder that is not an app); keeping the embed as a read-only browser (it is the reporting-to-nobody problem, restated as a feature); and hiding the column with CSS while leaving it in the document (the elements would stay live, `shotPane` would still rasterise them and the removed controls would still be focusable — a hidden pane is a pane).
  - **The system prompt** (`_split_system_prompt`) has a shape per pane shape, and is decided **per run, never cached**, so a folder being scaffolded into starts being described as a project the moment it becomes one. An **app folder** keeps the project wording (its HTML is an app fused-render serves through the `runPython` bridge; naming fused-render here rather than leaving it to the user's own `CLAUDE.md`, which we do not own — the D216 reliability argument). A **file** says whose page the pane is and that the viewer is never to be edited. An **ordinary folder** gets the folder-scoping instruction and **nothing about a pane** (D239): the paragraph that used to be here described fused-render's own file browser beside the chat and warned that `app_state` "reports the **browser**, not the folder", and it went with the pane it described — a prompt that tells the model what the user can see beside the conversation, when there is nothing beside the conversation, is a false claim about the screen. The **composer's placeholder** names the same three kinds and is set from the same resolution the pane already performs (stat's `is_dir`, then whether an entry html resolves) — *"Ask Claude about this **project** / **folder** / **file**…"*, with the markup shipping the kind-free *"Ask Claude…"* until stat answers; it was hardcoded to "this project", which was the wrong noun for an ordinary folder and for all 47 file keys, and the rule is the prompt's rule: the UI does not claim a kind the target does not have. That rule is **general, not just the placeholder's** — the footnote under the composer and the annotation block's own preamble both said "project" unconditionally too, so every piece of chrome that names the target reads one writer, and a test asserts no kind noun is hardcoded in the markup. The **app_state disclosure** rides the two shapes that HAVE a pane, for D235's reason (an un-announced tool is a tool that never gets called) — and only those two, since the ordinary folder is not offered the tool at all. Saying "this is a fused-render project" over `~/Downloads` is rejected as a lie that costs something — it invites the agent to hunt for a bridge that is not there and to read a folder of PDFs as a codebase.
  - **The tool roster varies by target kind (D239).** `mcp__fused_approvals__app_state` reads the page beside the chat; a target with no pane has no such page, so the tool is **not offered** there — absent from `tools/list`, absent from the dispatch, and absent from the spawn line's pre-allowance. One switch decides all of it, and it is the **channel's own existence**: `agent.py` spawns `permission_server.py` with the app-state directory only when `_has_pane` (the same `entry_html` predicate), and the server keys both its roster and its dispatch on having that directory. A roster that could vary independently of the channel would advertise a tool the server cannot serve. **Rejected:** offering it and answering with an explanatory error — the model would call it after every edit and spend the 20-second app-state timeout discovering the same thing once per turn; and offering it and answering instantly with "there is no pane" — a tool whose only possible answer is that it does not apply is a tool that should not be in the list. The `Read` rule for the screenshot directory stays unconditional, because it is a rule about a directory and not a claim that this target can annotate.
- **PT-8** `GET /api/fs/stat` carries the resolved mode list as **`templates`**: an array of `{"mode": <name>, "path": <abs template.html>, "icon": <abs icon.svg|null>}`, in order, first = default. An entry whose folder ships a `condition.py` gate (CT-12) additionally carries **`"conditional": true`** — stat only *marks* it (the gate is **not** evaluated at stat time; it may do real I/O), and the verdict arrives via `GET /api/fs/conditions` (CT-12). A conditional entry is **never the default while an unconditional entry exists**: the default is the first entry *without* `conditional`, falling back to the first (verdict-allowed) entry only when the whole list is conditional. `templates: []` when nothing applies — an unmapped file extension or a `null` binding. A **directory** always resolves at least the universal `/` key's `["_listing"]` (PT-13, D81), so it is empty only when a `null` binding disables it, whereupon the shell falls back to the built-in listing anyway (a folder must always render something). The old singular `template` field is **removed**.
- **PT-9** **`_mode` param (shell URL):** non-default modes are selected via reserved param `_mode=<template name>` on the **shell URL** (bookmarkable, same URL-is-state pattern D40 established for the old HTML `_mode=render|source` toggle — that toggle itself is now the ordinary `["_render", "code"]` mode list, PT-12; old `_mode=source` bookmarks fall to the default, accepted break). Absent `_mode` = default = the first non-`conditional` entry (PT-8; `templates[0]` when none is conditional); selecting the default **deletes** the param (clean URLs); an unknown/stale value falls back to the default with no error. Switching swaps the iframe src to the selected template's `/render?path=<template>&_file=<file>` with a fresh document per switch. A sentinel mode may render a **shell view instead of an iframe**: `_listing` (PT-12) mounts the shell's built-in listing component (no iframe, no `_file`) in place of the preview body, selected by `_mode=_listing` like any other mode (D81). Known accepted quirk: template params (e.g. `offset`) persist on the shell URL across mode switches; a param name used differently by two modes collides — documented, not prevented.
- **PT-10** **Mode switcher (shell, preview header):** rendered only when `templates.length > 1`, right side of the preview header bar. *One qualification, from the listing pane's self target (FS-11): "one mode is not a choice" holds because that one mode is the ACTIVE one — so when the caller's surface has **no** active mode, a single entry still renders, because the trigger is then the only way to pick anything. Nothing is marked active in that state: no checkmark, no accent row, and the trigger names the action ("Choose view") rather than reporting a mode it is not in.* Its contents are exactly the resolved list — the **available width is never an input** to it, so a mode cannot appear or vanish as a divider is dragged (PT-15). **Icon-only buttons**, mode name via native `title` tooltip, active mode in accent color. When an entry's `icon` is `null`, the shell renders a placeholder: the first letter of the mode name in a small rounded box. The `.html` Rendered|Source pair is **not a special case**: it is the ordinary mode list `["_render", "code"]` (PT-12) riding this same switcher — `_render` gets a shell-baked eye icon (sentinels have no folder to ship `icon.svg`); `code` gets its real folder icon. The `_listing` sentinel likewise gets a shell-baked list icon (D81). **No two modes a key BINDS together may be indistinguishable in one list.** This is a constraint on the bindings (PT-7/CT-3), not on the wording — display names live in `platform/lib/mode-name.ts` and are that module's business — and it is easy to break from a distance, because a name that is not in the table falls through to a humanizer: adding a mode to a key, or naming a new template folder into a string the humanizer already produces, is enough. Dispatch keys on `mode`, so a collision breaks nothing and is invisible to every other test, which is why it needs its own: `listing/mode-labels.test.ts` derives every co-offered set from the shipped registry — per key, and per preview-pane list (FS-12/`listing/pane-modes.ts`) across its `isDir`/`self`/`hasApp` permutations — and fails on any duplicate. The one pair that is named alike on purpose is the `app` template and the pane-only `_app` sentinel (the same view from two surfaces); the pane offers exactly **one** of the two carriers, never both, and that de-dup is a rule of the list rather than an exemption from the guard.
- **PT-11** **Icons:** a template folder may ship `icon.svg` — **monochrome** (single fill; the shell tints it via CSS `mask-image` + `currentColor`, so only alpha matters), square viewBox (24×24 suggested), legible at 16px. `icon` in the stat entry is the abs path of the `icon.svg` sitting next to the *resolved* `template.html` (the user folder's icon when a user template resolved), or `null`. The shell loads it through the existing `/api/fs/raw` endpoint — no new routes. Every built-in folder ships one. Sentinel modes (`_render`, `_listing`) have no folder, so the shell bakes their icons in (PT-12).
- **PT-12** **Sentinel modes:** a mode name starting with `_` is a **shell sentinel** — no template folder backs it; the shell knows what it means. Server resolution special-cases sentinels: the stat entry is emitted as `{"mode": "_<name>", "path": null, "icon": null}` without touching the filesystem. The `_` prefix matches the reserved-param convention (`_mode`, `_file`). The sentinel namespace is **shell-owned**; since D73 the server keeps a **known-sentinel set** (`KNOWN_SENTINELS = {"_render", "_listing"}`, D81) and a name in that set is referenceable from **any** registry list, built-in or user — any other `_`-prefixed name is invalid (dropped + `template_error`, CT-6). Two sentinels exist:
  - **`_render`** — "render the file itself" — the default mode of the built-in `.html`/`.htm` list `["_render", "code"]`. Shell handling: iframe src `/render?path=<the file itself>` (no `_file`), shell-baked eye icon.
  - **`_listing`** — "the shell's built-in directory listing" (sortable columns + in-folder search, FS-1/§13.4, plus the optional split preview pane, FS-9..FS-15) — the default of the universal `/` directory key (PT-13, D81), and a peer mode of `.zarr/`'s `["zarr_aoi", "_listing"]`. It backs no folder and takes no `_file`: when it is the active mode the shell **mounts its Listing component in place of the preview iframe** (no iframe at all). Shell-baked list icon.

  Users **can** rebind any registry key — including `.html`/`.htm` (CT-4 revised, D73) and the directory keys (D81) — dropping a sentinel, then listing it explicitly brings it back. Unknown sentinel entries (path `null`, mode not in the set) are filtered out defensively. Non-sentinel entries in the same list (e.g. `code`, `zarr_aoi`) work exactly like any template mode. Future modes are added to the server-side registry and flow through the framework normally.
- **PT-13b** **An explorer folder has no top-bar mode control.** Directories resolve modes like anything else (PT-13), but the shell's title-bar switcher is **not rendered for a directory outside the app route** — the folder's mode control is the preview pane's own (FS-10/FS-11), which sits with the thing it changes. **The accepted consequence, stated rather than discovered:** nothing in the explorer switches a folder INTO one of its other modes. The pane's menu writes `_panelMode` — what the *pane* previews — not `_mode`, so a folder's `git`/`versions`/`graph` views are **entered** by an explicit `?_mode=` (a typed URL, a bookmark, the file menu's **Open With**), or by a registry default that is not `_listing` (a `.zarr` store opens on `zarr_aoi`). **Getting back out is the BROWSER'S BACK BUTTON, and deliberately nothing else** (owner call). Every one of the ways in is a *navigation* — a typed `?_mode=`, a bookmark, Open With — so the navigation that got the user there is what undoes it, and it is already at the top of the window. This rule briefly shipped a second answer: the `Browse contents` chip (PT-13/D65) revealed in the explorer by an `is-exit` modifier. It is **removed**. Pinned absolutely over the template's iframe it landed on whatever that template drew in its own top-right corner — over a full-width `versions` history it sat across the HISTORY header and the newest commit — so in the two folder modes people actually open (`versions`, `claude`) it read as a stray tooltip rather than as a control. A bespoke affordance that has to be explained is worse than the standard one every user already has, and "the view must supply its own way back" was never the requirement; "the state must not be a dead end" was, and Back satisfies it. The chip keeps its **embed** reveal untouched — a different surface, where `.preview-header` and its switcher are hidden outright, so there the chip is the whole affordance rather than a second one. This is an owner call: two mode switchers in one view, a few hundred pixels apart and governing different halves, is not a choice a user should have to work out, and for a folder the pane is the explorer while its peers are opt-in tools rather than other ways of looking at the listing. Files and the app route (`appChrome`) keep the top-bar control.
- **PT-13** **Directory views (D65, revised by D73 and D81):** a preview target may be a **directory**. Directories resolve through the **same registry** as files (PT-7, CT-3): a key with a **trailing `/`** binds a directory's basename, and the **universal `/` key** (zero segments, CT-3) matches *every* directory at lowest specificity. The built-in registry ships `"/": ["_listing", "app", "claude", "versions", "git", "graph", "zarr_aoi"]` (D185 removed `preview`; `graph` per MD-2; `git` per §33/D193, directory-only per D235/GT-2; `app`/`versions` gated to app folders, `claude` — the one chat template since D237, which deleted the second one this key used to carry — gated only against mount-backed paths, §7.2's `/` row and PT-14/PT-16) and `".zarr/": ["zarr_aoi", "_listing"]` — so **every** directory carries a non-empty `templates` list (≥ `["_listing"]`), and dispatch is uniform: a directory previews its default mode exactly like a file. The built-in **listing is itself a mode** — the `_listing` sentinel (PT-12) — so it rides the ordinary mode switcher (PT-10) and `_mode` selection (PT-9): a plain folder's single-mode `["_listing"]` shows the listing with no switcher; a `.zarr` store shows the listing by default with the `zarr_aoi` map joining as a `condition.py`-gated peer (CT-12) once its background verdict confirms the store (`_mode=zarr_aoi` selects it). This replaces D65's one-way `?listing=1` "Browse contents" escape hatch, which is **removed** (D81) — the only way to the listing is now the `_listing` mode. In **embed** (the preview header, hence the switcher, is hidden), a corner chip toggles the `_listing` mode (writing/deleting `_mode`) so an embedded directory preview can still reach its members. Annotate (§17) is not offered for `_listing` (no iframe to overlay) — moot in the core registry since D235, where `annotate` is bound to nothing at all, but still the rule for a user who re-binds it (§16). A directory resolves to an **empty** list only when a `null` binding disables it (CT-2); the shell then falls back to the built-in listing regardless (a folder must always render something). Users bind directory views like any other key — `"/": ["_listing", "gallery"]` lists the built-in listing plus a gallery mode for every folder (built-in names are listed explicitly — there is no splice, D94); dropping `_listing` from a list forgoes the file listing for those directories (owner call, same "user can shoot themselves" posture as D73's `.html` rebind). Accepted break: old `?listing=1` bookmarks ignore the dropped param — a plain folder still lists (its default), and a `.zarr` bookmark also lists by default now (the `zarr_aoi` map is a gated peer reached via `_mode=zarr_aoi`, not the default). Accepted break (D185): the `preview` folder-preview template is **deleted** and gone from this key, and the two ways a leftover reference surfaces are **different mechanisms** — a **`?_mode=preview` URL or bookmark** is an unknown `_mode` value, so it falls back to the default (`_listing`) **silently, with no error** per PT-9 (and lands on the listing that now carries the split pane, FS-9..FS-15, which is what such a URL was asking for); a **user registry** still listing `"preview"` is instead a dangling name per CT-6/D95 — dropped from the mode list with `template_error` naming it on the stat payload and a broken (`exists:false`) row in the Templates view (§23).
- **PT-5** **User overrides:** DECIDED and specced as §16 (M7, extended by M8) — user template folders under `~/.fused-render/templates/` bound to extensions by `~/.fused-render/templates/registry.json`, replacing or extending the built-in mode list, using the exact same mechanism.

---

## 8. Server Requirements (cross-cutting)

- **SV-1** Single process, no external services, no database. State = file system + in-memory.
- **SV-2** *(follow-up)* WebSocket/SSE push channel (progress, file-change notifications). v1: plain request/response only.
- **SV-3** *(follow-up)* Structured execution logging + dev console panel. v1: server stdout logs only; Python print() output is returned to the calling page and logged to the browser console.
- **SV-4** Graceful shutdown; per-call subprocesses die with their call.

---

## 9. Security Model — DESCOPED (v1)

**Decision: no security layer in v1.** Base layer simplicity wins; everything below is a recorded follow-up, not a requirement.

- v1 keeps only: bind `127.0.0.1` (one line, free).
- **Follow-ups (documented, not built):** session token auth; `Origin`/`Host` validation (DNS-rebinding defense); sandboxed iframe + bridge for untrusted HTML. Note for later: a localhost server that executes Python and reads the whole disk is an RCE/exfiltration primitive for any website open in the same browser — revisit before this ever runs on a shared/edge machine.


---

## 10. Tech Stack (proposed)

| Layer | Choice | Rationale |
|---|---|---|
| Server | Python 3.11+, FastAPI, uvicorn | must run user Python; async + WS built in |
| Exec workers | `multiprocessing` pool or subprocess-per-call | isolation, kill-on-timeout |
| Shell UI | Vite + React + TypeScript | fast to build tree/table UI; any SPA framework acceptable |
| Data tables in templates | Arrow JS + a virtualized grid | large parquet/csv without choking |
| Packaging | `pipx install fused-render` → `fused-render` CLI | single-command local install |

---

## 11. Open Questions

1. ~~Whole-disk vs scoped root~~ — **RESOLVED: whole computer, no root concept (FS-3).**
2. ~~Python env~~ — **RESOLVED: the env the server was launched from. Good enough.**
3. ~~Streaming/partial results~~ — **RESOLVED: not required.**
4. **Editing** — is write support (rename/delete/save) wanted in v1 or strictly v2?
5. **Multiple entry functions per file** addressed by name (`runPython("f.py#chart")`) — needed, or keep `main`-only?
6. ~~Param change → auto re-run?~~ — **RESOLVED: manual — author wires `runPython` inside `params.onChange`. Declarative binding possible later, layered on top.**

---

## 12. macOS Distribution (DMG) — M3

Distribute as a DMG containing a menu-bar app; all UI stays in the browser.

- **DM-1** **DECIDED (v2, D33):** the `.app` is built by **py2app** from a framework-build python (Homebrew `python@3.12`, bootstrapped by the build script). py2app ships a real re-invokable interpreter in-bundle (`Contents/MacOS/python`) — `sys.executable` subprocess executor works unchanged — and its compiled stub gives proper LaunchServices/AppKit process identity (the earlier hand-rolled bash-shim caused flaky NSStatusItem behavior under Finder launches).
- **DM-2** **DECIDED:** user `runPython` code executes on the **bundled interpreter only**. `[bundled]` is the dev-install list and the Linux/Windows shipping list; on macOS py2app **copies** only what `scripts/setup_py2app.py` names — which now DERIVES that list from the installed distributions and excludes nothing, so all three platforms ship the whole extra (D176). `BUNDLED_EXCLUDED` is empty but stays as the mechanism: a `[bundled]` distribution the bundle does not carry must be named there with its measured cost, never merely absent. "Is this dependency available?" therefore has one answer today, and `tests/test_bundle_contents.py` is what keeps it that way — the templates that genuinely need an install declare dependencies **outside** `[bundled]` (`pyproj`, `imagecodecs`, `py360convert`, `pypandoc-binary`), which is what exercises the install loader on a shipped build. The extra ships preinstalled (numpy, pandas, requests, duckdb, polars, matplotlib, scipy, pillow, openpyxl, shapely, geopandas + core pyarrow). py2app note: these are force-copied via `packages` — the executor imports them only in child processes, so import tracing can't see them. Known gap: `mpl_toolkits` (3D axes) excluded (namespace-package vs py2app limitation). This holds under the fused engine too: a script whose folder declares no `pyproject.toml` runs on that same interpreter (PY-17), and only a folder that declares one gets an environment of its own (PY-16/PY-18).
- **DM-3** **DECIDED (v2, D34):** regular app — **Dock icon AND menu bar ✦** (Open in browser / Copy URL / Quit). No LSUIElement. Dock right-click → Quit is the discoverable lifecycle path.
- **DM-4** **DECIDED (v2, D73):** signing is credential-driven in `scripts/build_dmg.sh` — a **Developer ID** identity in the keychain (auto-detected or via `FUSED_RENDER_CODESIGN_IDENTITY`) triggers hardened-runtime, inside-out signing + optional notarization (`FUSED_RENDER_NOTARY_PROFILE`); with no identity it **ad-hoc signs** (local testing, unchanged). Developer-ID signing is also the general fix for the repeated Downloads/Desktop/Documents prompt (one Team ID unifies the app + its executor subprocess, complementing the D72 in-process reader split). Details: `docs/signing.md`. Supersedes the earlier "Briefcase external-app" plan (D35 — Briefcase's template breaks `sys.executable`).
- **DM-5** Launch flow: pidfile+portfile in `~/Library/Application Support/fused-render/`; liveness probe = GET `/` (file-backed, catches zombies); already running ⇒ open browser only; else start (1777, fall forward to 1787), write pidfile, open browser.
- **DM-6** **DECIDED (v2, D35):** DMG built by **dmgbuild** (app + Applications symlink, UDZO) orchestrated by `scripts/build_dmg.sh`; ~270 MB compressed.
- **DM-7** `fused_render/app.py`: menu-bar entry point (uvicorn on a daemon thread); py2app entry = `scripts/app_entry.py`; build spec = `scripts/setup_py2app.py`. CLI (`fused-render`) remains for dev.
- **DM-8** **Finder integration:** `CFBundleDocumentTypes` — `.parquet` rank Default, html + all template extensions rank Alternate (never steals user defaults, appears in Open With). Double-clicked files reach the app via the delegate's `application:openFiles:` (implemented by adding the method to rumps's delegate class); each file opens a browser tab at `/view/<path>`. Startup ordering: AppKit run loop starts first, server boots in the background after — the home-vs-file decision happens at server-ready, long after any launch document event has arrived, so a file double-click cold launch opens exactly the file view (no stray home tab).

## 12b. Milestones

*Historical build order, kept for context — not an exhaustive or current status list; later milestones (M10–M12, M15, M17–M18) ship as their own numbered sections (§18–§27), and the numbered requirement sections above are authoritative.*

- **M1 — Base layer:** server + shell, whole-disk browsing, raw streaming, live-rendered HTML in plain iframe, `runPython` → `main()` subprocess execution, params ↔ URL sync (strings, replaceState), server-side template registry + **parquet, image, text templates**. No security, no WS, no caching.
- **M2 — Sidebar & bookmarks:** SHIPPED.
- **M3 — DMG distribution:** menu-bar app + bundled CPython + build script (§12).
- **M4 — Live editing:** autosave + live change feed (WebSocket, D74) + auto-reloading views (§13).
- **M5 — Layout mode:** split-pane grid of embed views, layout + pane-local params in one bookmarkable URL (§14, D72).
- **M6 — Tab mode:** tabbed set of embed views on the §14 URL model; bookmark folders open as tab layouts (§15).
- **M7 — Custom templates:** user template folders in `~/.fused-render/templates/` + `registry.json` extension bindings, overriding built-ins (§16).
- **M8 — Template modes:** 1:n extension→template mapping — folder-per-template built-ins (renamed to public names), ordered mode lists (first = default), registry `list|string|null` grammar (the `"..."` splice shipped here was later removed, D94), `_mode` shell param + icon-only mode switcher, stat `templates` array replacing `template`, html folded in as the hardcoded `["_render", "code"]` sentinel list (§7, §16 / PT-6..PT-12, CT-10..CT-11).
- **M9 — Annotation mode:** annotate toggle over any preview mode, element/selection-anchored comment threads stored in the URL (§17).
- **M13 — Directory views:** directories resolve through the registry like files — the built-in listing becomes the `_listing` sentinel (PT-12), the universal `/` directory key (CT-3) makes it every folder's default mode, custom directory-view templates ride the same mode list + switcher, and `?listing=1` is removed in favor of `_mode=_listing` (§7, §16 / PT-12/PT-13, CT-3 / D81).
- **M14 — Explorer search:** the in-folder search's recursive walk goes breadth-first and streams NDJSON batches; client-side incremental fuzzy scoring, scroll-paged results, honest truncation, machine-noise pruning (§22 / SR-1..SR-11 / D85).
- **M16 — Pinned view:** the status item's only surface — any click drops an NSPopover whose native header row carries all app actions (menu removed, D98) above a live WKWebView of the pinned file's `/embed` view; detaches into a floating always-on-top window (§25 / PV-1..PV-8 / D97/D98).
- **Follow-ups (unordered):** remaining preview templates (csv/json/markdown/media/pdf/syntax-highlighted code); warm worker pool; DataFrame/Arrow returns; security layer (token, origin checks, sandboxed bridge); exec console; search/sort/tree/keyboard nav; caching; editing.

## 13. Live Editing — Autosave & Auto-Reload (M4)

Goal: a live-preview loop. Edit a file (in the built-in editor or externally) → it saves itself → every open view of it reacts. Combined with embed mode (D39) this gives "source in one tab, rendered output in another, updates as you type".

### 13.1 Autosave (code editor)

Applies to the `code` template (`templates/code/`), the only free-text editable surface (D37; the sqlite/duckdb grids added structured cell editing later via their own writer.py — see §13.5 for the read-only contract all edit surfaces share).

- **AS-1** The editor autosaves **250 ms after the last edit** (debounced). Manual Save / Cmd+S remain and save immediately, cancelling any pending autosave timer.
- **AS-2** Autosave uses the same optimistic lock as manual save (`expected_mtime`). On 409 the existing conflict banner shows and **autosave suspends** until the user resolves via Reload or Overwrite. Autosave must never auto-overwrite a conflict — that would reduce the lock to decoration.
- **AS-3** Status text is the save lifecycle: `Modified → Saving… → Saved`. A non-conflict save failure shows the error; the next edit re-arms autosave (transient failures self-heal).
- **AS-4** Always-on. No toggle, no setting. Consequence accepted: half-typed code reaches disk and triggers reloads of watching views (that is the point of a live-preview loop; the D17 traceback overlay makes broken intermediate states self-explanatory).
- **AS-5** The `beforeunload` dirty guard stays — it covers the sub-second window between last keystroke and autosave completion.

### 13.2 Change feed (server)

- **WF-1** Endpoint `/api/fs/events?path=A&path=B&…` — a **WebSocket** (D74; was SSE until the Chrome 6-connections-per-origin HTTP/1.1 cap starved every other fetch once ≥6 panes held streams open). Watched paths arrive as **repeated `path` query params** (paths may contain commas; repetition avoids a delimiter).
- **WF-2** v1 implementation: async loop stats every watched path every **200 ms**; baseline mtimes captured at connect. When a path's mtime differs from the last seen value (or the file appears/disappears) send one JSON text message: `{"path": "<abs path>", "mtime": <float|null>}` — `null` means deleted. No event replay: changes that happen while disconnected are missed by design (the client reloads on reconnect-relevant changes anyway).
- **WF-3** A `{"keepalive": true}` message every 15 s keeps intermediaries and buffers honest; clients ignore it.
- **WF-4** No filesystem-watcher dependency (watchdog/fsevents) in v1 — polling stat is cheap and dependency-free at local scale. A later upgrade to real FS events is internal to this endpoint; the client contract (WebSocket, same message shape) does not change.
- **WF-5** Read-only GET — no `X-Fused` guard, consistent with the other read endpoints (D36 covers only mutating/executing POSTs).

### 13.3 Auto-reload (runtime)

The reload logic lives **entirely in the injected runtime** — the shell needs no per-view watching, and every rendered page (view mode, embed mode, standalone `/render`) gets the behavior for free.

- **LR-1** Each rendered page watches the union of: **its own rendered file** (the `path` param of its `/render` URL), **`_file`** if present (templates watching their target), and **every Python file executed via `runPython` this page-life**.
- **LR-2** `POST /api/run` response gains a `resolved_py` field — the absolute resolved path of the executed file — so the runtime learns dependency paths authoritatively instead of re-implementing the server's relative-path resolution. Recorded for failed runs too (a broken py that gets fixed must still trigger reload).
- **LR-3** On any change event: debounce **300 ms** (coalesce bursts), then `location.reload()` on the iframe itself. Full reload is the honest re-execution — the runtime cannot replay what the page did with a python result. State survives because view state lives in URL params (D8/D20/D25).
- **LR-4** When the watch set grows (a new py runs), the runtime closes and reopens its watch `WebSocket` with the full set. Resubscribe is debounced so a page firing several `runPython` calls on load reconnects once. Unlike `EventSource`, a WebSocket does not auto-reconnect — the runtime retries a dropped socket after 1 s.
- **LR-5** Opt-out: `fused.autoReload(false)` disables watching/reloading for that page. The `code` template calls it — the editor must not reload out from under the cursor (its own autosave changes the mtime; external changes are the conflict lock's job). The `claude` template calls it too — Claude's own edit to the watched `_file` would otherwise reload the chat mid-stream, killing the poll loop and orphaning the run. Since D237 that is one template covering both kinds of target (PT-14), and the opt-out is the chat **frame's** only: its own left pane is a nested frame that keeps live-refreshing, which is what makes the edit visible while the chat survives it. To make the opt-out race-free, the runtime starts watching on `DOMContentLoaded`, after inline page scripts have run.
- **LR-6** Deletion (`mtime: null`) reloads too — the resulting 404/error view is the truthful state.
- **LR-7** Reload works identically for standalone `/render?path=…` pages (runtime is the same code).

### 13.4 Listing refresh (shell)

- **LS-1** The directory listing view watches the directory path via the same endpoint; on change it re-fetches `/api/fs/list` and re-renders, preserving sort params.
- **LS-2** Known limitation, accepted: a directory's mtime changes on create/delete/rename of entries — not when a child file's content or size changes. Stale sizes in an open listing are fine.
- **LS-3** The shell closes the listing's watch `WebSocket` when navigating away (to a preview or another directory).

### 13.5 Read-only files — the editability contract

Write surfaces are decentralized (the code editor via `/api/fs/write`; the sqlite/duckdb grids and the annotate sidecar via their own Python writers), so read-onlyness is decided close to whoever writes, using host primitives — never probed from JS.

- **RO-1** `/api/fs/stat` (and `/api/fs/write`'s stat-shaped response) carries `writable`: an existing path needs `W_OK` on itself, a not-yet-existing file needs `W_OK` on its parent. The flag means exactly "`/api/fs/write` would accept this path" — the two must never disagree.
- **RO-2** `/api/fs/write` refuses a non-writable target with `403 {"error": "readonly"}`. This closes the atomic-write loophole: temp-file + `os.replace` goes through the parent directory and would otherwise silently overwrite a `chmod -w` file. `runtime.js` `writeFile` surfaces the refusal as a typed error (`err.type === "readonly"`), mirroring the 409 `"conflict"` case — the backstop for a template that never checked the flag.
- **RO-3** Any template-side Python **writer** applies the same gate itself (`os.access(file, W_OK)` → `PermissionError`) before writing, for the same reason: writers that rewrite via `os.replace` (duckdb, annotate's sidecar, `shared/file_history.py`'s revert — §34/FH-7) bypass the read-only bit, and ones that don't (sqlite) fail late with an unhelpful mid-transaction error. The two `os.replace` writers that also consult the mount flag through `shared/appenv` (annotate's `_sidecar_writable`, `file_history.file_writable`) are the exception RO-8's known gap notes; `file_writable` additionally requires `W_OK` on the **directory**, since that is where mkstemp and the replace both land.
- **RO-4** Template **readers** fold fs writability into the editability verdict they already return — `editable` + `readonly_message` (short badge text) + `readonly_tooltip` (hover explanation). Filesystem read-onlyness is just one more reason alongside content-level ones ("View", "No rowid", "JSON"); the fs gate wins over a content-level "editable".
- **RO-5** UI treatment is shared: `/template-shared/ro-badge.js` (`fusedRoBadge.update(el, message, tooltip)`) renders the identical badge in every template with an edit surface. The code editor derives its verdict from `stat.writable` (no Python reader) and locks the CodeMirror buffer; the grids disable editing per their reader's verdict.
- **RO-6** Read-only never blocks *viewing*, and a template whose write target differs from the viewed file gates on ITS target: annotate checks the `<file>.json` sidecar (a `status` action), keeps commenting fully functional (the URL is the live store), and only warns that history won't be recorded.
- **RO-7** (D110) An archive member's **preview** copy (zip and tar readers) lands **0444**: it is a throwaway — an edit "saved" to it never reaches the archive — and the permission bit routes it through RO-1..RO-6 unchanged (stat.writable false, templates open read-only, `/api/fs/write` refuses, writer gates hold). The copy is written to a unique temp file and `os.replace`'d into place, so a re-preview swaps out a stale read-only copy and a concurrent preview of the same member never sees a half-written or permission-flapping file. Deliberate `extract`/`extract_all` output keeps the original semantics: writable, and failing loudly (EACCES) on a write-protected existing target rather than silently replacing it.
- **RO-8** (D110) A mount record persists `read_only`, re-detected **non-mutatingly on every attach** (rc `operations/fsinfo`: a present Features map with no Put/PutStream/Copy → read-only; `config/get`: anonymous S3 — no keys, no env_auth, no profile — → read-only) unless the flag was set explicitly via `read_only` in the create body (strict boolean, 400 otherwise; persisted with a `read_only_user` marker that detection never overrides). An **inconclusive** probe (rc failure, missing Features map) persists nothing — a transient hiccup must not freeze a wrong verdict; the next attach re-probes. `_writable` folds the flag in ahead of the `W_OK` check — under a mount `W_OK` lies (CacheMode=full takes any write into the local VFS cache and fails only at async upload) — so stat.writable and the write guard flip together per RO-1, via an mtime-cached mountpoint lookup (no mounts.json parse per stat). Unflagged mounts stay rw (pre-flag behavior; a credentialed-but-IAM-read-only remote is knowingly not caught — only a junk-writing probe could tell). Known gap, accepted for now: template-side writer gates (RO-3, `os.access`) don't see the flag — only `/api/fs/write` surfaces do; the deep fix is mounting read-only remotes with the VFS `ReadOnly` option so `W_OK` itself turns truthful, deferred because the serve and the mount must carry identical vfs option sets (see SERVE_VFS_OPT) and a per-mount option split needs its own validation. `mount_view` exposes the flag; the Mounts card labels the mount "read-only".

## 14. Layout Mode — Split Panes (M5)

Goal: view several files/directories side by side in a resizable grid of panes, with the **entire state — pane arrangement, each pane's location, and all view params — captured in one bookmarkable URL**. Combined with bookmarks (D20) this makes a saved layout a one-click dashboard.

### 14.1 URL & route

- **LM-1** Route: `/view/_panel?...` and `/embed/_panel?...`. `_panel` is a **sentinel pathname**, not a real file: the shell's `route()` intercepts it (under both prefixes) before calling `stat`. Zero server changes (the server already serves the shell for any `/view/*` and `/embed/*`). The pane tree lives in the reserved `_layout` query param (LM-2).
- **LM-2** The pane tree lives in the reserved query param **`_layout`** (underscore prefix → already invisible to `fused.params`, PR-6). Codec (borrowed from the reference grid-viewer):
  - `,` separates panes in a **row** (side by side), `;` separates **columns** (stacked), `(…)` groups for nesting. Single pane = bare path.
  - Each pane segment is the pane's **fs path plus optional pane-local query** (`/data/a.parquet?_mode=source&sort=name`). Within a segment, the characters `, ; ( ) % ?` occurring *inside* path components or the query are percent-encoded so the codec's delimiters stay unambiguous.
  - **URL grammar (D51): the entire `_layout` value is parenthesized and emitted last** — `?city=sf&_layout=(/data/a.parquet?_mode=source&sort=name,/notes.md)`. The parens delimit scope both visually (inside = iframe-local, outside = global) and structurally: **`&` is literal inside them**, so segment queries read exactly as they appear. Every read of a shell query goes through the codec's `splitShellSearch` (balanced-paren scan; the runtime carries a small standalone duplicate) — plain `URLSearchParams` cannot parse a layout URL. Strict read, no lenient fallback: an unwrapped `_layout` value is treated as absent (the key is dropped on the next sync); an unbalanced span (paste-truncated URL — auto-linkers may eat the trailing `)`, accepted breakage) is invalid and falls back per LM-2's missing-layout rule. Params appearing *after* the `)` are ordinary globals — position is convention, the parens are the boundary.
  - Example: `?_layout=(/data/a.parquet,/data/b.parquet;/notes.md)` → a and b side by side on top, notes below.
- **LM-3** Params are **pane-local** (D72 — supersedes the original merged-pool design). The panel shell marks its window as a **param boundary** (`window._fusedParamBoundary = true`, same contract as TM-3), so a page rendered inside a pane targets its own pane's `/embed/...` URL; each pane's full query — user params included — is captured **segment-local** inside `_layout` by the ordinary sync (LM-6). The layout URL's top-level query carries **only hand-typed globals**: the shell never promotes params there, but a user may type `?city=sf&_layout=(…)` themselves — such params are readable from every pane (LM-7), read-only.

### 14.2 Panes

- **LM-4** A pane is an **`/embed/<path>` iframe** (D39): a full navigable chrome-free shell — panes can browse directories, open previews, use templates, all existing behavior for free. A pane is also an **arbitrarily narrow** host (the divider drags freely), and nothing here adapts a template to it: a template whose layout needs width collapses itself at its own breakpoint, and the pane's mode list is never edited on account of its width (PT-15).
- **LM-5** Pane bar (top of each pane): clickable **path crumbs** (segment click navigates that pane), then buttons: **split right**, **split down** (new pane duplicates the current pane's location), **maximize** (transient — fills the layout area, not encoded in the URL), **close**. Closing collapses single-child splits; when a close leaves only **one** pane (including closing the last pane directly), the shell exits layout mode by navigating to plain `/view/<that pane's path>` — a one-pane layout is never left on screen.
- **LM-6** Pane navigation syncs up: the layout view observes each pane's URL (iframe `load` + the pane window's `fused:urlchange`, LM-8) and re-encodes `_layout` on the shell URL via `history.replaceState` — refresh/bookmark always reproduce the current arrangement.

### 14.3 Params — target & sync (runtime change)

- **LM-7** The injected runtime's param target is the **topmost same-origin ancestor window**, stopping **below** any ancestor marked as a param boundary (`_fusedParamBoundary` — both layout shells set one, LM-3/TM-3/D72). In normal view/embed mode this is the same window as before (parent = top), so behavior is unchanged; inside a layout mode the climb stops at each pane's own embed shell, so **writes always land pane-local**. Reads additionally fall back to the same-origin ancestor chain **above** the boundary: hand-typed globals on the layout shell URL are visible in every pane (nearer ancestor wins; pane-local wins over all). `set()` never writes above the boundary — a pane setting a key that also exists globally shadows it locally.
- **LM-8** Change notification: the shell wraps **both** `history.replaceState` and `history.pushState` to dispatch `fused:urlchange` (today: only replaceState). The runtime listens for `fused:urlchange` on its target window and re-notifies `params.onChange` listeners — but only when the **visible (non-reserved) param snapshot actually changed** (snapshot diff). The diff guard prevents notification loops and duplicate fires (a `set()` would otherwise notify twice: once directly, once via the event; direct notify is removed in favor of the event path).
- **LM-9** Consequence, intended (D72): two panes using the same param key are **independent** — each pane's `set()` writes only its own segment-local query. Cross-pane linking is opt-in and manual: the user hand-types the shared key on the layout URL's top level, where every pane reads it (LM-7).

### 14.4 Entry & chrome

- **LM-10** Entry: **split-right and split-down icon buttons** in the breadcrumb's crumb-actions (next to ★ Bookmark, same glyphs as the pane bar's split buttons). Click → navigate to `<prefix>/_panel?_layout=(<seg>,<seg>)` (split right) or `(<seg>;<seg>)` (split down) (D51 grammar) where `<seg>` is the current fs path + its **whole** current query (D72 — nothing is promoted to the top level) — two panes side by side or stacked, both the current view with its params carried over (a single pane on entry looked like nothing happened).
- **LM-11** In layout mode the sidebar stays visible (bookmarks reachable, ★ button works on the layout URL — bookmarking a layout needs zero bookmark-layer changes, D20). Breadcrumb shows a static "Panel" label. The armed-bookmark "Update bookmark" flow (D38) works unchanged: pane/param drift rewrites the shell URL via replaceState → `fused:urlchange` → `syncUpdateButton`.
- **LM-12** Module: **`views/panel.js`** — tree codec, tree ops (split/close/collapse), pane DOM + bar, URL sync. Imports `router.js` only (one-way deps, ARCHITECTURE §6). `main.js` gains one sentinel branch; `shell.css` a `.layout-*` section; sidebar/bookmarks/api untouched.

## 15. Tab Mode — Tabbed Views (M6)

Goal: the same URL-is-state model as §14, but as **tabs instead of a grid**: one page visible at a time, a tab bar to switch. Primary use: a **bookmark folder rendered as one view** — click the folder, get its bookmarks as tabs, bookmark the result as a dashboard.

### 15.1 URL & route

- **TM-1** Route: `/view/_tab?...` and `/embed/_tab?...` — a sentinel pathname exactly like `_panel` (LM-1), intercepted by `route()` under both prefixes. Zero server changes.
- **TM-2** The tab list lives in the same reserved **`_layout`** param, as a **flat top-level `,` row** of the §14 codec — a tab segment is a fs path + optional segment-local query, same escaping (LM-2). Produced URLs are always a flat list; on parse, any nested structure (`;`, `()`) is defensively **flattened to its leaves in document order**, each leaf becoming a tab.
- **TM-3** Params are **tab-independent** (same contract as LM-3 since D72). The tab shell marks its window as a **param boundary** (`window._fusedParamBoundary = true`, set on render, cleared on teardown); the runtime's ancestor climb (LM-7) stops **below** a boundary-marked ancestor, so a page rendered inside a tab targets its own pane's `/embed/...` URL. Each tab's full query — user params included — is therefore captured **segment-local** inside `_layout` by the ordinary sync (TM-7); the tab URL's own top-level query carries only hand-typed globals (readable from every tab, LM-7).
- **TM-4** A tab segment's path may itself be a sentinel (`_panel`, `_tab`): the iframe src is just `/embed/<segment path>` + segment query, so a panel layout nests inside a tab through the ordinary pipeline (D45 embed support), its `_layout` riding inside the segment query. A nested panel's panes stay pane-local too (D72 — its own boundary stops each pane's climb) while staying isolated from every other tab.

### 15.2 Tabs

- **TM-5** A tab is an **`/embed/<path>` iframe**, mounted **lazily on first activation** and kept alive afterwards (`display:none` when inactive) — scroll/editor state survives switching, and hidden tabs keep receiving `fused:urlchange` (the runtime listens on the top window, LM-8), so param sync is live while hidden.
- **TM-6** Tab bar (top of the layout area): one button per tab — label = basename of the tab's **current** path (sentinel paths label as `Panel` / `Tabs`) — plus a close `×` per tab and a trailing `+` that opens a new tab at the configured start dir. Click activates. The **active tab index is NOT encoded in the URL** (avoids "Update bookmark" churn on every switch): bookmarks and fresh loads open the first tab; refresh and Back/Forward restore the last active tab via `history.state` (`fusedActiveTab` — rides the entry, never the URL).
- **TM-7** URL sync up, same machinery as LM-6: iframe `load` + tab-window `fused:urlchange` → read the tab's live location → re-encode `_layout` via guarded `replaceState`. Closing a tab removes its segment; closing the **last** tab exits to a plain view of its location (active prefix, like LM-5).

### 15.3 Entry — bookmark folders

- **TM-8** Clicking a bookmark **folder's name or row** opens the folder as a tab layout: each child bookmark's pathname becomes the segment path and its **entire saved query stays segment-local** (TM-3 — no hoisting, no cross-child key collisions; every bookmark keeps exactly its own params). A child that is itself a `_panel`/`_tab` bookmark just works (TM-4). Opening also **expands the folder** if it was collapsed (the sidebar should show what the tabs now show); the **folder glyph** keeps the plain collapse/expand toggle.
- **TM-9** A folder is not a bookmark: opening it arms nothing. ★ Bookmark on the tab view saves the composed URL as a normal bookmark; a tab layout opened *from* such a bookmark gets the full armed/update flow (D38) unchanged. Breadcrumb shows a static "Tabs" label; no breadcrumb entry button (folder-only entry).

### 15.4 Module

- **TM-10** The §14 codec (escape/parse/encode/segment helpers) moves to a shared **`views/layout-codec.js`**; `views/panel.js`, the new **`views/tabs.js`**, and `breadcrumb.js` import it. `tabs.js` owns the tab bar DOM, lazy iframes, and URL sync; `main.js` gains the `_tab` sentinel branch; `shell.css` a `.tabs-*` section; `sidebar.js` changes only the folder-row click wiring.

## 16. Custom Templates — User Overrides (M7)

Goal: users replace or add preview templates using the **exact same mechanism** as the built-ins (§7). A user template is an ordinary renderable-HTML page (plus optional sibling `.py` readers) that receives the target file as `_file` — nothing new is exposed; only the server's extension → template resolution gains a user-controlled layer. The resolution layer is server-only: the shell obeys whatever `templates` list the stat response carries (PT-8), and `/render` already renders any absolute path with the runtime injected.

### 16.1 Layout on disk

- **CT-1** A user template is a **self-contained folder** `~/.fused-render/templates/<name>/` holding `template.html` plus any sibling files it needs (reader `.py` files, css, assets) and optionally `icon.svg` (PT-11) — identical in shape to a built-in folder (PT-3). `<name>` carries **no** binding-by-convention semantics (CT-7), but it is the template's public name: it resolves by the single rule of PT-6, so a user folder named like a built-in **shadows** it. Relative `fused.runPython("./reader.py")` works unchanged because the template renders from its real path (PT-3).
- **CT-2** Bindings live in **`~/.fused-render/templates/registry.json`** — a flat JSON object mapping **dotted extension keys** to a template name, or to `null`. Keys may be compound (`.tar.gz`), carry `*` wildcard segments (`.*.json`), or end with `/` to bind a **directory** basename (`.obt/`), and the bare `/` key binds **any** directory (the universal key, D81) — the full key grammar is CT-3, and it is the same grammar the built-in `templates/registry.json` uses (PT-7, D73):

```json
{
  ".parquet": "geo",
  ".geojson": "geo",
  ".tar.gz": "archive",
  ".*.json": "config-view",
  ".obt/": "bundle",
  ".png": null
}
```

  A name binds the extension to a single-mode list of that template, resolved by the PT-6 rule. **`null` (or an empty list `[]`) disables** templating for that extension entirely: the file gets no template at all and falls through to the shell's metadata/raw-download fallback (§7.2) — on a directory key, to the plain listing view. `[]` and `null` are exactly equivalent (D94).
- **CT-10** **Mode lists (M8):** a registry value may also be a **JSON list of template names** — the full ordered mode list for that extension, **replace semantics**, first = default (PT-7). The string form of CT-2 is exactly a single-mode list; existing registries keep working unchanged.
- **CT-11** **`"..."` splice — REMOVED (D94, owner 2026-07-09).** The list-splice grammar is gone: a `"..."` entry is no longer expanded to the built-in list. `.` is still forbidden in folder names (CT-6), so `"..."` resolves to no template folder and is treated as an ordinary **dangling name** — dropped from the rendered list with a `template_error` (CT-6), and surfaced as a broken (`exists:false`) ref in the registry view so the user is prompted to remove it (nothing is auto-removed). To include the built-in modes, list them explicitly.

```json
{
  ".parquet": ["geo-view", "geo"],
  ".md": "my-markdown",
  ".csv": null,
  ".log": []
}
```

### 16.2 Resolution

- **CT-3** **Key grammar and matching (revised by D73).** A key is a **dot-anchored suffix pattern**: one or more dot-led segments, optionally ending in `/` to bind directories — plus one special zero-segment key, the bare `/` (the **universal directory key**, D81), which matches *any* directory. A segment is a literal (`json`, `tar`) or the wildcard `*`, which matches **exactly one whole non-empty segment** — partial wildcards (`.geo*`) are invalid, and a malformed key (no leading dot, empty segment) never matches (silently ignored, as keys without a leading dot always were). Matching is **case-insensitive** against the basename and requires a **non-empty stem** before the matched suffix (a file literally named `.json` does not match the `.json` key; `.hidden.json` does — its stem is `.hidden`). Directory keys match only directories, file keys only files. **Specificity:** more segments beats fewer; at equal length, comparing from the **rightmost** segment, a literal beats `*` — so for `data.xyz.json`: `.xyz.json` > `.*.json` > `.json`. The universal `/` key has zero segments, so it ranks **below every** dot-anchored directory key (`.zarr/` > `/`); its stem is the whole basename (D81). **Both registries are matched by this same rule** (the old `splitext` single-extension built-in table is gone, D73); precedence stays **any user-registry match > built-in match** — a user `.json` binding beats a built-in `.xyz.json` one. Any extension may be bound, including ones no built-in handles.
- **CT-4** *(revised by D73 — the exemption is dropped.)* `.html`/`.htm` are **ordinary registry keys**: their default list `["_render", "code"]` ships in the built-in registry (PT-7), and users may rebind or reorder them like any other extension — rendered-HTML-by-default stays the shipped behavior (§4), no longer an enforced one. `_render` (and any future name in `KNOWN_SENTINELS`, PT-12) is referenceable from registry lists; all other `_`-prefixed names remain invalid — dropped per CT-6 with `template_error`.
- **CT-5** Registries are read **per stat/render resolution** (tiny local files — no restart, no cache invalidation problem); the built-in `templates/registry.json` rides the same loader (D73). Missing `~/.fused-render/templates/` or `registry.json` = clean no-op, built-in behavior; first run creates nothing.
- **CT-6** **Validation and fallback — per entry:** a folder name must be a single safe path segment (no `/`, no `..`, no `.`, not empty) — it is joined into a filesystem path, so a malformed name must not stat arbitrary locations (correctness guard, not auth — §9 stands). Within a mode list, an entry whose name cannot resolve (unsafe name, `template.html` missing in both PT-6 locations) is **dropped** from the list, and the stat response carries a **`template_error`** string naming the first problem, so a typo is visible (via stat / server log) instead of silently ignored. If the user's value resolves to nothing at all (unparseable JSON, every listed name dangling), fall back to the **built-in list** for that extension. An explicitly **empty** list `[]` is not this case — it disables (CT-2/D94), no fallback.
- **CT-7** **No convention fallback:** a folder in `~/.fused-render/templates/` without a registry entry is inert — a draft. Registration is only ever the registry line; deleting the line unregisters. One source of truth.
- **CT-12** **Conditional templates (per-folder gate, deferred evaluation).** A template folder may ship an optional **`condition.py`** beside its `template.html`, defining `def main(path): bool`, for **both** built-in and user folders (whichever `template.html` PT-6 resolves) — so one registry key can offer different templates for different files (e.g. gate on a file's actual contents, a path prefix, or a naming convention). No `condition.py` = unconditionally shown (the common case). Gates may do real I/O (the H3 gate reads a parquet footer), and over a **remote mount** that I/O would stall every stat of the extension — so stat does **not** run gates: resolution (PT-8) only *marks* the entry `"conditional": true` (an isfile() check), and the shell resolves the verdicts **in the background** via **`GET /api/fs/conditions?path=<file>`** → `{"path", "conditions": {<mode>: bool}, "error"?}` while the default (first unconditional, PT-8) template already renders. Until its verdict lands, a conditional entry shows in the switcher (PT-10) as a **disabled pending spinner** — not selectable, never the default — then either becomes an ordinary mode or disappears; a `_mode` deep-link to a gated mode holds the preview body on a "checking" placeholder until the verdict, and an **all**-conditional list holds the whole preview. Each `condition.py` is loaded **fresh per evaluation** (CT-5) — no restart — and never inserted into `sys.modules`; multiple gates on one file are **evaluated concurrently** (one worker per gate; the fixed-name, never-`sys.modules`-inserted load keeps parallel evaluation safe), so the cost is the slowest single gate, not their sum. A broken condition (no callable `main`, an exception, evaluated on the target path) reports the mode **denied** — fail closed, a template gated by code that can't decide is not silently shown — and surfaces the reason as the payload's **`error`** (first broken gate in list order, the same posture as `template_error`/CT-6). **Fail-closed is a rule about a GATE'S OWN VERDICT, and it is not a rule about the transport that collects verdicts.** A gate that cannot decide answers `false`, and every surface honours that `false` by hiding the entry — but the *response* always carries a key per gated mode, so a **missing** key is not a denial, it is evidence the `/api/fs/conditions` call itself failed. Treating a failed call as denial is how the shell used to behave and it was wrong in a visible way: it emptied the mode list, and a list of one renders no control at all, so a slow or failing probe made the whole mode control disappear while another surface still offered the gated mode. One policy now decides this for every mode surface — `frontend/src/platform/lib/mode-visibility.ts`, shared by the preview header (PT-10), the listing's preview pane (FS-12), the pane bars and the Open With menu: unconditional shows; verdicts in flight show as pending (never selectable, never the default); an explicit `false` hides; an absent verdict **shows**. The worst case of showing is a mode whose template then declines to render — visible and recoverable; a vanished control is neither. Sentinel modes (`_render`, `_listing` — PT-12, `path: null`) have no folder and are never gated. The registry stays the source of truth for *which* templates apply to an extension; `condition.py` only narrows *whether* a listed one shows for a specific file.

### 16.3 Pipeline & dev loop

- **CT-8** No new pipeline: stat carries the resolved user templates inside the ordinary `templates` list (PT-8); the preview iframe renders the selected mode via `/render` with `_file` exactly like a built-in (PT-2), and the switcher (PT-10) shows user modes indistinguishably from built-ins. M4 auto-reload (§13) covers template development for free — the rendered page watches its own html and every `runPython` file, so editing `template.html` or a reader live-reloads open previews. Registry edits apply on the next stat (navigate/refresh); open previews do not watch `registry.json`.
- **CT-9** **Authoring skill:** a repo skill `skills/fused-render-custom-templates/` covers folder layout, registry format, and registration workflow only; it **delegates all html/py authoring guidance to `skills/fused-render-authoring/`** (no duplicated instruction — one source for the runtime API and template patterns).

## 17. Annotation — An Ordinary View Template (M9, superseded)

Annotation shipped first as an app feature — an orthogonal `_annotate=1` overlay
injected into every view (M9) — and was then **rebuilt as an
ordinary view template**, the same pattern as `templates/claude/`:
`templates/annotate/` is a self-contained template.html, swappable/shadowable
like any template (PT-6). It **was** bound in registry.json as a trailing mode on
annotatable extensions (66 keys); as of **D235 it is bound to nothing** — the
annotation tools live in the chat template's pane, where the review and the chat
that acts on it are one surface instead of two modes the user switches between,
and a second staler way in was not worth keeping. **Since D237 the mode is
unreachable for two independent reasons**, which matters because either one alone
would be repairable by a registry edit: it has **no binding**, *and* its
Send-to-Claude handoff has **no receiver** — the receiving half
(`claudeComments`/`claudeReturn`) lived only in the plain chat template D237
deleted, and the surviving chat never grew it. A `_mode=annotate` URL therefore
falls to the target's default view (PT-9), and a hand re-binding of the folder
brings the review UI back but *not* the handoff. The folder still ships, so a
user can re-bind it themselves (`~/.fused-render/templates/registry.json`, §16 /
CT-2), and everything below still describes it accurately — which is why this
section is retained rather than deleted. It renders the file's normal view in a same-origin iframe (a
`view` param picks WHICH mode is being annotated) and implements the whole
experience itself — hover highlight, click-to-comment pins, sidebar,
resolve/delete. Comments live in an ordinary `comments` template param (synced
to the shell URL by the runtime — bookmarkable, shareable), stamped with the
view they were made on so anchors never cross-resolve between views.

Rationale: annotation is a review layer, not app chrome — as a template it
needs no shell code, no server injection, and users can replace or extend it
by dropping a folder into `~/.fused-render/annotate/`. The `_annotate` render
param, the header toggle, the injected `static/annotate.js`, and
the code template's selection adapter are gone.

**Containment invariant:** every line of annotation logic lives inside
`templates/annotate/template.html` — no other view template carries
annotation code, hooks, or references, and nothing is injected into the
framed view (the template attaches its listeners and one highlight-tint
`<style>` to its own nested same-origin iframe at runtime; that code ships in
the annotate file). Paged views (table, xlsx, pdf) render **stable element
ids** encoding an absolute address — `__fr_r<row>_c<col>`,
`__fr_s<sheet>_r<row>_c<col>`, `__fr_page_<n>` — inert, deep-linkable markup
useful independent of annotation. The annotate template owns an
`ID_RESOLVERS` table keyed on those id shapes: a recognized anchor id that
isn't in the mounted DOM is **off-page, not detached** — the sidebar card
gets a navigable chip ("row 5" / "Alpha · row 3" / "page 3") and clicking it
navigates the framed view there by writing the ordinary `offset`/`sheet`
params the view already watches (the same shell-URL params its own pagination
controls write). An earlier iteration had each paged view expose a
`window.__fusedAnnotateAnchorResolver` hook instead; removed (D78) because it
put annotation-aware code inside view templates. Accepted trade-off: annotate
cannot ask a view whether a row is truly gone from the data, so a comment
past the data's end keeps its "row N" chip instead of turning "detached".

**Which anchor strategy a view gets** is decided inside the annotate template
alone (the containment invariant above), and the rule is **a lineNumbers gutter
that is actually laid out** (`getBoundingClientRect().height > 0`), not "is this
CodeMirror" and not merely "does `.cm-lineNumbers` exist": the gutter is the only
place the line number can be read from, and `cmVisibleLines` pairs only gutter
elements with real height, so the probe must make the same measurement. The
markdown notes view ships `basicSetup` — the gutter element is there — but hides
`.cm-gutters` outright, being prose (MD-18a), so it takes the ordinary
element-path/quote anchors instead. Getting that wrong is silent by construction:
both gesture handlers `preventDefault()` before resolving and bail on an
unresolvable anchor, so the mis-detected view swallowed every click, dropped
every selection and painted no stored comment, with nothing logged anywhere
(fixed 2026-07-31). In a gutterless editor a quote anchor's container is hoisted
to `.cm-content`, because the `.cm-line` div the selection actually sits in is
remounted on scroll and its structural path drifts onto another line; the quote
plus its occurrence index locates the span within the stable content element, and
off-screen text simply has no pin until it scrolls back in — off-screen, not
detached, as with code anchors.

**Comment focus deep link:** an ordinary `comment` template param carries an
id-only deep link (the history→annotate contract, HV-8; mirrors the claude
`session_id` resume precedent — the id is the whole contract and is never
cleared after use). At boot, once the framed view is wired, the template reads
`comment`: if the id is in the live URL store it focuses it (jumping to the
comment's own view first when it differs, then lighting the pin/card); if it
isn't, the template does a **one-shot full-state hydration** — a single read of
`<file>.json`'s `comments` log that imports every LIVE entry (those without a
`deleted_at` tombstone; a tombstoned wanted id gets no import and no focus —
deleted stays deleted, owner call 2026-07-10), strips the server stamps
(`recorded_at`/`updated_at`/`deleted_at`), and merges them into the live set
(live entries win by id) — then saves once (re-recording, a harmless upsert
no-op) and focuses. Deletion is an **explicit** signal: the annotate delete
button drops the comment from the URL and sends its id as `deleted_ids` on the
SAME `record` call, so upsert and tombstone land in one atomic sidecar write
(two separate calls could interleave and lose the tombstone); `annotate.py`
stamps `deleted_at` (server `time.time()` SECONDS) on each named log entry.
The tombstone is **permanent** — recording an id never clears it, so a stale
bookmarked URL that still carries the deleted comment (or the hydration merge's
live-wins rule) cannot silently resurrect it in the log. Absence
from a `record` array NEVER deletes — each URL carries only its own review
subset, so a missing id means "not in this review", not "deleted". The live URL
`comments` param stays the sole live store; the sidecar read is one boot-time
hydration for a deep link whose id is absent from the live set, not a live-store
sync back from the sidecar. An unreadable/unparseable sidecar or a missing id
fails silently (no error UI, no focus).

**The whole Send-to-Claude round trip below is DEAD CODE as of D237, and it is
recorded rather than deleted.** Both legs are described exactly as built, because
the annotate half still exists and the design is what a future re-wiring would
start from; what no longer exists is the other end. The receiving half —
capturing `claudeComments`/`claudeReturn` at boot, stripping them from the shell
URL in one `replaceState`, and navigating back when the run that carried the
comments reports `done` — lived only in the plain full-width chat template, which
D237 deleted; the surviving chat (PT-14/PT-16) never grew it. So the outbound leg
still *navigates somewhere real* (`_mode=claude` resolves) and the payload is
simply dropped. Re-pointing the handoff at the surviving chat is a decision with
work in it, not a rename: the annotation tools are already in that template's
pane, so the honest question is whether a handoff should exist at all.

**Send to Claude is a ROUND TRIP, and each comment rides it once** (fixed
2026-07-31; it was previously a one-way trip that re-sent the whole review every
time). The handoff sends only the comments that are neither `resolved` nor
`sent` — `sent: 1` is an ordinary per-comment field, stamped on exactly the
comments in the payload and `save()`d *before* the mode switch so it lands in the
`comments` URL param and, through annotate.py's verbatim field merge, in the
`<file>.json` log. The flag is a bare truthy `1` because the URL store's ~6 KB
budget is the binding constraint and the sidecar's own `updated_at` already dates
the write. The flag is stripped from what the agent sees (the payload is
serialized before stamping) — it is bookkeeping for the URL store, not something
`formatComments` should hand the model to reason about. A comment **being sent
right now is off-limits to eviction**: the save that stamps `sent` is the save that
can evict on it, and since the stamp is what pushes the list over budget the
payload is then the whole sent tier — so the payload's ids are passed to `save()`
as protected, and an over-budget write is preferred over dropping a comment from
the live review to make room for a flag (the "removed" bar note explaining it
would be destroyed by the navigation milliseconds later, making that loss silent).
Eviction is not annihilation — the same `save()` mirrors every comment into the
`<file>.json` log, where absence never deletes, so an evicted comment stays
recoverable through the history view (§24); what it leaves is the **live** store,
the rail and pins and the shared URL, which for a comment mid-send is loss enough.
A sent card shows a "sent ↗" chip so its exclusion is visible — but **not** on a
resolved card, which is the terminal state, already explains the exclusion, and
takes the stronger dim (`.card.resolved.sent` encodes that precedence in
specificity, because the equal-specificity pair it replaced handed the win to
whichever rule was written second). The
**Reopen** action clears the flag, which is the one way to hand a comment over
again; and when nothing is sendable the button writes the inline bar note instead
of navigating. The URL budget's eviction order gains a second tier — oldest
`resolved` first, then oldest `sent` (already in a chat transcript), never an
open unsent comment. `sent` is deliberately **not** surfaced in the history
timeline (§24): the sidecar is a write-only log where absence never deletes, so
the un-send that Reopen performs (the key simply leaves the URL) cannot be
represented there, and a label that can go stale is worse than no label.

The return leg was the chat's — the half D237 deleted, per the note above; what
follows is how it worked, not what runs. Annotate sets `claudeReturn=<mode>` alongside
`claudeComments` (and re-asserts `view`, the param naming which sibling view is
framed), the chat template captured both into memory at boot and stripped both
from the shell URL in the same `replaceState` (a Back entry must not re-attach a
review that was already sent), and the *one* run whose message actually carried
attached comments navigates the shell back to that mode when it reports `done`
without an error. The ticket is **threaded, not latched** — it travels as an
argument from `composeAndSend` → `sendMessage` → `pollLoop`, so it is bound to one
message and, past `start`, to one run id. A module-scoped flag was the first
attempt and leaked: a failed `start` never reaches `pollLoop`, so the flag stayed
armed and the user's next unrelated question navigated the shell away mid-thread,
for a run that carried no comments. A later turn in the same session therefore
stays in the chat, an errored or aborted run stays put (there is nothing new to
look at, and leaving would hide the error), and a run re-attached on a fresh boot
never returns. A `start` that fails **hands the comment chips back** (`attached`
is restored and the user is told), because annotate has already stamped those
comments `sent` and nothing else would ever offer them again; past `start` the
agent has the message, so restoring would send them twice.
This is necessary because the chat calls `fused.autoReload(false)`
and owns the viewport — nothing else would ever bring the reviewer back to the
edited file. Both directions are **deliberate standard-breaks** documented in the
template comments: `_mode` is a reserved param name that `fused.params.set`
refuses, and in a pane the shell URL sits above the param boundary (D72), so a
top-level URL write is the only mechanism; the navigation reuses the history
template's `navigateShell` idiom (pushState + a `fused:navigate` event, with a
`location.href` fallback), and both sides excise and reattach the balanced
`_layout=(…)` span byte-for-byte before `URLSearchParams` sees the query.

Both legs read `window.top`'s search **raw**, so both must reckon with the
runtime's coalesced history write (D99): a param this page just set may still be
at its pre-click value in that string, and copying it forward silently reverts it.
Annotate sweeps every key the top URL already carries against
`fused.params.getAll()` (which reads through the pending overlay) before writing
its own — that covers the `sent` stamps, and `offset`/`sheet` from a reveal inside
the coalescing window, without pushing a pane-local param onto the shell URL; it
then re-asserts `comments` from what `save()` actually persisted, which the sweep
cannot know. The return leg fires `pagehide` — the runtime's own documented flush
hook — before reading the URL and pushing its entry, so a pending write neither
goes missing from the copy nor lands *after* the push and `replaceState` the
pre-return search over it. Known gap: the return discards whatever the user had
typed into the chat input when it fires.

**Revert + version timeline** (§34, D194) is the one surface in this template
that is not about comments: a footer block in the sidebar offering "Revert last
change" plus an expandable list of every checkpoint Claude Code holds for the
target, each individually restorable behind a confirm sheet. It is its own footer
rather than part of `#sidefoot`, which is hidden wherever the file has no
`claude` mode — reverting has nothing to do with whether a chat view exists.
`annotate.py` gains three actions (`history`, `revert_plan`, `revert`) over
`../shared/file_history.py`; all the store semantics, the safety gates and the
degradation rules live in §34, and everything there is deliberately
annotate-agnostic so `claude` and `history` can adopt the same reader. Its own
panel state (disclosure, last outcome) deliberately stays OUT of the URL
(§34/FH-15), which is also why the round-trip's D99 staleness sweep above has
nothing of the revert panel's to carry forward or revert: the two features share
this template's sidebar but not one byte of its param surface.

## 18. Export — Portable Bundles for Hosted Serving (M10)

Goal: pack a renderable page into a portable *bundle* that a **separate** hosting
layer (the `fused` wheel's `build_html_artifact`) can serve — without weakening the
local-only invariant (§1). Export is a **local, offline call on the already-running
server** (`POST /api/export {"page", "out"}`, both absolute paths): it uploads
nothing and reaches no network — it writes the bundle to a local directory, the same
as every other filesystem-touching endpoint. fused-render itself still hosts
nothing. Full detail: `docs/EXPORT.md`.

### 18.1 Bundle format

- **EX-1** A bundle (format **v2**) is a directory holding `manifest.json` (the hosting
  contract) and a single **`files/` payload dir** mirroring the page's folder — the page,
  each `runPython` target, each `rawUrl`/`readFile` target, and each first-party module a
  bundled entrypoint imports (EX-7), all at their real page-relative path. There are no
  `code/`/`assets/`/`resources/` category dirs; the bundle layout equals the author's
  folder equals the served runtime tree (docs/bundle-v2-design.md).
- **EX-2** `manifest.json` (`{"fused_render_bundle": 2, "root", "page", "entrypoints",
  "assets", "resources"}`) classifies each payload file by role: `page` (the shell), each
  `entrypoint` (`path` = the page's literal string for the runtime's seed map, `name` = the
  served route, `key` = payload-relative path), each `asset` (`path` = literal, `name` =
  payload-relative key + `_asset` allow-list entry), each `resource` (`key` = payload-
  relative path). The hosting layer wires the runtime from this map — it never re-parses the
  HTML. Every file's bundle location is `root/<payload-relative path>`, and that same path
  is its runtime key: it lands under the served project root (the runtime's cwd +
  `sys.path[0]`), so a page's own `open("data.csv")` / `import helpers` resolve unchanged.
  (The hosting layer's `load_html_bundle` still reads legacy **v1** bundles — category dirs
  + explicit `file` fields — for version-skew tolerance.)

### 18.2 Portable subset

- **EX-3** Only the transport-agnostic part of the injected `window.fused` API is
  portable: `runPython` (→ a served route the page posts to, including its RH-9
  `opts.key`/`opts.signal` cancellation), `rawUrl`/`readFile` (→ read-only bundled
  assets), and `params` (pure client-side URL state, unchanged). `writeFile`, `stat`,
  and SSE live-reload are **unsupported** — a hosted artifact is immutable and has no
  filesystem behind it. `fused.ai` (RH-11) is also **unsupported**: it runs the claude
  CLI on the author's own machine, which a hosted page cannot reach.

### 18.3 Static resolution & fail-loud

- **EX-4** Blocking errors — export writes nothing and reports all problems at once,
  rather than shipping a page whose calls 404 when hosted: a **computed `runPython`
  path** (its served route name is derived from the literal, so it can't be routed),
  an **unsupported API call** (`writeFile`/`stat`), an **absolute or `..`-escaping**
  path (including a symlink resolving outside the page dir), or a **missing target**
  (a referenced file, or an `include` file, not on disk).
- **EX-4a** Warnings — advisory, never blocking: a **computed `rawUrl`/`readFile`
  path** (the exporter can't discover the target from the HTML, but once the target is
  bundled — via an `include` glob in the page's manifest (EX-8) or an explicit `include`
  (EX-6) — the served `_asset` route resolves it by key at request time, and the hosted
  runtime resolves the computed path to that key; a call `fused.rawUrl("data/" + name)`
  is a string *prefix* + expression, so it is counted here as computed, **not**
  mis-collected as a literal `data/` target). This warning is **suppressed when a
  `manifest`-source asset (EX-6/EX-8) survives into the final bundle** — a `bundle`
  provenance pill in §19's list (DP-2a) that shows the user what backs the call, so the
  nag would be redundant. It keys on the *surviving* asset, evaluated after dedup and
  exclude, **not** the raw manifest globs: a manifest entry that is also a literal
  reference is deduped to a `reference` asset, and any manifest file can be dropped by
  `exclude` — in both cases no `bundle` row remains and the warning still fires. A
  per-deployment `include` (EX-6, source `include`) never suppresses it: that selection
  is not checked in with the page, so a fresh export without it would still 404. Also
  warned: an **`exclude` that drops a literally-referenced file** (honored, but that call
  404s when hosted).
- **EX-5** Route names derive from the `.py` stem (`sine.py` → `sine`), are prefixed
  `run-` when they'd collide with a reserved serve route (`data`, `health`, the
  `_`-prefixed control/shell/asset routes), and are suffixed `-2`, `-3`, … on
  duplicate stems — so the map is always valid and injective.
- **EX-6** The auto-detected set can be adjusted by an optional selection on
  `/api/export` (and the Deploy modal, §19): `include` — extra page-relative files
  bundled as assets beyond the literal scan (for a computed-path target or data a
  bundled `.py` reads at runtime), each validated like a scanned asset and deduped by
  key; and `exclude` — files dropped from the final set by literal path or bundle
  key. Both default empty (auto-only). Each bundled asset carries a `source` —
  `reference` (a literal `rawUrl`/`readFile` the scan resolved), `manifest`
  (declared in the page's EX-8 manifest), or `include` (added out-of-band via the
  selection) — attributed to the strongest claim in that order when a file is
  reachable more than one way, and surfaced on `/api/deploy/preview` so §19's list
  can label how each file is exposed (DP-2a). It is an in-process/preview
  classification only: `manifest.json` (EX-2) does not carry it — the hosting
  layer treats every asset the same.
- **EX-7** First-party **modules** a bundled entrypoint imports are discovered by a
  static AST scan of the entrypoint sources (transitively) and shipped as `resources`,
  so a served entrypoint's `import helpers` resolves without hand-listing. Only an
  absolute import resolving to a `<name>.py` **beside the page** is bundled (stdlib /
  third-party / subpackage imports are left alone; a relative `from . import x` is
  skipped — a hosted entrypoint runs flattened with no package context). Unlike an
  asset, a resource is runtime-only: it ships into the tree so `import` works but is
  **not** on the `_asset` allow-list, so its source is not web-served. A module already
  carried as an asset (assets land at the same real key) is not bundled twice; excluding
  a module a bundled entrypoint imports is honored but warned (the import will fail).
  Under v2 (EX-1) a discovered module is stored at `files/<key>` like every other payload
  file; it is still enumerated in the manifest's `resources` so the hosting layer knows to
  ship it (and to keep it off the `_asset` allow-list). Full design + rationale:
  [`docs/bundle-v2-design.md`](docs/bundle-v2-design.md).
- **EX-8** A page may declare its own bundle set **in the repo**, reproducibly, via a
  single embedded `<script type="application/fused-bundle">` block holding a JSON object.
  Only **`include`** is read today: an array of page-relative **globs** (`data/*.json`,
  `tiles/**/*.png`) and/or literal paths, expanded against the page dir through the same
  safety gauntlet as any asset (`..`/absolute/symlink-escape rejected) and folded in
  **beneath** the caller's EX-6 `include`. A glob matching nothing is a **warning**, a
  missing literal a **blocking error**. The block is **unversioned and forward-lenient**
  — the `type` attribute is the discriminator, and unknown keys are ignored so new
  directives can be added later without breaking an older exporter. It is **stripped
  before the dependency scan**, so its JSON body can never be misread as a `fused.*`
  call. `exclude` is **not** honored in the manifest (it would publish the withheld file
  names in the served page source) — it is warned about; drop files via EX-6 `exclude`
  (kept on the deployment record, off the artifact). This is what collapses a
  hand-maintained `RAW_URLS`-style table (or a fake `_bundle*()` scanner-bait function)
  down to `fused.rawUrl("data/" + name)` against a `data/*.json` glob.
- **EX-9** `manifest.json` carries a top-level **`cache_max_age`** (`"0s"` off by
  default; a duration like `"5m"`/`"1h"` — the same format the fused repo's
  `openfused.caching.parse_cache_max_age` accepts) — the Deploy modal's caching
  choice (DP-17), written fresh on every export so a redeploy always re-asserts
  the current setting. The hosting layer's `build_html_artifact` applies it
  **page-wide** — to every route uniformly (the shell, each `runPython` route,
  and the asset route), matching the managed backend's mount-wide caching; see
  the fused repo's spec/serve/fused-render.md § Caching. A bundle exported before
  this field existed omits it, which the hosting layer reads as off.

## 19. Deploy — Hosted Publish through the fused CLI (M11)

Goal: close the gap between §18's bundle and a working URL, from the shell. The
local-only invariant (§1) is unchanged in kind: fused-render still binds
127.0.0.1, hosts nothing, and mints no URLs — **deploying is an explicit user
action that delegates to the `fused` CLI** (bundled in the packaged app, a pip extra otherwise; `fused share`,
the fused repo's one URL-minting operation — its spec/serve/share-links.md and
spec/serve/fused-render.md; the same shell-out pattern the flow app uses for
project deploys). The server orchestrates the child process; nothing else in
the product gains network access.

### 19.1 Surface

- **DP-1** Any file preview whose mode list carries the `_render` sentinel
  **and** whose filename is `.html`/`.htm` shows a **Deploy** header action —
  both conditions, because that is exactly the set `/api/export` accepts: a
  registry rebind can put `_render` on any type (D73), but the exporter is
  extension-gated, and the button must never open a modal that cannot deploy.
  Additionally gated on the opt-in `deploy_enabled` pref (PF-8): Deploy is off
  by default, so the button is hidden entirely until enabled from Preferences
  → Deploy to Fused account (re-read on focus/visibility, so a toggle shows through
  without a remount).
  A green dot marks a page whose stored deployment reads active (a local
  pointer read — opening a preview never spawns the CLI; re-read on tab
  focus/visibility regain, so an out-of-band revoke — e.g. the Preferences
  page in another tab — shows through without a remount). Directories never
  show it. The action opens the Deploy modal.
- **DP-2** The modal handles its states in order: the fused CLI missing → an
  install panel; no hosted env configured → guidance (`fused env create` /
  `fused cloud setup`, naming the envs file); else the form — env picker,
  current-deployment card (status chip, URL with copy/open), a **"Will
  publish" preview** (DP-2a), a collapsible **Link** section (DP-9a), a
  collapsible **Caching** section (DP-17), an owner-only collapsible **Recent
  errors** diagnostics section (the deployed mount's captured failures via
  `fused share errors`; rendered for an undeployed page too, but **disabled**
  with a hint, so the chrome is consistent rather than popping in on first
  deploy), Deploy/Redeploy, and Revoke. The modal is scoped
  to the current page; the **env-wide** deployment list (DP-13) lives on the
  Fused account tab's Deployments section (AC-11, moved from Preferences
  when the account surface landed), not in the modal.
- **DP-2a** Before the click, the modal shows exactly what a deploy would
  publish (`POST /api/deploy/preview` → `preview_deploy`, the same pure
  `plan_export` scan the real export runs, resolved fresh with the current
  selection, no files written): the page plus each `runPython` target (and its
  served route name) and each asset. Every asset row carries a **provenance
  pill** driven by the preview's per-asset `source` (EX-6) so the list *mentions
  how a bundled file is exposed*: `rawUrl` — a scanned literal
  `fused.rawUrl()`/`readFile()` reference (the page fetches it via
  rawUrl/readFile); `bundle` — a file declared in the page's fused-bundle
  manifest (EX-8), which auto-shows here to back a computed rawUrl/readFile path;
  `added` — a hand-added include. All three are served read-only on the hosted
  `_asset` route (the pill's tooltip says so). Export
  blockers (EX-4) come back in the same response and **disable Deploy** with the
  full list — an unexportable page reads as "fix these" up front, never as a
  failed deploy; warnings (EX-4a) show alongside but never block. A preview
  *fetch* failure (unexportable type, file deleted since the header rendered)
  degrades to a blocker entry the same way — the dialog still renders its form; it
  never dead-ends on the preview call. (Preview is `POST`, not `GET`: it carries
  the include/exclude selection, which doesn't fit a query string; it stays
  read-only and unguarded.)
- **DP-2c** The "will publish" list is **editable** — the user layers a file
  selection (EX-6) on the auto-detected set: remove a listed file (× → `exclude`),
  restore an excluded one, add extra files via a picker over the page's folder
  (`walkDir`, gitignore-aware), "Add all in folder", or "Reset to default"
  (clear both lists). The selection is sent on Deploy and **persisted on the
  deployment record** (`include`/`exclude`, beside `entrypoints` — no separate
  sidecar), so a reopened modal reloads exactly what was last published. This is
  how a page whose data is fetched by a computed path deploys at all (EX-4a): the
  author bundles those files explicitly.
- **DP-2b** Login state, before and after the click (amended by §27/M18: the
  warning is now an *action*, not guidance).
  `GET /api/deploy/config` carries `fused_logged_in` — presence of the fused
  CLI's own control-plane credentials file
  (`~/.openfused/fused-cloud-credentials.json`,
  `OPENFUSED_FUSED_CLOUD_CREDENTIALS` honored). Presence-only by design: an
  expired-but-refreshable token still works (the CLI refreshes silently), so
  the CLI stays the authority at action time. With a managed `fused` env
  selected and no credentials on disk, the modal warns **before** the click
  and offers a working **Sign in to Fused** button — the AC-3/AC-4 in-app
  flow via the shared client hook, with a background config reload flipping
  the warning away on completion (AC-9). Likewise the no-envs state signs in
  in place or routes to the account tab's setup panel; no modal state
  instructs a terminal command for the managed path anymore. After a failed
  action, CLI errors that name `fused cloud login` are still suffixed with
  the packaged app's real wrapper path (fusedcli.py's `cli_error` +
  `setup_cli_hint`) — plain `fused` doesn't resolve inside the .app, and the
  CLI's error text must stay runnable as printed even though the app now
  offers the in-app path first.

### 19.2 The fused CLI seam

- **DP-3** CLI resolution (`deploy.fused_cli`) has **exactly two sources —
  one explicit, one autodetected — and nothing else**: (1)
  `FUSED_RENDER_FUSED_BIN` (verbatim, whitespace-split — compound commands
  work, and it is the test seam); (2) the `fused` package **importable in the
  server's own interpreter**, run as `[sys.executable,
  fused_render/_fused_cli.py]` — a shim that sets `argv[0] = "fused"` and
  calls `fused._cli.main()`, behaviorally identical to the console script.
  There is deliberately **no venv-bin scan, no PATH lookup, and no
  well-known-location guessing**: a CLI the server didn't get from its own
  interpreter runs only because the user explicitly configured it. (The old
  venv-bin step is subsumed — a venv whose bin/ has the script always has the
  package importable.)
- **DP-3a** Child-env hygiene: an **external** CLI (the override) is spawned
  with `PYTHONHOME`/`PYTHONPATH` scrubbed — inside the packaged app those are
  bundle-scoped and would break any other Python (the las template's
  external-spawn precedent); the in-interpreter shim keeps them (they are
  what make `sys.executable` work in the bundle). `OPENFUSED_ENV` targeting
  (DP-7) is unchanged for both.
- **DP-4** When the CLI is missing and installing is possible (Python ≥ 3.11
  per the wheel's marker, and the interpreter has pip), `POST
  /api/deploy/install` pip-installs **the wheel pinned by
  `deploy.PINNED_FUSED_REQUIREMENT`** into the server's interpreter — which
  makes the package importable there, i.e. lands in DP-3's autodetected
  source (finder caches are invalidated after the install so the probe sees
  it without a restart). The constant is the in-code source of the pin;
  pyproject.toml's `[fused]` extra must reference the same wheel and a test
  pins the two together. Reading the pin from installed dist-info metadata is
  rejected: metadata is absent on source-tree runs and stripped app bundles,
  and goes stale on an editable install that predates the extra — all of
  which disabled the button exactly when it mattered, while the constant
  ships in the same file as the code using it. When installing is impossible,
  the modal states why — old Python, or a pip-less embedded interpreter
  (point `FUSED_RENDER_FUSED_BIN` at a fused installed with another Python) —
  plus the manual `pip install "fused-render[fused]"` hint.
- **DP-16** The packaged macOS app **ships the CLI**: `build_dmg.sh` installs
  the `[fused]` extra into the bundle (py2app force-copies `fused` + its
  data-bearing deps — `setup_py2app.py`), so DP-3's autodetected source is
  always present and the install panel never appears in the .app (its sealed,
  notarized bundle could not be pip-installed into anyway). The build also
  ships a terminal wrapper, `Contents/Resources/bin/fused` (bundled python +
  the DP-3 shim), and smoke-tests real CLI verbs through the shim before
  signing, so a py2app packaging gap fails the build rather than the user's
  first deploy. Since §27/M18 the wrapper is a **power-user escape hatch**,
  not the setup path: sign-in and managed-env setup happen in-app (AC-3/AC-6),
  and the wrapper remains for what stays terminal-scoped — self-hosted AWS
  provisioning (`fused env create` / `fused infra serve`) and ad-hoc CLI use.
  The wrapper lives under `Resources`, not `MacOS`: everything in a bundle's
  `MacOS/` is nested code to codesign, and a shell script there cannot carry
  a code signature — the bundle seal fails ("code object is not signed at
  all"); a script under `Resources` is sealed by the resource rules instead.
  `GET /api/deploy/config` carries `setup_cli` — the wrapper's absolute path
  when frozen (`sys.frozen == "macosx_app"`), else `"fused"` — and CLI error
  suffixes plus the remaining AWS guidance name it.

### 19.3 Environments

- **DP-5** Eligible deploy targets are the **hosted** environments in the fused
  CLI's own store (`~/.openfused/envs.json`, `OPENFUSED_ENVS_FILE` override):
  backends `fused` (managed) and `aws` (self-provisioned serving plane) —
  never `local`, which has no serving plane. The store is read directly, so the
  picker renders even before the CLI is installed.
- **DP-6** Default pick: `OPENFUSED_ENV` when it names an eligible env, else
  the first `fused`-backend env (preferring the store default when it is one),
  else the store default, else the first eligible.
- **DP-7** The chosen env is targeted by setting `OPENFUSED_ENV` on the child —
  the CLI's own override channel; no config file is edited.

### 19.4 Deploy semantics

- **DP-8** Each deploy re-exports the page (§18) into a fresh temp directory
  and hands that bundle to the CLI; the bundle is deleted afterwards. An export
  error blocks the deploy (400, all problems at once — nothing is uploaded).
- **DP-9** Deploys are **public share links** (`share create --public`): an
  opaque, unguessable capability URL by default. Rationale for staying public
  (not authed): authed mounts cannot serve a hosted page's browser asset GETs
  yet (fused repo, spec/serve/fused-render.md § Limitations); gate pickers
  become an option when that lands.
- **DP-9a** The token is choosable through a **collapsible "Link" section**
  (like Caching, DP-17) whose one-line summary shows the current setting
  (`unguessable` / `custom: <name>`). It has two body modes:
  - **Picking** — a **random-vs-named radio**: **Unguessable link** (default)
    keeps the crypto-random opaque token; **Custom name** reveals a name input
    whose value rides through to `deploy_page`'s `custom_token`, appended as
    `--token <name>` on that `share create --public` call (the fused CLI's own
    allowed combination — a public mount with a chosen name is a **deliberately
    guessable** URL, never produced by an omitted field, only an explicit
    choice, so it is a two-way toggle rather than a "blank = random" field).
    Shown when the next Deploy would mint a FRESH mount (no deployment yet, a
    different env, or the recorded mount absent from `share list`), and in the
    Change-link flow below. Client-side the name is checked against the CLI's
    own token shape (`^[a-z0-9][a-z0-9_-]*$`); a malformed name (red error) and
    a missing one (Custom name chosen, field empty — a quiet prompt) both
    disable Deploy. An already-taken name is a `share create` rejection the CLI
    itself reports (surfaced verbatim, DP-15).
  - **Read-only summary** — once the mount's liveness is CONFIRMED
    (active/revoked) on the same env, the picker is replaced by a summary of the
    current link (custom name vs unguessable, read from the record's `named`
    provenance) plus a **Change link** action. A plain redeploy keeps the token
    (`repoint`/`recreate --same-token` take no `--token`, DP-10), so changing
    the URL needs `force_new`: Change link re-reveals the picker and the next
    Deploy takes the `force_new` path (mint a new token, best-effort revoke the
    old — DP-10). An *unconfirmed* same-env status (env unreachable at open)
    shows the picker, not the summary, since the next click may still fall
    through to a fresh create.
  The record persists a **`named`** boolean (whether the token is a chosen name
  vs the opaque default), set at the fresh create that minted it and carried
  forward unchanged on every token-reuse redeploy — the summary reads it rather
  than re-deriving named-ness from the token string. The always-public,
  **no-auth** posture (which the guessable/unguessable choice does not itself
  state) is a note kept directly beneath the Link section, always visible.
- **DP-10** Redeploy keeps the URL. Same-env pointer + mount active per
  `share list` → `share repoint <token>` (stable URL); revoked tombstone →
  `share recreate --same-token` then repoint (a failed repoint best-effort
  re-revokes, so a deliberately taken-down link never comes back silently live
  with old content; the pointer is then persisted to the TRUE resulting state
  and the raised error names it — compensation succeeded → the link is down →
  pointer `revoked`; compensation ALSO failed → the mount is live with its old
  content → pointer `active` (so the dot matches reality) and the error names
  the token for a manual `fused share revoke`); token absent from the list
  entirely (e.g. after an
  `infra teardown`) → fresh `create`. Deploying to a **different** env always
  creates fresh there and repoints the pointer — the old env's mount stays
  live, and the modal says so inline.
- **DP-20** The deployed app is **named after the page**, stated explicitly as
  `--name` on create *and* repoint (`deploy.app_name_for`: the page's stem, or its
  folder's name when the stem names nothing by itself like `index.html`, sanitized
  to the same conservative set a clone folder is; empty after that leaves the CLI's
  own default). `share create`/`repoint` otherwise derive the name from the source
  directory they are handed, and what this module hands them is a throwaway
  `tempfile.mkdtemp(prefix="fused-render-deploy-")` — so pages published as
  `fused-render-deploy-pabxq903`, which is the name the deployments list shows, the
  name the clone inventory reports, and therefore the folder name a viewer's clone
  inherits (§35 CL-1). Repoint restates it because repoint re-derives it too;
  deriving from the page path keeps it stable across redeploys.
- **DP-11** CLI output is parsed defensively (`token`/`id`/`url`/`status`
  only): the managed backend returns the URL on create/repoint/recreate; an
  AWS env prints token+path only, so `url` may stay null — the last-known URL
  is kept, never regressed to null by a URL-less repoint.
- **DP-15** Version dependency, surfaced not hidden: whether a *bundle* deploy
  succeeds on a given backend is the installed fused CLI's contract, not ours —
  the fused repo's spec/serve/fused-render.md publishes bundles via
  `share create` on AWS envs and classifies them for inline upload
  (`kind="html"`) on the managed backend, both as of fused 2.9.3.post6 (the
  wheel this package pins as of that decision; the pin has since advanced —
  see the `[fused]` extra in pyproject.toml); a control plane running an
  older fused rejects the upload server-side. fused-render passes the CLI's
  own error through verbatim rather than second-guessing the installed
  version.
- **DP-17** The modal carries a **caching control**: a checkbox ("Cache page
  results") plus a duration select (1m/5m/15m/1h/6h/1d/7d/14d presets, default
  **1h**, plus the current value verbatim when it isn't one of them — e.g. set by a
  direct `share create --cache-max-age` outside this dialog). 30 days is the true
  ceiling (the `results/` cache-bucket lifecycle GC backstop both backends fix at
  30 days — `RESULTS_CACHE_LIFECYCLE_DAYS` for a managed environment,
  `openfused-gc-results` for self-hosted AWS; a managed environment's
  `_build_cache_settings` rejects anything beyond it), but 30d itself is
  deliberately not offered as a preset — it would leave no margin against that
  backstop, whereas 14d keeps a comfortable half-window of slack. Seeded on open
  from the stored deployment record like `include`/`exclude` (DP-2c) and
  re-sent as `cache_max_age` on every Deploy — there is no "leave it as it
  was". It reaches the two backends **differently**, because they model
  caching differently (fused repo's spec/serve/fused-render.md § Caching /
  spec/serve/share-links.md §8): it travels in the export bundle's manifest
  (EX-9) for an AWS environment (read by `build_html_artifact`, so a later
  `repoint`/redeploy can change it too); for a managed `fused` environment the
  manifest field is not read at all — only the explicit `--cache-max-age` flag
  is, as the mount's own `cache_settings` (a control-plane concept independent
  of the bundle, defined by the managed Fused service, amended). `deploy_page`
  now sends `--cache-max-age` on every path — `create`, `repoint`, and the
  follow-up `repoint` after a revoked-token `recreate --same-token` — so a
  redeploy on either backend applies whatever the dialog's checkbox/duration
  currently says, same token/URL, no "Deploy as new URL" workaround needed. A
  `force_new=True` `deploy_page` call still exists as a general "mint an
  entirely fresh URL and take the old one down" action (skip token reuse,
  `share create` at a new token, repoint the page pointer to it, then
  **best-effort revoke the superseded mount** last so a create failure never
  takes the page down) — the modal just no longer needs to surface it as a
  caching-change escape hatch.
- **DP-18** **Clear cache** (`POST /api/deploy/clear-cache {"page"}` →
  `clear_cache_deployment` → `fused share cache-clear <token>`) forces every
  cached result for the deployment's mount to be recomputed on the next
  request, without touching its status, URL, or caching setting — for "the
  underlying data changed, not the code" (a redeploy dedupes to the same
  content address and would otherwise keep serving the old cached result until
  `cache_max_age` expires). Shown in the caching row (next to the duration
  control) whenever the deployment is active; its result (`{deleted, scope}`)
  renders as a one-line status ("Cleared N cached results…" / "Nothing was
  cached…").

### 19.5 State & truth

- **DP-12** A thin per-page pointer at `~/.fused-render/deployments.json`
  (shell/storage; keyed by absolute page path — env, backend, token, url,
  status, entrypoints, `cache_max_age` (DP-17), updated_at) lets the shell mark
  deployed files, re-show
  the URL (`create` returns it exactly once; `share list` never carries one),
  and redeploy to the same token. **`share list` on the env stays the
  authority**: the modal reconciles status against it on open (`--all`, so an
  AWS caller-identity change can't fake a revoke); an unreachable env returns
  the last-known pointer with `reconciled: false` instead of failing the
  dialog. A reconciled response also carries `live` (`active | revoked |
  absent`): absent persists as pointer-status `revoked` (the link *is* down)
  but the modal must not promise a same-URL restore for it — an absent mount
  redeploys as a fresh create with a new link (DP-10), and the stored URL is
  likewise never carried onto a *different* token (DP-11's fallback applies
  only while the token is unchanged). The action label's URL promises
  ("same URL" / "restore URL") render only from a **verified** `live`
  classification: when the reconcile never ran (unreachable env, `live`
  null) the button reads a plain "Redeploy" that promises nothing.
- **DP-12a** Store integrity: the pointer file is rewritten whole on every
  mutation, so two writers must not race and a corrupt file must not be
  clobbered. Writes serialize through one process lock (`_update_store`) —
  closing the lost-update window against the reconcile writer (a focus
  refresh) — and load via `_load_store_for_write`, which raises rather than
  overwrite a file that exists but doesn't parse (overwriting would drop every
  other page's pointer, orphaning live mounts). `deploy_page` validates the
  store before the CLI so a corrupt store fails fast instead of minting a
  mount it then can't record. Reads (`get_deployment`, the status/dot) stay
  lenient — a corrupt store shows as not-deployed rather than erroring a
  preview.
- **DP-12b** The open modal re-reconciles on tab focus/visibility regain (like
  the header dot, DP-1), so a page revoked out-of-band — e.g. from the
  Preferences tab — updates the open dialog instead of contradicting the dot.
  That focus refresh is a **background** load: it updates in place, never
  clearing the form to "Loading…" or replacing it with an error on a failed
  re-fetch (only the initial mount load does that). 
  It preselects the deployment's env only when that env is still configured
  (else falls back to the default and states the old env is gone), so a
  removed env never leaves Deploy silently disabled. The dialog is always
  closeable — even mid-action (the action continues server-side and the dot
  stays correct via `onChange`), so a slow CLI child can't trap the user.
- **DP-13** `GET /api/deploy/shares?env=…` is the "what's deployed on this
  env" view: every mount from `share list --all`, joined back to the local
  page that deployed it via the pointer store (`page: null`, rendered "not
  from this app"), local pages first, live before revoked. Its consumer is the
  **Fused account tab's Deployments section** (AC-11; formerly Preferences'
  PF-6) — a single env-wide list with Revoke — not the per-page Deploy modal. `share list` returns no URLs on
  either backend; each mount's URL is the pointer's recorded one, else
  **derived from the env's base URL**: every mount on one env serves as
  `<base>/<token>` (share-links.md §6), so any recorded absolute URL whose path
  ends in its own token reveals the base for all the rest (`_serve_base_url`).
  With no recorded link to derive from (e.g. only AWS deploys so far), URLs
  stay null and the cell says why on hover.
- **DP-19** *Source code* (§35): a collapsible section beside Caching and Link, headed
  **Source code** — the heading names what is at stake ("is my Python readable?") to a
  publisher who has never used the viewer-side flow, where "Cloning" named our mechanism and
  gave them no reason to open it. Its "Let viewers clone this app" toggle rides
  `POST /api/deploy` as `allow_clone` and becomes `--allow-clone` on `share create` /
  `--allow-clone`/`--no-allow-clone` on `share repoint`. Persisted on the pointer record
  like `cache_max_age`, but the MOUNT is the authority: `GET /api/deploy/status?reconcile=1`
  refreshes it from `share list` on the same read that reconciles `status`, so a posture
  changed from a terminal shows through instead of being re-sent stale. Repoint states it
  explicitly in both directions — the CLI preserves an omitted flag, which is right for a
  CLI and wrong for a dialog whose toggle is always a definite statement.
  The posture is only ever read from **deployment metadata** — the mount record, or our pointer
  record as its cache — and never inferred from what happens to be on disk or from which route
  the user came in through. That is what makes create / repoint / recreate predictable: the
  decision is carried, not recomputed, so the same source published twice cannot flip it.
- **DP-14** Endpoints (`fused_render/deploy.py`, an APIRouter like
  shell/bookmarks): `GET /api/deploy/config`, `GET /api/deploy/status`,
  `GET /api/deploy/preview`, `GET /api/deploy/shares`, `POST /api/deploy`,
  `POST /api/deploy/revoke`, `POST /api/deploy/clear-cache` (DP-18),
  `POST /api/deploy/install`; the POSTs carry the
  `X-Fused` guard (D36). CLI failures surface their last stderr line verbatim
  (click's `Error: ` prefix stripped) — the fused CLI's messages already name
  the fix (`fused cloud login`, `fused infra serve`, …).

## 35. Open a Deployed App — Clone a Page Back from its URL (D196)

The viewer half of app cloning; the publisher half is the Deploy dialog's toggle (DP-19).
Paste a deployed page's URL, and if its publisher allowed cloning, download its export
bundle and unpack it into `~/Documents/Fused` as an ordinary local page.

**Our side of a three-party boundary.** The contract is `fused`'s
`spec/serve/clone-protocol.md`: *a deployed app may expose an authorized, versioned clone
artifact; importing it is deterministic and does not require reconstructing deployment state.*
`fused` owns the artifact — its layout, path rules, byte assembly, size limits, and the
`protocol` version that states compatibility. The host serving it owns **authorization** only.
**This repo owns safe local import**, and nothing else: fetch, verify, unpack, atomically claim
a destination. So we read the protocol fields, use the advisory hints to place files and to
describe the download, and treat the archive's interior as opaque — a second copy of the bundle
schema on this side of the boundary would only drift from the one that defines it. What we do
*not* delegate is validating the two manifest path fields we consume (CL-5): they arrive over
the network and become paths on the user's machine, so checking them here is the trust boundary
working, not a second opinion about the format.

- **CL-1** Two steps, mirroring §26's confirm page: `GET /api/clone-app/info` previews
  (read-only — the file list, the size, and the exact destination folder, all of it advisory
  metadata the host may omit, in which case the confirm step simply shows less) and
  `POST /api/clone-app` performs it. The preview names the folder the clone actually uses:
  the client passes that folder back on the second call and the clone honours it while it is
  free, so a page appearing in the workspace in between can't invalidate what the user was
  shown. Carry-through rather than a lock — reserving the name would mean creating a
  directory during a preview the user has not confirmed — so in the race the clone's own
  response names where it landed, which is what the success view shows. The commit **claims
  the name by renaming** (`zip_import.move_into_new_dir`), never `shutil.move`: move onto an
  existing directory moves the payload *inside* it, which would report success with the page
  a level below where `view` points, so a taken destination fails the rename and the next
  name is tried. Running out of names is a stated refusal at both steps, not a 500.
  The preview's file list names the paths the clone will
  **create**, not the archive's member names: a v2 archive holds `manifest.json` plus
  `<root>/<key>`, while the clone makes the payload dir *become* the page folder and keeps
  nothing else — so echoing the inventory verbatim listed `files/sine.py` and `manifest.json`,
  neither of which ever appears, under copy promising otherwise. The bundle's `manifest.json`
  is read during the import (for `root`/`page`) and then **dropped with staging**: an earlier
  build kept it in the page folder as a dotfile "so a re-export could reproduce the bundle",
  but nothing read it back — `export_page` recomputes the manifest from the page's own files —
  so it was write-only clutter, one more path the confirm step had to predict, and a second
  move on the commit path that needed its own rollback. The commit is now a single rename.
  The folder is named after the **link**, falling back to the **app name**: a deployed page's
  URL ends in its token, which is either a name the publisher chose (`share create --token
  my-solar-map`) or a random opaque one, and a chosen name is the best name available — it is
  what the link the user followed says, what the publisher calls the deployment, and it is
  stable across redeploys (a repoint keeps the token). An opaque token (lowercased base32, 26+
  chars — `app_clone._OPAQUE_TOKEN`, mirroring fused's `mounts.is_opaque_token`; a shape check
  used for naming only, never as a gate) names nothing, so the app name wins there — which is
  why the publisher side states `--name` explicitly (DP-20) instead of letting the export's
  temp directory name the app. Both are reduced to a conservative allow-list before becoming a
  path, since both arrive from outside. The app name still heads the confirm step; only the
  folder prefers the link. The same order applies on the commit path, which may run with no
  preview at all. The confirm button says
  **Clone to local**, not "Clone to <folder>": the body above it already names the
  destination, and a generated folder name in a button label is noise. The modal is
  **undismissable during the write** (`Modal`'s `busy`, so Esc / backdrop / ✕ are all
  shut off, not just the Cancel button): the request keeps running, so an exit taken
  mid-clone lands files in the workspace while dropping the only delivery of the
  result — no success state, no navigation, and a folder the user has to find for
  themselves. The preview step stays dismissable, since it writes nothing. Opposite
  call from DeployModal, which stays closeable because its action completes
  server-side and the dialog reports on it afterwards.
  **Two triggers, one flow** (`CloneAppHost` at the shell, `CloneModal.tsx`): pasting an
  `https://` link into the **path bar**, which previously answered "can't open https:// URLs
  in the explorer" — true and useless — and hands the link to the confirm step rather than
  pre-judging it; and an **Open deployed app** entry on the **Apps page**, shown only while
  the PF-8 Deploy-apps toggle is on, since with deploying off the surface that produces
  these links is hidden and an import entry would advertise a feature the user has turned
  away from. The path-bar route is deliberately NOT gated: refusing a URL the user
  explicitly pasted is a worse failure than one extra button. It is mounted at the shell
  rather than in the sidebar — where it briefly lived — because Home and Apps render without
  a sidebar, so an entry there is unreachable from the page that should host it. The modal
  navigates to the cloned page on success.
- **CL-2** **Not §26's git flow, and deliberately not folded into it.** That flow relies on
  `.git` for identity, which is what lets it safely *update* an existing clone. An archive
  carries no provenance we can verify, so there is no update branch here: every clone lands
  in a fresh folder (`zip_import.unique_dir` → `name`, `name-2`, …) and can never overwrite
  or merge into an existing one. `fused_render/app_clone.py` — a separate module and
  separate routes (`/api/clone-app*`, not §26's `/api/clone`).
- **CL-3** **URL contract.** HTTPS only (`http://` refused, never upgraded — silently
  "fixing" it would hide that the user's link was insecure). The `_clone` URL is rebuilt
  from parsed components, accepting the shapes a page is served at (`…/<token>`, `/`,
  `_shell`, `_clone`) while dropping query and fragment; userinfo is **refused**, not
  stripped, since quietly removing it would clone from a different origin than the URL
  appears to name. No credentials are ever sent (CL-6).
- **CL-4** **The archive is hostile, and the fetch is boundary-hardened.** Two separate
  things, in order of importance. The *archive* is untrusted regardless of where it came from
  (CL-5), and the download is verified. Compatibility is checked **twice, from one table**
  (`_PROTOCOL_BUNDLE_VERSION`): the inventory's `protocol` gives an early refusal before
  megabytes move, and the archive's own declared bundle version is the **enforcing** gate — a
  bare `POST /api/clone-app` fetches no inventory, so a check that lived only there would be no
  gate at all. The bytes are then checked against the digest the host published as the
  download's **quoted** `ETag` (`"sha256:<hex>"`, RFC 9110 §8.8.3) — parsed, not string-matched,
  because matching the bare form against a compliant quoted value reads as "no digest" and skips
  the check silently; an unparseable tag (a proxy's opaque rewrite) means *cannot verify* rather
  than *failed*, so it does not block valid clones — bytes that arrive complete but wrong are the one failure a length check
  cannot see, and this makes "importing is deterministic" something we confirm instead of
  assume. An absent or unrecognised digest is a weaker guarantee, not an error. Separately, the
  *fetch* gets modest hardening because only a URL the user pasted is ever fetched and this
  process sits inside the user's LAN beside `169.254.169.254`: one seam
  (`_validated_address` + `_get`) resolves the host, refuses it unless **every** answer is
  public, dials the validated address while keeping the hostname for `Host`/SNI (so the check
  cannot be undone by re-resolution), follows no redirects, and caps on bytes actually
  received rather than on `Content-Length`. Deliberately centralized and small — it is a
  boundary control on a local app, not the architecture of this feature.
- **CL-5** **One unpacker, shared with §23's template import** (`fused_render/zip_import.py`).
  Shared *within this repo* — `fused`'s own validators cannot be reused here because the viewer
  must work with no `fused` installed at all (the deploy feature installs it as a pinned wheel
  into a separate environment and drives it as a CLI, so there is nothing importable in
  process). What crosses the boundary is therefore the **format contract**, not the code:
  entry validation before any write, symlink refusal, path-escape refusal, per-entry and
  total caps enforced on bytes actually decompressed (the declared sizes are attacker
  controlled), staging-then-move. A second extractor "just for clones" is how a hardened
  path and an unhardened one end up side by side. The manifest's own `root`/`page` get the
  same containment check the entries do — the entries can all be safe while a manifest field
  points outside the bundle, and `root` is what gets moved.
- **CL-6** **Public pages only, for now.** A gated page needs a token whose audience
  satisfies that mount's gate, and neither a query string nor a deep link is an acceptable
  place to carry one (both leak through history and logs). A `401`/`403` says so plainly. A
  `404` is deliberately ambiguous at the source — the serve gate must not confirm whether a
  mount exists or whether cloning is on — so the message names both possibilities rather
  than guessing one. Every refusal is **shown verbatim in the modal**, so it reads as a
  sentence: `app_clone._error` capitalizes at that one boundary rather than at each of the two
  dozen raise sites, because most of those messages are f-strings or `zip_import` refusals
  passed straight through — a per-raise fix covers the literals and misses the rest. A first
  word that is a URL or a path is left alone: those are verbatim tokens the user is meant to
  recognise, and "Https://…" would look like our mistake.
- **CL-7** **A `fused-render://open?app=<page URL>` deep link is DEFERRED to its own change.**
  §26's `?app=` sibling is the natural entry (`deeplink.py` already reserves new payload
  *params* on the same `open` action as the extension point), but it needs its own confirm
  surface and supervisor dispatch, and nothing routes one today. `CloneModal` takes an
  `initialSrc` and auto-previews it, which is the seam that entry will use — it is not
  evidence the link works.

## 20. Preferences — Shell Settings Page (M12)

Goal: one unobtrusive place for the shell's cross-cutting settings and
housekeeping. Entry is a muted gear row pinned to the **sidebar's bottom-left**
edge; it navigates to **`/view/_prefs`** — a shell-owned sentinel pathname like
`_panel`/`_tab` (no `/embed` variant: settings chrome inside a pane makes no
sense). Server state lives in `~/.fused-render/prefs.json` behind
`shell/prefs.py` (the D75 shell-state pattern: storage helpers + an APIRouter;
never imports server).

### 20.1 Store & endpoints

- **PF-1** `GET /api/prefs` → `{engine: {selected, effective, forced_by,
  fused_available}, deploy: {enabled}, reader: {enabled}, calls: {…}}` — and no
  `log` block (PF-5). `PUT /api/prefs`
  (X-Fused) applies a **partial** update — any of `engine`, `deploy_enabled`,
  `reader_enabled`, `calls_enabled`, `calls_params` or `calls_retention_days`
  present, so each control PUTs only its own field — and
  returns the same shape. An unknown engine value, a non-boolean
  `deploy_enabled`, or a body naming no known preference → 400; the file merges
  (future prefs are new keys, not new files).
- **PF-1a** The page renders its sections in this order: **Appearance**,
  **Call log**, **Deploy to Fused account**, **Accessibility**, and last
  **Execution engine** — last because it is the setting a user is least likely
  to have come here to change (builtin suits almost everyone, and an env var
  pins it where it matters). There is **no Tour button**: the tour still runs
  itself on a first visit (`maybeAutoStartTour`), because it is onboarding
  rather than a preference. (The spec subsection numbering below is
  organizational, not the visual order.)
- **PF-2** The page is a thin client over existing backends everywhere else:
  deployments via `GET /api/deploy/config` + `GET /api/deploy/shares`,
  revocation via `POST /api/deploy/revoke`, registry via `GET
  /api/templates/registry`, the call store's in-app browse via ordinary
  navigation.

### 20.2 Execution engine switch

- **PF-3** The persisted `engine` pref (`fused` default since **D204**, which
  reversed D70/D80's builtin-by-default; or `builtin`) drives `/api/run`
  dispatch, **read per request** so a switch applies to the next run with no
  restart (the registries' CT-5 no-restart discipline). Selecting `fused` — or
  storing nothing at all — is
  *effective* only while the fused local backend is importable
  (`prefs.fused_engine_available`, probed per call — an install mid-session
  shows through); otherwise execution degrades to builtin and the page says
  so. The fused option is disabled with an install hint when unavailable.
  **A two-way radio, not three**: there is no `auto` pref value — the
  availability AND is what "auto" would have meant, and a third stored value
  would be a second way to spell the default. A stored `builtin` **pins**
  builtin: D204 flipped the default, not the choice, so an importable `fused`
  never overrides a user who picked builtin. Since the default now depends on
  what else is importable, the **test suite pins `FUSED_RENDER_ENGINE=builtin`
  ambiently** (tests/conftest.py) so an incidental `/api/run` test does not
  silently cover a different runner on a machine with the extra installed.
  **One resolver, no divergence:** `prefs.effective_engine()` is the single
  function both dispatch (`server.current_engine`) and the page's reported
  "running" engine (`engine_state().effective`) go through, resolving the
  override + pref + availability **live** on every call — so the page can
  never claim a different engine than `/api/run` uses, even for a forced
  `=auto` after a mid-session install (an earlier startup-frozen resolution
  let those drift).
  **Both engines are local**: the fused engine instantiates the package's
  `LocalPythonComputeBackend` directly (engine.py — project venvs under
  `<home_dir()>/venvs`, ours not the backend's store, PY-16), never resolving a
  named environment; `envs.json`,
  the default env, and `OPENFUSED_ENV` play no part in page execution. Fused
  *environments* are exclusively deploy targets (DP-5) — a separate axis,
  and the page's copy states this so "Fused engine" is never read as "runs
  on my Fused env".
- **PF-4** `FUSED_RENDER_ENGINE` remains the **process-level override**: when
  set it beats the pref entirely. `server._forced_engine()` runs **once at
  startup** purely to validate (raises on a bad value; `=fused` still fails
  loudly when missing) and log the choice — dispatch itself goes through the
  live resolver (PF-3), so the override is re-read per request, not frozen.
  The page shows the switch locked with the variable's value; a PUT still
  persists (applies once the override is removed). `GET /api/config`'s
  `engine` reports the in-effect engine per request.

### 20.3 Logs

- **PF-5** **The app's own log is NOT on this page**, and `GET /api/prefs`
  carries no `log` block. It used to: a heading naming `logs.log_path` with an
  "Open logs location" reveal button. Removed because the app log is
  disposable temp-dir output (D68) whose only affordance was that reveal —
  which the desktop tray's "Open app logs" already provides on every platform that
  has a tray — and because once the call store moved to `~/.fused-render/logs`
  (CL-7), a second "Logs" heading beside the Call log section read as the call
  log's own settings. The durable log a user has settings for is the call log
  (§31); the disposable one belongs to the process, not to preferences.

### 20.4 Deploy to Fused account

- **PF-8** The section leads with an **opt-in toggle** for the Deploy
  affordance: the persisted `deploy_enabled` pref (default **off**), PUT via
  `{deploy_enabled}`. Deploy publishes a page to a public hosted URL through
  the fused CLI, so it is opt-in — the preview-header **Deploy** button (§19,
  DP-1) and its modal stay hidden until this is turned on. The gate is a UI
  affordance only, not a security control (the `/api/deploy*` endpoints keep
  their X-Fused guard); the preview re-reads the pref on focus/visibility so a
  toggle shows through without a reload. Any non-`true` stored value reads as
  off.
- **PF-6** *(moved by M18/§27 — see AC-11)* The per-env share list lived
  here before the account surface existed; Preferences keeps only the PF-8
  Deploy-button toggle plus a link to the Fused account tab, where the list
  now renders beside the environments table.

### 20.5 Tabs (D125)

- **PF-9** The page is split into two tabs, active tab in the URL
  (`?tab=account`, default clean-URL tab is **Render preferences** —
  Logs/Execution engine/Deploy to Fused account/Tour, unchanged): **Render preferences**
  and **Fused account** (§27's account panel, folded in here since it stopped
  being its own sidebar-footer entry). The **Fused account** tab button is
  offered only while the PF-8 Deploy toggle is on; requesting `?tab=account`
  while it's off falls back to Render preferences rather than showing a tab
  with nothing pointing at it. This is also where the sidebar footer's
  signed-in dot now points — see AC-1.

### 20.6 Template registry view

- **PF-7** `GET /api/templates/registry` returns the merged
  extension→templates bindings from both registries (SPEC §16): one row per
  pattern with its resolved mode list (first = default), `disabled`
  for `null` bindings, `source` (`builtin` / `user` / `user-override` — a
  user key identical to a built-in key replaces its row), and per-entry
  shape errors. Override detection is **case-insensitive**, matching how
  resolution actually matches keys (`_key_segments` lowercases): a user
  `.CSV` overrides a built-in `.csv` as one `user-override` row, never two
  mis-sourced rows.
  This is the table of bindings, not a per-file resolver: distinct keys
  coexist and CT-3 specificity decides per file. Read per request like every
  resolution (no restart).

  **Superseded (2026-07-09, owner call):** the read-only registry section was
  removed from the Preferences page when the full Template Management view
  shipped (§23, `/view/_templates`) — a single home for bindings rather than a
  glance in one place and an editor in another. The **`GET /api/templates/registry`
  endpoint stays** (unchanged contract, TV-4); it is now consumed by the
  Templates view instead of Preferences.

---

## 21. Session Restore — Per-File Last Params (D84)

Goal: opening a file the way most opens happen — a listing click, a Finder/DMG
double-click, the root redirect — should not lose whatever params you last had
on it. A **file** (never a directory, never an embed-mode pane) remembers its
last shell query in the same `.html.json` sidecar the `claude` chat template
(§7) and bookmark history (SB-7) already use.

- **LSN-1** A viewed file's last URL params are stored as `lastSession` in its
  `<file>.json` sidecar, sibling to the claude template's `claudeSessions` key
  and SB-7's `bookmarkHistory`.
- **LSN-2** `lastSession = {search, updated_at}` — `search` is the shell query
  string verbatim, no leading `?` (same literal-URL posture as bookmarks, SB-2).
- **LSN-3** Tracking upserts when the shell query has a param **other than
  `_mode`**, or when a `lastSession` already exists for the file (so once a
  session is going, a later `_mode`-only change is remembered too); a query that
  is empty, or `_mode`-only with no prior session, never starts one.
- **LSN-4** Opening a file with an **empty** shell query restores `lastSession`
  (if present) via `history.replaceState` before the preview mounts.
- **LSN-5** Opening a file with a **non-empty** query (bookmark, hand-typed,
  refresh) — those params win, no restore — and, if qualifying (LSN-3), become
  the new `lastSession`.
- **LSN-6** Directories and embed-mode panes (panel/tab, D72) neither track
  nor restore — layout mode already owns pane params.
- **LSN-7** Persistence is `GET`/`PUT /api/session` (`fused_render/server.py`);
  `PUT` carries the `X-Fused` guard (D36), `GET` is unguarded (read-only).
- **LSN-8** Sidecar writes read-merge-write the whole dict, so `claudeSessions`,
  `bookmarkHistory`, and `lastSession` never clobber one another (last-write-wins
  on a true simultaneous write — D3).
- **LSN-9** The preview is held (a brief loading state) until the restore
  decision resolves — no flash of default params before the restored ones apply.
- **LSN-10** Tracking writes are debounced (400 ms) and fire-and-forget; a
  sidecar read/write failure never blocks the view — it just renders bare.
- **LSN-11** Dropping params back to empty/`_mode`-only leaves the stored
  `lastSession` untouched — a later bare open re-applies it. Accepted quirk,
  not a bug.

## 22. Explorer Search — Streamed Recursive Walk (M14)

Goal: an in-folder search (FS-7) whose first results paint in tens of
milliseconds on any tree, whose coverage is never silently starved by one big
subtree, and whose truncation is always visible. The searcher is the shell
(client-side fuzzy scoring, fzf/VS Code Quick-Open model — the corpus is local
and per-keystroke re-ranking must not pay a network round trip); the server's
job is to deliver the corpus fast, shallow-first, and pruned of machine noise.

### 22.1 Walk order & pruning (server)

- **SR-1** `GET /api/fs/walk` traverses **breadth-first** (`_walk_bfs`): every
  depth-N entry is emitted before any depth-N+1 entry; within one parent, dirs
  first then files, each name-sorted. Any early stop (cap, disconnect)
  therefore keeps complete shallow coverage. The old depth-first walk let one
  big sibling eat the whole entry budget — a home dir looked like:

  ```
  depth-first + cap                      breadth-first + cap
  ─────────────────                      ───────────────────
  ├─ Desktop   ✓ dives to bottom,        ├─ level 1: ALL top dirs first ✓
  │    eats 15,926 / 20,000 slots        ├─ level 2: all their children ✓
  ├─ Movies    ✗ CAP DEAD — 0 children   ├─ level 3: …
  └─ Music     ✗ 0 children              └─ cap cuts the DEEPEST level only
  ```

- **SR-2** Machine-noise pruning is **gitignore-driven inside git
  repositories** (D100): entries the containing repo's own gitignore rules
  ignore are never emitted **nor descended** — the generic answer to `dist/`,
  `build/`, `.next/`, `target/` and every other ecosystem's junk, with the
  repo's own file as the authority (negations like `!keep.log` honored).
  Verdicts come from one streaming `git check-ignore --stdin` co-process per
  repo (`_IgnoreOracle`, ~14 µs/query, ≤ `WALK_MAX_ORACLES` open at once, all
  closed when the walk ends); each directory inherits its repo root through
  the BFS queue, a `.git` entry starts a nested repo with its own rules, and
  a walk rooted *below* a repo root resolves it via one `git rev-parse
  --show-toplevel`. A directory with a `.gitignore` but NO repo anywhere in
  scope (an un-inited project, an Obsidian vault) prunes the same way: the
  oracle grafts it onto a shared empty `GIT_DIR` as its `GIT_WORK_TREE`, so
  check-ignore honors standalone `.gitignore` files too (cascading into
  subdirs, negations included). Pruning is an optimization, never a
  dependency: git missing or failing degrades to no gitignore pruning.
  Known miss, accepted: walking a SUBDIRECTORY of a repo-less project looks
  upward for nothing (no work-tree boundary to find), so an ancestor's
  standalone `.gitignore` doesn't apply there.
- **SR-2a** `WALK_IGNORE_DIRS` (`node_modules`, `__pycache__`, `venv`,
  `.venv`, `.git`, `site-packages`) stays as the **universal floor**, checked
  by bare name everywhere: it covers junk outside any repo (a stray
  `node_modules` in `~/Downloads`, `Library/Python/*/site-packages`) and
  `.git` itself, which git never reports as ignored. Both SR-2 and SR-2a
  apply in hidden mode too — those trees are machine noise, not "hidden
  data" (a `.py` extension search must not drown in `.git` object files).
  `.git` *files* (worktree/submodule pointers) are ordinary files and do show.
- **SR-2b** Because the walk excludes gitignored entries outright, walk
  entries carry **no `ignored` dimming flag** — dimming remains a
  `/api/fs/list` (plain listing) concern, where ignored entries are still
  shown. Search excludes; the listing dims. (VS Code's split: explorer shows
  gitignored files, Quick Open doesn't.)
- **SR-3** macOS package directories (`WALK_LEAF_DIR_SUFFIXES`: `.app`,
  `.framework`, `.bundle`, `.photoslibrary`, case-insensitive) are emitted as
  a single dir entry but never descended — Finder semantics; one Electron
  `.app` alone is thousands of internal files nobody searches.
- **SR-4** Symlinks are emitted but never followed; unreadable dirs/entries
  are skipped silently (matches `/api/fs/list`).
- **SR-5** `WALK_MAX_ENTRIES` (200 000) is a **memory/latency safety valve,
  not a coverage budget**: with BFS it only ever cuts the deepest levels of
  pathological trees (mounted volumes, cache farms). The response carries
  `truncated` so the UI can be honest about it (SR-10).

### 22.2 Streaming wire format

- **SR-6** `?stream=1` returns `application/x-ndjson`: zero or more
  `{"entries": [...]}` batch lines (`WALK_BATCH_SIZE` = 500 per line), then
  **exactly one** terminal `{"done": true, "truncated": bool, "total": n}`
  line. Closing the connection cancels the walk server-side (the generator is
  closed on disconnect). Without `stream=1` the original single-JSON shape
  (`{path, entries, truncated}`) is unchanged — same entries, same BFS order.

  ```
  blocking (before)                      streamed (after)
  ─────────────────                      ────────────────
  type ▶ [  spinner ~1s  ] ▶ ALL         type ▶ ~10ms ▶ first results
         nothing until whole walk               ▶ list fills in live
         done + one giant JSON                  ▶ "N matches · M scanned…"
  ```

### 22.3 Shell search behavior

- **SR-7** The listing's search (`?q=`, URL-synced like sort) fetches **one
  hidden-inclusive dataset** (`hidden=1` always) and filters dot-entries at
  display time: a dot-leading query segment (`.py`, `sub/.env`) shows them,
  anything else hides them. One corpus means flipping intent mid-query never
  refetches, and `.py` works as an extension search. The walk starts lazily
  on first focus (warm-up) or a URL-seeded query, is cached until the dir
  watch fires, and the in-flight stream is aborted on refresh/unmount.
- **SR-8** Scoring is incremental and off the critical path: stream flushes
  commit at most every 200 ms (`STREAM_FLUSH_MS`), each flush fuzzy-scores
  **only the entries appended since the last one** and merges them into the
  prior ranked list; a full re-scan happens only when the query or
  hidden-intent changes (and then on React's deferred schedule). Rationale:
  re-scoring the whole grown array per network chunk saturated the main
  thread near the tail of a big walk — stuck stale-dim, queued clicks.
- **SR-9** Results render in pages of 250 rows; a sentinel row +
  IntersectionObserver reveals the next page as the user scrolls. The full
  ranked list stays in memory for the count text; ranking = longest
  consecutive run, then fuzzy score, then shallower path, then name.
- **SR-10** Truncation is always visible: a live `N matches · M scanned…`
  counter while streaming; a `+` suffix and tooltip on the final count when
  the cap hit; and the zero-match message names the covered entry count
  ("No matches in the first 200,000 entries — this folder tree is too large
  to search fully") instead of a bare "No matches".
- **SR-11** The query mirrors into the URL **debounced** (200 ms): Safari
  rate-limits `history.replaceState` (~100 calls/30 s, then throws), so
  per-keystroke sync is a crash, not a nicety. Input state stays immediate;
  only the URL lags.

---

## 23. Template Management — Sources, Bindings & Import/Export (M15)

Goal: a dedicated view that turns the read-only registry glance of §20.5
(PF-7) into a full editing surface for template bindings, plus the ability to
see the whole template inventory across sources and move user templates
between machines as zip files. Same underlying data as §16/§20.5 — this
section adds the write path, the inventory/provenance view, and
import/export; it does not change the resolution engine (PT-6/CT-3), the
registry file format (CT-2/CT-10/CT-11), or PF-7's read-only endpoint
contract (TV-4). The read-only glance itself is retired from Preferences once
this view ships (§20.5); the endpoint it used is now consumed here instead.

### 23.1 Sources model (extensibility)

- **TV-1** **DECIDED (D86):** the builtin/user pair (§7, §16) is generalized
  into an ordered list of **sources** — `Source { id, label, editable,
  precedence }`. Today exactly two ship: `core` (`id:"core"`,
  `editable:false`, `precedence:0`, the `TEMPLATES_DIR`/`BUILTIN_REGISTRY`
  pair) and `user` (`id:"user"`, `editable:true`, `precedence:100`, the
  `USER_TEMPLATES_DIR`/`USER_REGISTRY` pair, D76's paths). The list is
  modeled so a third source (org/project) can be appended later with zero UI
  rework — **not built now** (§23.4).
- **TV-2** Effective binding for a registry key = the value from the
  highest-precedence source that defines it — unchanged from PT-6/CT-3 (user
  beats core); the sources list is a presentation/provenance layer over the
  existing resolution rule, not a new one.

### 23.2 API

New endpoints live in `fused_render/templates_api.py` (a `templates_router`,
mirroring `shell/bookmarks.py`/`shell/prefs.py`), included from `server.py`
alongside the existing bookmarks/prefs/deploy routers. Mutating routes carry
the `X-Fused: 1` guard (D36); all paths resolve under `home_dir()`.

- **TV-3** `GET /api/templates/inventory` — the template pool across sources:
  `{sources, templates:[{name, source, editable, hasIcon, usedBy,
  shadowsCore}]}`, one entry per **resolved** folder (a user folder
  shadowing a core folder of the same name emits one `source:"user",
  shadowsCore:true` entry, not two). `usedBy` = registry keys whose effective
  ordered list contains the name.
- **TV-4** `GET /api/templates/registry` — **extended**, back-compat fields
  kept (`builtin_registry`, `user_registry` paths) so PF-7's Preferences
  section keeps working unchanged. Adds `sources` and, per entry, `keyKind`
  (`simple|compound|wildcard|directory`), the effective `templates` list
  resolved to `{name, source, exists, hasIcon}` (a name with no folder on
  disk resolves `exists:false` and stays in the list — surfaced as broken,
  not dropped), `resolvedSource`, `overridesCore` (true whenever the user
  registry defines the key, regardless of value equality), `disabled`
  (effective value is `null`), `coreTemplates` (what the builtin registry
  alone gives, or `null`; drives reset-preview + the known-keys list), and
  `userValue` (raw user-registry value, included only when a user key
  exists). `entries` covers every builtin key plus every user-only key.
- **TV-5** `PUT /api/templates/registry` **(D87)** — upserts **one** user
  key: body `{key, value}` (`value` = ordered name array, `null`, or `[]`).
  Validates the key against the CT-3 grammar; names need only be **non-empty
  strings** — an unknown name is **not** rejected, it saves as a **dangling
  ref** (surfaced broken in the UI, dropped at render) so a user can bind a
  not-yet-created template without being blocked (D95). Only structurally
  invalid entries (non-string / empty) → 400. Then a **read-modify-write of
  that key only** against `USER_REGISTRY` via the existing atomic
  `read_json`/`write_json` helpers (creates the file/dir if missing) — never a
  whole-file overwrite. Returns the recomputed entry (same shape as one
  `entries[]` item from TV-4).
- **TV-6** `POST /api/templates/registry/reset` **(D87)** — body `{key}`;
  deletes that key from the user registry (no-op if absent), reverting the
  effective value to the core one. Returns the recomputed entry, or
  `{key, removed:true}` if no such key resolves anywhere anymore.
- **TV-7** `GET /api/templates/export?names=a&names=b` **(D89)** — streams a
  zip (`application/zip`, `Content-Disposition: attachment;
  filename="fused-render-templates.zip"`) of the named templates — **core or
  user** (a user folder shadows a core folder of the same name; 400 on a name
  that resolves to neither). Names travel as **repeated `names=` params** (not
  comma-joined) so a folder name containing a comma round-trips. Each
  template's folder contents land at its own top level in the zip. **No
  `registry.json` in the zip** — template content is folders only; the one
  root-level file is the `recommendation.json` binding-recommendation sidecar
  (TV-22, D107), which carries *suggestions*, never registry rows.
- **TV-8** `POST /api/templates/import` **(D90)** — step 1 of 2, multipart
  (`file` field, the `.zip`), stages without committing: unpacks to
  `home_dir()/.import-staging/<importId>/` (`importId` = `secrets.token_hex`).
  Hardening (rejects the whole upload before anything lands outside
  staging): uncompressed total > 50 MB, entry count > 2000, or any single
  entry > 25 MB (zip-bomb guard); any entry that is absolute, contains `..`,
  normalizes outside the staging root, or is a symlink (zip-slip guard). A
  candidate template = a top-level directory containing `template.html`
  (`valid:true`). Returns `{importId, expiresInSec, items:[{name, valid,
  hasTemplateHtml, conflictsExisting, fileCount}], warnings}` —
  `conflictsExisting` flags a name already present under
  `USER_TEMPLATES_DIR`. Stale staging dirs past the TTL are swept
  opportunistically on every call.
- **TV-9** `POST /api/templates/import/{importId}/commit` **(D90)** — step
  2: body `{resolutions: {name: "overwrite"|"skip"|"keep-both"}}`
  (unresolved items default to `skip`). Per valid item: `skip` drops it;
  `overwrite` atomically replaces the existing folder; `keep-both` lands as
  `<name>-2` (then `-3`…, never clobbering). Moves (not copies) from staging
  into `USER_TEMPLATES_DIR`, then deletes the staging dir. Unknown/expired
  `importId` → 404/410. Returns `{imported, skipped, overwritten, renamed}`.
- **TV-10** Reveal and "open in explorer" add **no new endpoints**:
  inventory's Reveal action reuses `POST /api/fs/reveal`; "open in explorer"
  is a plain shell navigation to `USER_TEMPLATES_DIR/<name>`.
- **TV-19** `POST /api/templates/delete` **(D93, D109)** — body
  `{name, cleanRegistry?}`, `X-Fused` guarded; deletes **one user template
  folder** under `USER_TEMPLATES_DIR`. **Core templates are read-only and
  never deletable** — a core-only name resolves to no user folder and 404s
  (the core folder is untouched); unsafe names (path separators, `.`/`..`) →
  400; symlinks are rejected. With `cleanRegistry: true` (D109, default
  false) the **user** registry is swept after the folder delete: every user
  key whose value references the name drops it (exact match — names are not
  lowercased like keys), and a key whose value is **emptied** by the sweep is
  **removed entirely** (revert to core) — never left as `[]`, which means
  *disabled* (D95). The user registry is loaded — and a corrupt file refused
  with 400 — **before** the rmtree, so a refusal leaves the folder intact;
  the core registry is never touched. Without the flag, bindings are left
  as-is — a binding that referenced the name resolves broken (`exists:false`)
  until rebound, matching export/import being folder-only. Returns
  `{deleted: name}`, plus `registryKeysCleaned: [keys]` when the flag was set.
- **TV-20** `POST /api/templates/new` **(D105)** — body `{name, extensions}`,
  `X-Fused` guarded; **scaffolds a new user template and binds it**. Copies the
  starter kit (`fused_render/template_starter/` — shipped in the wheel but
  deliberately **outside** `templates/`, so it is never itself resolvable or
  listed in the inventory) into `USER_TEMPLATES_DIR/<name>`, then binds each
  extension via the **same per-key read-modify-write** as TV-5
  (`_apply_binding`, never a whole-file rewrite). The bind is **additive**:
  `name` is appended to whatever list the key currently resolves to (its user
  override, or the core default if there is no override yet) — an existing
  multi-mode binding is never replaced with just the new template.
  `name` must be a safe template folder segment (no `/`, `\`, `.`; not
  `_`-prefixed — CT-6, so the folder always resolves by PT-6); each extension
  is validated against the **CT-3 key grammar** exactly like TV-5. All
  validation runs **up front**, so a bad name/extension (400) or an existing
  `USER_TEMPLATES_DIR/<name>` (**409**) leaves nothing created and the registry
  untouched. `extensions` may be empty (scaffold a draft, bind nothing — no
  registry file written). Returns `{ok, name, path, bindings:[keys]}`. Editing
  the scaffolded files afterwards happens in the file explorer (D88), and the
  extensions are re-editable through the ordinary Row editor (TV-15).
- **TV-21** `POST /api/templates/open-in-claude` **(D105)** — body `{name}`,
  `X-Fused` guarded; opens **Terminal.app** in a user template's folder and
  starts the `claude` CLI there, so the author can iterate on the template with
  Claude Code. **macOS only** for now (`sys.platform != "darwin"` → a clear
  error, no other platform spawns a terminal yet). User templates only — a
  core-only name resolves to no user folder and 404s; unsafe names → 400,
  symlinks rejected (same guards as TV-19). The `claude` binary is located by
  the same PATH/`~/.local/bin`/homebrew search as `templates/claude/agent.py`
  (replicated, not imported — a template folder is not an import root); a
  missing binary is a clear error. The terminal is spawned via `osascript`
  (`tell application "Terminal" to do script "cd <folder> && <claude>"` +
  `activate`), paths `shlex.quote`d for the shell then escaped for the
  AppleScript literal. Returns `{ok: true}`.
- **TV-22** **Export recommendation sidecar (D107):** the TV-7 zip **always**
  contains a root-level **`recommendation.json`** — `{"version": 1,
  "recommendations": {"<template name>": ["<registry key>", …]}}` — recording
  each exported template's bindings *at export time*. Built by **reverse
  lookup over the MERGED registry** (user shadows core per key, TV-2): a
  template maps to every key whose effective ordered list contains its name.
  The shape is **template → keys**, deliberately *not* registry-key →
  ordered-list slices (D107) — the sidecar names *which* keys suggest a
  template, never *where in the list* it sits, so applying it can never
  clobber the importer's own mode ordering. Templates with zero bindings are
  **omitted** from the map; the file is written even when the map is empty
  (deterministic zip layout). Template names and each key list are sorted.
- **TV-23** **Import staging reads the sidecar (D107):** TV-8 parses a root
  `recommendation.json` and excludes it from the "ignored top-level file"
  warnings. Robustness is strictly non-fatal — recommendations are never
  worth failing a stage over: malformed JSON or a wrong shape → a warning and
  the recommendations are dropped (folders stage normally); `version != 1` →
  **silently ignored** (a future exporter's sidecar, not an error);
  individual keys failing the CT-3 grammar are filtered **at staging** with a
  per-key warning, so commit never has to reject a recommendation the user
  merely ticked. Each valid staged item then carries **`recommendedKeys:
  [{key, status}]`** (omitted when none) — `status` ∈ `new` (would bind) |
  `already-bound` (name already in the key's effective list) | `disabled`
  (the key has a user `null`/`[]` override; applying would re-enable it).
- **TV-24** **Commit applies accepted bindings (D108):** TV-9's body gains an
  optional **`bindings: {originalStagedName: [keys]}`**. The whole map is
  validated (CT-3 grammar, same as TV-5) **before any folder move** — a 400
  leaves the stage fully intact (retryable); a corrupt user registry is
  likewise refused up front (never rewritten blind). Bindings apply **after**
  the moves, against **FINAL names**: a skipped/invalid template's bindings
  are silently ignored; a keep-both rename binds the **new** name
  (rename-follows-bindings); an already-bound key is a no-op. Application is
  **append-only** (the TV-20 posture): a key existing only in core gets a
  user entry created as the **full core list + the appended name** (never a
  shorter shadow over core); a user-disabled key is re-enabled — as core's
  list + the name — **only when a binding for it was explicitly requested**;
  appends always land at the **END** of the list, never reordering the user's
  existing bindings. Response gains `bindingsApplied: [{key, template}]`.
- **TV-25** **Import wizard — recommendations UI (D108):** step 2 (TV-17)
  gains a master toggle **"Apply author's recommended bindings"** plus a
  per-template **chip strip** of its `recommendedKeys`. Chip defaults: **ON**
  for `new`; **OFF** for `disabled` (amber "disabled by you" badge + an
  inline warn line when toggled on — re-enabling is explicit opt-in);
  `already-bound` chips are **inert** (green badge, never sent — the server
  would no-op anyway). A **"+ add"** chip lets the user type a custom key
  (client-validated, server authoritative). Resolving an item to *skip*
  greys its strip; a keep-both resolution shows a "will bind as `<renamed>`"
  note. The commit button surfaces the pending binding count; step 3 lists
  `bindingsApplied`. A zip without `recommendation.json` leaves the wizard
  exactly as it was — the whole surface is additive.

### 23.3 Frontend — Templates view (`/view/_templates`)

- **TV-11** **(D92)** New route **`/view/_templates`** — a shell-owned
  sentinel dispatched in `App.tsx` the same way `/view/_prefs` is (§20):
  view-only, no `/embed` variant (a template-management page inside an
  embedded pane has no meaning). New component
  `frontend/src/views/Templates.tsx`. The active tab (bindings / library)
  lives in the URL as **`?tab=library`** (bindings = default, clean URL);
  switching tabs is a `pushState`, so browser back/forward moves between
  tabs (D94). The page is keyed by the nav epoch, so it re-derives the tab
  from the URL on each navigation — no separate tab state.
- **TV-12** Sidebar footer gains a "Templates" button next to the
  Preferences gear (`navigateUrl("/view/_templates")`), an inline SVG icon
  in the same style as the gear.
- **TV-13** `lib/api.ts` additions: `getTemplateInventory()` (TV-3),
  `getTemplateRegistry()` (TV-4, extends the existing type, keeps old
  fields), `putRegistryBinding(key, value)` (TV-5),
  `resetRegistryBinding(key)` (TV-6), `exportTemplatesUrl(names)` (builds
  the TV-7 GET url for an `<a download>` click), `importTemplates(file)`
  (TV-8 — the app's first `FormData` multipart call; `X-Fused: 1` header
  set, `Content-Type` left for the browser to fill in with the multipart
  boundary), `commitImport(importId, resolutions, bindings?)` (TV-9/TV-24 —
  the optional bindings map is omitted from the body when empty).
- **TV-14** **Bindings table** (one row per registry key): extension/key,
  ordered template chips (first badged "default"), a source chip
  (Core/User), a "● Modified" marker when `overridesCore`, a "Disabled" pill
  when `disabled`, broken-name chips (`exists:false`) in a warning style.
  Filters: All / Modified only / by source; a search box over key and
  template name. `+ Add extension` opens the row editor in create mode.
- **TV-15** **Row editor modal (D91)** (DeployModal-style: backdrop +
  dialog, Escape to close): in **create** mode, a key **pattern builder**
  covering all four CT-3 shapes — simple `.ext`, compound `.a.b`, wildcard
  `.*.json`, directory `.ext/` — via a segmented control with a
  live-rendered key preview and client-side grammar validation (server
  stays authoritative, TV-5); in **edit** mode the key is shown, not
  editable. Template list: ordered chips, drag to reorder (first =
  default), remove, "Add template" opens a picker sourced from `GET
  /api/templates/inventory` grouped by source, disallowing duplicates.
  Actions: **Save** (TV-5), **Disable for this type** (writes `null`,
  inline confirm), **Reset to core** (TV-6, shown only when
  `overridesCore`, previews the core default from `coreTemplates`),
  **Cancel**.
- **TV-16** **Inventory panel**: templates grouped by source, each group
  with its own search + source/used filters. A source's **editability** (the
  🔒 on core) governs only whether its *bindings/templates can be changed* —
  it does not gate read actions. Every row (core **and** user) renders its
  `icon.svg`, name, `usedBy` chips, a select checkbox, and per-row actions —
  Export (single), Reveal in Finder (TV-10), Open in explorer (TV-10) — since
  **core templates are exportable/inspectable too** (owner call: portable
  folders regardless of source). Toolbar: "Import zip" and "Export selected"
  — checkbox multi-select spans any rows (core or user) and drives the export
  download (`downloadTemplatesExport`, which surfaces server errors rather
  than saving a 400 body as a zip). **User** rows also get a **Delete** action
  (never core — the source is read-only); it opens a confirm modal (D109)
  with two default-checked checkboxes — "Export zip before deleting"
  (downloads a recovery zip first via `downloadTemplatesExport`; the delete
  proceeds only if that resolves, keeping D92's export-first guarantee) and
  "Remove registry bindings for this template" (sent as TV-19's
  `cleanRegistry`) — and exactly two buttons, **Delete** (danger) and
  **Cancel**, calling `deleteTemplate` (TV-19) and refreshing on success.
- **TV-17** **Import wizard modal**, three steps: (1) file chooser
  (`accept=".zip"`) → `importTemplates(file)` (TV-8); (2) manifest — a
  table of staged items with a per-conflicting-item resolution selector
  (Overwrite / Skip / Keep both — Overwrite visually distinct, a short
  inline caution suffices, no per-item confirm dialog), invalid items
  greyed and auto-skipped with their reason shown, warnings listed; (3)
  confirm → `commitImport` (TV-9) → a result summary
  (imported/renamed/skipped) → closing re-fetches inventory + bindings.
- **TV-18** Any mutation (put/reset/import commit) re-fetches inventory +
  registry and re-renders — no stale state between the two sections.
  Header copy states plainly that this view manages **bindings + inventory
  only**: editing a template's own files happens in the file explorer
  (D88).

### 23.4 Non-goals (this feature)

- Editing template file contents (`template.html`, `reader.py`, css, icons)
  in this UI — use the file explorer + the existing `/api/fs/write` (D88).
- A real third source (org/project) — TV-1 only models for it.
- ~~Registry bindings inside export zips, or merging/writing registry entries
  from an import~~ — **revised (D107/D108):** exports now carry a
  `recommendation.json` *suggestion* sidecar (TV-22) and commit can apply
  user-accepted bindings append-only (TV-24). D89's core stands: no registry
  slices in the zip, nothing auto-merged — an imported template stays inert
  (CT-7) unless the user opts in per key in the wizard (TV-25).
- Persisting a per-file "last selected mode" — unrelated, not part of this
  feature.
## 24. History View — Sidecar Inspector Template (D96)

A `history` view template renders a file's `<ext>.json` sidecar (§21, SB-7, D82–D84)
as a readable, sectioned history — every claude session, bookmark, last-session
snapshot, and review comment the file has accumulated. Reachable from both ends:
opening `sine.html` and switching to the `history` mode, or opening `sine.html.json`
(or `data.parquet.json`, or any other `<name>.<ext>.json` sidecar) directly, where
`history` is the default mode.

- **HV-1** An ordinary view template (`fused_render/templates/history/`) —
  `template.html` + `icon.svg` only, **no `.py`** (JSON is browser-parseable; same
  posture as `tree`). No shell/server code; navigation and validation live inside
  the template.
- **HV-2** Registry bindings: wildcard key `".*.json": ["history", "tree",
  "code"]` matches any compound `<ext>.json` sidecar (more specific than bare
  `.json`, which keeps its own tree-first list unchanged) — **no `annotate`**:
  annotating the sidecar log itself doesn't make sense, comments belong on the
  target file (HV-8). (Since D235 `annotate` is bound to *nothing* anywhere, §17;
  the rule stands for a user who re-binds it, and it is why this wildcard key was
  never given the authored-file pair either — a sidecar is a generated log, not an
  authored file, PT-14.) `"history"` is also appended to the target-side keys
  `".html"` and `".parquet"` (defaults stay `_render`/`table`). Only these two
  target extensions for now — others later by adding keys.
- **HV-3** Role resolution from `_file`: basename ends `.json` **and** its stem
  (after stripping `.json`) still has its own extension → the sidecar is the
  file itself, target = the name minus `.json` (matches the `.*.json` wildcard
  — a bare `name.json` is never treated as a sidecar); otherwise `_file` is
  the target and sidecar = `_file + ".json"`. Sidecar read via
  `fused.readFile`; absent sidecar → a friendly "no history yet" empty state,
  never an error.
- **HV-4** Validation is **per-key** against an inline `const SCHEMA` in
  `template.html` (a hand-rolled subset validator: `type`, `required`,
  `properties`, `items` — no vendored library). A key that fails renders a
  warning card **in that section only** (first error + collapsed raw JSON of
  that key); the other sections render normally. Only a whole-file parse
  failure (or non-object root) blocks the full view, showing the raw text.
- **HV-5** Unknown top-level keys are NOT corruption — the sidecar is a shared
  store and future writers may add keys. They render as one collapsed
  "Other keys" raw section.
- **HV-6** Entry schemas require only the fields the view renders; extra fields
  on entries are allowed (writers grow their records additively). Timestamp
  units are mixed by design (D83/D84 code comments): bookmark `created_at` and
  comment `createdAt` are **ms** epoch; `recorded_at`/`updated_at` and claude's
  `created_at`/`last_used` are **seconds**. The formatter picks the unit per
  field, never heuristically.
- **HV-7** Interactivity — plain shell navigation via `window.top.location`
  with the `/view/` codec (router.ts shape), the chat-template precedent:
  a claude session opens the target with **`_mode=claude&session_id=<id>`** (the
  resume contract), where `claude` is the one chat template (PT-14) and the
  target is always a FILE, so the mode always resolves; a bookmark-history entry
  and the `lastSession` card open the target with their stored `search`
  verbatim; a comment row is **inert** and navigates nowhere (HV-8).
  **The `_mode` literal in a resume link is load-bearing, and D235/D237 proved
  it twice.** A mode a target does not offer is not an error: PT-9 falls back to
  the target's default view **silently**, taking any `session_id`/`comment`
  beside it with it — so a "resume this session" row lands on the file, in the
  wrong view, with the session dropped and nothing said. D235 caused exactly that
  by moving the chat off the file keys, and D237's rename would have caused it
  again. The rule is therefore **enforced, not documented**: a test extracts
  every `_mode=<name>` string literal this template hardcodes into an outbound
  URL and resolves each one against the real registry for the kind of target that
  row can reach, so a rebinding or a rename fails loudly here instead of
  degrading into a wrong-but-plausible navigation.
- **HV-8** Comments render **read-only** (content, created/updated time,
  resolved badge, annotated view — the view never writes the sidecar, HV-9) and,
  as of **D237, entirely INERT — no row is a link.** A tombstoned entry (an
  explicit `deleted_at`, stamped via `record`'s `deleted_ids`) additionally
  renders dimmed and struck-through with a " · deleted" tooltip note; a deleted
  comment never comes back (owner call 2026-07-10). *The item previously specced
  a deep link:* a comment row with an `id` opened the target with
  `_mode=annotate&comment=<id>`, an id-only link mirroring HV-7's `session_id`
  contract, which annotate resolved against its live store or a one-shot sidecar
  lookup (§17). D235 deregistered `annotate` from all 66 keys, which made that
  URL an unknown mode value — so the click silently opened the file's default
  view with the comment id dropped (PT-9). D237 removed the link rather than
  leaving it: **a click that promises to focus one comment and instead quietly
  shows something else is worse than no click at all.** Rejected: retargeting it
  at the chat the way the session row was retargeted — the chat has no `comment`
  param, and its omission of the annotate handoff is deliberate and doubly true
  (§17), so routing around it from here would only move the silent failure. The
  comment's *text* is still worth reading, so the entry stays in the timeline as
  a plain row. Re-binding `annotate` (§16) restores the mode but not this link;
  making it a link again is a decision, not a rebinding. Supersedes both the
  2026-07-09 owner call that kept comments non-navigable and the 2026-07-10
  reversal that made them navigable.
- **HV-9** The view never writes the sidecar.

## 25. Pinned View — Menu-Bar Popover (M16)

The status item IS the app's whole surface: any click on the menu-bar icon
drops an NSPopover under it — a native header row carrying every app action
(the old dropdown menu is gone, D98) above a live WKWebView of the pinned
file's rendered view — the same `/embed/<path>` page the shell's panes iframe
(chrome-free, full runtime: `fused.runPython`, params, templates, live
reload). Dragging the popover off the menu bar detaches it into a floating
always-on-top window. macOS app bundle only (rides the rumps entry point,
SPEC DM-7); the CLI/browser experience is unchanged.

- **PV-1** Pin state: a single pinned filesystem path, persisted at
  `APP_SUPPORT_DIR/pin.json` (`{"path": "<abs path>"}`). Survives app restarts.
  Any path the registry can render is pinnable — html, parquet, images,
  directories — because the popover loads `/embed/<path>`, which dispatches
  modes exactly like a shell pane. One pin in v1; no pin list.
- **PV-2** Status-item click routing: every click — left, right, ctrl —
  toggles the popover. No NSMenu on the status item (D98: right-click-for-menu
  is undiscoverable; one icon, one gesture, one surface). The popover opens
  even before the server is ready (the body shows a placeholder) so Quit is
  always reachable.
- **PV-3** Header row (native NSButtons above the webview, in the popover):
  **Open in Browser** (home tab, same pending-queue semantics as before
  readiness), **Copy URL**, **Pin…** (NSOpenPanel; becomes **Change…** when a
  pin is set), **Unpin** (hidden when nothing is pinned), **Open app logs**
  (reveal in Finder — "app" because the call log, §31, is the other thing a user
  could mean and is the durable one with settings), **Quit**. Native, not web chrome: the header must work when the
  server is dead — a web-based Quit would die with it.
- **PV-4** Popover: `NSPopover`, transient behavior (click-away dismisses),
  default content 420×450 — a square 420×420 webview over the 30 px bar —
  and user-resizable (Resizable added to the popover window's style mask;
  edge-drag). The chosen size is saved on close (pin.json `size`, surviving
  re-pins/unpins) and becomes the new default. One `WKWebView` created with
  the popover and kept alive — view state (params, scroll) survives
  close/reopen. Re-pinning a different file reloads the webview; reopening
  does not. No pin (or server not ready) → the webview shows a built-in
  placeholder page.
- **PV-5** Detach: the popover is detachable (`popoverShouldDetach:` → YES).
  On detach the resulting window is raised to `NSFloatingWindowLevel` — it
  stays above other apps' windows ("pin on top"), is resizable, and closing it
  returns to popover-on-click. Closing/detaching never clears the pin. The
  popover, the detached window, and the open panel all carry
  `CanJoinAllSpaces | FullScreenAuxiliary` so they appear over fullscreen
  apps; the open panel lifts a Prohibited activation policy to Accessory
  (source runs) so it can hold key focus.
- **PV-6** Dependency: `pyobjc-framework-WebKit` joins the `[app]` extra and
  py2app's `packages` list. Like rumps it is macOS-only and imported lazily
  inside the app entry point — core install and CI stay cross-platform.
- **PV-7** New AppKit code lives in `fused_render/menubar_pin.py` (popover +
  click routing controller) and the pure-python pin store in
  `fused_render/pin_store.py` (unit-tested; AppKit code is not CI-testable).
- **PV-8** Fallback: the rumps menu (Open in browser / Copy URL / Open app logs /
  Quit) is still built but never attached while the popover controller is
  alive. If `menubar_pin` fails to import or construct (e.g. missing WebKit
  framework), the menu is attached as before — the app is never left
  unquittable.

## 26. GitHub Deep Links — fused-render://open?git= (M17)

A shareable link that lands a GitHub repository subdirectory in fused-render:
`fused-render://open?git=https://github.com/{owner}/{repo}/tree/{ref}/{subpath}`
— the original GitHub tree URL, verbatim, as the `git` query param (a link
author copies the GitHub URL and prefixes it). Clicking it launches (or
reuses) the app, shows a confirm page, sparse-clones the subdirectory into
`~/Documents/Fused/<subpath basename>`, and opens the folder's `index.html`
when one exists, else the folder itself.

- **DL-1** Link shape: `fused-render://open?git=<github URL>`. The action
  sits in host position (`open`) and payloads are query params, so future
  payload kinds (a hosted page, a single file, …) become new params on the
  same action instead of new grammar; the `git` value is taken verbatim to
  end-of-string (an unencoded URL with `&`/`+` survives). Accepted GitHub
  shapes: repo root (`/{owner}/{repo}`), `/tree/{ref}`, and
  `/tree/{ref}/{subpath}`; a `.git` suffix on the repo is tolerated; the
  embedded URL may be percent-encoded. `/blob/` (single files) and non-github
  hosts are rejected with a clear error. The first segment after `/tree/` is
  the ref — single-segment refs only (the URL grammar cannot delimit a
  slashed branch name from the subpath; same assumption most tooling makes).
  Refs must start alphanumeric (git forbids leading `-` too), and every
  URL-derived value reaching git sits behind a `--` separator — a crafted
  link cannot smuggle options (`-f`, `--stdin`) into checkout/sparse-checkout.
- **DL-2** OS registration: macOS via `CFBundleURLTypes` in the py2app plist
  (scheme deliberately not branch-suffixed, like the bookmark UTI — every
  build speaks the same links), delivered to `application:openURLs:` in
  app.py; Windows via an HKCU `Software\Classes\fused-render` URL-protocol
  class written by the same `--register` as the Open-With keys, delivered as
  `%1` to `fused-render-open`. Linux deferred. Both handlers reuse a live
  server or spawn one (the winopen/app dance), then open the browser at the
  confirm page — they never parse or clone themselves.
- **DL-3** Confirm gate (`GET /clone?src=…`): a self-contained server-served
  page (`static/clone.html`, no shell, no external assets). Nothing touches
  disk until its button is clicked. The page previews repo / subdirectory /
  ref / destination via read-only `GET /api/clone/info` and states the trust
  boundary in plain words: once opened, content from the repository renders
  same-origin and can run Python on this machine (trust-on-confirm, D110).
  The preview matches what POST will do: an occupied destination that is not
  a matching clone (non-git folder, other repo) is reported as blocked up
  front (`conflict`), never offered as an Update that can only fail.
- **DL-4** Clone (`POST /api/clone`, X-Fused-guarded like every mutating
  route): `git clone --filter=blob:none --sparse` + `sparse-checkout set
  <subpath>` (plain filtered clone for repo-root links) using the user's own
  git — public repos clone anonymously, private repos ride the user's
  existing credentials. Destination is `~/Documents/Fused/<subpath basename>`
  (repo name for root links); the repo root, `.git` included, lives at the
  destination, so the opened view is the nested `<dest>/<subpath>` path. A
  failed clone removes the partial destination (retryable). Git runs
  prompt-free (`GIT_TERMINAL_PROMPT=0`, ssh BatchMode — the server has no
  TTY) with a PATH widened to the usual helper locations (a Finder-launched
  .app gets `/usr/bin:/bin`, which silently breaks `gh`-style credential
  helpers); an https auth failure retries once over `git@github.com:` before
  reporting both errors with a how-to-authenticate hint.
- **DL-5** Re-click = update: for an existing destination whose `origin`
  matches the link's repo — `fetch --tags`, check out the LINK's ref (a link
  naming a different branch/tag than what's on disk lands on that ref, not a
  silent pull of the old one; refs check out after a `--no-checkout` clone,
  never via `--branch`, so commit SHAs work), then `pull --ff-only` iff that
  left HEAD on a branch (a tag/SHA is detached: SHA no-op, moved tag lands
  on its new target). A ref-less link onto a detached clone checks out the
  remote's default branch (origin/HEAD) — "no ref" means the default branch,
  never a silent stay-put. A same-repo link whose subdir shares the basename
  widens the sparse cone additively (`sparse-checkout add`) so its path
  materializes without unchecking earlier links' paths. A dirty or diverged
  tree surfaces git's own error and local edits are never clobbered. The
  link's subdirectory is verified against the target ref's tree (`ls-tree`)
  BEFORE any mutation — a link that would fail its target check leaves the
  existing clone exactly as it was (a fresh clone rolls back via rmtree; an
  update must be equally side-effect-free on failure). A destination that
  exists but is not a clone of that repo is refused, never overwritten.
- **DL-6** Open target: `<dest>/<subpath>/index.html` when present, else the
  subdirectory itself, via the standard `/view/` URL codec.

---

## 27. Fused Account — In-App Login & Setup (M18)

Goal: remove §19's remaining copy-a-terminal-command dead ends. Sign-in
(`fused cloud login`), first-time managed-environment setup
(`fused cloud setup`), and day-two env management happen in the app; the
§1 non-goals stand — this surface manages the **fused CLI's own** credentials
on the user's machine for deploy targets, and every mutation is a
`fused cloud …` / `fused env …` child process through the DP-3 seam
(fusedcli.py). The mechanics port the flow app's connect-fused surface (flow
repo, `spec/app/connect-fused.md`); the design rationale is in DECISIONS.md
(D111/D112). Scope line (deliberate, same as flow's): the
in-app path covers the **managed `fused` backend** only — self-hosted AWS
provisioning stays a documented terminal flow.

### 27.1 Surface

- **AC-1** *(amended by D125)* The account panel is the **Fused account** tab
  on the `/view/_prefs` Preferences page, alongside a **Render preferences**
  tab (Logs/Engine/Deploy to Fused account/Tour — SPEC §20), selected via `?tab=account`
  (bookmarkable, same pattern as Templates' bindings/library tabs). The
  account tab is offered only once the Deploy toggle (§20) is on — that's the
  only reason this app cares about a Fused account. There is no longer a
  standalone sidebar-footer entry for it: the green **signed-in dot** (the
  deploy-dot affordance — the presence-only `logged_in` signal, re-read on
  focus/visibility regain, errors keeping the last-known value) now rides the
  **Preferences** entry's icon instead, shown only when Deploy is enabled
  *and* signed in — the dot is not its own click target (too small to hit
  reliably), so clicking it just opens Preferences like the rest of the
  button. The old `/view/_account` sentinel still resolves: App.tsx redirects
  it (render-time `history.replaceState`, same technique as the `/` → start-dir
  redirect) to `/view/_prefs?tab=account`, so existing bookmarks and the
  Deploy modal's "Set up hosted environment" link keep working.
- **AC-2** `GET /api/account/status` composes: `cli` (DP-4's `cli_status`
  shape), `logged_in` (DP-2b's presence signal), `login_in_flight` (a login
  child is live), `creds_stamp` (the credentials file's mtime, or null — a
  cheap fingerprint the client uses to invalidate its cached probe across a
  credential change, see AC-8), `envs_file`, `store` (the RAW env store: every backend,
  each entry flagged `hosted`, plus the store's own `default` pointer —
  distinct from DP-6's derivation; the deploy picker's derived view stays on
  `GET /api/deploy/config`), and `probe` (null unless requested). The plain
  read is an open GET like deploy's config; `?probe=1` EXECUTES (it spawns a
  control-plane child) and therefore carries the D36 X-Fused guard — a
  foreign page must not be able to trigger subprocess/network work with
  blind cross-origin GETs. `?probe=1` — only when logged in and a CLI
  exists — shells
  `fused cloud orgs` (the authoritative check: it exercises/refreshes the
  token): `{ok, admitted, orgs: [{org, env, provision_state, role}], error}`;
  a probe failure degrades to `ok: false` with the CLI's message via the
  DP-2b error mapping, never an HTTP error (the page renders from the
  presence signal first and fills the probe in).

### 27.2 Login

- **AC-3** `POST /api/account/login {return_url}` spawns
  `fused cloud login --no-browser` and returns `{authorize_url}` — the first
  `http(s)://` URL captured from the child's output; **opening it is the
  client's job** (`window.open`; the server never drives a browser). Child
  env carries `PYTHONUNBUFFERED=1` (Python block-buffers piped stdout — the
  URL line would otherwise sit past the capture window) and
  `OPENFUSED_LOGIN_RETURN_URL=<return_url>` so the CLI's post-login callback
  302s the browser back into the app. `return_url` must be an http(s) URL on
  a loopback host (400 otherwise — mirrors the CLI's own rule; this server is
  loopback-only, D2/D3). **Single-flight**: a concurrent POST joins the live
  child (same URL back; its return_url is ignored) — never a second callback
  server. The capture window is 30s (a COLD external CLI compiles bytecode on
  first run; observed >15s); a child that exits **without** a URL fails the
  request immediately (an exit watcher wakes waiters — no burning the
  window), 502 carrying the CLI's last line via the DP-2b mapping. Every
  kill path confirms death (SIGTERM → SIGKILL escalation, inline or on a
  daemon thread): a merely-SIGTERM'd child could keep its callback server
  alive and complete a late round-trip against a retried login.
- **AC-4** Completion is **polled, not pushed**: the client polls status
  (~2s) until `logged_in` flips; the CLI child owns the OAuth round-trip
  (localhost callback, self-terminating after ~5min). A child that exits
  signed-out (abandoned browser tab, timeout) surfaces as a retryable
  message, detected as `login_in_flight` dropping without `logged_in`.
- **AC-5** `POST /api/account/login/cancel` terminates the child.
  `POST /api/account/logout` terminates **and waits out** (SIGTERM →
  SIGKILL escalation) any in-flight login BEFORE running
  `fused cloud logout --no-browser` — a login child outliving the credential
  delete could complete its callback later and silently re-write the JWT.
  Optional `{env}` forwards `--env NAME` (also drops that env's stored
  data-plane key — the CLI's full-signout semantics). A RUNNING setup job is
  canceled too (account-scoped work; its record reports "canceled by signing
  out" and frees the single job slot) — no wait needed there, a setup child
  can't resurrect the JWT. Returns fresh status.

### 27.3 Environment setup & management

- **AC-6** `POST /api/account/setup {org?, env?, env_name?}` runs
  `fused cloud setup --no-browser [--org O --env E] --env-name NAME` as
  **the one tracked background job**: 202 `{job_id, env_name}`; 409 when a
  job is already running, and 409 when signed out — the interactive login
  flow lives in ONE place (AC-3); a setup child silently waiting on a
  sign-in URL nobody sees would just burn its timeout. Presence isn't
  proof: before spawning, the sign-in is VERIFIED with one `cloud orgs`
  probe, so an expired credential with a dead refresh token gets an
  immediate actionable 409 instead of ~5 minutes of doomed spinner. `org`/`env` go
  together (both or neither — omitting them lets the CLI discover the
  account's workspace, self-creating a personal org for an admitted org-less
  account); `env_name` is validated as a single safe token and defaults to
  flow's convention (`fused` for the default managed env, `fused-<env>`
  otherwise). The child's stdout+stderr are merged into one pipe (progress
  goes to stderr, the final line to stdout — one pipe keeps terminal order)
  and pumped into a bounded tail; `PYTHONUNBUFFERED=1` again; a 900s
  backstop kills a wedged child. The CLI does everything real: waits for
  provisioning, mints the data-plane key into the local secrets store,
  writes the env into `envs.json` — the app never touches a secret.
- **AC-6a** `GET /api/account/setup` reports
  `{state: idle|running|done|failed, job_id, env_name, detail}` — `detail`
  is the CLI's own lines (mapped error when failed; keyring-less Linux
  hosts get the CLI's error naming the `fused[local]` remedy verbatim). The
  client polls (~1.5s), **matches job_id** (a stale job's terminal state
  must not complete a newer attempt), and **adopts** a running job on mount
  (the page reopened mid-setup shows live progress; one-job-at-a-time makes
  it unambiguous).
- **AC-7** `POST /api/account/envs/default {name}` →
  `fused env default NAME`; `POST /api/account/envs/delete {name}` →
  `fused env delete NAME --yes` — the CLI's **local-pointer-only** delete
  (no cloud teardown, no key revocation), stated in the confirm dialog and
  the table copy. Names are rejected when flag-shaped (leading `-`): the
  name lands in argv, where `--help` would be parsed as a click option that
  exits 0 — a silent no-op the endpoint would report as success. Both
  return fresh status so the client updates in one round-trip; the client
  merges it over its cached probe (env actions don't change org
  membership), so the signed-in summary never flickers away.

### 27.4 Tab & Deploy-modal behavior

- **AC-8** The account tab's states, in checking order (the DP-2 pattern):
  CLI missing → the DP-4 install panel (same one-click/manual split);
  signed out → sign-in (waiting + Cancel while connecting; a sign-in
  started elsewhere — Deploy modal, another tab — is adopted read-only with
  its own Cancel); signed in → account summary (probe orgs/roles table,
  not-admitted note, and — when the probe FAILED — a **Sign in again** action
  inside that note, because `logged_in` is presence-only: stored credentials the
  identity provider no longer accepts (an expired, revoked or rotated refresh
  token, surfaced as the CLI's own `403 … invalid refresh token`) leave a state
  that *looks* signed in and cannot be retried out of, so the remedy has to be
  reachable from where the error appears rather than only from the CLI. It reuses
  the one sign-in path — see AC-8b for why completion is not presence), the environments management table (default marker,
  with make-default and forget-with-confirm behind a per-row overflow
  ("⋯") menu — one quiet control per row instead of a button pair), and
  the setup panel — presented
  as CONNECT when the account already has a workspace (`cloud setup
  --org --env` connects the existing environment; nothing is created) and
  as create-your-workspace when it has none: workspace picker when >1
  org/env, the single workspace shown read-only when exactly one (the
  user must see WHICH environment will be connected). The CONNECT path is
  a one-click import of the discovered environment — the primary button
  names it ("Connect <org> / <env>") and the local env name (a nickname
  for this machine's store, prefilled by convention) is demoted behind an
  "Edit name" reveal so the common path needs no typing; the create path
  (no workspace) shows the editable name up front, since naming is the
  point there. Live progress log; prominent while no managed env exists, else collapsed
  behind an "Add managed environment" toggle. The deep probe is CACHED:
  focus/visibility refreshes re-read only the cheap presence status and
  keep the orgs view they have, re-probing only when it is missing (initial
  load, right after a sign-in), forced (setup completion — self-serve may
  have created the workspace), or when `creds_stamp` changed since the cached
  probe (a re-login as a different account that never flipped `logged_in`
  false in this tab — the cache must not show the prior account's orgs). All
  return-to-tab refreshes ride the shared `useRefreshOnReturn` hook
  (lib/hooks.ts), which coalesces the double focus+visibilitychange firing.
- **AC-8b** **A sign-in completes on FRESH CREDENTIALS, not on their presence.**
  `useFusedLogin` captures `creds_stamp` before spawning the child and finishes
  only once the poll reports `logged_in` **and** a stamp different from that
  baseline. Presence alone is the wrong signal for a **re**-authentication: the
  credentials file already exists, so `logged_in` is already true and the first
  poll tick would declare success before the browser round-trip had happened —
  reporting a fixed account while the probe still fails. For a signed-out start
  the baseline is null and the condition collapses to the original presence
  check, so that path is byte-for-byte unchanged. If the pre-flight read of the
  baseline fails the hook degrades to presence — eager for a re-auth, correct for
  a fresh sign-in — rather than refusing to complete at all.
  **Cancel's reconcile applies the same test** (one shared `isFreshLogin`, not two
  copies of the rule). Cancel re-reads the status because the sign-in may have landed
  in the gap before the cancel took effect, and that read must ask the same question
  the poll asks: testing presence there meant that on the re-auth path — credentials
  present, merely rejected — pressing Cancel announced a completed sign-in that never
  happened and dismissed the note that had asked the user to sign in again.
- **AC-11** The page also hosts the **Deployments** section — the env-wide
  `fused share list` view with per-mount Revoke that PF-6 previously placed
  on Preferences (semantics unchanged: `/api/deploy/shares` joined to local
  pages, revoke by env+token via `deploy.revoke_mount`). Each row's actions
  (Open ↗ / Copy link, and the destructive Revoke behind a separator) live in
  the same per-row overflow ("⋯") menu as the environments table, so the
  section shows one control per row rather than an Open link + Revoke button
  pair; a row with no link and nothing to revoke shows a muted "—". Environments and
  Deployments render in BOTH auth states: the env store and an AWS env's
  share list need the CLI, not a managed-Fused sign-in — an AWS-only user
  must not pass through an irrelevant sign-in to revoke a link. Only the
  account summary and the setup panel gate on `logged_in`.
- **AC-9** The Deploy modal never dead-ends into a terminal for the managed
  path: its signed-out warning carries the working sign-in button (DP-2b as
  amended), and its no-envs state signs in in place or routes to the account
  page's setup panel. AWS env creation keeps naming
  `<setup_cli> env create` — out of scope by the §27 scope line — and that
  hint renders in BOTH branches: an AWS-only user who is signed out must
  not be funneled into an irrelevant managed-cloud sign-in to learn it.

### 27.5 Trust & credentials

- **AC-10** No credential ever touches fused-render: the CLI owns the JWT
  (`~/.openfused/fused-cloud-credentials.json`) and the data-plane keys
  (the CLI's local secrets store); this surface reads *presence/status* and
  runs the CLI, and persists nothing of its own under `~/.fused-render`.
  All mutating endpoints carry the D36 X-Fused guard; `return_url` is
  loopback-constrained (AC-3). The D3 stance is unchanged — this is not
  authentication *of* fused-render, and the §1 non-goal stands as annotated.
## 28. Canvas View — Conditional Layout Viewer for `canvas.toml` (D114)

A `canvas` view template renders a Fused **canvas definition** (`canvas.toml`,
v2) as a read-only **layout viewer**: nodes drawn as positioned boxes, folder
groups behind them, edges wired between node borders, honoring the stored
viewport. It is the **first consumer of the conditional-template mechanism**
(CT-12): listed first for `.toml` but gated so only genuine canvas files ever
offer it — a plain `.toml` never shows the mode at all.

- **CV-1** Files (`fused_render/templates/canvas/`): `template.html` (the
  viewer), `reader.py` (the toml→JSON parser), `condition.py` (the gate),
  `icon.svg`. Registry binding: `".toml": ["canvas", "code", "claude",
  "versions", "reader"]` — canvas listed first, then the ordinary tail every
  authored config key carries (`annotate`, the trailing mode this item originally
  named, was deregistered from every core key by D235 — PT-14, §17). Under deferred CT-12 the immediate default is the first
  *unconditional* entry (`code`); `canvas` resolves in the background and joins
  the switcher when its verdict allows (or disappears when it doesn't).
- **CV-2** **Condition gate (CT-12, deferred).** Stat only *marks* the canvas
  entry `conditional`; `condition.py` is evaluated via
  `GET /api/fs/conditions` in the background (PT-8/CT-12). The gate itself is
  cheap and fail-closed: a **basename pre-check** (`canvas.toml`, no I/O)
  before any open, a **2 MB size guard**, then a `tomllib` parse asserting
  top-level `type == "canvas"` (the content sniff, D114). Any exception →
  False; the mode is denied and `code` stays. No `template_error` on a fail —
  an ordinary toml is not an error.
- **CV-3** **Reader (`reader.py`, `@fused.udf`-registered).** One `tomllib`
  pass → `{name, version, previewImageUrl, nodes, folders, edges, viewport,
  viewportBounds, siblings}`. `type == "udf-folder"` entries go to `folders`
  (folderName, folderColor, childUdfOrder, isLocked); the rest to `nodes`
  (title defaults to udfName, visible defaults true — the §28 defaults). Edges
  are `[src, dst]` name pairs; malformed nodes/edges are **skipped, never
  fatal**. `siblings` maps each node's udfName → the sibling file extensions
  (`.py`/`.json`/`.md`/`.html`) present next to the toml, from one
  `os.listdir`. **Engine isolation:** the whole body — helpers and imports —
  lives inside `main()`; nothing but the entrypoint and its registration shim
  is at module level.
- **CV-4** **Viewer (`template.html`).** A single full-viewport `<canvas>` in
  world space (toml coordinates), visually replicating the Flow app canvas
  (its widget.css tokens: #070a0f bg with a 50-gap dot grid, #0d1219 node
  cards with a #11171f header bar, bezier edges at 22% text tone with a
  bg-colored legibility outline and target arrowhead, folder regions as a
  color wash with a solid title pill above; folderColor `series-N`/`chart-N`
  keys map into the series palette, default purple). Draw order matches Flow's
  zIndex layering: folder regions → edges → node cards (title header,
  description/udfName body, sibling-extension chips; `visible:false` ghosted
  at 40% alpha) → folder title pills. Geometry, text, and borders are drawn in
  **world units** so everything scales with zoom exactly like ReactFlow's
  transformed DOM; only edge strokes are screen-constant (`min(5, 1.5/zoom)`).
  Hovering a node brightens its border and shows a title/description tooltip.
  Empty canvas → a centered "empty canvas" note; a reader error surfaces
  through the runtime's traceback overlay (the header still renders first).
- **CV-5** **Camera & URL sync.** Start from `[canvas.viewport]` (x/y/zoom)
  when present, else **fit-to-bounds** of all nodes with a 10% margin (fit
  clamps zoom to ≤1; interactive zoom clamps to [0.1, 2], Flow's min/max).
  Wheel zooms to the cursor, drag pans; a bottom-right glass cluster offers
  zoom in/out and an animated **Fit** (600 ms cubic ease-out, instant under
  `prefers-reduced-motion`). Camera
  state mirrors to URL params `cx`/`cy`/`z` (translate + zoom) on interaction
  (150 ms debounce) and is read on load, where it **overrides** the toml
  viewport — so refresh/share restores the exact camera. Params are strings
  (`set` throws otherwise); parsed at the boundary. The template's own writes
  are echo-guarded in `onChange`; a `_file` change reloads.
- **CV-6** **Detail panel.** Clicking a node opens a footer panel: title,
  udfName, description, size, and sibling files as links that open
  `/view/<abs sibling path>` in a **new tab** (no in-shell navigation, v0).
  Clicking a folder shows its name and child list; clicking empty space clears.
- **CV-7** Dark theme matching the explorer, no external assets, ES2020, and —
  like every template — no runtime script tag (`window.fused` is injected).
  Out of scope (v0): editing/writing the toml, rendering widget contents inside
  nodes, executing UDFs, in-shell sibling navigation, non-v2 canvas versions.
## 29. Recents — Sidebar Last-Opened Files (D115)

Goal: getting back to what you were just working on is one click — the sidebar
lists the last files opened in the app, each carrying the params it last had.

- **RC-1** A collapsible **Recents** section in the shell sidebar shows the
  last **3** files opened (display order per RC-11). Row label = basename of
  the file (D22 naming); the full decoded path is the tooltip. Recents rows
  carry **no active/selected state** (owner call — the section is a jump
  list, not a location indicator; bookmark rows keep theirs).
- **RC-2** An entry stores the exact shell url **verbatim including the query
  string** (D20 posture — the URL is the whole state). Click = plain
  query-preserving navigation (`navigateUrl`); opening a recent arms no
  bookmark.
- **RC-3** Entries update **live**: while a file is open, every param write
  re-records the entry's url (500 ms debounce against slider churn) — a recent
  reopens with the file's latest params, not the snapshot at open time. The
  currently-open file IS listed.
- **RC-4** Files only. Directory navigation and sentinel routes (any
  `_`-prefixed top-level view — `_panel`, `_prefs`, `_templates`, ...) are
  never recorded; embed panes neither (layout modes own pane state, D72).
- **RC-5** Store: `~/.fused-render/recents.json` —
  `{collapsed, entries: [{url, openedAt}]}` — via the shell/storage atomic
  helpers (last-write-wins, no locking, D3). `collapsed` is persisted with the
  data itself, like D44's folder collapse.
- **RC-6** Dedupe by target fs path: recording an already-listed file moves it
  to the top and replaces its url. The store caps at **20** entries — a buffer
  so 3 valid rows survive RC-7 filtering.
- **RC-7** Entries whose file no longer exists are **hidden silently** from
  the GET response — never deleted from disk (the file may come back).
- **RC-12** A **delete or Move-to-Bin drops the row immediately**, without
  waiting for RC-7. RC-7 only bites on a GET, and deleting a file triggers none —
  so the row used to sit in the sidebar pointing at a file that no longer existed
  until the user's next navigation happened to refresh the cache. Every delete /
  trash site instead calls **one** function with the path that went away
  (`fs-actions.notePathDeleted`, which also prunes the clipboard — one call so the
  next thing that needs to hear about a delete is added there rather than hunted
  for across the views), and `recents.dropRecentsFor` drops that path plus
  anything under it (prefix + `/`, since deleting a folder takes its contents).
  **Local, no request**: the store runs deeper than the three displayed rows
  (RC-6's buffer), so the freed slot refills from the cache already held, by the
  same RC-11 arithmetic a vanished-on-refresh entry gets. A re-GET would be the
  wrong tool — RC-7's existence check fails *open* on an indeterminate answer, so
  a row the user just deleted could come straight back. Nothing is written to the
  store, so a file restored from the Bin reappears in Recents as RC-7 intends.
- **RC-8** API (`fused_render/shell/recents.py`): `GET /api/recents`
  (unguarded read, filtered per RC-7), `POST /api/recents/open {url}` and
  `PUT /api/recents/collapsed {collapsed}` (both X-Fused-guarded, D36). The
  POST validates the url is an existing file's `/view/` url and no-ops
  otherwise (`recorded: false`) — the client stays dumb about the target's
  kind.
- **RC-9** Recording is fire-and-forget (a recents failure never affects the
  view being opened); the recording hook rides the StatView seam beside
  session tracking (same confirmed-file gate, LSN-6 posture).
- **RC-10** The section is hidden entirely while there are no entries.
- **RC-11** **Data is MRU, display is stable slots.** The store stays strict
  MRU (RC-6), but the visible top-3 must never move under the user's own
  interaction: a displayed file keeps its slot for the page session — a
  re-open or live param update (RC-3) changes its row **in place**, never its
  position (so clicking a recents row moves nothing). The only movement is a
  file NOT currently displayed entering at the top (a real navigation to a
  new file), pushing the bottom row out. A displayed file that vanishes
  (RC-7) leaves its slot and the next MRU entry fills in at the bottom —
  surviving rows never reshuffle. The slot order is session view state, not
  persisted: on boot the display seeds from server MRU order. Rows are keyed
  by fs path (not url) so a param write never remounts a row; a url update
  DOES notify (hrefs/click targets must stay fresh per RC-3) but re-renders
  the row's attributes in place — zero movement — and a refresh that changes
  nothing visible triggers no re-render at all.

---

## 30. Appearance — System / Light / Dark (D134)

Goal: the app opens in the appearance the user's desktop already asked for, and
they can pin Light or Dark if they'd rather. fused-render was dark by constant,
not by choice.

- **AP-1** One preference with three values — **System** (default), **Light**,
  **Dark** — persisted per **browser profile** in `localStorage` under
  `fused-render:theme`, using the best-effort read/write posture of
  `lib/viewstate.ts` / `lib/sidebarstate.ts` (silent on failure, never
  blocking). A browser profile and the desktop app window legitimately hold
  different choices. **There is no server-side store, endpoint, config file or
  machine-wide syncing** (deliberately — D134).
- **AP-2** The **resolved** theme (`light` | `dark`) is `System` →
  `prefers-color-scheme`, otherwise the pinned value. It lives in exactly one
  place: `data-theme` on the document's `<html>`. Dark is the CSS default, so a
  document with no attribute renders exactly as it always did.
- **AP-3** **No flash.** The resolution runs in an **inline** `<head>` script in
  `frontend/index.html`, ahead of every stylesheet — the attribute is on the
  document before first paint, not after React hydrates. That script duplicates
  the key and the resolution rule because nothing importable exists that early;
  a test pins the spellings together (`tests/test_theme.py`).
- **AP-4** While the setting is **System**, an OS appearance change mid-session
  — including macOS's automatic sunset switch — restyles the shell and every
  open opted-in view **live**, with no reload. When pinned, the OS change is
  ignored.
- **AP-5** Shell chrome is fully tokenized: **every** colour in `shell.css`
  comes from a palette token, and `:root[data-theme="light"]` redefines the
  whole set. Translucent washes ride an `rgba(var(--tint), a)` triple that flips
  white→black, since the originals assumed a dark backdrop. Dark is unchanged
  value-for-value; this is not a redesign.
- **AP-6** The setting's surface is an **Appearance section on the Preferences
  page** (`/view/_prefs`, §20) — a three-way radio group, one option per choice,
  sitting above the server-backed sections. It is the one section there that is
  *not* server-backed (AP-1): it writes `localStorage` synchronously and needs no
  busy/error plumbing. There is no picker in the sidebar.
- **AP-7** **Views are pushed to, never re-mounted.** View documents are
  iframes, and a React re-render must never touch a live iframe (§14, D45). The
  injected runtime (`static/runtime.js`) instead resolves the same key in the
  view's own document and writes `data-theme` there. Cross-window convergence
  rides the `storage` event; a System-mode OS flip rides each document's own
  `matchMedia`. No reload, no remount, no lost scroll/selection/unsaved edits.
- **AP-8** That push is **opt-in**: only a document whose `<html>` carries
  `data-fused-theme` is touched. The attribute must be on `<html>`, not a
  `<meta>` — `runtime.js` is injected at the top of `<head>` and is
  parser-blocking, so it runs before the rest of the head is parsed (which is
  also why the view has no flash either).
- **AP-9** **Tier 1** — views with a real light palette authored: `code`,
  `text`, `markdown`, `plist`, `api`, `sqlite`, `duckdb`, `xlsx`,
  `vector`, `structure`, `tree`, `log_studio`, `claude`, `history`, `annotate`,
  `zip`. Each carries the identical
  structure: a `:root` dark palette, a `:root[data-theme="light"]` twin defining
  the same token set, and the AP-8 opt-in — and **nothing else**: no `data-theme`
  literal on `<html>` (dark is the CSS default, AP-2, and the runtime overwrites
  the attribute before first paint anyway), no reading of the storage key, no
  private theme param. A tier-1 view whose canvas/JS colours are sampled at draw
  time re-reads them from a `MutationObserver` on `data-theme` (`code`
  reconfigures CodeMirror this way; `log_studio` redraws its charts).
- **AP-10** **Light by design** — always light, ignore the setting entirely, no
  opt-in: `map`, `pano`, `latex`, `slides`, `usd`, `pyramid`, `docs`,
  `pdf_studio`. (`docs` is exempt even though it otherwise sits in the tier-1
  text/code group. `claude` was on this list and did not belong: it was a *dark*
  view mislabelled light-by-design, which is why the error stayed invisible —
  it is tier-1 as of D157.)
- **AP-11** **Self-toggling**: `excel` and `tableau` keep their own in-view
  theme buttons and their private storage keys, ignore the app setting entirely,
  and the shell never pushes a theme into them (no `data-fused-theme` opt-in, so
  the runtime leaves them alone). **A built-in view does not get both**: either
  it opts in and the shell's setting is the whole answer (AP-9), or it owns its
  appearance completely (here) — a view that opts in *and* keeps a switch has
  two resolvers for one question, and the switch is the one that loses, because
  every `storage`/`matchMedia` change re-applies over it. (`log_studio` was on
  this list; its button is gone and it is tier-1 — D154.)
- **AP-12** **Deferred** — the media, geospatial and studio/tool groups keep
  today's appearance in both modes until a later pass: `image`, `photos`,
  `media`, `pdf`, `glb`, `canvas`, `geotiff`, `pmtiles`, `h3`, `zarr_aoi`,
  `netcdf`, `las`, `geometry_editor`, `reader`, `tar`. Accepted
  consequence: in Light mode these render dark inside a light shell. (`preview`
  left this list by being **deleted** — D185 folded its split pane into the
  shell's own listing, which follows the app setting like the rest of the shell,
  so the one dark-only folder view is simply gone rather than converted.)
  (`history`,
  `annotate` and `zip` left this list in D157 — they are ordinary DOM chrome
  with no canvas or map underneath, which is what made them cheap to convert;
  `annotate`'s *stage* is still whatever the framed view chose to be.)
- **AP-13** **User-authored `.html` views get no theme signal at all** — by
  default. Their CSS stays entirely theirs and nothing is written into their
  document. The AP-8 opt-in is a property of the *document*, not of who wrote
  it, so a user view that sets `data-fused-theme` itself is asking for the
  attribute and gets it on the same terms as a built-in; that is documented in
  the authoring skill as the way to match the app. What must never happen is a
  view being themed without having asked.
- **AP-14** The four lists above (AP-9..AP-12) are **exhaustive** over
  `fused_render/templates/`, and a test asserts it — a newly added template
  cannot quietly skip classification.
- **AP-15** The native macOS menubar popover (`menubar_pin.py`) keeps following
  `prefers-color-scheme` on its own and does **not** honour an in-app pin. The
  Linux tray icon likewise follows the *desktop's* preference, not this setting
  (D135) — the two are independent.

---

## 31. Call Log — What API Calls a Page Made (D136)

Goal: a page's API calls stop being invisible. Every call a rendered page makes
through the injected runtime is recorded — with its duration, result size,
output and any traceback — so "why is this page slow", "what did my app just
do", and "did it error when the user opened it" have answers that survive a
reload. Design + rationale: `docs/CALL_LOG_DESIGN.md`.

- **CL-1** **What is a call.** Every request `static/runtime.js` issues on a
  page's behalf: `runPython` (`POST /api/run`), `writeFile`
  (`POST /api/fs/write`), `stat` (`GET /api/fs/stat`), `readFile`
  (`GET /api/fs/raw`). NOT in the log: requests the shell makes for itself
  (they carry no attribution, CL-3), a page's own `fetch()` to a third party,
  the sci templates' loopback tile daemons (outside the server by design,
  D122), and `rawUrl()` used as an element `src` — a synchronous URL string
  has nowhere to carry a header, and adding a query param would change every
  raw URL (cache keys, and the hosted runtime's bundle-key resolution). The
  log is honest about what it sees; it does not claim to be complete.
- **CL-2** **The record** (`calls.py`, `version: 1`) is the serve plane's
  error record (the `fused` repo's `spec/serve/error-reporting.md` §1)
  **widened to successes** — same field names, units, and caps, additive under
  `version` — so a page's local numbers and its deployed ones are comparable
  and render in one viewer. Adds `outcome`, `result_bytes`/`result_kind`/
  `result_rows`, `server_ms`/`run_ms`, `page`/`target_file`/`first_party`,
  `route`, `engine`, `call_id`. Caps verbatim from that spec: `error` ≤ 16 KiB,
  `stdout_tail`/`stderr_tail` ≤ 4 KiB each, `params` ≤ 2 KiB serialized, whole
  record ≤ ~32 KiB. Truncation is **marked** in the record (`truncated`,
  `params_truncated`), never silently grown, and text is capped at the **tail**
  — the end of a traceback is the exception. Never stored: file contents (a
  write records its byte count only), request headers. Each record also carries a
  conventional **`level`** (`INFO` for ok, `ERROR` for error/conflict and a
  page-error, `WARN` for readonly, `DEBUG` for the stale outcomes — thrown-away
  work is normal for a slider, not a warning), emitted **early** in the object
  because a generic log viewer takes the FIRST level word in the line: ahead of
  the page path, params and traceback, so `/x/error-demo.html` or an "INFO" in
  stdout cannot outvote the real severity. Without it a healthy record contained
  no level word at all and `log_studio` bucketed everything as **OTHER**, its
  level facets empty. **Null-valued keys are omitted on write**: a narrow record (a `stat`, a raw read) was otherwise
  mostly `null`s, and — because generic log viewers infer a level by sniffing
  the raw line for level words — the field NAME `"error": null` made every
  healthy call render as ERROR in `log_studio`, which the registry offers for
  this file. Absent-means-null is safe for every consumer here (all read through
  `.get()`), and matches the sidecar's additive-records posture (HV-6). The
  last-resort shrink for a record still over ~32 KiB after the per-field caps
  **marks** the fields it drops by setting them to `None`, so its result goes
  back through the same prune before serialization — writing it directly emitted
  those fields as explicit nulls and omitted `level` and `recorded_at`,
  reinstating the `"error": null` ERROR misread on precisely the records most
  likely to be worth reading.
- **CL-3** **Attribution is by header, exclusion by construction.**
  `runtime.js` sends `X-Fused-Page` (the page's own path), `X-Fused-Target`
  (`_file`, when the page is a template) and `X-Fused-Call` (a per-call id) on
  every call it issues, plus `X-Fused-Supersedes` (comma-separated ids this call
  abandoned, CL-5a) on the request that caused a supersession.
  `X-Fused-Page` is the whole test for "is this an app
  call": the shell's own `/api/fs/list`, the conditions probe, and any other
  caller carry none and are therefore never logged — no endpoint blocklist to
  drift. Like `X-Fused` these force a CORS preflight; this is attribution, not
  auth (D3/D36 unchanged).
- **CL-4** **One write point.** The record is created by the ASGI middleware on
  the way in (`request.state.fused_call`), **enriched in place** by route
  handlers (`/api/run` adds the resolved `.py`, params, engine, output tails
  and traceback; `/api/fs/write` adds bytes and the conflict/readonly
  outcome), and written by the middleware on the way out — the single
  `calls.record()` call. A handler that enriches nothing still yields a valid
  thin record, so a new endpoint is logged by default. Because
  `@app.exception_handler(Exception)` runs in `ServerErrorMiddleware`
  (**outside** user middleware), a 500 is recorded from the middleware's
  `except` branch. `server_ms` is time-to-response-**object**, not
  time-to-last-byte (a `FileResponse`/mount proxy has not streamed yet) — the
  same property the SV-3 access line already has; `result_bytes` for a
  streamed route comes from `Content-Length`.
- **CL-5** **Superseded calls are counted, never averaged.** `runPython`'s
  latest-wins cancellation (RH-9/D114) means one slider drag issues dozens of
  calls of which one completes. Records carry `outcome`
  (`ok | error | conflict | readonly | superseded | aborted | disconnected`),
  and every latency statistic — `p50`/`p95`/`max`, per bucket and per target —
  **excludes** the stale outcomes while still counting them separately.
  Thrown-away work is a signal worth seeing (it is the "my page is hammering
  Python" tell); folding it into percentiles would report a dozen slow calls
  for what the user experienced as one request.
- **CL-5a** **The page reports supersession; the server cannot infer it.**
  Aborting a `fetch` does **not** raise into the handler — the run completes and
  the middleware would record an ordinary success — so the page names the
  abandoned `call_id`s and `finish()` stamps the outcome from a short-TTL,
  hard-bounded set of pending marks. Because the store is append-only and the
  outcome is stamped in place, the mark has to arrive **before** the abandoned
  call's record is written, which makes the transport a correctness question and
  not a detail: it rides the **`X-Fused-Supersedes` header on the superseding
  request**. A supersession only ever happens because the page is issuing a new
  call on the same channel, and that request leaves in the same synchronous task
  as the abort, so the server takes the mark in `begin()` — the earliest point it
  sees the request. The separate `POST /api/calls/event` (`kind: "superseded"`)
  remains as the **unload backstop**, where there is no request left to carry the
  ids; it is no longer the primary path, because deferring it by one macrotask
  put it ~19 ms after the abort (measured), and every abandoned call that
  finished inside that window was written `ok` and averaged into the percentiles
  — the failure this rule exists to prevent, reachable by any in-process helper
  (D72). A mark is consumed once, so an id can never reclassify a second record,
  and the header path *takes* the queued ids rather than copying them so a
  duplicate cannot arrive after the record was written. **Known gap:** a closed tab or
  a reload is not reported by anyone and still records as `ok`. Server-side
  detection is **not available under this app's shape**, verified twice rather
  than assumed: from a route, `BaseHTTPMiddleware` wraps the downstream
  `receive` so `request.is_disconnected()` never observes `http.disconnect`;
  from the middleware it does observe it, but `is_disconnected()` peeks by
  *consuming* a message off the receive channel, so polling it starves the
  downstream route of its `http.request` body and every request with a body
  hangs (a body-less spike hides this entirely). Closing the gap means
  converting that middleware to pure ASGI so it can tee the receive channel —
  its own change to the server's hottest path. The abandoned run also completes
  in full either way: `runPython`'s cancellation frees the browser's connection,
  not the compute, and killing it needs the executor to expose its Popen.
- **CL-6** **`page-error` records: the record for when NO call happened.** A
  page whose JS throws before it reaches `runPython` is, in the log, identical
  to a page nobody opened — so `runtime.js` reports uncaught errors and
  non-runPython unhandled rejections to `POST /api/calls/event`
  (`kind: "page-error"`, carrying message/source/line/col/stack), capped per
  page load. `POST /api/calls/event` carries both page-originated kinds — this
  one and `superseded` (CL-5a) — since both are facts only the page can know.
  A page error is the one record that is not written by the middleware,
  deliberately: it is not an HTTP call, it is what happened instead of one. A
  runPython failure the page did not catch is NOT re-reported here — the
  server already recorded it against the `/api/run` call, with the real
  traceback.
- **CL-7** **Store.** `~/.fused-render/logs/<partition>/<date>-<pid>-<part>.calls.jsonl`
  — append-only JSONL under the branch-aware shell home, partitioned per app
  (CL-18). The root is `logs/`, which is NOT where `logs.py` writes: the app log
  is disposable and lives in the system temp dir (D68), while this store is
  durable and pruned by code (CL-10), so the two never share a directory despite
  both being called logs in the UI.
  One file per day per
  process (per-pid for the same reason `logs.py` is: two live servers must not
  interleave lines, and the reader merges the day back together, CL-12), rolled
  to the next `part` past `MAX_FILE_BYTES` (CL-9). `part` is zero-padded so parts
  of one pid sort in order, which the oldest-first size trim depends on. Name
  order is date, then pid, then part — and only the **date** segment orders
  records in time. The pid segment does not (it is arbitrary, and compared
  lexically, so pid 8000 sorts after pid 12345), which is why NO reader may treat
  "last file by name" as "newest records" — not the store walk (CL-12), and not
  any bounded newest-first probe a future gate does (CL-11), which must order by
  **mtime**: on reverse name order its whole window can be stale same-day files. Not the `<file>.json`
  sidecar (§21, D82–D84): every writer there does a whole-file
  read-merge-write, which at call volume is O(n²) plus a lost-update race —
  the sidecar is right for low-frequency history, wrong for a firehose. Not
  the app log (`logs.py`): that file is disposable by design (D68) and
  unparseable. `.calls.jsonl` is a compound registry key, so the store opens
  in the `calls` view by default and `.jsonl`'s `duckdb` binding still queries
  it with no new code.
- **CL-8** **Fail-open is normative.** Logging must never fail — or
  meaningfully delay — the thing it observes. `record()` only does a
  `put_nowait` onto a bounded queue; a background writer thread does the
  append, so nothing on the request path touches the filesystem. An unwritable
  directory, a full queue, an unserializable value, or a rate-cap hit **drops
  the record and counts the drop** (surfaced as `dropped`); none may alter the
  response. The writer thread swallows write errors and keeps draining — a
  dead writer would silently stop logging while callers kept queueing.
- **CL-9** **Four independent bounds**, because a diagnostics store that fills
  the disk would be a worse bug than the one it exists to find: per-record caps
  (CL-2), a **per-page token bucket** (600/min, burst 200 — a runaway render
  loop drops its excess and cannot silence other pages), a **per-file cap**
  (`MAX_FILE_BYTES`, 32 MB, rolled to a new part — without it a single day's
  file grows unbounded, since the directory cap can only delete whole files),
  and **retention by both age and directory size** (default 14 days, matching
  the serve plane's `errors/` lifecycle rule, plus a 200 MB cap trimmed
  oldest-first). **A file dated today is never trimmed**: it may be open for
  append by this process or another server, and deleting it would silently
  discard the whole day (the writer simply recreates it) — so within-day growth
  is bounded by the per-file roll, not by the directory cap, and a store still
  over cap with only today's files left logs a warning rather than pretending
  the cap held. **Retention runs on the writer thread, never a request**: once
  when the thread starts, then whenever the UTC date rolls or 24 h have elapsed
  — checked both after a write and on an idle wake, **whichever comes first**.
  The queue wait is bounded (`SWEEP_POLL_S`) precisely so the second of those
  exists: with an indefinite wait the due-check only ran just after a record
  landed, which made retention a side effect of writing and left an app that
  went quiet after a busy afternoon holding its expired files until something
  called Python again — "nothing is happening" being exactly when nobody
  triggers the cleanup. A busy server still sweeps at most once a day; the
  check gates on the same interval either way. (The writer thread is started
  lazily by the first record, so a process that records nothing at all prunes
  nothing — accepted: such a process is also adding nothing, and the next
  session that makes a single call clears the backlog.) D68 chose the temp dir
  for the app log precisely because "nothing prunes the directory"; this store
  is durable instead, so the pruning is code.
- **CL-10** **Reads of the store are recorded like any other call; nothing
  *watches* a store file.** Everything that opens the store (`log_studio`,
  `code`, `duckdb`, `tree`) **is** logged: what a viewer costs to open a large
  log is worth knowing, and a blanket exclusion would be a special case in the
  record contract. The one exclusion this rule used to carry — the `calls`
  view's own reader, matched by shape so a polling viewer's reads could not
  inflate the numbers it was reporting — went with the view (CL-11) rather than
  sitting unreachable, and comes back with it: a viewer that POLLS and attributes
  its reads to the page being analysed is a feedback loop, and that is a
  property of the viewer, not of the store. That is only safe because **the runtime never adds
  a call-log file to its auto-reload watch set** (`calls_dir`/`calls_suffix` from
  `/api/config`, applied beside the existing mount-backed exclusion): viewing the
  file appends to it, so a watcher would reload, re-read, append and reload
  forever. Removing the watch kills the loop at its source rather than by
  withholding data, and it must live in the runtime rather than per template —
  `code` opts out of auto-reload unconditionally, `log_studio` only while Tail is
  on (default off), and `duckdb`/`tree` not at all, so template-side opt-outs
  would leave the default path looping. Watching a store file was never useful
  anyway: the viewers that want live updates poll, and both `log_studio`'s Tail
  and the `calls` view's Follow switch auto-reload off while engaged so a reload
  cannot rebuild the frame mid-poll. The `/api/calls*` routes are likewise never
  logged.
- **CL-11** **The in-app view is DEFERRED to its own change; this spec section
  covers the store and the CLI.** A `calls` view template was written and then
  pulled: it failed in practice (a worker could not import the package — the
  bootstrap since retired, PY-6a/PY-15),
  and rather than keep debugging a surface inside a change that is really about
  the record and the store, it comes back on its own. Nothing is unread in the
  meantime: `fused-render calls` (CL-13) is the agent's read surface and runs
  in-process with no worker at all, and `.calls.jsonl` is bound to **log_studio**,
  which renders each record as fields (records carry a conventional `level`, so
  its facets and histogram work unchanged). When the view returns it is an
  ordinary template per HV-1/D78 — no shell code, forkable into
  `~/.fused-render/templates/` — bound as a **conditional peer** (CT-12), never a
  default, with a `condition.py` gate on "this file has records" so a page nobody
  has run grows no dead mode. That gate must duplicate the store-path resolution
  (it runs standalone in the user template dir) and the duplicate must be
  **pinned by a test** against `store_dir()` and `partition_name()`, not left to
  match by inspection — the shape D144/D151 already paid for twice.
- **CL-12** **The reader pre-aggregates; the template draws.** Ops mirror
  `log_studio/reader.py`: `overview`, `page` (cursor-paged), `series`
  (bucketed points), `targets` (per-entrypoint rollup), `detail`, `config`.
  Bucketing and percentiles happen server-side — the template sees one point
  per bucket, never 100k records. Reads are also bounded by the **window**, not
  just by the response size: files are read backwards from the tail, a file
  whose mtime predates the window is skipped whole (for an append-only file its
  mtime IS its newest record), and within a file the first record **appended**
  before the window ends it. The early stop compares `recorded_at` (append time,
  stamped at write) and **not** `occurred_at` (call start, stamped in `begin()`):
  the file is ordered by COMPLETION, so a long call sits at the tail carrying an
  old start time, and stopping on `occurred_at` skipped newer short calls
  appended before it — a short window over ordinary overlapping traffic could
  return nothing at all. Since `occurred_at <= recorded_at` always, a record
  appended before the window cannot have started inside it, which makes the stop
  exact rather than merely conservative; a record without `recorded_at` never
  stops the walk. Files are skipped but never stopped at, because same-day files
  from different processes interleave in time. Without this a one-hour question
  parsed the entire retention window. For the same reason that interleaving
  forbids stopping at a file, same-day files are **merged** on `recorded_at`
  rather than read one after another: pid order is not time order (CL-7), so
  draining one file before the next returned a stale process's tail as the newest
  records — with two live servers, `query`'s cursor stuck on the lexically-later
  pid and `--follow` never woke, because the live server's writes sorted first
  and were reached only after the stale file ran out. The merge is **per day**
  (whole days cannot interleave — a file only takes appends while the UTC date
  still matches its name), which bounds open handles to one day's files and keeps
  the walk lazy: a `limit` satisfied by today never opens last week's.
  These are the query helpers in `calls.py`; the CLI (CL-13) calls them
  directly, and the deferred view (CL-11) will too.
- **CL-13** **A cursor, not a wall-clock guess.** `query` accepts a
  `call_id` cursor and returns the newest id with every page, so a caller —
  usually an agent verifying a page it just wrote — asks for "everything since
  I last looked" instead of guessing how long the human took to open it. The
  returned cursor is always an id the caller was **shown**: the newest record
  that passed the filters, never merely the newest in the store. A cursor drawn
  from outside the filtered stream advances on traffic the caller cannot see, so
  `--follow --page X` woke on another page's calls and then reported none for X —
  the same false negative the feature exists to prevent. When nothing newer
  matched, the caller's own cursor is returned unchanged (returning `None` would
  read as "start over" and answer with an unbounded newest page); with no matches
  at all it is `None`, and the CLI omits the cursor line rather than printing one
  that cannot be passed back. The walk still stops at the cursor by **identity**,
  checked before the filters, so a cursor that no longer matches them ends the
  walk correctly instead of reading as purged. A caller that passes a cursor is
  telling you what it has already seen, so `--follow` **waits only when that
  cursor already has matching records behind it**: those landed between the
  caller's last read and this command and are already the answer, and waiting for
  the tip to move past them timed out holding exactly what was being waited for.
  That test is a **bounded read, not an id comparison** — `cursor != tip` answers
  a different question, and a cursor from a broader read is not the tip of a
  narrower one, so comparing ids made `--follow --page X` with a global cursor
  skip the wait entirely and report nothing. A cursor that cannot be found is not
  treated as "already new": absence proves nothing about what arrived, so it falls
  through to the wait — and when that happens the follow resumes from the
  **baseline**, not from the ghost id: keeping it made the post-wait read fall
  back to "the newest page", which the bounded digest then reported as what
  arrived. `cursor_missing` reaches the caller on **every** exit — including the
  timeout, which is the likelier one for a bad cursor (an agent holding a ghost id
  usually has nothing arriving either) and which reported it only once activity
  happened to save it, i.e. never when it was the whole explanation for an empty
  answer. And because the seeking
  walk gives up after a bounded scan, `cursor_missing` on its own cannot tell
  "purged" from "never reached" — `scan_truncated` says which, is carried into the
  CLI's JSON, and switches the text note from a confident "purged by retention, or
  wrong" to a statement that the store is deeper than this read and the gap is not
  shown. Claiming absence the walk never verified is the error to avoid. On the
  follow path that flag comes from the **probe**, not from the post-wait read: once
  the cursor is replaced by the baseline that read is no longer looking for the
  caller's id, so its `scan_truncated` says nothing about the caller's cursor.
- **CL-14** **CLI.** `fused-render calls [--page P] [--since 1h] [--failed]
  [--entrypoint E] [--since-cursor ID] [--json] [--verbose] [--follow]` reads
  the store directly off disk (no server needed). **Digest by default**: the
  outcome tally, the per-target rollup, then failures in full and page errors
  named separately — dumping hundreds of raw records would burn the context of
  the agent that is the main consumer of this surface. `--follow` blocks until
  new records appear, so "open the page and I'll check" is one round trip
  rather than two.
- **CL-14b** **One path form in the store: the shell's canonical form.** Every
  path-valued field (`page`, `target_file`, `entrypoint`) is written
  forward-slashed, via `_view_url_codec.canonical_fs_path`, at the single write
  point (`record()`). That is the form a path already has everywhere above the
  OS — what a `/view` URL decodes to and what the runtime sends as
  `X-Fused-Page` — but not what `os.path` returns on Windows, where
  `normpath`/`abspath`/`join` answer with backslashes. Enforced at the writer
  rather than per producer because one backslashed field is enough to make an
  exact-match filter miss a record forever; readers may then compare with `==`,
  which is what CL-10's three-role match assumes. `--page`/`--entrypoint` are
  canonicalized the same way on the way in. Normalization applies **only** to
  drive-letter paths: on POSIX a backslash is a legal filename character and
  must round-trip untouched.
- **CL-14a** **Preference reads are snapshot-cached for 1 s.** `enabled()` and
  the param-redaction mode are consulted per call, and each was opening and
  parsing `prefs.json` — measured ~2.8 ms per run, most of the feature's
  overhead (now ~1.0 ms, 2.4%). The prefs endpoint invalidates the snapshot on
  write, so a toggle still applies to the very next call and CT-5's no-restart
  rule holds. The invalidation carries a **generation counter**, because the
  `prefs.json` read deliberately happens outside the cache lock (holding it
  across file I/O would serialize every logged call behind one disk read): a read
  already in flight when the write lands must not store its now-superseded
  result, or the stale snapshot would be served for the whole TTL and the toggle
  would look ignored. `FUSED_RENDER_CALLS` remains the process-level override that beats
  the pref entirely.
- **CL-15** **Preferences** (§20) carries capture on/off (default **on** — a
  diagnostic you must enable before the thing you wanted to diagnose is
  worthless), the param redaction mode (`full` default / `keys` / `off`), and
  the retention window; `FUSED_RENDER_CALLS=0` is the process-level off switch
  and `FUSED_RENDER_CALLS_RETENTION_DAYS` overrides retention. Params are
  recorded by default as the same named trade-off the serve spec makes — they
  are the inputs the author's own code already received, usually the whole
  repro, and already visible in the URL — with `keys` one click away for a
  page that passes a secret. The payload also reports the store's location as
  `dir` **and whether it is there yet** as `dir_exists`: the writer creates the
  directory on its first append (CL-7), so between "capture on" and "a page
  actually called something" `dir` names a path that does not exist, and
  `Browse call logs` waits on the flag rather than sending the explorer to a
  path that fails to stat. Reported, never provisioned by the read — `GET
  /api/prefs` must not create storage, and the lazy create is also what keeps
  an empty store from appearing for someone who never records a call. (The
  `log.dir` beside it needs no such flag: logging creates its directory at
  startup, which is exactly why the two differ.) Capture and retention are both
  **overridable per process**, so the payload additionally reports
  `effective_enabled` / `effective_retention_days` — taken from
  `calls.enabled()` / `calls.retention_days()`, **the same resolvers the writer
  calls**, never a second copy of the precedence rule — plus
  `enabled_forced_by` / `retention_forced_by`, the raw env value when it is
  genuinely **in force** and null otherwise. In force is not the same as set:
  every set `FUSED_RENDER_CALLS` value decides something, but
  `FUSED_RENDER_CALLS_RETENTION_DAYS` is honoured only as an integer, so an
  empty or non-numeric one leaves the pref deciding and must report null — a
  presence check there disables the retention control and blames a variable
  that is setting nothing. Both flags therefore come from
  `calls.enabled_override()` / `calls.retention_days_override()`, the writer's
  own answer to "does this win", on the same ask-the-writer discipline as the
  `effective_*` pair. The
  controls bind to the stored prefs (a PUT round-trips, and the choice applies
  once the override is removed) while a muted line states what is actually in
  force and names the variable, exactly as the engine block's
  `selected`/`effective`/`forced_by` does — a page must never report capture as
  on while the process has it off. The param-redaction mode has no env override
  and so gets no such pair, rather than an always-null one implying otherwise.
- **CL-16** **Template readers are apps too.** Previewing a parquet really does
  make the `duckdb` template call Python, so those calls are real records
  attributed to the template's own `template.html`. Correct, but "my app's
  calls" then needs a deliberate filter: records carry `target_file` (for a
  template, the identity that matters) and `first_party` (the page lives under
  the packaged, staged-core, or user template dir). `query()` accepts
  `scope: mine|templates` on top of those fields — the capability the returning
  view (CL-11) needs, and what `fused-render calls` filters on today.
- **CL-17** Not a security or audit log. D3 stands — this is a local
  single-user diagnostic, not an attestation, and nothing may be built on it
  as if it were tamper-evident.
- **CL-18** **The store is partitioned per app** (D151; design §4.7). A
  record lives under `<slug>-<hash>/`, where the identity is the page's
  containing directory — an app being an `.html` plus its sibling `.py`s, the
  folder is the unit that lets both the page's and a data file's lookups land
  in one place. `partition_name()` is the ONE resolver of that name
  (`canonical_fs_path(normcase(realpath(dir)))` hashed, slug ≤24 chars for the
  human), duplicated standalone in the gate exactly as `_store_dir` is and
  pinned to the writer by a test. Records with no resolvable page go to
  `_unattributed/`. Reads stay MERGED — `store_files()` spans every partition
  and the day-merge (CL-12) is unchanged — so the bare-`call_id` cursor
  contract (CL-13, D140–D146) carries over verbatim, and `query()` deliberately
  does NOT narrow to a partition: "this file" is a three-role match (CL-10,
  CL-16), and a template's record about your file lives under the *template's*
  partition, so a narrowed walk silently loses designed behaviour (a test
  proved it). Only the gate narrows — its probe was always a bounded heuristic,
  so partition-first with the old global scan as fallback strictly improves
  it. The size cap trims the **largest** partition's oldest
  non-today file first (a chatty app must not evict a quiet one), the sweep
  reaps emptied partition dirs, and `index.json` at the root is an advisory
  partition→app-dir map written only on partition creation, rebuildable, never
  load-bearing. A renamed app folder is a NEW partition; the old history ages
  out unclaimed (owner-accepted over a rename chain). A `.py` borrowed by a
  page in another folder logs under the borrower, since a record lives where
  its `page` lives.

## 32. Markdown — Notes, Wikilinks & the Link Graph (D153)

Goal: `.md` stops being read-only. One template reads, writes, resolves
`[[wikilinks]]`, shows backlinks and draws a link graph, with read/write
behaviour copied from Obsidian rather than invented. Design + rationale:
`docs/markdown-graph.html`.

- **MD-1** **One surface, not a reading mode and a writing mode.** The **note
  view** (`markdown`, the `.md` default) is a **Live Preview editor and nothing
  else**: always editable, markup rendered in place, and no mode switcher.
  Obsidian splits Reading view from Editing view; this deliberately does not,
  because a mode switch here is a *template swap* rather than a sub-mode of one
  persistent editor — the caret and the undo history would not survive it, so
  the toggle would cost more than it does in Obsidian and buy less. A read-only
  file is the reading view: the same decorations over a locked buffer (MD-15),
  which unifies that path instead of branching it. The **local graph** is a
  panel inside the view (MD-19), and the **folder graph** (`graph`) is a
  directory mode over every note under the folder you are standing in. All
  three read `templates/markdown/graph.py` — one module owns parsing,
  resolution and assembly, so no two surfaces can disagree about what a link is
  or where it points. The folder mode calls it as `../markdown/graph.py`
  (`/api/run` resolves a relative `py` against the page's directory) rather
  than shipping a second copy.
- **MD-1a** **Read-only and editing are a MODE, and the mode is writability
  only — never appearance.** The same Live Preview decorations over the same
  document, with CM's two read-only facets on or off
  (`EditorView.editable.of(false)` + `EditorState.readOnly.of(true)`) — which are
  *exactly* the facets the unwritable-file path already used, so **a read-only
  file's locked buffer and read-only mode are one mechanism, not two**. There is
  no different typography, no restyled surface and emphatically no second render
  pipeline; that was removed deliberately (MD-1/D158) and does not come back to
  serve a mode. With `editable=false` there is no caret, so nothing reveals and
  the document reads as fully rendered — that *is* the reading view, obtained
  without a second pipeline. The reveal is additionally suppressed by one guard
  in `selectedLines`, because a browser text selection inside a non-editable view
  still reaches the state's selection and would un-render whatever was swiped
  over; the guard makes the mode deterministic rather than dependent on that.
  **A note opens READ-ONLY** (owner call 2026-07-31, reversing the earlier
  Obsidian-matching "editing is the default"): opening a file in an explorer is a
  read, and on the always-editable surface a stray keystroke on a note you opened
  to look at rewrites it — editing is one click away and, once asked for, stays
  in the URL. It also makes the view a better annotate stage (§17): the framed
  document takes no edits and reveals no markers under the reviewer's clicks.
  The preference lives in `fused.params` under
  `edit` (`"0"`/`"1"`), like `graph` and `depth` (MD-20), so it survives a refresh
  and travels in a shared URL; an absent param is read-only, and only an explicit
  `"1"` grants editing. `aria-pressed` on the corner button therefore tracks
  **editing** — the non-default state, so the accent marks the surface you can
  change — while the glyph names the current mode (padlock read-only, pencil
  editing). It is layered **on top of** the file's real
  writability and can never override it: for a genuinely unwritable file (MD-15)
  the toggle is *disabled* with a title saying why. Switching to read-only
  flushes pending edits first (`await save()`), for the same reason navigation
  does (MD-16). The control is a second 26px button in the same corner cluster as
  the sidebar toggle — not a toolbar row, which MD-2a still forbids — and
  switching rebuilds the view (`editable` is chosen at construction), which is
  invisible because `buildEditor` carries the caret and the scroll position
  across.
- **MD-2a** **No toolbar.** The shell's own breadcrumb already names the open
  file, and Obsidian shows no save state, no dirty indicator and no mode
  buttons — which left the bar holding nothing. What survived it went where it
  belongs: the read-only badge floats (the shared `ro-badge.js` idiom), a save
  *failure* floats as a pill and is invisible when there is nothing to say, and
  the reload-or-keep banner (MD-17) takes a row only while a conflict is
  unresolved. The only persistent chrome is a top-right cluster of two 26px
  buttons — the read-only/editing mode (MD-1a) and the sidebar toggle (MD-19).
  The sidebar's glyph is a **panel**, not a graph: the panel holds backlinks and
  the graph together, and its accessible name says so.
- **MD-2** **Registry.** `.md`/`.markdown` are `["markdown", "code",
  "claude", "versions", "reader"]` — `markdown` now supersedes `code` for
  notes, and `code` stays **unchanged** as the raw-source escape hatch. The chat
  mode sits where it sits on `.html` (after the editors, before the trailing
  meta-modes) and for the same reason: the chat template is file-type agnostic — it
  works off `_file` and its own sidecar — so binding it here is what
  gives a note a chat about itself. **D235** rebound this key from the plain chat
  to the split one (`claude_split`) because a note is a FILE; **D237** deleted the
  plain chat and renamed the survivor back to `claude`, so the spelling here is
  `claude` again while the *template* is the split one throughout (PT-14/PT-16).
  Either way the review tools that used to need annotate's **Send to Claude**
  handoff (§17, owner call 2026-07-31) are in that pane already — nothing to hand
  off to a separate mode, which is also why the handoff itself is now dead code
  (§17). Two editors for one extension
  with two save models is accepted rather than reconciled: `code` is the
  generic text editor and keeps AS-1 (250 ms autosave, Save button), `markdown`
  is the notes editor and keeps MD-16 (2 s idle, no button). A user who picks
  `code` for a `.md` asked for code behaviour. The universal `/` directory key
  gains `graph`:
  `["_listing", "app", "claude", "versions", "git", "graph",
  "zarr_aoi"]` (as of D185, which removed `preview`, D193, which added `git` —
  §33 — D235, which made `git` directory-only and put a folder chat beside it,
  and D237, which folded the two chat entries this key briefly carried into the
  single `claude`; the full order and its reasons are in §7.2's `/` row).
  `graph` ships a `condition.py`
  (CT-12), so `_listing` remains the immediate default and the graph joins the
  switcher only where the background gate allows it (PT-8).
- **MD-3** **What a link is.** Parsed from the source with **code elided**:
  `_mask_code` blanks fenced blocks, indented blocks, inline code spans and the
  YAML frontmatter to spaces of the same length, so offsets and line numbers
  still line up and a `[[Note]]` in a code sample is not an edge. Five forms
  count: `[[Note]]`, `[[Note|label]]`, `[[Note#Heading]]`, `![[embed]]`, and an
  ordinary relative markdown link `](./rel.md)` — a URL, a `mailto:` and a bare
  `#anchor` do not. `[[#Heading]]` is an anchor inside the same note, not an
  edge. **There is no tag concept** (D165): an inline `#tag` and a frontmatter
  `tags:` key are ordinary prose, parsed by nothing and drawn by nothing. The
  editing surface does **not** re-implement the masking rule: wikilinks are not
  in the vendored markdown grammar, so they are matched by regex and then
  checked **against the syntax tree** — a match inside
  `InlineCode`/`CodeText`/`FencedCode` is not a link. The tree is asked what a
  range *is*; no second block parser exists in JS. The guard must **not** list
  `Link`/`Image`: the grammar wraps the inner brackets of `[[Wiki]]` in a `Link` node and of
  `![[embed]]` in an `Image` node, so including them silently renders no
  wikilinks at all (found by executing it — see MD-18a).
- **MD-3a** **What a note is called: its file, always.** Every `title` in every
  payload — the note view's own, a link row's, a backlink row's, a graph node's
  label, a `[[` candidate's — is the filename with directory and extension
  stripped, and nothing else. `parse_note` emits **no** `title` at all, so a
  frontmatter `title:` and a leading `# H1` are both inert (the H1 survives as a
  heading). Previously either could rename a note, which meant the name on a
  graph node was not the name you would search for, rename, or type inside
  `[[…]]` — and an H1 that merely repeated the filename made the two agree often
  enough to hide the cases where they did not. Obsidian names a note by its file
  for the same reason. This also makes the name available for a note the scan
  never parsed, since deriving it needs only the path.
- **MD-4** **Resolution: the shortest path that is unambiguous.** Tried in
  order — relative to the linking note's own folder, then from the vault root,
  then as a path suffix, then as a bare basename — case-insensitively, with the
  `.md`/`.markdown` extension optional. **A tie is a ghost, not a guess:** two
  notes sharing a basename means the link did not carry enough path to say
  which, and silently picking one makes the graph assert an edge the author
  never wrote. Unresolved targets render dashed and clicking one **creates the
  note** (MD-16 makes that possible). An `![[embed]]` resolves through the same
  tiers over the folder's non-note files, so a picture in an `img/` subfolder is
  found from anywhere.
- **MD-4c** **A ghost is a promise, so only a target that could BE a note gets
  one.** A ghost says "click and this note appears", which makes it a lie for a
  target that can never name a note. Two are excluded, on the target **string**
  alone — no stat, no listing, because this runs once per link per note: a
  **directory** (`](../examples/)`, trailing slash), and a **file of another
  kind** (`](../scripts/run.py)` — an extension that is not `.md`/`.markdown`,
  where the suffix must begin with a letter so `[[Chapter 1.2]]` stays linkable).
  With no node there is no edge either. Creating one is also **bounded by the
  scan root**: a note written above the root is invisible to the graph that
  offered it, so the offer would be incoherent — the resolved path is checked
  against the root (with a `/` boundary) and refused with a message rather than
  written, on both graph surfaces. The ghost node carries the authored
  **`target`** next to its display `label`, and the create path reads the target;
  a write driven by whatever happens to be drawn on the canvas is fragile by
  construction. Both halves were a real bug: `[`../examples/`](../examples/)`
  produced a ghost that tried to create a file called literally `.md` one level
  *above* the vault root, because the page joined a `../` target onto the root
  while `resolve_link` joins it onto the linking note's folder. **The page's one
  path computation must follow `resolve_link`'s order** — own folder, then root —
  or it creates notes the graph will never look for, which is the same class of
  divergence MD-3 exists to prevent.
- **MD-4a** **A relative link is resolved for the shell.** The document is
  served at `/render?path=…`, so a browser resolves `](../CONTRIBUTING.md)`
  against the *server root* and misses the file. The link's widget therefore
  resolves the target against the note's own folder and sets `data-path` (plus
  `data-heading` for a `#Heading` suffix), which is what the one delegated click
  handler already listens for — so a relative link and a wikilink navigate by
  the same route and get the same pre-navigation flush of unsaved edits. A real
  `href` is set alongside it so hover and ⌘-click behave like ordinary links.
  Percent-escapes are decoded first, and a malformed escape falls back to the
  authored text rather than throwing the decoration pass away. Left untouched,
  matching MD-3's line for what is an edge: absolute paths, any scheme (opened
  in a new tab), and a bare `#anchor`. **No extension filter** — the shell opens
  any path with its own template, so a link to a `.png` or a subfolder is as
  navigable as one to a note.
- **MD-4b** **An arriving `?heading=` scrolls, it does not select.** There are no
  rendered headings to scan, so the document is the index: the matching ATX line
  is found by text and scrolled into view with `scrollIntoView`. The caret is
  deliberately **not** moved onto it — that would reveal the heading's own
  markup the instant you arrived.
- **MD-5** **Backlinks** are computed by resolving every other note's links and
  keeping the ones that land on this note — never from a stored reverse index,
  for the reason in MD-6. Each carries the linking note's title, relative path
  and the label/heading it used.
- **MD-6** **Resolve at assembly time, never at index time.** The index stores
  the **raw** authored target. Renaming `Foo.md` silently changes what every
  other note's `[[Foo]]` points at, so a cached resolved edge would be *wrong*
  rather than merely stale — the one mistake that makes incremental updates
  incorrect instead of slow. The page holds a raw-target → resolved-row map and
  contains no resolution rule of its own.
- **MD-7** **Where the index lives.** `<home_dir()>/graph/<sha256 of
  realpath(root)>.sqlite`, resolved against `home_dir()` on **each call** so
  `FUSED_RENDER_HOME` (and the per-branch nesting) work — the established
  `core_templates.CORE_TEMPLATES_DIR` pattern. Keyed on `realpath`, so a symlink
  and its target share one index. Home dir, never an in-folder sidecar: no repo
  pollution, nothing to gitignore. The absolute root is stored **inside** the
  db, so a moved folder or a hash collision is detected and the rows discarded
  rather than attributed to the wrong vault.
- **MD-8** **Three tiers.** (1) Per-file rows on disk — `rel`, `mtime_ns`,
  `size`, and the parse as JSON — invalidated when `(mtime_ns, size)` differs
  from disk or when `parser_version` moved, which invalidates everything at once
  so changing `parse_note` needs no migration. (2) Nodes and edges, assembled on
  every request and **never** cached (MD-6). (3) Nothing above that: the walk is
  cheap enough warm. Cold costs one walk plus N reads; warm costs a **stat-only**
  walk plus reads for changed files, typically zero. Deletions are free —
  assembly only uses rows the current walk found — and vanished rows are dropped
  so the file cannot grow without bound.
- **MD-8a** **The index is a cache, and every failure mode is treated as one.**
  No sqlite, a corrupt file, an unwritable home: each costs a full walk and
  nothing else, and none may become an error the user sees. An unusable db is
  discarded and rebuilt once, then given up on.
- **MD-9** **The write path keeps the graph live.** Each autosave re-parses
  exactly that note, updates one row, and re-assembles in memory — no rebuild.
  The open graph and the `[[` candidate cache are both invalidated by the same
  save.
- **MD-10** **Bounded by count, never by size, and honest about it.** A walk
  stops at 5000 notes, 5000 assets, or 20000 enumerated entries; any cap that
  fires is **reported** (`truncated`) and surfaced in the footer and the graph
  panel, never silently applied. The walk is deterministic — sorted directories
  and files — so a cap that fires drops the same tail every time. There is
  **no per-note size cap**: a 256 KB ceiling (and its `skipped_large` report)
  was removed, because a skipped file landed in neither the note index nor the
  asset index, so every `[[…]]` aimed at it resolved to nothing and drew a
  **ghost** — asserting the note did not exist when it plainly did. A long
  decision log is precisely a note people want backlinks into. The read cost is
  bounded by MD-8's cache instead: a big note is read on the open after it
  changes and stat-only on every open after that. The editor's own 2 MB
  inline-edit ceiling is a separate guard and always was.
- **MD-11** **Mounts are out of scope for the graph, structurally.** The
  recursive walk is exactly the shape that wedges an rclone-NFS mount (a kernel
  listing on a flat million-key prefix), so it simply never happens there.
  **Two independent halves:** the folder mode's gate returns `False` for any
  mount-backed path, so the mode is never offered; and `graph.py` refuses a
  mount-backed root **before it walks or even creates the index dir**, returning
  a clear `mount_unsupported` result — never a partial walk. Both use the app's
  own `shell.mounts.is_mount_backed`, not a second copy of the rule, and both
  treat a failed import as *refuse* rather than *guess*. The gate is the UX;
  the module is the guarantee. Reading and writing a single `.md` on a mount
  stays fully supported — that is one bounded read and one bounded write, which
  is what every template already does. A relative markdown link still navigates
  (the page resolves it against the note's own folder, MD-4a, which needs no
  scan).
- **MD-11a** **Unknown is not the same as missing.** On the no-scan path —
  a mount, a refused scan, a failed one — the page holds no resolution map at
  all, and that state is **three-valued, not two**: "scanned, and this target did
  not resolve" is a ghost (dashed, click-to-create, MD-4); "not scanned, so
  resolution is unknown" is **inert** — no ghost styling, no `data-create`, no
  create-on-click, and a title saying targets are not resolved here. A ghost
  there asserts the target does not exist when nothing ever looked, and offers to
  create a note that may be sitting next to this one. The unknown flag is part of
  the widget's reuse key, so a scan landing later replaces the inert links
  (MD-9). The **sidebar** says the same thing rather than hiding an empty
  backlinks list — an empty list reads as "nothing links here", which is also an
  answer nobody computed — and it says it in `graph.py`'s own words, so a mount
  refusal, a note outside its root and a crashed scan each read as themselves.
  The page still resolves **nothing** itself (MD-6): the only two answers it can
  render are graph.py's map and "we do not know".
- **MD-12** **Scope is the vault the note sits in, found by climbing to a
  marker.** The folder mode's scope is still the folder you are standing in,
  which matches the explorer and needs no setup. The **note view** does not use
  the note's own directory: that was tried and is too narrow to be useful — a
  note in `v/docs/` linking `../spec/overview.md` got a ghost for every link
  leaving its folder and an empty backlinks panel, because nothing outside
  `v/docs` was ever scanned. The default root is therefore the **nearest
  ancestor carrying a vault-root marker** — `.obsidian/` or `.git` (a directory
  in a clone, a *file* in a worktree, so both shapes are probed) — and the
  note's own directory when none is found. Never `$HOME`,
  never `/`. An explicit `root` param still wins; the ascent only supplies the
  default. **The ascent must not enumerate**: a fixed set of `isdir`/`isfile`
  probes per level and no `listdir`/`scandir`/`walk`/`glob` anywhere, the same
  CT-12 discipline as the folder gate and for the same reason — it runs on every
  note opened, and it is deciding the scope of a walk. It is bounded to 8 levels
  and stops at the filesystem root, and it **never enters a mount-backed path**
  (MD-11): a local note living under a mounted folder must be scanned in the
  folder it is in, not answered `mount_unsupported`. A failed mount import means
  "cannot tell", which reads as "do not climb". A wider root makes MD-10's
  `truncated` cap matter more, not less, so the "only the first N notes were
  scanned" notice stays surfaced in the sidebar. Dotdirs and the usual vendored
  trees are still skipped by name inside the walk. A third marker,
  `.fused-graph.json`, was specified here and is **gone** (D165): nothing ever
  read it, so it was a marker only a fused user who had read this SPEC could
  have placed. *Not built:* per-vault tuning of any kind (root override,
  include/exclude globs, colour groups, default depth) and gitignore-awareness
  of the walk — both additive.
- **MD-13** **Vendoring.** One rebuild of `scripts/vendor-codemirror/` adds
  `@codemirror/lang-markdown` (GFM base), `@codemirror/autocomplete` and
  `@codemirror/commands`, and re-exports `WidgetType`/`ViewPlugin`/
  `MatchDecorator`/`keymap`, `EditorSelection`/`RangeSetBuilder`/`Prec`,
  `syntaxTree`, `autocompletion`/`acceptCompletion`, `indentMore`/`indentLess` and
  `markdown`/`markdownLanguage`/`markdownKeymap`. Anything not re-exported is
  tree-shaken, so `entry.js` is the whole gate on what the template can reach.
- **MD-14** **Link authoring: every place a target can be typed completes.** All
  of it comes from one `candidates` action off the **same scan the graph reads**,
  so the popups are free once the index exists and can never offer a note the
  graph disagrees about; cached ~5 s so a fast typist does not spawn a run per
  keystroke. Six contexts, registered as **separate sources** in one
  `autocompletion({ override })` (CM runs them all and merges, so each is a
  single trigger with a single answer instead of one function branching through a
  chain of regexes):
  * `[[` — notes. What it inserts is the **shortest form that `resolve_link`
    itself resolves back to that note** (each candidate form is run through the
    resolver to pick it) — Obsidian's "shortest path when possible", made correct
    by construction rather than by a parallel rule, and pinned by a property
    test.
  * `![[` — the same notes **plus every asset**, because `![[image.png]]` is the
    common Obsidian embed and an embed resolves through the asset index (MD-4).
    An asset carries **the same run-it-through-the-resolver guarantee a note
    does** (D191), which takes three things the earlier root-relative-and-
    unshortened form did not have. **The index has to be the embed resolver's**:
    `_resolved_links` sends a non-note embed target through notes *plus* assets
    and everything else through notes alone, so a row carries **two** validated
    forms — `link` for `[[`, `embed` for `![[` — and one field could only ever be
    right for one of the two popups. **The note it will be inserted into has to
    be known**: tier 1 of resolution is the linking note's own folder, so
    `![[img/a.png]]` written in `docs/` binds to `docs/img/a.png` the moment that
    exists, and a form validated from the root is not validated at all for a note
    in a subfolder — the `candidates` action therefore takes the open `file` and
    validates every form from it (which fixes the same hole for `[[` notes), with
    a **`../` form** as the last one tried, for the case where the note's own
    folder shadows every suffix of the target. **And "no form" has to be
    sayable**: on a stem-key collision (an asset `img/a.png` beside a note
    `img/a.png.md`) nothing resolves back to either, so `_link_form` returns
    `None`, the payload ships `null`, and the popup **drops that row** instead of
    inserting a path the resolver reads as a ghost or as another file. The page
    still invents no form of its own — a shortening rule reimplemented in JS is
    exactly the divergence MD-3 and this rule exist to prevent. The per-candidate
    resolver work is bounded by memoising resolution's third tier
    (`_suffix_index`): ~1.9s of `endswith` at both caps became a ~40ms payload,
    with an equivalence test pinning the memo to the scan's own answers.
  * `[[#` and `[[note#` — headings, of this note or of the named one.
  * `](` — **path completion**, the same list of notes and assets, each written
    **relative to the open note's own folder** (a sibling as `img.png`, a child
    as `sub/img.png`, `../` only where the target really is above the note).
    Relative-to-own-folder is `resolve_link`'s first tier, so that form resolves
    back to that exact file with no basename ambiguity to lose to, and it is also
    a real relative path — which is what the shell navigates by (MD-4a) even
    where nothing was scanned. The target is **percent-encoded on insert**
    (spaces, and parentheses, which close the target early): `[x](my file.png)`
    is not a link to the GFM parser at all, so the readable path is *displayed*
    (`displayLabel`) while the encoded one lands in the document, where MD-4a's
    widget decodes it again. This is pure string work — no filesystem call, and
    no path rebuilt from segments, so the Windows drive form `C:/…` never gains
    the leading slash `resolvePath` warns about.
  * `![](` — the same, filtered to **image extensions**, because only an image
    renders there.
  * `](#` — this note's own headings, mirroring `[[#`, encoded the same way.

  Each source sets `validFor`, so keystrokes filter the list locally instead of
  re-running the source. **Two structural caveats, stated rather than absorbed:**
  on a **mount-backed root** the `candidates` action refuses by design (MD-11),
  so path completion is *structurally absent* there — and a **truncated walk**
  (MD-10) yields a partial candidate list, so a target past the cap is simply not
  offered. Neither may look like an empty vault: a failed or absent scan shows
  **one non-selectable informational row** carrying graph.py's own reason, so a
  mount refusal, a note outside its root and a crashed scan each read as
  themselves — the popup's version of MD-11a's rule that unknown is not missing.
  The failure is cached exactly as hard as a success, so a root that can never
  answer costs one run per TTL rather than one per keystroke.

  **What a row shows.** The **label is the readable form and the inserted form is
  the encoded one** — `displayLabel` for the eye, `label` for the document — which
  is a correctness rule, not a nicety: an un-encoded space is not a link to the
  GFM parser at all. Beside it, a **dimmed right-hand `detail` column** carries
  only what the label does not already say: a note's title, `embed` for an asset,
  `H2` for a heading. Nothing repeats the label (the path source used to set the
  root-relative path as its detail, which for a target under the note's own
  folder is the label verbatim). A heading row says its nesting by **indentation
  plus that level marker**, matching the sidebar outline (MD-19b) rather than
  printing `### ` into the label. **No icon column**: CM's glyph for these `type`
  values is the literal `abc`, and the row already names its kind (D192). The
  whole popup is styled through **the editor's own `theme` extension**, so it
  survives the appearance flip's `StateEffect.reconfigure`, in this view's tokens
  and at the sidebar's density — with the selected row marked by weight and an
  accent edge as well as fill. The typed substring is emphasised, since the list
  is every note plus every asset: that takes each result's **`getMatch`**, because
  CM matches the label and will not guess where those characters sit in a
  `displayLabel` — it hands such a row an empty match, so without the mapping
  every row that displays its own form (which is all of them here) drew no
  emphasis at all.
- **MD-15** **Read-only comes from the shell's persisted flag**, read off
  `stat.writable` (`server._writable`, which consults `mounts.mount_read_only`),
  never `os.access`: on an rclone mount with `CacheMode=full` a doomed write
  succeeds locally and only 403s later on async upload, so the editor must open
  **disabled** rather than apologise afterwards. Both CM facets
  (`editable=false` + `readOnly=true`), the shared read-only badge, and a
  `readonly` rejection from the server locks the surface too.
- **MD-16** **Saving is Obsidian's model.** No save button, no dirty indicator,
  no prompt — the absence of save ceremony *is* the behaviour being copied. A
  2 s idle timer, plus a flush on blur, on tab switch, on `pagehide`, and before
  any navigation this view initiates (through `__fusedFlushEdits`, so a mode
  switch cannot silently drop edits). `⌘S` only forces the flush **early**; it
  is not *the* save. Saves are single-flight and re-check the buffer after the
  write, so edits landing mid-write stay dirty instead of being masked.
  `fused.autoReload(false)`: this view owns its own reload rule, and its own
  autosave moves the mtime on every write.
- **MD-17** **One deliberate deviation: conflicts.** Obsidian assumes it is the
  only writer in the vault; fused-render sits on shared and mount-backed paths
  where that does not hold. So: **clean buffer → silent reload**, exactly as
  Obsidian. **Dirty buffer *and* the mtime moved → a reload-or-keep banner**,
  never last-write-wins; autosave is suspended while the banner is up, and
  "keep my version" is the one write that goes without `expectedMtime`, because
  the user has been shown the conflict and chosen. External changes are noticed
  **on focus and on becoming visible**, plus at the next save via the
  `expectedMtime` lock — deliberately **not** by a stat ticker, because a poll
  is the traffic that killed a mount once already (the `fs/events` stat-storm
  incident) and this template stays mount-agnostic. The cost is that an
  external change is seen a moment later rather than instantly.
- **MD-18** **Editing behaviours.** Smart lists, list renumbering and blockquote
  continuation are `markdownKeymap` — the same code Obsidian's own editor runs —
  not a hand-rolled copy; auto-pairing comes from `basicSetup`'s
  `closeBrackets`. On top: `Tab`/`⇧Tab` indent and outdent list items, `⌘B`/`⌘I`/
  `⌘⇧X` (strikethrough) / `⌘⇧E` (inline code) / `⌘K` as **toggles**, `⌘⏎` cycling
  a line through task states, pasting a URL
  over a selection making a link of it, and the caret position remembered per
  file. The four wrapping toggles are one marker-agnostic function, and the
  markers stop there: `==` highlight is **deliberately absent**, because the
  vendored grammar has no rule for it and Live Preview would leave the `==` bare
  on the page (D189). Obsidian ships no default hotkey for strikethrough or
  inline code, so `⌘⇧X`/`⌘⇧E` match nothing and were chosen for being free of
  `markdownKeymap`, `basicSetup`'s default/search/close-brackets keymaps and the
  completion keymap. `⌘E` specifically is left unbound: the read-only↔editing
  mode (MD-1a) is the corner button only, by owner call.
  Two behaviours are Obsidian's rather than the naive form. A toggle with **no
  selection wraps the word under the caret** — `state.wordAt`, so there is no
  second definition of a word here — and toggles it off again from the same bare
  caret; only where there is no word (whitespace, an empty line) does it fall
  back to an empty pair of markers with the caret between them. And `⌘K` with the
  caret **inside an existing `[label](target)` selects the target**, so it edits
  that link instead of nesting a second one; the enclosing node comes from
  `syntaxTree` and must *also* parse as a plain inline link, because the grammar
  wraps a `[[wikilink]]`'s brackets in `Link`/`Image` nodes too (MD-18a, D189).
  `Tab` **accepts an open completion** before it indents (MD-14's popups), since
  CM's own `completionKeymap` binds only `⏎` to `acceptCompletion`; with no popup
  open it is `indentMore` as before. Every one of these commands builds its own
  dispatch, and neither read-only facet filters a dispatch, so each checks
  `state.readOnly` and swallows the key rather than relying on `editable=false`
  keeping the key away from the view (MD-1a/MD-15).
  A **rendered checkbox** is clickable and writes back, disabled on a read-only
  file; its position comes from `posAtDOM` at click time rather than from a
  count of markers in the source, so an edit between render and click cannot
  tick the wrong box.
- **MD-18a** **Live Preview.** One `Decoration` set over the CM6 document —
  Obsidian's own mechanism, not an approximation of it. Markup is replaced by a
  widget or hidden everywhere except the lines the selection touches, where the
  raw source returns so it can be edited; reveal is **per line**, matching
  Obsidian, so putting the caret on a line un-renders that whole line rather
  than one node. Covered: headings, bold/italic/strikethrough/inline code,
  fenced blocks (markers kept and the embedded language highlighted, as in
  Obsidian), blockquotes, list bullets, horizontal rules, pipe tables,
  `[label](target)` links, `![alt](src)` images, and wikilinks/embeds/ghosts.
  **What a range is comes from `syntaxTree`**, so this template holds no block
  parser (MD-3).
  Two behaviours are deliberately unlike the rest: a **checkbox stays rendered**
  under the caret, because it is a control and not markup you edit by hand; and
  widgets whose *content* needs editing (image, table) are **click-to-edit**
  — a click lands the caret inside them and the source appears — whereas links
  are opaque so a click navigates.
  **Frontmatter is a special case with a real trap:** the vendored grammar has
  no frontmatter rule, and what it produces instead is actively wrong —
  `---\ntitle: x\n---` parses as a `HorizontalRule` followed by a
  `SetextHeading2`, so YAML rendered as a horizontal rule plus a large heading.
  The block is therefore found by a line scan, dimmed, and **every tree
  decoration inside it suppressed**. Dimmed rather than hidden because a
  properties table is separate work (MD-18b) and a silently invisible block is
  worse than a plain one.
  Because CM *throws* on a decoration set whose replacements overlap, every
  replaced range is recorded as it is made and the regex passes skip anything
  landing inside one; a `block: true` replacement is avoided entirely, since it
  additionally demands exact line boundaries.
  This is the one part of the template whose correctness is invisible in a diff
  — it depends entirely on what the grammar calls each range, and the grammar
  was wrong twice in ways no source assertion would have caught (the frontmatter
  case above, and `Link`/`Image` wrapping wikilink brackets, MD-3). It is
  therefore covered by **execution**: `scripts/vendor-codemirror/live-preview-probe.mjs`
  runs the real builder against the real grammar headlessly and
  `tests/test_markdown_live_preview.py` asserts over the resulting decoration
  set. Those tests skip where the gitignored vendor `node_modules` is absent.
- **MD-18b** *Not built:* a frontmatter properties table; inline note embeds (an
  embedded note renders as a link, which avoids a second parse and a
  recursion); inline markup **inside a table cell**, which a decoration cannot
  reach because it cannot span into a widget's DOM (clicking the table shows the
  source, which is where a cell is edited). Paste-or-drop of an image *was*
  listed here as blocked on a binary write; it is now built (MD-23).
- **MD-19a** **Backlinks and the graph are one right sidebar**, as they are in
  Obsidian, behind the single 26px toggle (MD-2a) — not a footer under the
  document, which a full-height editor has no room for. Backlinks scroll in the
  upper section; the graph canvas and its depth control sit below.
  One toggle opens both: they answer the same question about the open note. The
  panel is **resizable** by dragging a thin handle on its left edge (15rem to
  45rem, arrow keys on the focused handle too), and the width is persisted in
  **`localStorage`, deliberately not in params**: params are the state a shared
  URL should reproduce (MD-20), and how wide someone dragged their panel is
  window furniture that a link must not carry. Each resize `nudge()`s the canvas,
  which is a fixed-size bitmap and does not otherwise learn that its box moved.
- **MD-19b** **The outline is the sidebar's TOP section, and it reads the live
  document** (D190). The open note's headings get a nested, click-to-scroll list —
  the navigation aid a long note needs, which the panel had headings for
  everywhere (`[[#`, `](#`, `?heading=`) except on screen. It is a **section of
  the one right sidebar**, not a panel and not a second toggle: MD-19a allows
  exactly one right sidebar behind one 26px toggle and MD-2a forbids the toolbar
  row a second control would want. It sits **above** backlinks because the
  ordering is by subject rather than by size — the outline is about the note in
  front of you, backlinks and the graph are about the rest of the vault, so the
  section describing the open document is nearest it, as in Obsidian. Being first
  it is also the section that clears the floating corner cluster. Sized like the
  backlinks list (content-sized, scrolling, capped) so neither list can starve
  the canvas.
  **It reads `view.state.doc`, never the payload.** `notes.headings` re-parses
  only on save (MD-9), so a payload-fed outline would lag every heading typed by
  up to one autosave interval, and a stale outline sends you to the wrong place —
  worse than none. Reading the document is the same move MD-4b already makes.
  **No timer**: the editor's existing `docChanged` listener is the only trigger,
  and this template stays poll-free (MD-17). The heading scan mirrors graph.py's
  `_mask_code` — frontmatter and fenced code masked, ATX only — so a
  `# not a heading` inside a fenced block is as absent here as it is from every
  other heading surface. Mirroring it includes the case that bites while typing:
  an **unclosed** `---` is not frontmatter (`_frontmatter_span` returns no span,
  and MD-18a's decoration scan finds the end before it dims anything), so the
  closer is found before any line is skipped. A standing "in frontmatter" flag
  emptied the whole outline from the keystroke that opened the block to the one
  that closed it, while the other two surfaces kept those headings — caught in
  review. Indentation is **nesting depth over the levels present**, and the
  `[[#` popup calls the same function rather than a matching copy (MD-14), since
  a note that starts at `##` or skips a level is where two copies diverge. Not the syntax tree, tempting though it is: it is parsed
  only as far as CM has got, so a long note would silently lose its tail
  headings. Rows scroll by **line**, not by heading text (they were built from
  that line; matching by text hands the second `## Notes` to the first one), and
  MD-4b's caret rule lives in the one `scrollToLine` both paths call. Indent is by
  **nesting depth over the levels present**, so a note that starts at `##` is not
  an indented note. **No state, therefore no param** (MD-20 carries what a shared
  URL must reproduce; a section that is always drawn with its panel has nothing
  to carry), and an unchanged heading list is not redrawn — which is what keeps
  the list's own scroll position while you type inside a heading. Empty says
  so, in the backlinks list's voice. Fully functional read-only (MD-1a): an
  outline is a reading affordance first, its rows are buttons outside the editor
  on the one delegated click handler, and a scroll is not an edit.
- **MD-19** **Rendering the graph.** One implementation, in
  `templates/shared/graph-canvas.js`, served from the `/template-shared/` mount
  and used by both graph surfaces — extracted the moment the second one
  appeared, so the layout and the interaction rules cannot drift into two
  versions. Canvas 2D, no library vendored. **Positions are assigned, not
  simulated** (D164): a spring sim was tried and its two forces — label
  spacing and column alignment — pull opposite ways in the hub-shaped graphs
  this panel actually shows, so the alignment side won and printed labels over
  each other. Nodes **glide** to their assignment (eased, snapped at 0.5px),
  which keeps re-sends calm; a NEW node is born at its spot rather than
  animated in from nowhere. Behaviours copied from Obsidian: the layout is
  **fitted to the canvas**, node radius scales with degree, labels fade past a
  zoom threshold, hover lights the neighbourhood, drag pins a node, ghost
  nodes are dim with dashed edges, a click opens the note (a ghost click
  creates it). Colours are read from the CSS custom properties **at draw
  time**, because `var()` cannot resolve inside a canvas `fillStyle`, and a
  `data-theme` change redraws (§30). **Spacing is set for the label, not the
  node** — a label is drawn above its node, so node radius is the wrong unit.
  Each label owns a **slot as wide as it measures** (plus padding, with a
  floor), so two settled labels cannot be assigned overlapping ground; and
  which labels **print** is still decided per frame — focus first, then the
  hovered neighbourhood, then bigger nodes, and a label whose box would
  intersect one already kept is dropped for the frame (hover summons it back),
  which covers what the layout did not place: a node dragged onto a
  neighbour, two labels passing mid-glide. **Edges are vertical S-curves**,
  not straight lines — near-vertical is the honest shape of a cross-band
  link, and the curve keeps two edges into one hub separable where straight
  lines fused into a rope; a same-band edge bows downward instead. The fit
  yields permanently as soon as the user pans, zooms or drags, and is not
  reset by new data, because every autosave re-sends the graph (MD-9).
- **MD-19a** **The layout is layered by folder** (D163, D164). This is the one
  place the graph deliberately stops copying Obsidian: a free layout spends
  both axes on nothing in particular, and the feedback on it was that the
  picture said less than the backlinks list. So the vertical axis carries the
  tree. **One band per distinct folder** (not per depth number — sibling
  folders are different places), ordered by depth then name, with the
  folderless ghost nodes in a trailing `unresolved` band rather than lumped
  into the root's. Band names are drawn in **screen space** at the left edge, so
  the legend stays readable and on-screen at any zoom or pan; **names (and the
  left gutter they reserve) appear only where there is more than one band** —
  with a single band the vertical axis distinguishes nothing, so the legend is
  one folder name repeated down the edge of a panel it labels in whole, which
  the breadcrumb already does; dropping it hands the gutter's width back to the
  lane layout (owner call 2026-07-31, reversing the earlier "draw it even for one
  band"); alternate bands carry a whisper of foreground fill,
  because a fill says "this strip is one folder" everywhere the strip is where
  a lone separator hairline read as a stray edge. `y` belongs to the layout,
  except for a node the user dragged, which keeps the position they chose.
  **A band is a block, not a line.** Its nodes wrap across as many lanes as
  their slots need to fit the surface, and the band grows to hold them: a wide
  window gives a folder one airy line, a narrow panel the same folder as a
  compact labelled block. One row per folder was tried first and failed twice —
  labels ran together (`READMEauthoring`), and nine nodes in a row is ~1080px,
  which in a 320px panel fits only below the zoom that hides labels.
  **Within a band, order is by barycenter**: three alternating sweeps sort each
  band's nodes toward the mean position of their neighbours (a tie between a
  linked and an unlinked node resolves toward the linked one, whose position
  carries information), so a chain of links descending the tree reads as a
  column and crossings go away by ordering rather than by force. The whole
  layout then shifts to put the focus note on the canvas mid-line.
  **Edge weight falls away with density** — a near-complete folder carried ~30
  edges among 9 notes and drew as a hairball with more ink than the nodes — so
  the resting field washes out in proportion to edges-per-node; the focus
  note's own edges hold a step above the field, and hover is what makes any
  individual link fully legible again.
- **MD-20** **Graph state is params.** Panel open and depth live in
  `fused.params`, so a graph view is refresh-proof and **URL-shareable** — which
  Obsidian's is not. Nodes are notes and per-**name** ghosts (five notes linking
  `[[Roadmap]]` share the node they are all asking for) and nothing else; an
  embedded picture is deliberately **not** a node, or a vault of screenshots
  would drown the graph. A focused graph BFSes out `depth` hops
  following edges in **both** directions, because an inbound link is as much a
  neighbour as an outbound one. `depth` also carries an **`all`** option, sent
  as the sentinel **`-1`**: a negative depth skips the neighbourhood filter
  entirely, so the panel shows the whole vault with the focus note still
  reported (and still drawn apart). The sentinel is negative rather than `0`
  because `0` already means something on the focused path — the focus node
  alone — and the folder graph relies on `depth: "0"` with no focus meaning
  "nothing to filter by".
- **MD-21** **The gate never enumerates** (CT-12). It answers two questions in
  order: mount-backed → `False` always (MD-11), then exactly two
  `os.path.isfile` probes for the **vault marker `index.md`** (and `Index.md`,
  because only a case-insensitive filesystem answers one for the other). A
  README is deliberately **not** a marker: `README.md` is in essentially every
  code repository, and a link graph over a repository is meaningless, so probing
  it offered the mode on every checkout on the disk. No `listdir`, `scandir`,
  `glob` or recursion — doubly binding here, because this gate runs on every
  directory the user opens and the mode it gates is itself a walk. The tests
  make enumeration **fatal**, so a listing added later fails rather than ships.
  The cost of the marker being wrong is one-directional and small, and it is
  discoverability rather than capability: a vault with no `index.md` is not
  *offered* the mode (the local panel in the note view still works, and
  `_mode=graph` still reaches the folder mode), whereas the content sniff that
  would avoid that needs the listing this rule forbids. Fails closed on any
  error.
- **MD-22** **Out of the template's reach, by design.** Rename-updates-inbound-
  links needs a hook on the explorer's rename plus a multi-file write, and
  vault-wide search / a quick-switcher are shell surfaces. Both belong to the
  shell, later, elsewhere.
- **MD-23** **Pasting or dropping an image or a video writes it beside the note
  and links it in** (D199). Two entry points, one pipeline. "Copy image" in a
  browser puts the image *bytes* on the clipboard rather than a file reference,
  so ⌘V is the ordinary `paste` event's `clipboardData.files`; a drag from
  Finder is the same `FileList` on `dataTransfer.files`. Both filter to
  `image/*` and `video/*` — a plain-text paste and a drag carrying no media
  fall through untouched, so CodeMirror's own text drag-and-drop still moves
  text — and both then run the *same* helper: ensure `assets/` next to the note
  (`fused.mkdir`, whose `exists` 409 from the second paste onwards is the
  expected case), upload each blob, insert `![](assets/<name>)`. The insert is
  an ordinary dispatch, so **undo removes it in one step**.
  - **Names are timestamps**, `pasted-YYYYMMDD-HHMMSS.<ext>` in local time.
    A timestamp is not by itself unique — two pastes inside one second, and
    every file of a multi-file gesture, produce the same name, and the upload
    replaces unconditionally — so the name is **probed with `fused.stat` and
    bumped** (`-2`, `-3`, bounded) until it is free. Losing the first file to a
    silent overwrite is not an acceptable cost for a tidy name. The extension
    comes from the blob's MIME type, since a pasted screenshot has no filename
    at all; an unmapped vendor type has its `x-` prefix stripped, and every
    `video/*` mapping is pinned to produce an extension the video widget
    recognises (`video/x-m4v` once produced `.x-m4v`, which rendered as a
    broken image).
  - **A drop lands at the POINTER**, `posAtCoords` at the drop coordinates,
    falling back to the caret where that answers null (a drop past the last
    line). A paste lands at the caret. That difference is the only one, which
    is why the shared helper takes the position as a parameter rather than
    reading the selection.
  - `dragover` **must** `preventDefault` for a drag carrying files, or the
    browser never fires `drop` and instead navigates the webview to the dropped
    file — indistinguishable from the editor vanishing. Gated on
    `dataTransfer.types` containing `Files`, which is the only thing askable
    that early (`dataTransfer.files` is empty during a dragover).
  - **`drop` therefore prevents the default for EVERY file drag**, before it
    filters for media and before it checks read-only. CodeMirror only calls
    `preventDefault` for a handler that returns **true**, so returning false
    for a dropped PDF would hand the event back to the browser — which
    navigates the webview to the file, losing every edit since the last
    autosave (`pagehide`'s save is best-effort and may not land). `dragover`
    committed to owning file drags; `drop` honours that for all of them, and a
    drop it cannot use **says so** ("Only images and video can be added to a
    note") rather than doing nothing. Only a genuine *text* drag falls through
    to CodeMirror — the test is whether the drag carried files, never whether
    media matched.
  - **A read-only note never gains media.** A *paste* falls through untouched,
    the same posture `whenWritable` takes for every writing key (MD-1a/MD-15).
    A *drop* still prevents the default — the editor must not navigate away —
    and reports that the note is read-only.
  - **The upload is awaited before the link is inserted**, so a link can never
    point at a file that failed to write; a failure surfaces through the same
    status element a failed save uses, never silently. The cost is that a very
    large video briefly stalls the editor — accepted for a first cut.
  - **Video renders as a player.** Markdown has no video syntax, so a clip is
    written as `![](…)` too (what Obsidian writes, and it degrades to a
    recognisable broken image elsewhere) and the live-preview widget picks
    `<video controls muted preload="metadata">` over `<img>` off the
    extension. Removing the link does **not** delete the file: unreferenced-media
    collection is its own feature, and Obsidian behaves the same way.
  - The binary write itself is **`POST /api/fs/upload`** (multipart) behind
    `fused.uploadFile(path, blob)` — `/api/fs/write` takes a string only. It
    reuses `_fs_write`'s guard sequence exactly (X-Fused, the mount-backed /
    `mount_read_only` branch before any kernel probe, `_writable`, the `readonly`
    403 of RO-2) and, like it, never creates intermediate directories. It is
    logged like `/api/fs/write` — path and byte count, never the payload — so
    pasted media is not the one mutation that leaves no trace (CL-*).
- **MD-24** **A bare URL is a link in Live Preview, and the document is not
  rewritten to make it one** (D200). The vendored GFM grammar already parses
  `https://example.com` as a `URL` node and `<https://example.com>` as an
  `Autolink`; the decoration builder simply named neither, so a typed or pasted
  URL rendered as unclickable grey prose beside an explicit `[lbl](url)` that
  rendered as a link.
  - **A bare URL gets a MARK, not a replacement widget.** The mark carries
    `tagName: "a"` plus `href`/`target`/`rel`, so the range *becomes* an anchor
    with the existing `lp-link` styling — bare and explicit links look and
    behave alike. A widget would replace the URL's characters with an element
    rendering the identical characters (its display text already equals its
    target), buying nothing while taking away the caret's ability to sit inside
    the URL and edit it.
  - **An angle autolink hides its brackets** under the ordinary reveal rule:
    `<` and `>` are markup, so they are replaced away and come back dimmed as
    `lp-mark` on the caret's line, exactly as `**` and `# ` do. A bare URL
    hides nothing and therefore has nothing to reveal — it stays marked
    wherever the caret is, rather than flickering when a line is entered.
  - **Nothing is written.** No paste or edit converts a URL into `[url](url)`,
    so the note on disk still says what its author typed — which is also what
    makes this repair every URL in notes written before the rule existed. The
    ⌘K and paste-over-a-selection behaviours (`[selected](url)`) are unchanged.
  - **A URL inside a fence or a code span stays plain text**, the same line MD-3
    draws for what counts as an edge. The grammar does not emit a `URL` node
    inside code at all, so the guard is the narrow `CODE_NODES` set the wikilink
    pass already uses rather than a new "is this code?" rule — an over-broad one
    of those once silently stopped every wikilink from rendering.
  - **A `[lbl](target)` link's own `URL` child is left alone**, since it belongs
    to a node the link branch already replaced whole (or deliberately left as
    source, as for a titled or reference link).
  - GFM autolinks three shapes, and two are not URLs yet: `www.x.com` gets an
    `https://` scheme and `me@x.com` a `mailto:` one, or the href would resolve
    against `/render?path=…` (MD-4a's trap).

- **MD-25** **Editing an ordered list renumbers it — every way of editing it,
  not just Enter** (D201). `markdownKeymap`'s `insertNewlineContinueMarkup`
  already renumbered on Enter, and nothing else did: Backspace
  (`deleteMarkupBackward`), selecting a row and deleting it, ⌘X, ⌘⇧K, and a
  paste in the middle all left the numbers below the edit stale. The grammar
  package's `renumberList` is internal and unexported, so this is our own pass.
  - **It hangs off a transaction filter, not more key bindings.** One place sees
    every edit whatever produced it, which is the only way the list of gestures
    above stops needing to be enumerated.
  - **Only the list the edit landed in** — and the edit has to have landed *on*
    it. A blank line always extends a list's region (it separates the items of a
    loose list) but anchors one only when list items sit on **both** sides of it,
    because a blank line is also what separates a list from the prose beside it.
    Anchoring on every blank means typing in a paragraph next to a list
    renumbers that list, putting changes into an undo step the user never made;
    anchoring on none means clearing an item's text — which leaves a blank line
    inside the list — stops the items below it following.
  - **The first item's number anchors the sequence.** A list written `3.` `4.`
    `5.` stays that way, and deleting the head of `1.` `2.` `3.` leaves `2.`
    `3.` — the two cases are identical text, and anchoring is what
    `markdownKeymap`'s own Enter renumbering does, so Enter and Backspace agree.
  - **Undo and redo are excluded**, along with every programmatic dispatch (a
    reload, a read-only rebuild): the filter opts in to `input`, `delete` and
    `move` user events. Undoing back to a deliberately odd numbering must not be
    corrected straight back.
  - **The renumbering rides in the same transaction** as the edit that caused
    it, so one undo takes back both and the selection maps through the digit
    rewrites for free.
  - **Digits inside a fenced block are left alone** — the one place
    digits-then-dot at the start of a line is not a list item.

- **MD-26** **Vertical spacing is a line decoration, and it does not change when
  the caret arrives** (D202). Headings had a size and a weight but no space
  around them, because `.lp-h1`…`.lp-h6` are *inline marks* and an inline mark
  cannot carry a vertical margin. Blocks — fences, quotes, lists — ran flush
  against the prose above and below them.
  - **Spacing lives on `Decoration.line`**, the same mechanism `lp-fence-line`
    already used, applied to headings (`lp-h1-line`…), fences, blockquotes and
    lists.
  - **Every spacing decoration is unconditional.** This editor un-renders the
    line the caret is on, so a margin that appeared or vanished with the caret
    would shift everything below it on every arrow-down. The heading *size* mark
    is already caret-independent for the same reason, and the spacing matches it.
  - **A block is padded at its two edges, not per line**, via `-top`/`-bot`
    classes on the first and last line of the range — which headings need too,
    since a Setext heading spans its text *and* its `===` underline and a top
    margin on both would split the two apart — otherwise a fence renders
    as a stack of separated tinted rows, and a list's rows get spaced apart from
    each other rather than the list being spaced from its surroundings. A nested
    list is skipped, since it lies inside its parent's range.
  - **Padding, never margin.** CodeMirror keeps its own height map and measures
    each line from its bounding rect — padding is inside it, margin is outside
    it and collapses besides. A margin makes CM believe a line is shorter than
    the space it occupies, so `posAtCoords` (mouse clicks *and* arrow up/down)
    lands on the wrong line, drifting further with each spaced block above it.
    The first draft of this used margins and made the editor unusable below the
    first heading; a source guard now refuses one.
  - **The bottom side stays small.** Markdown's own blank separator line is
    already a full line-height of space; these rules add what markdown cannot
    express — the space *above* a heading, and the gap around a block.

## 33. Git View — Source Control Scoped to the Open Path (D193, D229)

A `git` view template answers one question for whatever the user currently has
open: **what is going on here?** Not "what is going on in this repository" — a
repo-wide view is what a terminal is for — but what is going on with *this
folder*: its uncommitted changes, its commits, the diff of any of them restricted
to that path, **and the operations that change them**. Offered for
**directories**, always as a `condition.py`-gated companion mode, never as a
default. **`git` is the WORKING TREE view and `versions` is the HISTORY view,
and they are offered together on everything inside a work tree** — file or
folder alike (GT-2). The path-scoping machinery below is unchanged and still the
point: a folder deep inside a monorepo asks about itself, not about the
repository.

The view was read-only through D193 (the original GT-11) and is not any more:
D229 replaced that item with a **VSCode Source-Control-style GUI** — branch
management, staging, stashing, committing and pull/push — rebuilt **in place**,
as one mode in the switcher rather than a second template. GT-12..GT-17 below
are that surface, and everything GT-1..GT-10 says about bounds, refusals,
pathspec hygiene and the theme still holds for it unchanged.

**History left the view.** For a while it stayed on as one section of the
reorganized GUI, alongside a selected-commit diff pane and a "load more" window
(`pages`/`sel` params). It is gone: the commit log is what `versions` renders,
with a timeline this view never had, and since both modes now sit on every
target the section was one story told twice. The view neither draws commits nor
asks for them — its `overview` read passes `history=False`, so opening it runs
no `git log` at all. `log.py`'s `op="log"` and `op="commit"` remain, for a
caller that wants the log on purpose.

- **GT-1** An ordinary view template (`fused_render/templates/git/`) —
  `template.html`, `log.py` (the reader), `ops.py` (the mutations),
  `condition.py` (the gate) and `icon.svg`. No shell or server code: everything
  is the ordinary template contract (`_file`, `fused.runPython`,
  params-as-state). The read and write halves are **two modules, not one**: a
  reader that also mutates has no honest place to draw its validation line, and
  "what can this template DO to my repository" must be one file to audit rather
  than a grep for verbs across an 800-line reader.
- **GT-2** **Registry bindings — wherever `versions` is, on files and on
  directories alike.** `git` sits immediately after `versions` in the universal
  `"/": ["_listing", "app", "claude", "versions", "git", "graph", "zarr_aoi"]`
  and in every one of the ~47 **authored-file** keys that carry `versions` —
  code, config, prose, notebooks, tabular, geo, images, record streams. The two
  are the working tree and the history of one repository, so they are bound as a
  pair and the tests enforce the pair in both directions (neither may appear
  without the other). `_listing` stays the directory default (PT-8) and `git` is
  never a default anywhere; on a file key the pair slots in **before the trailing
  meta-modes** (`reader`, `history`), so RD's `reader`-is-last invariant holds.
  The set is still deliberately **withheld** from spreadsheets, media, 3D,
  archives, PDFs and generated tool files — "did a human write or analyse these
  bytes" is unchanged as the question.

  *What this overturns (D235).* `git` was bound to the `/` directory key and
  **nothing else**, the ~40 file keys stripped from it, on the reasoning that a
  file's commit story was `versions`' and a file offering both would be two
  commit-log modes for one story. The premise held only while `git` drew a
  commit log; it does not (see the §33 preamble), so the exclusion was
  protecting against a collision that no longer exists. The **consequence** was
  worse than the redundancy it prevented: since #424 the explorer gives a FOLDER
  no mode switcher of its own — the only mode surface a browsing user has is the
  preview pane's, and the pane acts on the **selected row**, which is a file far
  more often than a directory. Bound to no file key and unreachable from a
  folder's own chrome, `git` could be reached only by hand-writing `?_mode=git`
  or through the file menu's Open With. It was, in practice, not visible
  anywhere. Binding it beside `versions` is what makes it reachable at all.
  Also overturned: the `.jsonl`/`.ndjson` carve-out, for the same reason (its
  argument was about diffing a stream's commit log).
- **GT-3** **The gate (`condition.py`, CT-12) is `git rev-parse
  --is-inside-work-tree`, one bounded subprocess — never a search of the tree.**
  **It passes `close_fds=False`, like every other subprocess in this codebase,
  and that is not a tuning knob.** Without it CPython takes the `fork()` path
  instead of `posix_spawn`, and in the SERVER process — which has PROJ loaded —
  the child dies in PROJ's SQLite atfork handler (SIGSEGV, `returncode == -11`).
  The gate reads that as "not a work tree" and fails closed, so `git` and
  `versions` were silently missing from every mode list in the UI while both
  gates passed their own tests and answered correctly from a plain shell. The
  same fork hazard is documented for the pyramid worker; the fix is the same
  one.  Any gate that grows a subprocess inherits this rule.
  A directory asks about itself. A **file** would ask from its parent (handing git
  a file as `-C` is an ENOTDIR, not an answer), and that branch is still in the
  gate even though D235 left it with no binding to serve: it costs one
  `os.path.isdir`, and a gate that answered only one of the two shapes would be a
  trap for whoever re-binds the mode (a user may, §16) — the file branch is
  **defensive code, not a documented offer**. It **never enumerates**
  (`os.listdir`, `os.scandir`, `glob`, recursion) and never walks, the CT-12 rule
  `zarr_aoi/condition.py` documents and doubly binding here because this gate runs
  on every directory the user opens; the tests make
  enumeration **fatal**, so a listing added later fails rather than ships. A
  `.git` stat fast path was considered and **dropped**: `.git` exists only at the
  repository ROOT, so a probe would have to ascend to answer a nested path
  (`repo/pkg/` has no `.git` of its own), and the two shapes it would then need to
  know about — a `.git` *directory* in a clone, a `.git` *file* in a linked
  worktree or submodule — are exactly what a hand-rolled probe gets wrong.
  `rev-parse` answers all of them from any depth in one fork, and git's own ascent
  is O(depth) stats, never a descent. True for an **empty** repository
  (initialized, no commits — it IS a repo, and GT-9 gives it a real state);
  **False** for a bare repository and for anything under `.git`, where there is no
  work tree, hence no `git status` and no path to scope history to — not offered
  beats offered-then-broken. Fails closed on a missing binary, a timeout, a
  non-zero exit, stdout that is not literally `true`, or any exception.
  **There are no peer exclusions left.** The gate used to answer False for a
  fused app folder (a git-initialized directory exactly two levels under the
  workspace) and then, with a whole SECOND `rev-parse` fork on every stat, for a
  git-backed registered linked app — because that history was `versions`'. Both
  are gone with the History section that motivated them, and so is the mirror
  rule in `versions/condition.py` (which refused every directory that was not a
  fused app). The two gates now ask git the same single question and both say
  yes, which is also why the instruction to keep them and `app_git.app_dir_for`
  "in step" is no longer needed: there is one rule, not three. Nothing about
  WRITE authority moved — `versions.py` still refuses `revert` outside a fused
  app, and only real app folders get `app_git.py`'s auto-commits; being offered a
  timeline never implied being given one (MD-11: the gate is the UX, the module
  is the guarantee). The file branch is likewise no longer "defensive code, not
  a documented offer" — files are bound now, so it is the offer.
- **GT-4** **Mount-backed → refused, before any subprocess.** The same refusal
  `graph/condition.py` makes and for the same shape of reason, worse here: the
  reader runs `git status` / `git log` on the path, and git over an rclone-NFS
  mount stats and lists its way through the work tree — the pattern that wedges a
  flat million-key prefix. The detector is the app's own rule via
  `shared/appenv.is_mount_backed` (PY-15), not a second copy; an unavailable
  detector means "cannot tell", which reads as "refuse". The ORDER is part of the
  requirement: a refusal that still forked git at the mount would have paid the
  cost the refusal exists to avoid. And the refusal is repeated in **`log.py`**,
  because a hand-written `?_mode=git` URL bypasses the switcher entirely — the
  gate is the UX, the module is the guarantee (MD-11).
- **GT-5** **The reader (`log.py`) shells out to git and parses machine formats;
  it reimplements nothing.** Not what a repository is, not what "dirty" means, not
  rename detection, not "3 months ago" (that is `%ar`, a human string passed
  through verbatim rather than re-derived). Six ops: `overview` (header + scoped
  uncommitted changes + the first page of the scoped log, one round trip), `log`
  (a later page), `commit` (metadata + a diff restricted to the scope),
  `worktree` (working tree vs HEAD for one uncommitted entry), `branches` (the
  local branches, D229) and `stashes` (the stash list, D229 — every entry
  carrying its commit id `%H` as well as its index, because an index is a
  *position* and a destructive op has to be able to prove it is acting on the
  entry the user saw; GT-16). The branch list is
  read from **`for-each-ref` with a machine format**, never from `git branch`,
  whose output is a *human* format — column-aligned, colourable through a key
  `color.ui=false` does not cover, and marking the current branch with a leading
  `* ` that a branch name could itself contain. `for-each-ref`'s format language
  is also NOT `git log`'s: it spells a literal byte `%00`, and leaves a `%x00`
  borrowed from the log format as the four characters "%x00" — which parses as a
  field count that is never right, i.e. a silently empty branch list. The stash
  list is `git stash list` (a reflog walk, so it takes `--format` and
  `--max-count`), and an entry's **index is positional rather than parsed out of
  `%gd`**: `stash@{n}` is *defined* as the nth entry of that list, so enumerating
  it is not an approximation of the truth, it is the truth.
- **GT-6** **Every invocation is pinned, hardened and bounded.** `-C <repo root>`
  and `--no-pager` on all of them — the root is resolved **once** by `rev-parse
  --show-toplevel`, the single call deliberately pinned to the target rather than
  the root, and everything after is pinned to the root so a relative pathspec
  means one thing. **argv lists only, never a shell string**, and `--` before
  every pathspec, wrapped in `:(literal)` so a filename holding `*`, `?`, `[` or a
  leading `:` matches itself instead of becoming a glob or pathspec magic. A `sha`
  arrives from a URL param and is validated as a hex object name **before any argv
  is built**, so an option-shaped value cannot cause even one fork. Containment of a
  working-tree **entry** is two checks in two places, and which place matters: the
  entry's realpath must resolve under the repository root — for EVERY entry, before
  any git call, since that also catches an ordinary file reached through a symlinked
  parent directory — while the refusal of a **symlinked entry** belongs only on the
  **untracked** branches, because those are the ones that read bytes off a resolved
  name (`git diff --no-index` renders the target under the link's name, and
  `os.path.isdir` follows a link into the untracked-directory listing). A **tracked**
  symlink must diff normally: `git diff HEAD -- <rel>` treats it as a symlink,
  diffing the link's target *path text* without reading through it — so putting that
  refusal one level too early refuses every symlink row, including the safe one.
  Residual, accepted and stated: a tracked symlink whose target is outside the repo
  is refused by the realpath check even though its branch would be safe, because
  containment stays ONE rule for the whole op rather than varying by a trackedness we
  only learn from a git call. Log records
  are `%x00`-delimited fields, one commit per line (every field in the format is
  single-line by construction, so the newline is an unambiguous record separator);
  status is `--porcelain=v1 -z`, whose rename form puts the NEW path first.
  Non-interactive by environment (`GIT_TERMINAL_PROMPT=0`,
  `GIT_OPTIONAL_LOCKS=0`, no pager, no askpass, no LFS smudge) with a timeout on
  every call. The user's git config is **left alone** — no
  `GIT_CONFIG_GLOBAL=/dev/null` — because `safe.directory` lives there and a repo
  the user marked safe must keep working; only the knobs that could corrupt
  parsing are overridden per command with `-c`, plus `--no-ext-diff` as a **flag**
  (clearing `diff.external` tells git to run the empty program, which is a hard
  failure).
- **GT-7** **One unscoped `git status`, filtered to the scope in Python.** The
  header's clean/dirty light describes the **repository** (that is what the word
  means) while the list below it describes the **scope**, and both facts come out
  of the same walk of the index this way rather than out of two calls.
  **Measured, not assumed** (2026-07-31, this machine, the exact argv the reader
  uses; first run then warm median of five):

  | repo | tracked | worktree files | first | warm |
  |---|---|---|---|---|
  | superset | 6,535 | 6,558 | **254 ms** | 111 ms |
  | fusedudfs | 5,331 | 5,759 | 72 ms | 31 ms |
  | application | 4,096 | **572,320** | 36 ms | 8 ms |
  | fused-render | 651 | 126,373 | 2 ms | 1 ms |
  | synthetic, 100k tracked, clean | 100,000 | 100,000 | 38 ms | 38 ms |
  | synthetic, 100k tracked, ALL modified | 100,000 | 100,000 | **160 ms** | 160 ms |
  | synthetic, 40k untracked non-ignored | 1 | 40,001 | 9 ms | 7 ms |

  Three things that table settles. **Cost tracks TRACKED files, not worktree
  size** — the 572k-file checkout is the second *fastest*, because a
  directory-ignored `node_modules` is pruned without descending, and 40k untracked
  files collapse to 400 `dir/` entries under `--untracked-files=normal` (which is
  precisely the ancestor case `_in_scope` handles). **The worst number here is
  254 ms against `TIMEOUT_S = 12.0`** — a 47x margin, and the view paints its
  skeleton first regardless, so this is not on any critical path a user perceives.
  **And the two-call alternative is measurably worse, not better:** on superset a
  scoped status is 2.1 ms but the cheapest repo-wide dirty probe,
  `git diff --quiet HEAD`, is 100.2 ms — 102 ms total against 111 ms for the single
  unscoped call, an ~8% saving — while on the 100k-all-modified synthetic that
  probe (171 ms) is *slower* than the whole status (160 ms). It is also less
  correct: `diff --quiet HEAD` cannot see untracked files, so a repo dirty only by
  untracked files would report clean. So one unscoped call stands, on evidence.
  Revisit only if a repository an order of magnitude past 100k tracked files shows
  up; the bound that catches it is already there (`TIMEOUT_S`, refused as a calm
  empty state, GT-9). The filter
  reproduces git's own pathspec semantics including the case a naive prefix test
  gets wrong: with `--untracked-files=normal` git collapses a wholly-untracked
  directory to `dir/`, so an entry can be an *ancestor* of the scope rather than a
  descendant, and the scope is still dirty because of it. Entries carry the raw
  `XY` letters (`M`/`A`/`D`/`R`/`??`…) plus derived
  `staged`/`unstaged`/`untracked` flags and, for a rename, both paths.
  **Both sides of a rename are scope-tested**, and the entry is listed when EITHER
  matches: testing only the new path dropped a file moved *out* of the open folder
  from the list entirely, which is precisely the change a scoped view exists to
  show. This is also the concrete reason the filter is ours rather than git's —
  verified, not assumed: `git status --porcelain -- pkg` omits `R outside/gone.py`
  ← `pkg/leaving.py`, so delegating the scope to a pathspec would reintroduce the
  same defect. A rename with only one side in scope is a **move relative to that
  scope**, a different fact from a rename, so the direction is reported
  (`moved: "in" | "out" | null`) and labelled rather than flattened; nothing is
  "moved" relative to the repository root, where both sides are always in scope.
- **GT-8** **Nothing is unbounded, and every bound is VISIBLE.** The log grows a
  **window** (`limit = PAGE_SIZE * pages`, one call, so a restored URL costs one
  round trip) rather than paging with client-side accumulation, and `limit + 1` per
  request is the `has_more` probe, so "load more" needs no count-everything call.
  Because the window grows, the ceiling on it (`MAX_LOG_LIMIT`, 500 commits for one
  path) is reported as its own field — **`capped`** — and never applied as a silent
  `min()`: `has_more` answers "git had more records than we returned" and stays
  honestly true once the clamp bites, so a UI driven by `has_more` alone offered a
  "load more" that refetched the identical rows forever. `has_more and not capped`
  is the only state in which another click can achieve anything; `has_more and
  capped` is the terminal state the UI states as "showing the most recent N commits
  for this path"; `not has_more` is the end of history. `capped` is also
  independent of the malformed-record guard — `has_more` counts the records **git
  emitted**, not the ones that survived parsing, because counting survivors let one
  dropped record on a full page report the end of history one page early. The two
  defects sit on one expression and are opposite in direction (premature "end of
  history" vs endless "load more"), so both signals are needed and neither may be
  derived from the other. Diffs **stream** through a byte cap AND a line
  cap, whichever hits first, with a watchdog that kills the process: streamed
  rather than captured because `subprocess.run` would buffer the whole
  hundred-megabyte diff into memory before it could be trimmed, and a manual read
  loop has no `timeout=`. Truncation is **reported in the UI**, never silent. The
  change list is capped too (a build tree can hold 100k untracked files), and so
  is everything D229 added: the branch list (`MAX_BRANCHES`, asked for as
  `--count=N+1` so "there were more" is *observed* rather than guessed, and
  sorted newest-committed-first with the ref name as tie-break, because the limit
  has to cut somewhere and the branches you touched recently are the ones you are
  looking for), the stash list (`MAX_STASHES`, likewise `--max-count=N+1`), the
  out-of-scope staged **paths** (`MAX_STAGED_OUTSIDE`, with the count kept a true
  total — GT-14), and the path list one mutation may carry (`MAX_PATHS` — GT-12).
- **GT-9** **Every awkward state is a first-class state, and refusal is a
  PAYLOAD** (`{ok: false, reason, message}`) rather than an exception: not a
  repository, missing path, mount-backed, no git binary, timeout, empty repository
  (no commits yet), detached HEAD (reported by short sha, `branch: null`), a path
  with no history, a path outside the repository, binary files (git's own "Binary
  files … differ", never dumped), renames. **An untracked DIRECTORY is one of those
  states, not a diff:** `--untracked-files=normal` collapses a wholly untracked
  directory to a single `dir/` row, and a directory has no patch — so the entry
  answers with what is *inside* it (`kind: "untracked-dir"`, each file clickable
  through to its own whole-file diff) instead of an empty pane wearing the
  commit-oriented sentence, which was wrong twice over since it is not a commit and
  the path IS in scope. The listing is `git ls-files --others --exclude-standard`,
  i.e. "what `git add` would pick up here" with .gitignore and nested excludes
  already honoured rather than reimplemented, read through the same streamed byte
  cap as `git status` and capped again by entry count — never an `os.walk`, which
  would be an unbounded recursion inside a template. Every empty state names its
  OWN situation: a worktree entry that diffs to nothing says its contents match
  HEAD, which is not the sentence for a commit that missed the scope. The view
  renders a calm empty state
  from the payload — a folder without git is an ordinary situation, so even a
  reader crash is caught into that state rather than the red traceback overlay,
  which is a debugging affordance for a view's own bug.
- **GT-10** **The view.** `data-fused-theme="shell"` with both palettes defining
  the same token set and no colour literal in any rule (AP-9, tier 1). *(The
  LAYOUT this item describes is D229's predecessor — the two-section
  history-only page. GT-17 has the current one. Everything else here — the
  no-header ruling, the params-as-state discipline, the diff pane's behaviour,
  the truncation wording — carried over unchanged and is stated once, here.)*

  **A template has NO header of its own** — owner ruling 2026-07-31: *"a template
  is not the same as app, it does not require any headers."* A template renders
  inside the shell's chrome — breadcrumb, preview header, mode switcher — so
  app-like furniture of its own is duplication at best and competing chrome at
  worst: a heading naming the path is the page's loudest element spent restating
  what sits directly above it, and it pushes the lists that ARE the subject below
  the fold. The general rule is therefore stronger than "state it smaller": the
  shell owns identity, and a template ships content. (This supersedes an earlier
  one-line-status-header compromise, which was the same argument stopped halfway.)

  What survives is the facts that are **data rather than chrome**, folded onto the
  **section labels** — labels being content:

  ```
  UNCOMMITTED 2 · main
  HISTORY 30
  ```

  Nothing here repeats the shell: no repo-name title (the breadcrumb has it) and no
  scope line (the breadcrumb is the scope). The **branch** stays because it is the
  one fact the shell cannot supply, and it rides the `UNCOMMITTED` label. A
  **detached HEAD** reads `detached at <short sha>` in the branch's place, and an
  **unborn branch** appends `· no commits yet` — both are properties of the HEAD
  being named, so they belong beside it rather than in a band of their own. There is
  **no clean/dirty light**: `UNCOMMITTED n` already carries that, and a count is a
  strictly better signal than a lamp. The one case a count cannot carry honestly is
  a truncated read (GT-8's status byte cap, where `dirty` is forced true and `n` is
  a floor rather than a total) — which the label states as **`n+`**, with the list's
  own truncation note below it, rather than by reintroducing a light. All UI
  state in URL-synced params — `pages` (how much log is loaded), `sel` (selected
  commit) and `wt` (selected uncommitted entry), the last two **mutually
  exclusive** so there is at most one diff target — hence a refresh or a bookmark
  reproduces the view with its diff pane already open. Every row is a real
  `<button>`, so tab/enter work natively; Escape closes the pane. The skeleton is
  laid out at the real thing's dimensions and a selection change repaints from the
  last good snapshot, so nothing jumps and the lists never blank while a diff
  loads (a serial guard drops a stale response). The diff is the one wide thing on
  the page, so it scrolls inside its **own** container and the page body never
  scrolls horizontally. Stacked (narrow) the diff pane sits **above** the lists,
  because appending it below thirty commit rows puts the response to a click below
  the fold, which reads as nothing having happened.
- **GT-11** ~~Read-only, always.~~ **Superseded by D229 / GT-12.** The original
  item said the view never stages, commits, checks out, fetches or writes
  anything, and cited `GIT_OPTIONAL_LOCKS=0` as saying so to git as well. The
  owner replaced it: the read-only line kept the view honest but left it a
  dead-end — every question it answered ("this file is modified", "this is
  staged", "you are 3 behind") ended in "now go somewhere else". The **reads
  remain exactly as specified above** and still never write, never fetch and
  never take an optional lock; what changed is that `ops.py` sits beside them.
  The number is kept rather than reused so the overturn stays legible.
- **GT-12** **The write surface (`ops.py`) is a second module, and it is the
  only thing in this template that can change a repository.** Same shape as the
  reader: a `main(...)` returning a JSON-native dict, refusal as a payload
  (`{ok: false, reason, message}`), the same `@fused.udf` shim, the same
  `-C <root>` / `--no-pager` / argv-list / `--`-before-every-pathspec /
  `:(literal)` discipline, and the same mount refusal — **GT-4 applies to the
  write path too**, because a hand-written `?_mode=git` URL must never reach a
  *mutating* git call across an rclone/NFS mount. The ops are `stage`,
  `unstage`, `stage_all`, `unstage_all`, `discard`, `discard_all`, `commit`,
  `branch_create`, `branch_checkout`, `branch_delete`, `stash_push`,
  `stash_apply`, `stash_pop`, `stash_drop`, `fetch`, `pull`, `push`. Three
  bounds differ from the reader's and each has a reason: **`TIMEOUT_S` is 25s
  rather than 12s**, because a mutating command runs the user's own hooks and a
  network command talks to a remote, neither comparable to a local plumbing read
  (still inside the 30s `fused.runPython` ceiling); the **path list is capped**,
  because an unbounded path list is an unbounded argv whose failure mode is
  `E2BIG` rather than a sentence; and **`GIT_OPTIONAL_LOCKS=0` is dropped**. That
  last is deliberate and the opposite of a nicety carried over by habit: the
  variable only ever suppresses locks git takes *optionally* — the opportunistic
  index refresh a read does while answering — and a mutating command takes the
  index lock it needs regardless, so carrying it would state a promise the module
  cannot keep, and would suppress precisely the refresh that makes the `git
  status` right after a mutation accurate. `GIT_TERMINAL_PROMPT=0` and the
  askpass knobs **stay**: with a credential helper or an ssh-agent configured
  pull/push work, and without one they **fail fast with a readable message**
  rather than hanging on a prompt nobody inside an iframe can answer. `GIT_EDITOR`
  is additionally pinned to a program that exits non-zero, so anything that would
  have opened an editor (a conflicted `stash pop`, a `commit` git decides needs a
  message) is an ordinary error instead of a wait with no end.
- **GT-13** **Mutations are scoped to the open path, and the scope rule is
  STRICTER than the reader's.** `stage` / `unstage` / `discard` / `stash push`
  and their `_all` forms are restricted by the `:(literal)<rel>` pathspec derived
  from the opened file or folder, exactly like the reads. Every user-supplied
  path is validated in **three passes that catch three different things**, before
  it can become an argv entry: the **string** must not be absolute, hold a `..`
  segment, or start with `-`; its **realpath** must resolve under the repository
  root (which is what catches a symlink, including an ordinary file reached
  through a symlinked *parent* — the case an `islink` test on the final component
  misses entirely); and it must sit under the **open scope**. The reader lists
  one out-of-scope entry on purpose — a rename with only one side in the scope,
  because "this file left the folder you are looking at" is a change the view
  exists to show (GT-7) — and the write rule does **not** inherit that exception:
  listing a change and changing it are different acts, so the row is shown
  without its action buttons. The reader's *ancestor* case is likewise absent: a
  collapsed `dir/` row that is an ancestor of the scope covers files outside it,
  and discarding it would reach them. **The view MIRRORS this rule rather than
  approximating it** — a row the module would refuse renders without its action
  buttons, because a button that always refuses is worse than no button: it
  advertises an act ("delete this whole directory") that cannot happen.
  **`branch_checkout` is exempt by nature** and does not go through the path rule
  at all — a branch *is* a repository concept, and there is no such thing as
  checking one out "just for `pkg/`". That is expected, not a hole.
- **GT-13a** **A name GIT produced is validated exactly like a name the user
  typed, and `--` still precedes it.** git echoes refnames and remote names
  **verbatim** out of files inside `.git`, and those files are content, not API:
  a hand-written `.git/HEAD` holding `ref: refs/heads/--upload-pack=cmd` makes
  `symbolic-ref --short HEAD` print `--upload-pack=cmd`, and a hand-written
  `[remote "--receive-pack=cmd"]` makes `git remote` print that. Neither is
  reachable through git's own porcelain — `git branch` and `git remote add` both
  reject them — so a repository carrying one arrived some other way (a tarball, a
  zip, a shared drive; `git clone` does not copy config). Both values flow into
  the argv of `fetch` / `pull` / `push`, so a **leading-dash refusal is applied
  to them centrally** and the whole repository is refused rather than the bad
  name filtered out: silently using the *other* remote would hide that the
  repository is malformed. **Every** repo-derived value is additionally written
  after a `--` terminator. The two are independent guarantees and both are kept,
  because relying on either alone is precisely how one missing `--` turns a
  hostile repository into local command execution. Same class, same reasoning as
  GT-6's rule that a non-hex `sha` never becomes argv — the difference is only
  that the untrusted input arrives from the *repository* rather than from a URL.
- **GT-14** **Commit is index-based, and the UI is honest about it.** `git
  commit -m <msg>` with **no pathspec, ever**. The alternative — `git commit --
  <paths>` — is not a scoped commit but a different operation: it bypasses the
  index and records the *working tree* for those paths, so a file deliberately
  staged in one state would be committed in another. That is a silent data
  surprise, and refusing it is the point. The cost is that a commit made from a
  view scoped to `pkg/` also carries whatever is staged elsewhere, so the reader
  **reports it**: `overview` gains `staged_outside: {count, paths}`, collected
  inside the existing unscoped `_status` walk (GT-7) rather than by a second `git
  status`, with the path list bounded and the **count a true total** — the number
  is the part that decides whether you look. The view turns that into
  *"⚠ N staged change(s) outside this scope will also be committed"*, with the
  paths listed. An **empty or whitespace-only message** and a **nothing-staged**
  commit are the module's own refusals with readable text, not git's ("Aborting
  commit due to empty commit message" reads like a malfunction; the whole status
  printed as advice is a wall of text answering a question nobody asked). A
  message is **one argv element** to `-m` and may hold anything — newlines,
  quotes, backticks, a `$(...)` — because there is no shell anywhere in the
  module for it to mean something to. A successful commit returns the new short
  sha and subject.
- **GT-15** **No history rewriting, and no path to it.** No `--amend`, no
  `reset --hard`, no rebase, no force push, no `branch -D`. `branch_delete` is
  **`-d` only** — git refuses a branch whose commits are reachable from nowhere
  else, and that refusal is surfaced **verbatim**, because it *is* the safety
  property: the one thing a GUI must not make easy is throwing away commits.
  `pull` is **`--ff-only`**, and a non-fast-forward is a **refusal pointing at a
  terminal**, never an automatic merge or rebase — a divergence is a decision,
  and both automatic answers take it on the user's behalf (a merge writes a
  commit they did not ask for, a rebase rewrites commits they already have).
  `push` never forces. `discard`'s untracked half is `git clean -fd` and
  **`-x` is forbidden**: an ignored path is where a `.env`, a virtualenv and a
  build tree live, a scale of loss completely unlike "throw away the edit I just
  made", and one no confirmation could meaningfully warn about because those
  files are by construction invisible in this view. A test greps the module for
  the forbidden spellings, so a future change that needs one is a conversation
  rather than a diff.
  **Two deliberate decisions inside these bounds.** `branch_checkout` uses `git
  switch` and falls back to `git checkout` only on a git older than 2.23,
  detected by the attempt's own exit code and message rather than by parsing
  `git --version` (a version string is one more format to get wrong, and the
  question that matters is "did this git understand the verb"); `switch` is
  preferred because it cannot be talked into restoring files, which is
  `checkout`'s other, path-shaped meaning and the reason `checkout` is easy to
  misuse. And a **`push` from a branch with no upstream sets one**
  (`--set-upstream <remote> <branch>`) rather than surfacing git's advice, which
  is a sentence no GUI button can act on; the view labels that button **"Publish
  branch"** rather than "Push", because it is a bigger act. It is a narrow
  exception and not a force: it can only create a ref that does not exist, and if
  the remote branch *does* exist with commits we lack, git refuses exactly as it
  would for any other non-fast-forward and that refusal is shown. The remote is
  the branch's own, else the sole configured one, else `origin`; with several
  remotes, no `origin` and no upstream there is no defensible guess, so the
  answer is "no remote" and no button rather than a choice made for the user.
  **Every network command names its remote AND its refspec explicitly** —
  `push -- <remote> HEAD:refs/heads/<upstream branch>` and
  `pull --ff-only -- <remote> <upstream branch>`, never the bare form. A bare
  `git push` means whatever `push.default` and `remote.pushDefault` say, which
  can be "every matching local branch" or "a different remote than the success
  message names": in both cases doing more, or something else, than the button
  said, decided by config this view never shows. `--ff-only` bounds the
  *outcome*; naming the refspec bounds the *input*, and both are wanted. The
  refspec is written `HEAD:refs/heads/<name>` so it preserves the recorded
  upstream mapping when the local and remote branch names differ, is fully
  qualified against matching a remote ref of another kind, and — being prefixed
  by `HEAD:` — cannot be option-shaped whatever the refnames in the repository
  are (GT-13a).
- **GT-16** **Anything that can lose uncommitted work is confirmed in-view, and
  is individually addressable in the module.** Exactly three ops are destructive
  — `discard`, `discard_all`, `stash_drop` — and the module names them in one
  constant the view mirrors, so an op added later without being classified ships
  *without* a confirmation loudly rather than quietly. **Confirmation is the
  view's job**; the module's job is that each destructive op is its own call and
  **never a side effect of a safe one**. The confirmation is an **inline
  affordance, never `window.confirm`**: this document renders inside an iframe,
  where a native dialog is at best chrome the view cannot place and at worst
  suppressed — and inline puts the question next to the row it is about, which is
  why it is being asked. Its pending state lives in a param (`ask`), so a refresh
  reproduces the question rather than silently dropping it.
  **A confirmation carries IDENTITY, not position, and it says which row it is
  about.** The confirmation is not modal and its param can sit in the URL
  indefinitely, so the world may move between the question and the answer, and
  the `ask` key has to be self-sufficient. Two ways it was not, both fixed by
  putting the missing fact in the key: a **stash** is addressed by index *and*
  commit id (`log.py` puts `%H` on every entry, `ops.py` verifies it and refuses
  with "that stash moved" otherwise), because `stash@{n}` means "the nth entry
  right now" and any `stash push` — from this view, another tab, or a terminal —
  renumbers every entry, so a merely bounds-checked index would irreversibly drop
  a different, still-wanted stash; and a **discard** names the SECTION its row
  was in (`discard:staged:<path>` vs `discard:worktree:<path>`), because an `MM`
  file appears in both lists and a key of just the path could not tell the two ↩
  buttons apart — the CHANGES row's, whose question mentioned only the unstaged
  edit, was composing the staged form and throwing the staged version away too.
  Each row's question text states exactly what its own call will do. `ask` is
  also cleared after every mutation, so an answered question cannot be re-asked
  over freshly-fetched lists or leave a stale index behind. Discarding a **staged**
  change is two explicit calls — `unstage`, then `discard` — composed by the view
  behind ONE confirmation, because `restore --worktree` restores from the *index*
  and a single-call discard would put back the very content being thrown away.
  When the second half fails the first has still **happened**, so the failure
  message says so ("the change was unstaged, but discarding it failed: …")
  instead of leaving the user believing nothing moved.
  `branch_delete` is deliberately **not** in the confirmed set: `-d` cannot lose
  work, so git's own refusal is already the safety step and a second one would be
  the kind of ceremony that trains people to click through.
- **GT-17** **The view is the Source-Control shape, and the params are still the
  state.** Toolbar (branch ▾ · ⟳ fetch · ↓ pull *n behind* · ↑ push *n ahead* ·
  stash ▾) → commit message box with the ✓ Commit button and the GT-14 warning →
  **Staged changes** / **Changes** / **Untracked** / **Stashes** / **History**,
  The commit box is **present only when the index has something in it** — a
  permanently disabled button reading "nothing staged" is a control teaching you
  it does not work, and it sat at the top of the view in the state the view is in
  most of the time (a clean tree), pushing the sections that *do* have something
  to say further down. The test is what a commit would RECORD, which is the index
  and not this scope: a scope with nothing staged of its own still shows the box
  when something is staged elsewhere, carrying the GT-14 warning that says whose
  changes those are — gating on the scoped count alone would hide the box while
  `git commit` still had work to do, the same dishonesty in the other direction.
  A typed message survives the box disappearing (it lives in `msg`, GT-16), so
  unstaging everything and staging again does not cost the user their message.
  with the existing diff pane on the right unchanged. History is a *section* of
  this page now, not a page of its own. Every piece of UI state stays in the URL
  as before (`pages`, `sel`, `wt`) plus `panel` (which dropdown is open), `msg`
  (the commit message draft) and `ask` (the pending confirmation) — the last two
  being the ones easy to argue against and right anyway: a commit message is the
  most expensive thing in this view to retype, and a confirmation that survives a
  refresh cannot be bypassed by one. `msg` is deliberately **excluded from the
  structural signature the repaint compares**, or every keystroke would repaint
  the textarea out from under the caret. A mutation is: disable the control that
  was clicked (only that one, so a slow `push` does not read as a freeze) → call
  `ops.py` → **refetch the overview and repaint**, never patch the DOM from the
  op's own reply, because git is the authority on what the repository now looks
  like and a locally-guessed state is how a UI drifts out of sync with the repo
  it claims to show → announce through the existing `aria-live` region. The
  serial guard is bumped **by the mutation as well**, so an overview still in
  flight from before it cannot repaint stale data over the fresh one. A failed
  mutation renders its message **in-view** — never the red traceback overlay
  (that is a debugging affordance for a view's own bug, and a git refusal is an
  ordinary answer) and never a silent no-op. Everything GT-10 requires still
  holds: both palettes define the identical token set, `data-fused-theme="shell"`,
  no colour literal outside them, **no page header of its own** (the toolbar is
  not a header — every item on it is a control, and the branch name is there
  because it is what you click to change branches), the body never scrolls
  horizontally, rows are real `<button>`s, Escape closes the innermost open thing
  (confirmation → dropdown → diff pane), and `prefers-reduced-motion` is honoured.

**See also §34** (`file_history`), the other history view. It is complementary
rather than an alternative: this one drives the repository's own commit graph and
index, i.e. everything git already knows about; that one reads Claude Code's
per-edit checkpoints — which exist for files in no repository at all, and for
edits made since the last commit — and can put content back. FH-1 states the
split from the other side.

## 34. File History — Revert from Claude Code's Checkpoints (D194, D195)

Goal: give a template view an **undo for the agent's edits**, with no version
control involved. Claude Code already writes a full copy of every file it is
about to change; this reads that store, presents the file's version timeline,
and restores from it. Wired into `annotate` first (§17); the reader is shared so
`claude` and `history` can adopt it — and **`claude` now has** (FH-18 below).

**Reachability (supersedes the D235 note).** The capability used to be reachable
only through `annotate`, which is bound to nothing (§17), so on a default
registry it was unreachable. It now ships on `claude`, which is bound to every
authored-file key and to the universal directory key, so the SHOWING half is on
by default for any file with checkpoints. The reader
(`templates/shared/file_history.py`) is unchanged — being host-agnostic is
exactly what let the adoption be a wiring change rather than a redesign — and
`annotate` keeps its own (unreachable) copy, which is not deleted here.

**Two history views, and which one answers your question.** §33's `git` view and
this one are **complementary, not alternatives** — the distinction is *whose*
history you are asking about. (All three now sit in a file's mode
list: `git` is the working tree, `versions` is the repository's history of the
file, and this panel is the agent's checkpoints — GT-2. The comparison below is
about what a repository's history can and cannot answer.) `git`
answers "what happened to this path in this repository": commits, uncommitted
changes, diffs, everything a human or a tool ever committed, over the whole
recorded life of the file — and it is read-only, by design (GT-11). This one
answers "what did this file look like before the agent last touched it", which is
a strictly narrower and much younger window, and it is the only one of the two
that can put content BACK. So they differ on both axes: scope (a repository's
commit graph vs one agent's per-edit checkpoints) and capability (read vs
restore). Two consequences worth stating, because they are the reason neither
subsumes the other: `git` has nothing to say about a directory that is not a
repository, or about edits made since the last commit — which is exactly the
window this feature exists for; and this one has nothing to say about anything
Claude Code did not do, including the user's own saves (FH-6), which is exactly
what `git` is for. A reviewer wanting "undo the agent" wants this; a reviewer
wanting "what has this file been through" wants §33.

- **FH-1** **The store, and why it is the authority instead of git.** Content
  lives at
  `<claude-config-dir>/file-history/<sessionId>/<sha256(abspath)[:16]>@v<N>`,
  where each `@vN` is a **full copy** of the file at a checkpoint, never a diff.
  `<claude-config-dir>` is `CLAUDE_CONFIG_DIR` when set, else `~/.claude`,
  resolved through `expanduser` on a `join` rather than a literal `"~/.claude"`
  (this package ships a `windows/` dir, and a hardcoded forward slash survives
  `expanduser` unchanged there and then never matches a normalized path). Git
  answers "what did the last commit say"; this answers "what did this file look
  like before the agent touched it", which is the question a reviewer sitting in
  a view actually asks — and it has an answer in a directory that is not a
  repository at all, which git does not. The two are complementary, not
  alternatives; a separate git-backed template is its own work.
- **FH-2** **Enumeration is filesystem-only, because the filename key is a pure
  function of the path.** The hash is verified as `sha256` of the **absolute**
  path truncated to 16 hex chars (13/13 files of a real session), so one file's
  entire timeline is `<history-root>/*/<hash>@v*` — no transcript parsing on the
  render path. That is the whole reason this is cheap enough to call on a view
  boot: the session transcripts reach **5 MB+** each and reading one per render
  would be a performance trap. `abspath` is load-bearing, not cosmetic: a
  relative path hashes to something the store never heard of, so the lookup
  would silently find nothing rather than fail.
- **FH-3** **Versions are checkpoints, not per-edit pre-images, and undo is
  POSITIONAL: find where disk sits in the chain, then step backwards.** Two
  wrong rules were ruled out, in order, and both matter.

  *"Restore the highest `@vN`"* is wrong roughly half the time. Measured on a
  real session: 6 of 13 files matched their highest version, 7 did not, because
  the file moved on after the last checkpoint. Restoring the highest N would
  frequently be a no-op that reads as a broken button.

  *"The newest version whose content differs from disk"* replaced it, and is the
  sharper lesson: it reads as obviously correct, survived two reviews, and
  **oscillates on the second press**.

      disk == v3  ->  v3 differs=False, v2 differs=True  ->  target v2
      disk == v2  ->  v3 differs=True and is newest      ->  target v3

  ...for ever, with v1 unreachable at any point. It answers "which checkpoint is
  most recent and isn't what I have", which is not what undo means.

  The rule is therefore positional-then-differs, over the newest-first list:
  **(1)** `position` = index of the **newest** entry whose content equals disk
  (newest, not oldest: with duplicate content on both sides of a real checkpoint,
  walking from the oldest match steps straight over it). **(2)** target = the
  first entry **older** than `position` that still **differs** from disk — the
  differs-check stays inside the walk rather than taking `position + 1`, because
  identical adjacent versions are common and restoring one writes the same bytes
  back. **(3)** `position == -1` (disk is in no checkpoint at all) means the first
  step back is "discard to the most recent checkpoint": target = entry 0. This is
  the same index that yields `unique_current`, derived once so the two can never
  disagree. **(4)** no such older entry ⇒ `at_earliest`, a distinct terminal state
  that DISABLES the button and says why — falling back to something newer is
  exactly what made the previous rule a toggle. Result: `v3 → v2 → v1 → delete →
  disabled`, monotonic, whole chain reachable. "Redo" needs no new UI, because
  the timeline already lets the user click a newer row explicitly; that asymmetry
  is right for a button labelled "Revert last change".

  `differs` is a byte comparison per version. An ABSENT file makes every content
  version differ — which is what gives "the agent deleted my file" an undo — while
  making a did-not-exist boundary MATCH, so the walk terminates there instead of
  offering a delete that would do nothing.

  Presentation follows the rule but does not duplicate it: the dot column has
  exactly **two** states — `●` in the neutral foreground for the current position,
  `○` muted for everything else — and the revert target is **row treatment** (an
  accent left-stripe plus wash, with the stripe's width reserved as a transparent
  border on every row so marking nothing reflows nothing). Three glyphs over three
  colours was the first attempt and read as noise: `◉` and `●` are barely
  distinguishable at 11px, and the two facts are unrelated — the dot answers
  "where are you", the stripe answers "what does the button do", so they belong in
  different visual channels. It also means `at_earliest` needs no fourth marker:
  `revert` is null, so no row is striped, and "no accent anywhere" already reads
  as "nothing to go back to". The position row is exempted from the
  identical-to-disk dimming, since dimming the one row the marker exists to
  emphasise defeats it.

  `at_earliest` may only be claimed by an **enriched** scan (FH-5). The boot
  timeline skips the transcripts, cannot see the creation boundary, and so reports
  it one step early; a view that believed it disabled the button on a file whose
  remaining step back was a delete the plan would have offered. The unenriched
  answer is provisional (`enriched: false` on the payload) and the click asks
  `revert_plan`, which always enriches and is the authority.
- **FH-4** **Chains are per-session and version numbers collide, so the timeline
  merges on TIME.** A path edited across several sessions has a separate chain
  under each `<sessionId>/`, and N **restarts**: two sessions both holding a
  `@v2` for one path is ordinary, not an edge case. Order is the backup file's
  mtime, with N only as a tiebreak *within* a session, never across. A version is
  therefore identified by the **pair**, surfaced as one opaque id
  (`"<session>@v<N>"`), and the row's tooltip names the session because that is
  the only thing distinguishing two rows that both say `v2`.
- **FH-5** **A null `backupFileName` means the file did not exist, so reverting
  across it is a DELETE.** Not a restore of empty content, which would leave a
  zero-byte file the agent never created. The filesystem cannot represent "no
  content", so this fact lives only in the transcript
  (`<config>/projects/<cwd with / -> ->/<sessionId>.jsonl`, records of type
  `file-history-delta` / `file-history-snapshot`) and arrives only through
  **opt-in enrichment**: the view's boot call does not read transcripts, and only
  an expanded History panel pays for one. Three guards keep that affordable and
  quiet — a byte cap checked by `stat` before anything opens; a per-line
  **substring prefilter** so `json.loads` runs on the handful of candidate lines
  rather than the file (a 5 MB transcript becomes a 5 MB `in` scan); and a
  blanket except per transcript, so corrupt/truncated/half-written degrades to
  "no extra rows", never to an error. Transcripts are reached by a **glob** on
  `projects/*/<sessionId>.jsonl`, not by deriving the slug: the slug is the cwd
  with separators replaced, which is lossy (a directory name containing `-` is
  indistinguishable from a separator) and was never evidence of anything.

  **Attribution is an identity test on the record's absolute `realParentDir`, not
  a path suffix** — `join(realParentDir, basename(trackingPath)) ==
  abspath(target)` — and this is the sharpest correctness rule in the feature.
  `trackingPath` is repo-relative and this code has no idea what the repo root is,
  so the original suffix match had no project attribution at all: `src/main.py`,
  `README.md` and `index.ts` recur across every checkout on the disk, so an
  unrelated project that CREATED its own `src/main.py` injected a boundary row
  into this file's timeline — and since boundary rows sort by the transcript's own
  timestamp it was typically the newest entry, hence the revert target, turning
  "Revert last change" into a **DELETE of a file the agent never created**, behind
  a confirm sheet asserting "Claude created it" about a file it had never seen. A
  record with no usable `realParentDir` is **refused**, never guessed: the cost of
  refusing is one missing row, the cost of guessing is deleting the wrong file.
  Such a row carries its own record's timestamp —
  never a neighbouring row's; verified on a real chain where the creation
  boundary's `backupTime` is nine minutes before the next checkpoint's and only
  *looked* duplicated at display granularity — and when that stamp will not parse
  the view renders "time unknown" rather than 1970. It also wears its own version
  number (the creation boundary is `version: 1` in a real store), so it is
  numbered like every row below it with "did not exist" as the annotation
  explaining what restoring it does; a dash appears only for a record with no
  usable number, since `v0` would invent a version the store never wrote. Its id
  is `"<session>@none<N>"`, which keeps it distinct from a content `@vN` that
  shares the number.
- **FH-6** **Strictly read-only with respect to the Claude config dir.** Nothing
  writes, moves or unlinks anything under it, ever: it is the user's live edit
  history and this is a guest in it. Asserted as a **whole-tree byte snapshot**
  across every action rather than per call, so a write added anywhere later trips
  the test. A corollary accepted deliberately: the user's own annotate saves are
  not checkpointed by anyone, so they are **not revertible** — this reverts the
  agent's edits, and only those.
- **FH-7** **The one write is to the target, gated and atomic.** `file_writable`
  is the same three-part gate as `annotate.py::_sidecar_writable` (RO-3, RO-6):
  the read-only-mount check FIRST, through `shared/appenv`'s env contract, because
  `os.access(W_OK)` **lies** under a read-only mount with CacheMode=full — the
  write lands in the local VFS cache and only 403s at the async upload; then W_OK
  on the **directory** (mkstemp and the replace both land there — this half the
  sidecar's gate does not need and a replace does); then W_OK on the file itself
  when it exists, since `os.replace` goes through the directory and would
  otherwise blow past a `chmod -w`. The write is mkstemp + `os.replace` in the
  target's own directory — atomic, never cross-device — and carries an existing
  file's mode onto the replacement, because a fresh mkstemp is 0600 and a revert
  has no business changing permissions.
- **FH-8** **Path confinement is a matching problem, not a sanitizing one.** The
  selector a client sends is an **opaque id matched against the enumerated
  timeline** and never joined into a path, so an id carrying `..`, separators or
  an absolute prefix has nothing to traverse — it simply resolves to no entry.
  Every path opened is one this code built itself from (history root, a session
  dir it listed, a hash it derived). And a restore may only touch a path the
  store **already has a version for**, which is the only target guard available
  to a module that cannot see the view: a crafted `file` param reaches nothing
  the agent never edited. Directory targets are refused outright, and so are
  **symlinks**: `os.replace` swaps the LINK for a regular file rather than
  writing through it, so a "successful" revert left the real file untouched with
  its pre-revert content while the stash captured a file that was never
  overwritten. Refusing is chosen over `realpath`-ing first, deliberately — the
  store's key is the sha256 of the path the VIEW opened, so following a link
  would revert a path whose own timeline is a different chain, and silently
  editing a file the user did not name is worse than declining to.
- **FH-9** **A confirm step is mandatory, because the content it overwrites is
  often the only copy.** Current on-disk bytes are frequently in NO checkpoint
  (FH-3), so a naive restore vaporizes work with no undo — the sharpest hazard in
  the feature. The plan payload reports `unique_current` (no version holds what is
  on disk) and the view gates an explicit warning on it, alongside byte counts,
  line counts and the line delta, before any write.

  **The plan also reports whether a stash will actually be kept** (`stash`,
  `stash_note`), computed by the same predicate the write runs — a stat, a decode
  and an access, so there is no reason for the sheet not to know. Without it the
  sheet carried a permanent hedge ("a copy is kept ... unless too large or not
  text"), which made the one genuinely unrecoverable combination —
  `unique_current` AND no stash — read exactly like the safe case; the write then
  destroyed the only copy and the user learned about it in the past tense, beside
  "Reverted to v3". That combination now gets **stronger wording plus a button that
  says what it does** ("Overwrite permanently" / "Delete permanently") rather than
  merely "Revert". It briefly also required a tick-to-confirm before the button
  enabled; the owner removed that as friction, so the confirm button is enabled
  unconditionally and the escalated warning is the **only** thing between the
  click and unrecoverable loss — which is why it carries real visual weight in
  that case instead of reading as a footnote. Note this is a UI-side signal only:
  the bridge's `confirm_unique` token below is a separate mechanism, derived from
  the plan and never from any widget state, so removing the tick did not weaken
  the programmatic guard at all.

  **The bridge enforces the gate too, not just the page.** `revert` requires the
  plan's `id` echoed back (also a freshness check — a plan built against one disk
  state and applied against another is how a user confirms one diff and gets
  another) and an explicit `confirm_unique` when the plan set `unique_current`,
  refusing as data otherwise. The previous guard was a source grep asserting one
  `action: "revert"` call site, which pins today's template rather than the bridge,
  so any future second caller inherited an unguarded file-destroying entry point.

  The delta is stated as **what
  the restore does** — lines it introduces, lines it takes away — because that is
  the number a confirm step has to show; the reverse framing reads identically on
  symmetric edits and lies on every asymmetric one. Above a byte cap, or for
  content that is not UTF-8, the delta degrades to net counts (or none) and
  **says it is inexact** rather than implying a diff nobody computed: difflib is
  quadratic in the worst case and a timeline renders every version.

  **The sheet shows the diff itself, not only the aggregates** (`diff`:
  `{lines, changed, truncated, reason}`). Bytes-now / bytes-after / `+N / −M`
  answer how MUCH changes and never WHAT, and on the one destructive action in
  this view the second is the question being confirmed. It rides on the **plan**
  rather than on a fourth action, deliberately: the plan already enriches, already
  runs on an explicit click, and its `id` is already the freshness token the write
  demands back — a separate `action="diff"` would diff a second scan and reopen
  exactly the confirm-one-diff-get-another gap above. Same direction as the delta
  (disk-now is the `from` side), headers naming the **checkpoint and its session**
  rather than the store's hashed path (which the user cannot open or act on), and
  the same `DIFF_BYTE_CAP` guard — over the cap, or with either side not UTF-8,
  there is no diff and `reason` says which. A delete diffs as every current line
  removed, and an absent target as every target line added; both fall out of
  `_current` modelling absence as `[]` lines. Capped at `DIFF_LINE_CAP` (400)
  lines with `truncated` set and `changed` counting the **full** diff, so the view
  states what it is not showing instead of presenting a prefix as the whole
  change. `reason` is also how a genuinely EMPTY diff arrives (an explicitly
  clicked version holding exactly what is on disk): an empty box reads as a broken
  diff, so no-diff is never silent. `ok: false` plans carry no `diff` key at all.
- **FH-10** **Second line of defence: the pre-restore content is stashed in the
  target's own `<file>.json` sidecar**, under `revertStash`, through the same
  read-merge-write `annotate.py` already uses — so `claudeSessions`,
  `bookmarkHistory`, `comments` and every other unowned key round-trip. Never
  into the Claude dir (FH-6). Bounded to a few entries and skipped above a byte
  cap or for non-text content, because the sidecar is a small JSON file three
  other writers rewrite constantly, not a version store — `file_history` already
  is one. A skip is **reported** (FH-9) so the confirm step can escalate rather
  than silently losing the net, and a revert the user confirmed — having been told
  whether a copy would be kept — is never then blocked by a sidecar that could
  not be written.

  The stash is **byte-faithful**: the target is read in BINARY and decoded
  explicitly, and the recorded `size` is the byte count. Text mode applied
  universal-newline translation, so a CRLF file stashed as LF — the recovered
  content was not the bytes that were destroyed, and it disagreed with the `size`
  stored beside it. This package ships a `windows/` dir, so CRLF is a live case,
  not a hypothetical. Each of the three refusals (too large, unreadable, not
  UTF-8) is reported separately and truthfully; folding an EACCES into "not UTF-8
  text", or a `getsize` failure into "nothing on disk to stash" (which reads as
  "the file is absent"), describes a fixable machine problem as a fact about the
  content.
- **FH-11** **Every failure crosses the bridge as data.** Anything raised out of
  a template's `main` becomes the red traceback overlay, and "no store on this
  machine", "no versions for this file", "already matches the latest checkpoint",
  "read-only mount", "stale version id" are all ordinary states of this surface.
  So each is an `{"error": ...}` or `{"ok": false, "error": ...}` dict, and the
  timeline payload carries its own `note` for the empty states. A missing store
  entirely hides the panel — that is "this feature does not apply here", not an
  empty state worth chrome — while a store that exists and holds nothing for
  *this* file gets a line of text, because there the absence is a fact about the
  file. Unreadable session dirs, stray non-directories in the history root and
  malformed `@vN` filenames are skipped, never fatal; only the exact decimal form
  the store writes is accepted, so `@v01` stays invisible rather than becoming a
  second, ambiguous "version 1".

  Two distinctions inside that, both of which were collapsed at first. An
  **unreadable** store or session dir is NOT the same as an empty one: it is
  reported with its path and errno, because `chmod` is actionable and "no versions
  for this file" sends the user looking at the wrong thing. And a **missing**
  `appenv` degrades to the pure `os.access` rule (there is no flag to consult),
  while a read-only-mount probe that RAISES fails **closed** — a blanket
  `except Exception` around the call re-opened exactly the incident the probe
  exists for, letting a malformed `FUSED_RENDER_RO_MOUNTS` fall through to the lie,
  report `ok: true`, and surface the 403 later at an async upload this UI never
  sees.
- **FH-12** **It lives in `templates/shared/file_history.py`, stdlib-only, and
  is reached by `sys.path`, not by importing the package.** Same reason
  `appenv.py` sits beside it: a template child under the fused engine has **no
  PYTHONPATH**, so `import fused_render...` always fails there — which is exactly
  why `annotate.py` reaches appenv this way. A `fused_render/file_history.py`
  would be unreachable from the only place that needs it. A copy of a template
  folder taken without its `shared/` sibling degrades to "revert not offered",
  the same shape appenv already has.
- **FH-13** **Non-goals.** No writing to the store, no restoring the store
  itself, no per-hunk revert, and no reconstruction of what an individual edit
  changed — the store holds checkpoints, not edits (FH-3), so a per-edit undo is
  not derivable from it. The `claude` and `history` templates adopting the reader
  is later work; nothing here is annotate-specific except the UI.
- **FH-14** **A dropped version is surfaced, and it BLOCKS the automatic choice.**
  A version that cannot be read (or stat'd) is excluded from the timeline, and
  under the positional rule an exclusion moves both where the walk starts and
  where it lands — so it cannot be a silent `continue`. Every drop is recorded in
  `skipped` with its path, reason and errno and named in the payload's `note`; and
  when a skip sits at or newer than the chosen target (or its own time is unknown,
  so its place in the chain is unknowable), `revert_plan` **refuses** rather than
  walking to a different point in history and presenting it as "the last change".
  Older skips are deliberately harmless *when a target exists*: one unreadable
  ancient checkpoint must not cost the user their undo. An **explicitly chosen**
  row is never blocked — the user named that version, so there is nothing to guess.

  When there is **no** target, every skip blocks, and that is not the same
  situation wearing the same message. "No target" means the walk found nothing
  older that differs — and a version it could not read is precisely a candidate for
  the older differing entry it did not find, so there is no subset of skips that
  could not matter. Claiming `at_earliest` from a scan with a hole in it would
  assert terminality that cannot be proved, the same class of error as the
  oscillating rule: a confident answer from an incomplete read. So the state is
  reported as **`unconfirmed`**, distinct from `at_earliest` and mutually exclusive
  with it — the first is a fact ("there is nothing older"), the second an admission
  ("whether there is anything older is unknown") — and it gets its own wording
  rather than the misleading "the last change cannot be identified", which
  describes the target-exists case. Both are equally terminal for the button:
  `unconfirmed` is a STABLE state that would refuse identically on every press, so
  the panel disables the button and shows the reason instead of leaving a live
  control whose only behaviour is to reproduce the same error. One `_selection`
  helper computes position, target, `at_earliest`, `unconfirmed` and the blocking
  set for both `timeline` and `revert_plan`, so the panel and the plan cannot
  disagree about what the button will do.
- **FH-15** **The panel's disclosure state and the last outcome survive the
  post-revert reload, through `sessionStorage` keyed by the target path.** A
  successful revert changes the file, so the shell's own fs-event watch reboots the
  whole preview — which left the panel collapsed with the outcome discarded, so a
  revert that worked looked like nothing had happened. Not a URL param, and the
  reason is this template's own contract: `comments` lives in the URL *precisely*
  so a review can be shared, whereas whether a disclosure widget is open is a
  workspace habit (the same argument that kept pane geometry out of the URL in
  D185), and a transient "Reverted to v2" carried in a bookmark would be a lie the
  moment it was opened.

  The disclosure state is sticky; the outcome is a **carry slot** with exactly one
  writer and two readers that both clear it — `carryOutcome` (called once, before
  the refresh, because the fs event is already racing by then: written after, it is
  lost exactly when the reload is fastest), `takeCarriedOutcome` (boot:
  read-and-clear) and `dropCarriedOutcome` (in-page: spent, because it was
  displayed here). **Nothing writes after a read**, which is the entire lifetime
  rule and the thing the earlier version could not express: clearing only on boot
  meant a reload that never arrived — an unwatched mount-backed file, the user
  navigating away, the race simply lost — left the note behind for the NEXT open of
  the file to present as fresh news, and a failing refresh on a dying page could
  write a composed message back after a newer boot had already consumed it. Only a
  SUCCESSFUL revert is carried: a failure changed nothing on disk, so there is no
  reload to bridge and no reason to greet a later visit with it. This mechanism
  produced three separate defects (ordering, the count/enrich jump, lifetime), so
  the third fix was a restructure rather than a fourth guard. A restored-expanded panel
  boots ENRICHED, or its row count would disagree with the list it labels; the
  refresh observes the post-revert file (the write completed before the call
  resolved), so the position marker points at the version just restored; and the
  carried message is applied *after* the refresh and through the same composer as
  everywhere else, so a failed refresh is never hidden behind a carried success.
  A hostile `sessionStorage` (private mode throwing on `setItem`, a corrupt value)
  degrades to "the panel does not persist" — persistence is a nicety here, never a
  dependency.
- **FH-16** **An unwritable target never reaches the confirm sheet, and the
  reason travels with the verdict.** `writable` is a FIELD on a perfectly
  successful plan, not an error — so gating only on `plan.ok` let a read-only file
  open a destructive confirm sheet that could then fail only server-side, while the
  main button had already gone correctly dead. Both layers are closed: rows are
  inert when the target is unwritable, AND `revert_plan`'s verdict is checked
  before the sheet is shown. The plan-level check is the one that matters, because
  it closes the class rather than one entry point — the same reasoning that made
  the bridge, not the page, the authority for the confirm token. The payload also
  carries `writable_reason`, because "it cannot be reverted" with no cause is a
  dead end across three genuinely different situations the module already
  distinguishes in order to reach its answer: a read-only MOUNT (nothing local to
  fix — the remote rejects the write, and `os.access` cannot see it), a `chmod -w`
  FILE (fixable), and an unwritable DIRECTORY (fixable, and a different fix).
- **FH-17** **ONE authority answers "may this action be offered, and if not
  why" — `offer_reason` — and the panel, the rows, the confirm sheet and the
  bridge all read it.** This exists because THREE findings had a single root
  cause: a guard living below the layer that decides whether to offer the action.
  The read-only verdict was enforced in `apply_revert` but invisible to the plan,
  so the sheet opened on a doomed target (FH-16). A symlink was refused the same
  way and one layer lower still, so the sidecar stash ran first — and read
  *through* the link, leaving the wrong file's content in `revertStash` for a
  revert that then failed. And the blocking-skips refusal was computed inside
  `revert_plan` while the timeline went on publishing a striped target and an
  enabled button whose only possible outcome was that refusal. Each was a
  different symptom of the same missing seam, so the fix is the seam: `timeline`
  publishes `offer` (may the button be pressed) and `offer_reason` (the sentence
  the plan would refuse with, already on screen so no press is needed to discover
  it), `revert` names a row only when that row is genuinely actionable, and the
  bridge re-reads the verdict *before* `_stash` — which must be gated rather than
  merely followed by a raise, because it runs first by design and there is nothing
  left to copy afterwards. `writable_reason` absorbs the directory and symlink
  refusals for the same reason: they are answers to "can this be reverted", and
  keeping them somewhere else is precisely what made them invisible to the caller
  that needed them.

  Two deliberate asymmetries inside it. **An explicitly chosen version is not
  subject to the automatic refusals** — "revert the last change" is a question
  this module answers and may decline to answer from an incomplete scan, whereas
  "revert to THIS version" is the user naming the target, where nothing is left to
  guess; it is still gated on writability, which no amount of naming fixes. And a
  refusal is **provisional** in exactly one case: FH-3's unenriched terminality,
  where the scan cannot see the creation boundary, so the button stays live and the
  click asks the always-enriched plan. That case is why `offer` is a field of its
  own rather than being inferred from `revert` — there is no target to name there,
  and the click is still the right thing to allow.
- **FH-18** **The write is visible while it runs, and its outcome lands where the
  click was.** Four separate ways the revert reported itself to the eye rather
  than to the file, all of them silent failures of feedback rather than of logic:

  **The sheet stays up for the round trip.** It used to close *before* the await,
  so a stash write, an `os.replace` and a full re-enumeration of the store all
  happened with nothing on screen changing, and the click read as having done
  nothing. The confirm button goes into a disabled "Reverting…"/"Deleting…" state
  instead and its label is *restored* afterwards rather than recomputed (the sheet
  chose between four wordings; rebuilding that decision is how the two drift). The
  in-flight flag also makes `closeConfirm` **refuse**, so Cancel, Escape and the
  backdrop — all three of which route through that one function — cannot pull the
  sheet, and `pending` with it, out from under the call still reading it. This is
  the one window in which the button is disabled, and it is the opposite of the
  gate FH-9 removed: it means the click was accepted.

  **A failure reports inside the sheet; a success reports on the stage.** The
  outcome used to be only an 11px muted line at the bottom of the sidebar
  (`#histnote`), reached by looking away from a centered modal that had just
  vanished — and with History collapsed it was the *entire* signal. A failed revert
  changed nothing on disk, so the sheet is still describing the truth: it stays,
  with the reason in it. A successful one closes the sheet and adds a transient
  toast at the top of the stage. `#histnote` is still written on every path — it is
  the durable copy, the carry slot (FH-15) reads it, and `reportOutcome` is the
  only thing that qualifies a success with "the timeline could not be reloaded".

  **The framed reload is covered.** The boot skeleton `remove()`d itself on the
  first load, so the post-revert `location.reload()` had no placeholder and the
  pins/highlight overlays sat in stage coordinates over an empty document — pointing
  at nothing — until the next `load` ran `render()`. The node is now kept and
  toggled (one cover, two occasions), the overlays go down with it, and the cover is
  lifted only *after* `render()`, which is the first moment an anchor has been
  re-resolved against the new document. That handler re-runs on every reload, so it
  also **disconnects the previous MutationObserver**: one left watching a discarded
  document is a leak that additionally drives renders for a document nobody is
  looking at.

  **The fresh timeline rides back with the write.** `revert` returns the post-write
  `timeline` (computed in the bridge, from the module it already holds, always
  ENRICHED — this is what the panel displays, and an unenriched one cannot see the
  creation boundary), so the row list no longer shows the *pre*-revert position for
  the length of a second round trip: exactly the window in which the user is looking
  at it to find out whether the revert worked. Best-effort and **absent** when the
  computation fails, because the write already landed and is already reported; the
  page falls back to its own `history` call, whose error channel is what tells the
  user the panel on screen is stale.

- **FH-18** **`claude` shows the snapshots AND goes back to them.** The
  panel is a COLLAPSED section of the chat template's LANDING page (`#snaps`,
  beside "Recent chats"), fed by `agent.py`'s `action="snapshots"` — a
  pass-through to `file_history.timeline`, adding nothing but the offer. The
  store stays
  strictly read-only; what a restore writes is the TARGET FILE and the sidecar.
  - **The action is not called `history`.** That name is already taken on this
    module by the chat SESSION TRANSCRIPT replay. Two meanings on one action
    name is a collision only ever found in production.
  - **FILES only, twice.** The store keys on one absolute file path
    (`sha256(abspath)[:16]@vN`), so a folder has no chain. The page gates on
    `targetNoun === "file"` — the template's ONE answer to what the target is,
    set by `paneURL` from the stat, awaited via `paneReady` because both boot
    IIFEs start together and an unordered read gets `""`. (Deliberately not
    `paneNoun`, which names the left pane's DOCUMENT — "preview"/"app" — and is
    never `"file"`.) `_snapshots` refuses a directory independently, so a
    hand-written call cannot reach a state the panel does not offer: the gate is
    the UX, the module is the guarantee (MD-11).
  - **COLLAPSED by default, and read only when OPENED.** The heading
    ("Claude snapshots", a `button` carrying `aria-expanded`) is the control and
    **wears the same bordered row as the snapshots it opens into** — full-width
    `--surface` on `--border`, radius 12, ending in a muted `show`/`hide` hint
    and the ▸/▾ caret — because a caret plus a hover state is an affordance you
    can only find by already suspecting it is there. Its type stays the section
    heading's, so it reads as a section and not as a stray button; the whole row
    is the hit target, and the hint names the ACTION rather than the state. The
    list and its note live in `#snapsbody`. "Can I get that back?"
    is a question a user arrives at deliberately, and the timeline was being
    fetched — a worker round trip — on every single file open for a history most
    opens never look at. So `mountSnapshots()` only REVEALS the heading and asks
    the backend nothing; `loadSnapshots()` runs on the FIRST expand.
    A loaded timeline is then **cached for the life of the page** (the target
    file cannot change under it), so re-opening costs nothing; a FAILED read
    caches nothing, so the next expand retries rather than leaving the section
    stuck on the failure. The one event that appends to the chain from here is a
    **revert**, and that already repaints from the post-revert timeline the write
    returned (falling back to a refetch) — so the cache is never the stale copy.
  - **The read does not enrich.** Enrichment reads session transcripts (5 MB+),
    so the panel takes the unenriched timeline — which is why it never claims a
    chain is complete (FH-3). (A `snapshot_plan`/`snapshot_revert` always
    enriches: it is paid once, on an explicit click, and an unenriched plan
    cannot see the did-not-exist boundary.)
  - **The panel is offered on EVERY path onto the landing page**, the boot with
    no session and "Back to chats" alike. A page opened straight into a resumed
    chat (`?session_id=`) never runs the boot's landing branch, so a panel
    mounted only there could never appear for the rest of that page's life.
    Returning to the landing page does **not** re-collapse an opened section or
    drop its cache: it is the same file's history either way.
  - **Every absence is a LINE OF TEXT inside the opened section**, the reader's
    own `note`: "no store on this machine" (Claude Code has never run here) and
    "a store with nothing for this file" are distinguished by that sentence
    rather than by whether a panel exists. *This overturns the earlier "no store
    at all → no panel" rule, which `annotate` also drew:* it was affordable when
    the timeline was fetched before the section was drawn, and it is not once the
    section is something the user OPENED — a heading that vanishes under the
    click that opened it reads as a bug, not as "this feature does not apply".
    The reader still returns its empty states as data rather than raising, which
    is what makes a sentence available to print.
  - **A row expands to its diff and carries one action: "Go back to this
    snapshot".** A list that can only be looked at answers no question the user
    actually has. Clicking a row fetches `action="snapshot_plan"` (never cached
    — a plan is a statement about the file as it is NOW, and a stale one is how
    a user confirms one diff and gets another) and renders `revert_plan`'s
    `diff`, because the counts on the row answer how MUCH changes and never
    WHAT. The confirm is INLINE — the button becomes "Really go back? Yes / No"
    — so the diff being confirmed stays on screen; no sheet, no modal.
  - **The two-call contract is annotate's, unchanged (FH-13/FH-14).**
    `snapshot_plan` describes, `snapshot_revert` applies and is handed the
    `id` the plan returned — it never picks a version itself, and refuses
    outright without one. `unique_current` (the bytes on disk are in no
    checkpoint, so the write destroys the only copy) additionally demands
    `confirm_unique`, which the page sends ONLY for that case: a token passed on
    every call is a token nobody reads. Before the write, the current content is
    stashed into the sidecar's `revertStash` (`STASH_KEEP` 3, `STASH_BYTE_CAP`
    256 KB) — and an unwritable target is refused BEFORE that stash, or a failed
    revert would still have mutated the sidecar. The write answers with the
    POST-revert enriched `timeline`, so the list never spends a round trip
    showing the pre-revert position back to the user who just clicked.
  - **A refusal is shown in the row, not by hiding it.** An unwritable file, a
    read-only mount, a version the store can no longer resolve: the row stays,
    states the reason, and offers no button — a row that vanished would read as
    a bug rather than as an answer.
  - **Every snapshot action refuses a MOUNT-BACKED path first, before any
    stat.** The bytes under the mounts dir come from a remote over FUSE and a
    kernel stat on a wedged mount hangs the worker; `condition.py` already keeps
    the whole chat template off those paths, and `_snap_target` is the module's
    own guarantee of the same answer (cannot tell → refuse, CT-12).
