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
- **FS-5** Opening a file shows its preview (§5); opening a directory navigates into it. **What OPENS is a double-click (or `Enter`); a plain press only selects — on every row, in every folder, at every window width** (FS-15). *This clause used to say a single click opened, with a second click model that took over "with the split preview pane on". Both are gone: two press models chosen by a pane the user no longer switches on (FS-9) meant the same click in the same folder opened a file or not depending on how wide the window happened to be.*
- **FS-6** The current directory/file is reflected in the URL path so browser back/forward and refresh work: `http://localhost:1777/view/<url-encoded-path>`.
- **FS-7** **DONE (M14):** in-folder filename search over a streamed recursive walk — see §22.
- **FS-8** "Open raw" escape hatch for any file: streams bytes with correct MIME type (used for download and by templates for images/video/pdf).

### Split preview pane (D185)

The listing may show the selected entry's preview beside the list — Finder's list view. This is the one feature the deleted `preview` directory template had and the shell did not (D185); it lives here so it inherits the listing's watch (LS-1), file operations (§24), multi-select, streaming search (§22) and theming (§30) instead of re-implementing them.

- **FS-9** **THE PANE IS NOT A CHOICE AND NOT A MEASUREMENT — a `Listing` that has a pane has one, at every width** (D282). The whole of "is there a pane" is *which* `Listing` this is: not an embedded one (the pane's own `_listing` mode — no nesting), not a frozen-tree snapshot, not a panel pane. Those are facts about the SURFACE, and each is the host-side question a measurement could never answer: a snapshot's listing and a panel pane's are both top-level listings handed a column by someone else, and both were wide enough to pass any threshold while being exactly the cases that must not split. **There is no width GATE and no visibility threshold anywhere in this path.** *A container measurement does exist again (D283) — the split container is observed so the companion's SHARE can step to 50% on a small one (FS-12) — and it decides how wide the pane is, never whether there is one. The distinction is the whole of FS-9: presence is a property of the surface, proportion is a property of the room.* *This retires `PANE_SPLIT_MIN_W` (700 px, measured on the split container) and everything that hung off it, on the owner's instruction to remove the breakpoint logic outright — the same instruction that flattened the width to one share (FS-12). **The consequence is stated rather than hedged, and it has two regimes.** Down to about **285 px** of container — the pane's 220 px floor, the 5 px divider and the list's 60 px floor (FS-12) — a narrow window shows a narrow listing beside a floored pane instead of the whole listing, and the way out is `_side=off` (FS-10), the same control it is on a wide one. **Below that the split OVERFLOWS HORIZONTALLY**: the floors no longer fit, `.listing-split` is a plain flex row with no `overflow` rule, and nothing clamps the RENDERED fraction — only the drag is clamped (`dragPaneFrac` refuses a container under 280 px, which is a rule about recording a choice, not about painting one). The old gate made this unreachable by refusing to split at all under 700 px. *Whether it stays reachable is deliberately left open: clamping the rendered fraction, or withholding the pane under the floor sum, is a width condition of exactly the kind this decision removed, so it is the owner's call rather than a fix to slip in beside it. Reachable in practice only in a very narrow embed or a heavily zoomed window.* The gate existed to answer "is a half-width listing beside a half-width preview still worth reading" — a judgement the app was making on the user's behalf, and the answer it is now allowed to make for itself.* *Before the gate there was a **Default OFF** clause and everything that hung off it. The pane used to be opt-in per folder, and that one bit lived in three places that had to agree — a toggle button, a sticky `?preview=true` URL param carried across directory navigation, and a `pane=0` view-state key so a folder remembered being closed. Three writers for one bit, and the bit was almost always a proxy for a question the layout can answer itself.*
- **FS-10** **THE SURFACE DECIDES WHETHER THERE CAN BE A PANE; the user decides what it shows and whether it is up.** The surface half is FS-9's three flags and nothing else — no width in it, so nothing to re-decide on a resize and nothing that can disagree with what is on screen. *A resize does re-decide the pane's SHARE (FS-12's 1000 px step, D283); it can never re-decide its existence.* The *user's* half is **`_side` on the folder URL** (`listing/pane-side.ts`): which of the pane's **three modes** it is showing — **Preview** (the selected row's own default view, FS-11), **Claude** (the chat, `chat_only=1`, about the selected row) and **Git** (the OPEN FOLDER's working tree, which belongs to the folder and not to the row) — or `_side=off`, that the user has shut it. **An ABSENT `_side` means OPEN at `preview`**, deliberately the opposite of the file preview's sidebar, where absent means closed: the pane is not an extra beside a complete view, it is the folder view's other half, and every folder URL, bookmark and recent predates the param. `pane.on && sideState.open` is therefore the pane on screen, and an unknown value (a stale `_side` carried in from a file view) falls back silently, as an unknown `_mode` does — **to the first mode on offer, which is not always `preview`**: a selected FOLDER row has no Preview at all (FS-11, D281), so there the absent-or-unknown param resolves onward to Claude. An explicit choice that this target cannot offer is left in the URL untouched, so hopping out of a repository and back in does not silently reset the pane. There is still no separate mode, no `_mode` value and no registry entry for "listing with a pane" — the pane is a property of *how this folder is being viewed*, not a different view of it (precisely the distinction the `preview` template got wrong). The pane's **header strip** is present in every pane state (loading skeleton, error, metadata card, multi-selection placeholder, self target), and it is the PANE's bar rather than the row's: the way out of the column at its left end, the three-mode pill at its right (`SideChrome`, the one component both this pane and the file preview's sidebar render), with the previewed row's identity between them. **Closing and reopening is ONE AFFORDANCE IN TWO PLACES, chosen by state:** closing is the chevron in that header, on the seam the column collapses toward; reopening cannot live there, since a shut column hosts nothing, so that half is a mode-icon button in the listing's **search row** — the folder's own chrome, beside the folder's own search box — rendered *only* while the pane is shut, wearing the icon of the mode that would come back. Both on screen at once is two buttons for one bit of state a few pixels apart across a divider, which reads as a rendering fault rather than as a choice. *This clause used to specify the toggle as **one affordance in two places, chosen by state**: an icon-only "Hide preview" as the first item of the pane's header strip (on the seam it collapses toward), and a labelled glyph + "Preview" button in the listing's search row for reopening, the two never coexisting. **What was deleted is the TOGGLE, not that shape.** An on/off button for a bit the layout can answer itself went with `?preview=true`; the two-place, chosen-by-state affordance above is a **mode** control — it says which of the three would return, which is a question no measurement answers — and it keeps the rule that killed the old one, **closing needs a way back**, because its reopening half renders exactly while the pane is down. The **drag-to-close gesture** stays deleted: a divider dragged to the right edge shut the pane with no reopen affordance in that era, so it stayed shut until the window was resized. The drag now just holds at the pane's floor, which is what the clamp already did all the way to the edge.*
- **FS-11** **Selection-driven content, in the pane's `preview` mode.** Only this one of FS-10's three modes is about the selected row's own templates — `claude` is the chat aimed at the row, `git` is the open folder's working tree and does not follow the selection at all — and in it the pane renders whatever the currently selected row is. **THE PANE FOLLOWS THE LEAD ONCE IT HAS SETTLED** (`listing/pane-settle.ts`, 250 ms): a move from rest — a click, the first press of a key — reaches it at once, but a move made mid-burst re-arms, so a held arrow key mounts only the row it stops on. *A pane mount is an iframe load, and on the `claude` side that iframe spawns `agent.py` through `/api/run` before it can draw, so chasing a held key down a listing meant one subprocess per keystroke — on a path with this repo's fork-crash history. Same "settle before acting on the lead" as the `?sel=` write (FS-16).* The rows below are what a settled lead resolves to:
  - a **file** → its default template mode (PT-8/PT-9) in an **iframe**, `/render?path=<template>&_file=<file>`, exactly as the preview view builds it (PT-2) — one code path, so a file previews identically in the pane and full-screen. **No switcher over the ROW's templates** — the pane shows the default, and full-screen is a double-click on the row (FS-15). *A per-row switcher did sit in this header, synced to a `_panelMode` URL param, offering every mode the row resolved (image/photos/pano for a `.png`). It was retired when the header became the pane's own three-mode bar (FS-10): two mode controls over one row, and the companions in the new bar are not row views at all. What is genuinely lost is picking a NON-DEFAULT content template inside the pane; the expand button opens the row full-screen, where the content switcher lives. Nothing writes or reads `_panelMode` any more.* "Default" means PT-9's rule in full, including its tail: the first **unconditional** entry renders immediately (CT-12 — no waiting on a gate), and an **all-conditional** list resolves its gates and shows the first **allowed** one, so the pane never claims "no preview" for a file that opens fine full-screen. A file with no usable entry at all (empty list, or every gate denied/broken — fail closed) gets the metadata card below. **The pane never edits the mode list on account of its own width** (PT-15): it is a narrow host — 220 px at the floor, and 30 % of the container by default (FS-12) — and a template whose layout needs more than that collapses itself at its own breakpoint, which is why nothing here knows how wide any template wants to be.
  - a **directory** → **no `preview` at all** (D281). A folder is not a thing this pane previews: rendering the page it holds is what D280 stopped, and the only other candidate — the embedded listing peek — is the listing already open on the left. So the pane's three-mode pill (FS-10) drops its **Preview** row for a selected folder row and the pane lands on **Claude**, the chat about that folder, sent `chat_only=1` so the chat template gives up its own preview pane (which for a folder it would fill with that folder's entry page — the same render, one level deeper). An explicit `?_side=git` still wins; only the absent param, which parses as `preview`, is resolved onward. **NO surface offers a folder a "Preview" any more**, which is the owner's bar and is met in two different ways: the pill drops the row above, and no list of TEMPLATE modes ever carried one — the full-screen topbar menu, the panel/tab pane menus and the Open With menu all render `modeTitle(mode)`, and a directory's modes label as `Claude, Listing, Git, Graph, …`. **`_listing` is "Listing" there, not "Preview"** (`mode-name.ts`'s sentinel names), which is exactly what lets a folder lose its Preview everywhere while keeping a plainly-named way to its file table. *A guard reads the shipped registry and fails if any directory key ever binds a mode that humanizes to "Preview" — the deleted D185 folder-preview template's own name is the likely way that returns.* **While the folder's companion probes are still OUT the pane has NO MODE and holds its skeleton** — it does not fall back to `preview`. A probe that has not answered is not a denial, and treating it as one put the pill on "Preview" over a rendered chat (the row's own `claude` default) for the window after a folder opens, then remounted and re-spawned `agent.py` when the verdict landed. The pill's two companion rows spin in that window (CT-12's spinner), so the header says "resolving" while the body does. A FILE row is unaffected: its `preview` is its own template list, which the folder's companion gates say nothing about. **Where NEITHER companion is offered** — a mount-backed folder, whose `claude` and `git` gates both refuse, this time answered — `preview` comes back as the fallback and shows the embedded `_listing` peek: the pane must show something, and that sentinel is unconditional, renders no template and runs no Python, so the fallback is never heavier than the default it replaces. *This is the answer to "what if a folder's `claude` is denied", and it is why `_listing` must stay in the universal directory key (PT-13) however the key is ordered.* *The pill and the body used to disagree here, which is how this was found: with `claude` leading the folder's mode list (D280) the pane rendered the chat while the pill still read "Preview" — two controls naming one thing differently. D280 deletes D269's pane half: A folder holding a top-level `.html` used to be RETARGETED to that page and previewed as an ordinary file — the whole pipeline downstream (the template list, the `/render` iframe, the expand button) being the page's — on the rule that such a folder IS its page. In this pane that made a SELECTION an execution: arrowing onto a row mounted the folder's app, ran its template's Python and put a live UI with working buttons in the sidebar for a folder the user had merely highlighted. The owner's words were "we don't want rendering". The `/api/fs/list` the retarget cost per selected folder goes with it, and so does the frontend copy of the entry rule (`apps/explorer/lib/app-entry.ts`, deleted — this pane was its only caller). D269's OTHER half stands: an app CARD still opens the entry page, and the server (`app_listing.app_entry`) and the claude template (`templates/shared/app_entry.py`) keep the rule for the surfaces that ask which page a folder is. Selecting a folder was already NOT the same as navigating INTO it, which opens its listing with nothing selected (FS-16); the two now agree that a folder is a folder.*
  - a directory's **`_listing`** mode — the switch behind that default, and the default itself wherever `claude` is gate-denied → mounted as the **real shell `Listing` component, embedded** (`embedded` prop: full sorting, search and selection, but no URL writes, no global keyboard and no pane of its own — a listing inside a listing's pane is the one nesting case). *Three earlier answers are gone. It was the folder default itself until `claude` took the registry's lead (D280). Before that, a folder holding exactly one top-level page used to embed that page instead, through a pane-only `_app` sentinel that stood in wherever the registry's `app` mode was absent or gate-denied; both went with the app concept (D264), and the sentinel had to go with them or it would have become its only surviving carrier, labelled "Preview". Before either, the peek was a **read-only mini child list** (names + icons, no sort, no file ops, no search, not navigable) — deliberately read-only, because two live listings on screen means two watch sockets, two selection models and an ambiguous target for a delete. That argument lost: the pane exists to answer "what is in here" before you commit to going in, and the embedded listing answers it with the same columns, sorting and search as the folder you would have walked into, instead of a second, worse listing maintained beside the real one — which is D185's own lesson about forks. The `embedded` prop answers the half of those concerns that were about the HOST — no URL writes, a pane-local sort that never re-sorts the real listing, no global keyboard and no pane of its own — while the second dir-watch is simply accepted: the pane is showing a folder, and a stale picture of one is worse than a socket. Its `.pane-mini-*` styles are still in `explorer.css` with nothing rendering them.*
  - **nothing selected** → **the chat about the OPEN FOLDER** (D284). The subject is the folder already open on the left, and *a folder is not a thing this pane previews* — the same rule and the same words as a selected directory above, applied to the state **every folder opens into** (FS-16). So this state takes the identical treatment: **no `preview` on the pane's mode list and no Preview row in its pill**, and it lands on the first companion on offer, which is `claude` aimed at the folder (`paneSideTarget` already falls a null row back to the folder, so nothing new carries it). While the companion probes are still out the list is EMPTY and the pane holds its **skeleton** — resolving `preview` in that window is the bug D281 records, and here it would fire on every single folder open. **The `self` target survives as ONE case: neither companion offered** (a mount-backed folder, both gates refusing), where the body is the neutral hint `Select a file to preview.` (the `.pane-hint` the empty pane already had) and that hint is the pane's only possible content. *Until D284 the hint WAS this state's answer, under a pill reading "Preview" — which is what the owner reported: D281 gave a selected folder row this treatment and left the no-selection state, then FS-16/D278 made that state the default landing for every folder, so the wrong pill and a hint over a non-previewable folder were what a user met on every open.* A **multi-selection** is deliberately untouched and keeps both its Preview side and its "N items selected" placeholder: several rows are not a folder. The header carries the folder's icon and its name — no actions. *A folder's "Open as app" was once handed down into this slot; a folder has no primary action at all now, because D264 deleted the app concept itself — the mode, the button and the lone-app probe alike.* The body is the neutral hint `Select a file to preview.` (the `.pane-hint` the empty pane already had). **There is NO PER-ROW mode picker**, and therefore no row mode: the self target never builds a row mode list, resolves no default, and issues no stat — there is no row to resolve templates for. The pane's **own** pill (FS-10) is in the header here exactly as in every other state — minus its Preview row (D284), so what it offers is what the pane can be: the two companions, disabled with their reasons or spinning while their probes are out. It is the control this state used to lack. *What was removed was the per-row picker: the `/` key's peers are heavyweight opt-ins (D235/D237 put the chat on every folder), so in this state that picker offered a chat on the folder from a header that otherwise said "select something" — a `Choose view` chip pointing at a view nobody came for. Claude-on-the-folder is a named mode of the PANE now, so it is offered plainly instead of through a picker with nothing else in it.* The folder's own modes remain one click away on the **left** half (the browse chip / the folder's own view), and every entry in the folder previews on selection. **This is the state EVERY freshly opened folder is in** (FS-16, D278): a folder opens with nothing selected, so **the chat about that folder is the first thing its pane shows** (D284 — the hint was, until this state stopped claiming to preview a folder), and the state persists until the user picks a row. *It was briefly the rare case instead — while opening a folder auto-selected a row for it (D263, D240) it was reached essentially only by an empty folder or by a deliberate **Escape**; clicking the listing background still does not deselect (FS-15), so once a row is picked the pane keeps showing it.* *There used to be an elaborate rule here instead — drop `_listing`, offer the peers, but land on no mode unless the folder had a lone app of its own, with a pane-only `_none` entry before that. Hiding the picker deletes the question those were answering.*
- **FS-12** **Draggable divider** between list and pane. **The split is a FRACTION of the split container, not a pixel width** — state is `{ on, frac }`, rendered as a percentage `flex-basis`, defaulting to **`PANE_DEFAULT_FRAC = 0.3`, which is the file view's sidebar share imported by name** (`COMPANION_FRAC`, `lib/side-width.ts`): after D280/D281 a folder's pane holds the same companion the file view's sidebar holds, so **one number serves both** and neither surface spells it (D282 — the owner's "they are the same concept now"). Two literals are how they came to differ. A proportion is what the user actually chose ("about a third of the window to the list"), and it is the only form of the answer that stays right when the window resizes: the previous model resolved a measured half into pixels at mount and then never rescaled, so a pane sized on a wide monitor became the whole view on a laptop. A percentage also needs **no measurement** — it is correct before the first paint, whatever the container turns out to be — so the measuring layout effect and its unmeasurable-container fallback (`PANE_FALLBACK_W = 420`) are both gone. **The pixel floors remain, and they are where the pixels live now:** the drag runs the cursor's distance from the container's right edge through `clampPaneWidth` (pane ≥ 220 px, list ≥ 60 px, the pane's floor applied last so a container too small for both keeps the pane and scrolls the list) and stores `clamped ÷ container width`. **A container too narrow to hold both floors (< 280 px) expresses no split at all** — the clamp returns the pane's floor whatever the cursor does, so the fraction would describe the container rather than a choice (exactly `1.0` at 220 px), and one drag in a narrow pane would leave the list at its 60 px sliver on every window thereafter. There the drag moves nothing and records nothing (dragging to the edge no longer closes the pane — FS-10); above 280 px the ceiling is `(W − 60) ÷ W`, so a real drag can never reach 1. `.listing-pane-slot` and `.listing-main` carry the same two floors as CSS `min-width`, which is what holds them when the *window* — not the divider — is what changed. Only a **dragged** fraction is recorded at all (FS-13); the default is never recorded. **UNDRAGGED, THE FRACTION IS ONE STEP: 30 %, or 50 % in a container of 1000 px or less** (`companionFrac`, `lib/side-width.ts` — the same function the file view's sidebar calls, so the two companion columns cannot drift apart; 1000 itself counts as small, matching CSS `(max-width: 1000px)`, and an unmeasured container is not small). *D283 restored that step after D282 deleted it, and the argument D282 deleted it on was **wrong**: it claimed the 280 px column floor already reached the same answer, but 280 of 720 is 39 %, not 50 % — a floor stops a column being unreadable, it does not give a cramped layout the half it needs. The owner reported the gap from the case that shows it, a small browser window. **One step is not the ladder**: the 30/50/70 % tiers, their two breakpoint constants and the 700 px visibility gate all stay deleted, and this changes the pane's share and never whether it exists.* **Moving the boundary to 1000 px CLOSED the flex-basis/`min-width` disagreement it briefly had** and opened no new one: at 720 the pane is now on the 50 % side (360 px, well clear of the 220 px floor), and just above the new boundary 30 % of 1001 is 300 px, clear of it too. The two only disagree below ~440 px, where they always did and where both shares are floored alike. *It used to STEP with the container's width through a `defaultPaneFrac`: 30 % of a container that only just had room for two panes, 50 % at a normal window (`PANE_MID_W` 1000), 70 % once it was wide (`PANE_WIDE_W` 1440), with a `220 px ÷ container` floor folded in so the flex-basis could not disagree with the CSS `min-width`. The argument for it was that a single fraction is wrong at both ends — half of 720 px being a preview too narrow to read, half of 1920 px being 960 px of file names — and the steps were the answer. **D282 deletes all three tiers and the measurement they needed**, on the owner's instruction to remove the breakpoint logic: the companion is a companion at any size, the pixel floors below still stop a 30 % pane from being unusably narrow, and a user who wants a different proportion drags the divider. Both breakpoint constants and the `ResizeObserver` are gone rather than left computing something nothing reads.*
- **FS-13** **NOTHING ABOUT THE PANE IS STORED — not in view state, not in `localStorage`, not in `sessionStorage`. Whether there is a pane follows from the surface; what the user chose is `_side` on the URL; the dragged width lives in memory for the session and nowhere else.**
  - **Whether there is a pane → not persisted at all**, because it is not a choice (FS-9/FS-10). It is three flags about which `Listing` this is, evaluated on every render, so there is nothing to write, nothing to restore, and nothing that can disagree with what is on screen. *This sub-clause used to say "recomputed from the container on every measurement"; D282 deleted the measurement, and the property it describes is stronger without it.* **The user's half — which of the three modes, or that the pane is shut — is `_side` on the folder URL and nowhere else** (FS-10): it belongs in the URL for the reason SB-2 gives, that a bookmark captures what the right side currently shows, and `_side=off` is spelled as a word rather than as a deleted param because an absent one already means something different here (open at `preview`). Reopening keeps the mode the pane was shut on, and only "shut" is written down: the open-at-default state gets the clean URL, per PT-9's rule that selecting the default deletes the param. *This replaces **`?preview=true` on the shell URL** (that literal spelling) plus a `pane` view-state key, with the URL authoritative on mount, the view state consulted in its absence, and the param carried by `navigate()` onto directory targets so folder-to-folder movement never silently opened or closed the pane. All of it is deleted: the param, its stickiness, the reflect-into-the-URL effect, and the `pane` key. **A stale `?preview=true` in an old bookmark is inert** — no reader remains — which also retires the D72 ancestor-climb caveat that kept the param off file targets. SB-2 still captures the URL verbatim; there is simply no pane state in it.*
  - **Width → a module-level variable, for the lifetime of the DOCUMENT** (`listing/pane-store.ts`), never the URL and **never storage of any kind**. One fraction, shared by every folder and every file. It survives everything the shell does, since the shell navigates by `history.pushState` and never reloads: folder → folder, folder → file, Back and Forward all keep the width. A **refresh clears it** and the pane returns to the flat 30 % (FS-12), and that reset is the *feature* — once dragged, a pane keeps that proportion for the whole session, and the way back has to be something a user can find without being told about a gesture. Reloading a page is that. `sessionStorage` would survive the refresh and `localStorage` the browser, so neither is an option, and there is deliberately no double-click-to-reset gesture. Only a **dragged** fraction is recorded (FS-12), at three decimals — a tenth of a percent of the container, well under a pixel on any window.
  - ***Width used to be PER FOLDER, and that was the bug.*** *It lived as a `panew` key in the per-path view state (`lib/viewstate.ts`), alongside the folder's sort, on the reasoning that two sibling folders should keep independent splits. In use that reasoning does not hold: a width is a statement about this window and this pair of panes, not about whichever folder was open when the divider moved. The result was that the divider **jumped on ordinary navigation** — out of a folder you had dragged, into a sibling you had not — snapping between your width and the breakpoint default on every hop. The file preview's own `_side` sidebar had always used a single global width and never had this problem. **Every stored `panew` is purged**, once, at `listing/pane.ts` module init (`purgeViewStateParams`, which also clears the older `pane` key); nothing is translated forward, because a width chosen for one folder is not a statement about the session, so every user starts on the adaptive default until their next drag. **The sort stays per folder** — that one really is a fact about the folder — which is why the purge names its params instead of clearing the map. The `parsePaneFrac` reader went with the key, and with it the rule that **legacy pixel values are ignored, not translated** (anything `>= 1` in `panew` predated the fraction model and was measured against a container this window may not have): there is nothing stored to validate on the way in any more.*

  **`navigate()` carries exactly one piece of pane state, `_side`, and only across DIRECTORY hops** — a folder hop keeps the pane as the user left it, open on the same companion or shut, which is the same stickiness the deleted `?preview` param had and for the same reason. It is a reserved (`_`-prefixed) name, so unlike `preview` it cannot shadow a template's own param through D72's ancestor-climb, and it is withheld from **file** targets for that era's other reason: a file view hosts a template iframe that reads every shell-URL param. The width gate is re-measured at the destination (FS-9) and the dragged width is already global for the session (FS-13), so both are simply right on arrival. **A top-bar (breadcrumb) directory hop still always lands on the plain listing:** it drops `_mode` rather than carrying the current folder's view onto the target — hopping out of a full-screen moded folder (`claude`, `graph`) into another folder that offers the same mode looked like nothing had happened, and the breadcrumb is how a user leaves a mode. *A sticky `?preview` param used to ride this rule: carried onto **directory** targets so folder-to-folder movement never silently opened or closed the pane, and deliberately withheld from **file** targets, because `preview` is an *unreserved* param name and the runtime's ancestor-climb (D72) exposes shell-URL params to a template iframe as global fallbacks, so a file URL carrying it would shadow a user template's own param of that name. The param is gone (FS-13), and with it that whole hazard — including the residual caveat this clause used to state rather than fix, that a template rendered **inside the pane** (FS-11) saw `preview` through the same climb while the listing URL carried it. `_side` is ONE param name on two surfaces — the file preview's companion sidebar and this pane — read differently on each (absent = closed on a file, absent = open at `preview` on a folder; `listing/pane-side.ts` states both), which is why an unknown value carried between them falls back rather than erroring. The `_panelMode` that used to ride beside `_mode` here — the pane's per-row template switcher — is retired with that switcher (FS-11): nothing writes one, so there is nothing to carry or to drop.*

  View state stays best-effort `localStorage`, silent on failure, same posture as the rest of viewstate and AP-1.
- **FS-14** **Skeleton shimmer** while the pane's content loads (the iframe's first paint, an embedded listing's own fetch) — the pane occupies real width the moment it opens, so an empty rectangle would read as "this folder has no preview" rather than "loading". The list never blocks on the pane: a slow or failing pane leaves the listing fully interactive.
- **FS-15** **ONE PRESS MODEL, and the pane does not own it.** A plain press **selects only** — for **files and directories alike** — and a **double-click opens** (navigates into a folder, opens a file full-screen), on every row in every folder at every width. A folder is deliberately not special-cased: the pane shows it (FS-11), so a single click that navigated would make that preview unreachable for exactly the rows it is most useful on. **The press is answered on `pointerdown`, not on `click`**, because rows are drag sources and WebKit does not reliably deliver the `click` after a press on a `draggable` element — which is how Shift/Mod-click once went silently dead. Deciding on the press removes that failure class rather than working around it, and it is what Finder and Explorer do: the highlight lands while the button is still down. The one case that cannot be answered on the press is a plain press on a row already inside a **multi-selection** — collapsing there would make a multi-row drag impossible, since every such drag begins with a press on one of the dragged rows — so that one **defers to the release** and collapses only if the press never became a drag or a sweep. *There used to be TWO models, chosen by whether the pane was showing: pane off, a single click selected AND opened (the classic FS-5 behaviour); pane on, it only selected. That was defensible while the pane was something the user switched on, and stopped being so the moment the split became a measurement of the window (FS-9) — the same click on the same row would open a file or not depending on how wide the window was when you clicked it.*

  **Clicking the listing BACKGROUND — the empty area below or beside the rows — does nothing to the selection.** Finder deselects there and this listing used to copy it, but the pane changed what that click costs: a stray click in the whitespace of a short listing threw away the row the user was reading and blanked the preview beside it, with no gesture to get it back other than finding the row again. **Escape** remains the deliberate clear, and the right-click background menu is unaffected.

  **`Enter` opens** — the same target the double-click does, so the keyboard keeps a binding that opens whatever the mouse model is. **Shift+click** (contiguous range) and **Mod+click** (toggle + re-anchor) are untouched by all of this: they build a selection and have never navigated.

  **No single/double-click delay timer.** Distinguishing the two clicks by waiting would put a deliberate ~250 ms lag on the pane preview — the one interaction the pane exists to make fast. Instead the first click of a double-click just selects, which is harmless: the pane fetch it starts is superseded (the row's navigation unmounts the listing), so the cost of the extra click is a request that nobody reads, not a wrong view or a flash of one.
- **FS-16** **Opening a folder selects NOTHING.** A freshly opened folder has an empty selection: no row is highlighted, and its pane sits on the folder's own **self target** — showing **the chat about that folder** (FS-11, D284), since a folder is not a thing the pane previews — until the user picks a row. There is no auto-selection over a folder's rows at all — not a function with no caller, not an effect held back by a guard (**D278, superseding D263's answer and, with it, D240's**; the halves of those that were about a SELECTED row still stand, below). *This overturns the argument both of them rested on — "the pane exists to show something, so put something in it". Guessing is a real action taken on the user's behalf: it highlights a row nobody chose, mounts a `/render` iframe (and a template's Python) for a file they may never look at, and aims the keyboard and every row-scoped action — delete, rename, the pane's expand button — at whatever the active sort happened to put first. An empty pane costs one click; a wrong selection has to be noticed first and then undone.* **Two seedings are NOT auto-selection and are untouched:** a `?sel=` on the URL is seeded into the selection at mount (`pathFromSelParam` in `useListingSelection`), so a reload and a shared deep link come back to their row; and a click made in the pre-stat provisional listing carries across the scaffold→resolved swap (`recallSelection`). Both are the user's own claim rather than the app's guess. **SEARCH still lands on its top hit** (SR-12) and is the only auto-selection left in the listing — typing a query is itself a request to look at something, which is exactly what opening a folder is not. **A directory is previewed like any other row** (D240's surviving half, overturning the original skip-directories rule): SELECTING one shows it in the pane — its entry PAGE when it is an app, else an embedded listing (FS-11, D269) — never a navigation, so landing on it is as harmless as landing on a file. *Deleted with the rule: `autoSelectPath` and its `isPageRow` page test — the one place `.htm` counted as a page where `lib/app-entry.ts` (mirroring `app_listing.app_entry` and `templates/shared/app_entry.py`) accepts `.html` only — and `selectionClaimed`, the "has something already claimed the selection?" guard that existed only to hold that one shot back.*

  **The `?sel` URL param** — the lead row mirrored into the address bar and seeded back on load — is the whole of how a row can be selected without the user clicking it in THIS visit, and it survived the removal above precisely because it is not a guess: something named that row. It **seeds the selection at mount** (`pathFromSelParam`), so a reload or a shared link comes back with the row highlighted and previewed. **A `?sel=` that MISSES selects NOTHING** (D279): a bookmark, a recents entry or an upward hop naming a file since deleted or renamed seeds a lead the folder has no row for, and the reconcile resolves it to the empty selection. *It used to clamp to row one — the vanished-row re-anchor reading the "never seen in these rows" marker (`-1`) as row zero — which selected a file nobody named and mounted a preview iframe nobody asked for, the same guess this rule removed from the folder open, arriving by another door. The re-anchor itself is untouched where it belongs: a row that vanishes WHILE the folder is open (an external delete, a rename the user made) still lands the selection on whatever took its slot, because there the user was demonstrably on that row.* **The write is DEBOUNCED** (`SEL_URL_DELAY_MS` = 300 ms, `listing/useListingSelection.ts`): it used to fire on every arrow-key press, and arrow-keying down a long folder is a burst of selection changes with no reason to record any but the last — thirty `replaceState` calls against the ~100-writes/30 s cap the listing's own `types.ts` documents. Trailing the movement spends about one write a second and still has the URL current before a user could copy it. It is **not carried between folders** (a row name from the folder you left names nothing in the folder you arrive in), though a caller may SET one for its destination — how an upward hop lands with the folder you came from selected — and an **embedded** listing (the pane's `_listing` mode) neither reads nor writes it, since the URL belongs to the host view. The selection otherwise lives in component state plus the cross-remount recall store, which carries a click across the provisional→resolved swap. Every other explorer param is untouched (`sort`/`order`, `q`, `_side`, `_mode`). *`preview` and `_panelMode` appeared in that list until they stopped existing — the pane's on/off (FS-13) and its per-row switcher (FS-11).*

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

The bar's bordered controls — the labelled mode trigger, and any `.bar-ctl-bordered` beside it — take their border and fill from `--ctl-quiet-*` / `--ctl-plate-bg` (`styles/tokens.css`) rather than from an alpha spelled into the component rule. **The same alpha is not the same contrast in both themes:** `rgba(var(--fg-muted-rgb), 0.35)` over the light bar resolves a hairline slightly darker than `--border`, and over the dark bar it resolves ~30 luminance steps *lighter* than `--border` — because `--fg-muted` is the light ink there. They read as lit pills on the dark toolbar while looking correct on the light one. In dark the border now sits a step above the app's hairline and the mode trigger's fill is transparent (a `--bg` plate under a `--bg-alt` bar was a darker well inside a lighter ring). **The pill is painted by ONE rule for both of its hosts**, the explorer's portaled actions slot and the preview header: the slot's generic `button` rule excludes it by name (`:not(.open-as-app-btn)`) rather than being out-specified, because the re-assert that tried to outrank it — `.preview-actions .open-as-app-btn` at (0,2,0) against the generic (0,3,1) — never applied, so in the explorer bar the button had always drawn as generic chrome while drawing correctly in the header. Excluding beats re-asserting: it removes the fight instead of winning it.

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
- **PY-17** A script whose project root declares **no** `pyproject.toml` (or one with no `[project]` table) runs on **the app's own interpreter** and gets no venv at all: the app ships `[bundled]` + its core `dependencies`, so numpy/pandas/pyarrow/duckdb/pillow/… are available with no download and no first-run wait — a deliberately small set since D276, which moved polars, matplotlib, scipy, the PDF stack and the geo stack out of `[bundled]` and into the manifests of the templates that use them. The interpreter is **verified, not assumed** — it is run once per server process and must report this app's own `sys.prefix`, probed under the child's stripped environment (the backend removes PYTHONHOME/PYTHONPATH, which a packaged interpreter may need to locate its stdlib). An autodetected candidate whose basename is not python-shaped is rejected without being spawned. If the direct candidate fails, the app generates a **wrapper script** that restores the `PYTHONHOME` this process depends on and `exec`s the real interpreter, then verifies THAT the same way. This is the packaged-macOS path, not an edge case: measured on a real DMG, the bundled interpreter stripped of `PYTHONHOME` reports the *build machine's* Homebrew framework as its prefix, and the bundle ships no `venv` module, so a venv-based rescue is impossible there. The wrapper sets the child's `sys.executable` to itself (`exec -a`), so a daemon re-spawned as `[sys.executable, …]` — geotiff, zarr_aoi, usd — keeps working even though those templates scrub `PYTHONHOME` from the environments they spawn into. Wrappers are POSIX-only and generated **only** when this process actually needs `PYTHONHOME`; Windows and the Linux AppImage self-locate and stay on the direct candidate. If no interpreter can be verified, such a script **fails with a configuration error** naming `FUSED_RENDER_APP_PYTHON` — it is never silently degraded to a venv, because with no baseline requirements that venv has no data stack and would fail on the first import, and because a core template that declares nothing must never reach the network. Nothing in this resolution installs anything. `FUSED_RENDER_APP_PYTHON` overrides the candidate (still probed) (D172, D175). **Both probes spawn with `close_fds=False`, and a probe that reached no verdict is not cached** (D277). They run in the server process, where PROJ is resident, so the default `close_fds=True` takes the `fork()` path and the child dies in PROJ's atfork handler at ~1ms — the crash GT-3 documents, one layer up. The probe is therefore **three-valued**, like the sibling-venv probe D212 already made three-valued and which names this one as its model: a candidate that RAN and answered (a foreign `sys.prefix`, a non-zero exit, unparseable output) is a definite rejection and is remembered, while a spawn that never got a verdict — killed by a **signal**, timed out, or failing with a transient `OSError` — leaves the resolution **unresolved and retryable**, at the cost of one subprocess on the next request. The split is by exception **type**, not by errno: a missing, unreadable or not-a-directory path is a fact about the candidate and stays definite. Rung 2 cannot launder rung 1 — the wrapper is built FROM the candidate, so an inconclusive direct probe makes the whole answer provisional even when the wrapper reached a real verdict. For the same reason `app_packages()` no longer caches its `None` when there is no interpreter: it used to, justified by the interpreter's answer being terminal, which this rule voids. Getting this wrong is not a slow path but a dead one — the resolution is per process and no HTTP route resets it, so a single unlucky spawn disabled **every** header-less script until the server restarted.
- **PY-18** A script whose **project** declares something not installed yet gets an **explicit install flow**, never a blocking download inside `/api/run`: the endpoint answers `needs_install` (venv key + the project root, its display name and its declared requirements, alongside a normal `error` object), `POST /api/env/install` spawns a detached worker that runs `uv sync` and writes `{stage, pct, detail, done, error, pid, ts}` to `progress.json`, `GET /api/env/progress?key=` polls it, and `POST /api/env/cancel` stops it by the recorded pid. **That spawn takes the `posix_spawn` path and the worker detaches ITSELF** (D292): it runs from the server process, where PROJ is resident, so `start_new_session=True` — which forces `fork()+exec` — killed the worker in PROJ's atfork handler before it could write anything, leaving an empty `worker.log`, a record stuck at `spawn`, and an install that failed identically on every retry for the life of the process. `close_fds=False` selects `posix_spawn`; `os.setsid()` as the worker's first statement restores the session `killpg` needs. **The venv readiness probe (D212) obeys the same rule and treats a signal as no verdict at all**: forked, it died `-11`, which read as "this venv cannot run its own python" and unlinked a healthy venv's ready marker — charging the user a full re-download for a crash in the probe. `runtime.js` shows the loader and retries the run **once**, so every template gets this without its own code; concurrent callers resolving to one project share a single POST, poller and progress row. Installer failures reach the user **verbatim** — uv's own message ("no matching distribution / no wheels with a matching platform tag") is the answer, never a generic engine error. Progress is deliberately coarse (`uv sync` captures its output, so per-package progress is unavailable) and reports only stages it can observe. Scope is **per-folder** (PY-16): one venv per project root, shared by every script in it — the sharing D173 deferred. Once the venv exists the run is handed its interpreter directly, so the environment can live under the app's home dir rather than in the backend's store.

---

## 6. Params & URL Sync

The core state-sharing mechanism between an HTML view and the browser URL.

- **PR-1** The **shell URL** is the single source of truth: `http://localhost:1777/view/path/to/sample.html?city=oslo&limit=50`.
- **PR-2** On load, the runtime hydrates `fused.params` from the shell URL's query string.
- **PR-3** **`fused.params.set(k, v)` writes the target window's URL, and the FIRST write of a visit that a USER GESTURE can be held responsible for costs one history entry — every other write replaces (D8, amended by D268).** The v1 rule was "`replaceState` always, no entries"; it was replaced because a param change the user made *is* a step they took, and Back had nothing to go back to. The current rule in full: the entry the page loaded on is **pristine** until a gesture-attributable write lands on it, at which point that write **pushes** a new entry (flagged `fusedParamEntry` on `history.state`, so the flag travels with the entry and survives reload) and every later write **replaces** on top of it — param churn therefore costs at most one entry per visit, and Back restores the params as they were on arrival. **The gesture condition is normative, not an implementation detail.** A write issued before this document has seen any `pointerdown`/`keydown` — a page seeding a default it computed for itself during initialization — replaces instead, and deliberately leaves the entry pristine so the visit's one push is still available for the first thing the user actually does. Without that condition a seeding view is a **Back trap**: the init-time write pushes, Back returns to the pristine entry, the view remounts and seeds again, and the re-push truncates the forward branch — the URL bounces back, `history.length` never shrinks, and the view cannot be left (D268). The runtime detects the gesture with its own capture-phase listeners rather than `navigator.userActivation`, because WKWebView is a supported host and cross-frame activation semantics differ between engines. Refresh and bookmark still reproduce state at every point, which is the invariant all of this is subordinate to. History writes are additionally **coalesced** — the value applies instantly through a pending-search overlay while the actual `replaceState` lands at most every 400 ms with a trailing flush (D99), because WebKit throws past 100 history writes per 30 s. A page that would write a param whose value the URL ALREADY means must simply not write it: a no-op write is guarded by the runtime, but a write that *changes* the URL to a state the page considers equivalent (normalising an absent param to its default) is a real change and is charged a real entry — see PT-15's `annmode`, where that cost showed up as needing two presses of Back to undo one action. **`set()` takes an optional third argument, an options object, and both knobs exist because a value guard cannot express them (D271).** `{history: "replace"}` forces the coalesced replace path whatever the gesture state and leaves the entry PRISTINE — used for stamping a true default into the URL, and for a load-time write that genuinely changes the URL from behind an `await`, where the gate is already open because the gate is read when the write LANDS and a click during a Python round trip precedes the seed. `{default: d}` declares that an ABSENT param MEANS `d`, so writing `d` over an absence drops: `set()` can only short-circuit on a byte-identical search string, and which absence means which default (absent `mode` is `edit`, absent `offset` is page 0) is per-key knowledge only the caller has. Absence is read through `get()`, so a hand-typed global (D72) counts as present and writing a local copy over it is a real change, and through the coalescing overlay (PR-8/D270), so a drop inside the 400 ms window cannot let a stale queued value land. The two compose. **A bad option THROWS and is never ignored** — silently dropping a misspelled `history` hands back the very push the option was passed to avoid, and the symptom, a dead Back press, surfaces nowhere near the cause. Two-argument calls are unchanged.
- **PR-4** Views must treat params as reactive inputs: `onChange` fires on **both** ways the visible params can change, and they arrive on two different events (D270). A **URL WRITE** — the runtime's own `set()`, or the shell's wrapped `push`/`replaceState` — arrives as `fused:urlchange`. A **HISTORY TRAVERSAL** — Back/Forward across the entry PR-3's once-per-visit push created — writes nothing at all, so it arrives only as **`popstate`**. Both route through the same snapshot diff, so a self-set still never notifies twice (D46). `popstate` is listened for on this window *and* on the target/ancestor chain, because which browsing context the event fires in depends on whose entry moved; a pending coalesced write (PR-8) is cancelled BEFORE the notification, so a listener reading `getAll()` sees settled state rather than the value the user just navigated away from. In the browser shell the traversal case was masked by App's nav-epoch remount (the view is rebuilt and re-reads its params); on a standalone `/render` page, the hosted stub and the WKWebView popover nothing remounts, and without `popstate` the URL was correct while the template's UI stayed stale indefinitely. The layout shells subscribe per embed frame for the same reason: a pane traversal must re-encode the top-level `_layout` (LM-6), or a reload or bookmark silently undoes the Back.
- **PR-5** **DECIDED (v1): strings only.** Param values are strings, period — `set()` rejects non-strings, `get()` returns strings. Users JSON-encode themselves if they need structure. Zero magic.
- **PR-6** **Reserved namespace:** param keys beginning with `_` belong to the app shell (e.g. `_file`, `_raw`). User HTML cannot set them; the runtime rejects the call.
- **PR-7** Full page refresh reproduces the exact view: same file, same params, same rendered state (assuming user code is deterministic in its params).
- **PR-8** History writes are coalesced (D99): a `set()` takes effect immediately for all readers via a pending-search overlay, but the underlying `replaceState` lands at most once per 400 ms (trailing flush; flushed on pagehide). WebKit throttles history writes to 100/30 s and throws past the cap — scrub-speed param churn in the popover's WKWebView (§25) must never hit it, and a throttle error is caught, never propagated into the calling view. **What is queued is a key→value DELTA plus the pathname it was aimed at, never a formed URL (D270)** — a formed URL is a snapshot of where the browser was up to 400 ms ago, and replaying it OVERWRITES the shell navigation, the traversal or the concurrent write that moved the location underneath it. The flush therefore MERGES into the live `target.location.search` (the raw `_layout=(…)` span preserved byte-for-byte and emitted last, D51), so every key the runtime did not touch survives by construction; and the queued write is DISCARDED, not landed, on a `popstate` or a pathname change, because in both cases the entry it was meant for is gone.

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
| `.parquet` | `duckdb`, `structure`, `h3`, `claude`, `geometry_editor` | paged grid + SQL over the file; binary — no `code` mode. `claude` is the authored-file companion of PT-14 |
| `.csv .tsv` | `duckdb`, `excel` (`.csv` only), `code`, `claude`, `reader` | paged table + SQL over the file |
| `.xlsx` | `xlsx`, `excel`, `reader` | sheet select + paged table. No authored-file pair (PT-14): a spreadsheet is not authored text |
| `.json` | `tree`, `code`, `duckdb`, `claude`, `reader` | collapsible tree; the dominant hand-authored config format, so it carries the authored-file pair (PT-14) |
| `.geojson` | `vector`, `map`, `tree`, `code`, `claude`, `geometry_editor` | map + tree over the same bytes |
| `.md` | `markdown`, `code`, `claude`, `reader` | notes editor (§32) + raw source + the authored-file companion (PT-14): chat about the note with the note itself in the left pane |
| `.svg` | `image`, `code`, `claude` | `<img>` via raw endpoint; svg source is text, so it is authored and carries the chat (PT-14) |
| `.png .jpg .jpeg` | `image`, `photos`, `pano`, `claude` | `<img>` via raw endpoint; an image asset is committed and discussed like any other authored file (PT-14) |
| `.gif .webp` | `image`, `photos` (`.webp` also `pano`) | `<img>` via raw endpoint |
| `.pdf` | `pdf`, `pdf_studio`, `reader` | browser-native embed. No authored-file pair (PT-14): a PDF is a published artefact, not authored source |
| `.mp4 .mov .m4v .webm .mp3 .wav .m4a .ogg .flac` | `media` | raw endpoint w/ Range |
| `.py` | `code`, `api`, `claude`, `reader` | editable CodeMirror; `api` = swagger-style run form over the `main()` entry point (D63) |
| `.js .ts .tsx .jsx .cjs .mjs .cts .mts .sh .zsh .fish .ps1 .csh .zsh-theme .vim .yaml .yml .toml .ini .cfg .conf .tf .hcl .css .plist` | `code`, `claude`, `reader` | editable CodeMirror. `.toml` leads with `canvas` (§28) and `.plist` with `plist`, then the same tail |
| `.txt .log` | `code`, `text`, `claude`, `reader` | editable CodeMirror, with the plain `<pre>` view a click behind it; `.log` leads with `log_studio`. `code` outranks `text` on every key that offers both: they render the same bytes, and `code` renders them better |
| `.jsonl .ndjson` | `code`, `duckdb`, `claude`, `reader` | append-only record streams. They carry the chat like any other text key: PT-14's question is "is this authored", not "does this diff well". No `git`, for the reason no file key has it (GT-2): the working tree is the folder's, not the file's |
| `.tif .tiff` | `geotiff` | GeoTIFF/COG via vendored geotiff (in-browser decode, no reader.py); full metadata + dump, photometric routing (RGB/palette/YCbCr), band select + RGB stretch + colormaps, histogram, hover. Small files full-fetched; >32 MiB range-request `fromUrl` |
| `.nc .nc4 .cdf` | `netcdf` | NetCDF-3 via vendored netcdfjs (HDF5/NetCDF-4 → graceful card); leading-dim sliders, colormaps + stretch, histogram, hover |
| `.zarr/` (directory) | `zarr_aoi`, `_listing` | Zarr v2/v3 store — a *directory*, bound by the trailing-`/` directory key (PT-13). `zarr_aoi` is the server-side AOI tile-streaming map viewer (opened via zarr-python, tiles streamed as PNG); it ships a `condition.py` store-detection gate (CT-12), so it is a conditional peer rather than the immediate default — the built-in `_listing` (PT-12) shows first and the map joins the switcher when the background gate confirms the store. `_listing` also stays reachable as the raw member listing, replacing the old "Browse contents" escape hatch (D81) |
| `/` (any directory) | `claude`, `_listing`, `git`, `graph`, `zarr_aoi`, `model_card` | The **universal directory key** (CT-3) — the built-in default for *every* folder. `_listing` is a sentinel (PT-12), not a template folder: the shell's built-in directory listing (sortable columns, in-folder search, file ops, and the optional split preview pane — FS-1, FS-9..FS-15). Zero segments, so any dot-anchored directory key (`.zarr/`) beats it (D81). Every entry but `_listing` is a `condition.py`-gated peer (CT-12) — the AOI map here is the same viewer the `.zarr/` row describes, offered to *any* folder its store-detection gate confirms — and each joins the switcher only where its background gate allows. **`claude` LEADS the row and `_listing` follows it (D280), and the two surfaces read that order differently — deliberately.** The full-screen folder view resolves the first entry *without* a gate (PT-8), so it lands on `_listing` from second place and opening a folder still shows its file table; the listing's preview pane takes the first offered mode *literally*, so a selected folder row lands on the chat. **Which is why `_listing` is DEMOTED here and never dropped:** as the row's one unconditional entry it is what keeps every folder browsable, and a key without it resolves five gated templates, sends the full-screen route to `visible[0]` — a chat — and leaves a folder with no listing and a one-entry mode menu that hides itself. **There is exactly one chat entry, and as of D237 it is `claude` for both kinds of target** (this key used to carry both `claude_split` and `claude`, the second of which is deleted). Its gate is the *weakest* on this row — any existing directory passes — but it is a real gate and not the "no gate at all" D235 specified: it refuses a **mount-backed** path, because an agent turned loose on an rclone/NFS mount walks and rewrites the tree through FUSE (PT-16, MD-11). The order is deliberate and reads left to right: **the chat** — where a folder's contents get built, and now the row's lead (D280) — then the built-in listing, then the folder-wide views. *The row used to close with `history`, the per-path timeline: directly after the chat at first, because for an app folder the version timeline was the answer and the raw commit log one click further (#361), then moved to the END by #434. The mode is deleted (PT-14) and the row no longer carries it.* `model_card` joined the row with §37 (D249) — another `condition.py`-gated peer, answering only for a Hugging Face cache folder or a model checkout, so an ordinary folder pays a verdict and nothing else. **This row used to open with `app`** — the folder rendered full-bleed as its own page, gated to a workspace app folder or a registered linked app, and pinned with `claude`/`history` into an `APP_MODES` allowlist by a route of its own. The route went with D262 and the template with D264: there is no app VIEW, and D269 brought none back — what it restored is the DESTINATION, an ordinary file view of the folder's entry page, which is where an app CARD lands. *D269 also gave that destination to the preview pane, which resolved a selected folder to its entry page and rendered it; **D280 deleted that half** — merely selecting a row ran the folder's app — so a folder previews as a folder here (FS-11) while the cards are untouched.* A folder with no top-level page is a listing, and this key is unchanged either way: **six** directory modes, no `app` row. A folder's history answer is `git` (the repo-wide Source Control view, §33, directory-only as of D235/GT-2); the single-file `history` timeline is deleted (PT-14). The chat is **full width** on a folder as of D239 — it is the one target of this mode with no left pane (PT-16). `graph` is MD-2's link graph. The `preview` folder-preview template that also sat here is **deleted** — its split pane is now `_listing`'s (D185) |
| `.html .htm` | `_render`, `code`, `claude`, `reader` | defaults shipped in the built-in registry like any other key — user-rebindable since D73 (CT-4 revised); `_render` is a shell sentinel (PT-12) rendering the file itself live (§4). A page is authored, so it carries the authored-file pair (PT-14): the chat's left pane renders the page itself and the chat edits it. This is also the key where a `?_mode=claude` link written before D235 works again for free, because D237's rename put the chat back on the file keys (`examples_seed/tutorial/`) |
| unknown | shell fallback | metadata + raw/download link (built into shell, not a template) |

- **PT-14** **ONE chat template serves both kinds of target, and the companion that used to split by kind is GONE (D235, chat half overturned by D237; the timeline half removed with the `history` template).** *Original (D235) form: four companion modes split by target kind — a directory offered `claude` + `git`, a file offered `claude_split` + `history`, with two separate chat templates. **The two-chat premise is void, and so is the second companion.*** There is now a single chat template, **`claude`** (`claude_split` renamed after the plain full-width `claude` was deleted, D237), and it is bound to **both** the universal `/` directory key and all 47 authored-file keys — 48 keys in all. The other companion those keys used to carry, `history` (a per-path commit timeline that also materialised a commit as a browsable tree), **is deleted**: the `git` view answers the same question without a second surface, because its commit list is SCOPED to whatever target it was opened on and selecting a commit renders the open file as of it (§33, GT-17 — resolved on read through `/api/git/show`, with nothing written to disk). So the companion set is now the chat plus `git`, and **`git` (the Source Control view, §33, GT-2) is FOLDER-ONLY: the universal `/` directory key and no file extension at all.** Everything `git` offers — staging, discarding, stashing, committing, branches, push/pull — is a REPOSITORY-level act, and the working tree a file sits in is its FOLDER's working tree, not the file's: you do not stash a file, you stash a tree. A file therefore has no binding of its own for it; what a file's reader wants — "what happened to this file" — is served by the SAME view borrowed from the file's parent folder into the preview sidebar (`apps/explorer/lib/dir-mode.ts`), scoped to the open path. `git` did briefly ride along on all 48 keys, and the reason was a gap in a different surface rather than anything about the mode: the explorer gave a FOLDER no mode switcher of its own, the only mode surface a browsing user had was the preview pane's, and the pane acted on the SELECTED ROW, always a file — so a mode bound to `/` alone was unreachable without hand-writing `?_mode=git`. The pane selects and previews FOLDER rows now (the folder peek, FS-10/FS-11), so the folder has a mode surface and the workaround is retired. Two rules, not 47 table rows: the per-extension lists above simply say *which* extensions count as authored files. The **authored-file set** — source, config, prose, notebooks, record streams, tabular data, geo data and image assets, 47 keys — is deliberately withheld from spreadsheets, PDFs, media, archives, 3D and generated tool files: a chat is for bytes a human authors or analyses, and those lists are left alone rather than churned. The `/` key's gating asymmetry is the visible consequence of D237 (its third party, `app`, is gone entirely — D262 deleted the app-builder route and D264 the template): the chat's gate accepts **any** directory, while `git`'s takes any folder in a work tree and refuses a file outright (GT-3), which is the binding stated a second time — so a hand-written `?_mode=git` on a file is not offered a repository-level view of something that is not a repository. The chat's own contract (its gate, its left pane's three shapes, and the system prompt that must agree with the pane) is **PT-16**. **What the deleted timeline mode leaves behind, deliberately.** It materialised a commit by `git archive`-ing it into `~/.fused-render/app-versions/<key>/<sha>/` and framing that directory, and two mechanisms outlive it because trees an older version already extracted are still on disk and still immutable: (1) `server/mount.py::_is_under_snapshot_root` makes every `/api/fs` mutation handler refuse a path under that root with the existing `readonly` contract (403 + `{"error": "readonly"}`) and makes `_writable` report `false` there, so a framed editor draws read-only mode up front instead of only failing at Cmd+S; a **copy out** is still allowed — read-only, not sealed. (2) `?snapshot=1` on an embed URL (`router.ts` `IS_SNAPSHOT` → `body.snapshot`) still says FROZEN TREE, NOT A LIVE FOLDER: it opens no preview pane of its own and suppresses the breadcrumb and the corner chips, all of which would act on a frozen copy as though it were live. **Neither has a producer inside the app any more** — nothing writes `app-versions/` and no view frames a snapshot — and that is recorded here rather than left for a reader to rediscover from dead code. What is **still rejected**: (a) keeping `git` on file keys. The original argument was "two commit-log modes for one story", which stopped applying the moment `git` dropped its commit log (GT-2) — and on that basis the mode WAS bound to every file key for a while. The reason it is rejected again is not about duplication: a file key is the wrong place for a repository-level view. Staging, stashing and pushing are done to a tree, the tree in question is the folder's, and putting that behind a single file offers the user a control whose scope is not the thing they selected. What the file-key binding was really buying was REACHABILITY, back when a folder had no mode surface — and that is now bought properly, by the preview pane peeking folder rows and the file sidebar borrowing the parent's entry, rather than by binding the mode to the wrong target. (b) keeping `annotate` as a standalone mode — its tools live in the chat's pane, so the mode was deregistered from every core key rather than left as a second, staler way in (§17), and its comment handoff is now doubly unreachable (no binding, no receiver). What is **no longer** rejected, and is the reversal itself: D235 rejected "binding ONE chat template to both kinds" on the grounds that the split pane renders a target and an ordinary folder has no app entry to render, so one template would have to branch on kind and carry a dead pane for half its bindings. It does branch on kind, in two places — the pane and the prompt — and D239 has since conceded half of D235's premise while leaving its conclusion overturned: an ordinary folder really does have nothing to render, so it gets **no pane**, not a substitute for one (PT-16). What that does not follow is that the template must therefore fork. A no-pane target is a *layout* the one template resolves — the pane is removed and the conversation takes the width — and everything the two kinds actually SHARE is the part that costs something to duplicate: the transcript, the composer, the approval cards, the permission modes, the run/resume/stop machinery, the session sidecar and the transcript restore. D235's own evidence is the argument here: the second chat template WAS that fork, and it drifted into the feature-poor twin (8 mentions of the annotation machinery against 277) precisely because a fork's two halves are maintained by whoever happens to be editing one of them. So the branch is one predicate read in two places, and the cost of the case that has no pane is a flag and a removal — not a second copy of a chat.
- **PT-15** **A template whose layout needs width is responsible for collapsing itself; the shell offers modes by *binding and gate* only — never by how much room a host happens to have (D236).** The set of modes a target gets is decided by the registry (PT-7/CT-3) and, for a gated folder, by its `condition.py` verdict (CT-12): those two inputs and nothing else. A **split-layout** template — two panes and a divider, like `claude` (the chat, PT-16) or `history` — therefore has to survive every host the shell renders it in, and three of them are narrow by design: the listing's **preview pane** (floor 220 px, default width *half* its split container — FS-12), a **Panel pane** dragged freely (§14), and **`/embed`** in a small window. The rule: the template ships a **media query** at the width its own layout stops being **useful** — the sum of its panes' minimum *useful* widths, rounded up, which is **not** the width at which they merely stop overflowing — and below it shows **one view at a time with a toggle**, the idiom `log_studio` (780 px), `map` (650), `duckdb`/`sqlite` (560) and `bundle` (640) already use. `claude` collapses at **800 px** (its useful floor: `#left` 420 + divider 4 + `#chat` 440 = 864; the breakpoint sits a deliberate notch below it, trading a slightly-squeezed band for keeping the split alive on more hosts) and `history` at **640 px** (`#side` 200 + divider 4 + a 420 px preview frame that is still a page = 624, rounded up) — the two deliberately do NOT share a figure, because `history`'s non-preview column is a 200 px commit spine where `claude`'s is a 440 px chat. The arithmetic **scopes to the targets that have two panes**, which since D239 is `claude`'s file and app-folder shapes only: an ordinary folder has no `#left`, so there is no sum to satisfy, no collapse to perform and no toggle to offer — a single-column layout is already the thing the breakpoint exists to produce, at every width. This is not an exemption from the rule; it is the rule having nothing to do, and it is why the collapse logic is short-circuited outright for that target rather than left to run against a column that is not in the document. The figures sit **at the useful floors and not the ~560 the overflow floors give** because the listing preview pane defaults to *half* its split container — ~700 px on a 1700 px window — so a breakpoint set at the overflow floor engaged the split in every host that could hold it without breaking and none that could hold it usefully; the arithmetic is written down beside the query, so the figure is checkable rather than a taste call. Three sub-rules the two built-ins establish, because getting them wrong is silent: **(a) park the hidden half, do not `display: none` it** — an iframe with no layout box gives its document a 0×0 viewport, so a screenshot of it rasterises 1×1 and every element rect an annotation pin is anchored to collapses (§17); out of flow + `visibility: hidden` + `pointer-events: none` keeps a real viewport and shows nothing. **(b) An inline width written by the divider's own JS outranks the media query**, so the collapse must neutralise it — either from CSS (`!important`) or by having the apply function skip the inline write while narrow; the split *ratio* param is never touched either way, so crossing back restores the user's width with no reload. **(c) A control that acts on the hidden half is absent, not disabled**, and any **armed** state it owns is reset on the flip — a disabled control still asserts the feature exists, and an armed control over an invisible document swallows input or attaches something the user cannot see. Only the view toggle itself (navigation, not a feature) and content the user has already authored stay reachable from both views. The toggle **names what its destination is FOR**, not merely where it goes: `claude`'s reads **"Comment on preview"** outbound and **"Back to chat"** on the return (D239; the verb was "Annotate" until D298 relabelled the whole feature "Comment" in the UI, ids and params unchanged), because the preview column is where the annotation tools live and that is the only reason a person leaves the conversation for it — "Preview"/"Chat" named the two halves and said nothing about why one would move. It stays navigation and is **not merged** with the annotate switch (§17), which arms the mode once you are there: one control moves the view, the other changes what a click in the frame does, and one button doing both would arm a mode in the same gesture that reveals the surface. The label and the `aria-label` are **one string**, since a second wording is a second thing to keep in step; the longer labels are what `#viewbtn`'s `flex-shrink: 0` and the annotate switch's own ellipsis exist for, so a 220px host truncates the mode name rather than overflowing the row or half-hiding the only way back. Which view **leads** is the mode's subject, not the wider pane: the chat opens on the chat, `history` on the commit list (a snapshot must be picked before there is a preview). **Pane-local params** are the persistence channel and stay pane-local under D72's boundary: `claude` carries `split` (the ratio), `annotations` + `annmode` (§17's notes and armed mode — and **`annmode` is written only when the URL does not already MEAN the new state**: it is effectively-on, so absent and `1` are the same answer, and the boot default normalising an absent param to `1` was a semantic no-op that still cost a history entry under the runtime's first-change-push rule (PR-3), which is why expanding the preview pane to full screen took TWO presses of Back to undo. Writing `0` over an absent param is a real disarm — a narrow pane boots that way — and still pushes; the single-writer funnel is unchanged, it just has a no-op guard), **`leftmode`** (which of the offerable stat entries the left pane frames, PT-16 — a listbox picker at the RIGHT-HAND end of the pane's own bar, showing each template's `icon.svg` beside its name, hidden below two choices, an unknown value falling back to the default silently as in PT-9) and **`paneview`** (`chat`|`preview`, which of the two the narrow layout shows, chat by default) — all four of which an ordinary folder's chat **ignores silently** rather than strips (PT-16), since they describe a layout that target does not have; `history` keeps its narrow view in a **body class only**, deliberately not a param, since which half a temporarily-narrow host shows is not state a bookmark should reproduce. What was **rejected**: having the **shell filter split-layout modes out of narrow hosts**. A pane's width is *dynamic* — the listing pane defaults to half its container, so on a wide window the split fits and the mode should be offered — which makes a host-based ban wrong in the one place it was aimed at; a width-based filter makes modes appear and disappear from the switcher (PT-10) mid-divider-drag and can yank the **active** mode out from under the user; it needs per-template width knowledge in the shell, i.e. a new `registry.json` field plus a new field on stat's template entries (which carry only `mode`/`path`/`icon`/`conditional`, PT-8), applied separately in three hosts (`ListingPreviewPane.tsx`, `PaneModeMenu.tsx`, `/embed`); and **user templates (§16) would never inherit it**, whereas a media query in the template is something a user template gets for free. **The `annmode` clause above has since GENERALISED past boot and SPLIT along D271's policy.** Generalised: every always-live control asks the same question and gets it wrong the same way — see **PT-17**, which is the rule `annmode` was the first instance of. Split: "do not write it" was the right answer for `annmode` because effectively-on is what an absent param already MEANS, but a param whose default the reader could have CHOSEN is stamped into the URL instead, with `{history: "replace"}` so the stamp costs no entry (PR-3), while a value the view DERIVES from something the URL does not record is not written at all and its MODE is the bookmark.
- **PT-16** **The chat template's contract: one gate, TWO pane shapes plus a no-pane case, and a system prompt that cannot disagree with the pane (D237, revised by D239).** `templates/claude/` is the single chat mode (PT-14). Because it is bound to two kinds of target it branches on kind in exactly two places — the left pane and the prompt — and both read the **same** predicate, `shared/app_entry.entry_html`, so what the prompt claims is beside the chat is what is beside the chat. *This clause said "three pane shapes" until D239: the third shape — fused-render's own file browser framed for a folder with no app entry — is **removed**, and an ordinary folder now gets a full-width chat with no pane at all. The predicate and the two-places rule are unchanged; what changed is that one of the two answers is "there is nothing beside the chat", and the prompt says nothing about a pane there because there is none.*
  - **The gate** (`claude/condition.py`, CT-12) accepts **any existing regular file and any existing directory**, and nothing else: `os.path.isfile` / `os.path.isdir`, never `not isdir` (the loose form also swallows every path that does not exist, and "cannot tell" must read as "refuse"), and it never lists, walks, globs or resolves symlinks, because it runs for every path the explorer stats. That reduces to "the path exists", which the shell already knows — so the gate exists for **one** refusal: a **mount-backed** path (`shared/appenv.is_mount_backed`). The bytes under the mounts dir arrive over FUSE and an agent turned loose there rewrites the remote tree, the same reason every peer gate refuses those paths (MD-11). This is a **capability deliberately removed** relative to the deleted plain chat template, which shipped no `condition.py` at all and therefore did offer a chat over an rclone/NFS mount. **Rejected:** deleting the gate outright now that everything else about it is always-true — an always-true gate would be worth removing, a gate that still says no to remote mounts is not.
  - **The left pane, TWO shapes and a no-pane case (D239).** A **file** → the file in its OWN default template: `GET /api/fs/stat` for the target, drop `conditional` entries (their verdict lives behind `/api/fs/conditions` and is deliberately not fetched — an unresolved gate reads as "not offered") and drop the chat mode itself (a pane framing the chat again is a mirror, not a preview), then frame `/render?path=<that template>&_file=<file>` — or the file itself when the entry is the `_render` sentinel. That is the shell's own `defaultTemplate` rule (PT-8) reused rather than a per-extension table inside the chat, which would drift from the registry on the next rebinding and ignore a user override (§16); and it is a **default, not a lock** — the pane-local `leftmode` param (PT-15) selects any other offerable entry from that same stat payload, unknown values falling back silently as in PT-9. **The picker sits on the pane it controls, at the RIGHT-HAND end of it:** in the split layout it is a row across the top of the LEFT column, not a control in the chat pane's strip across the divider — and it is pushed to that row's far end (`margin-left: auto`, scoped to `#leftbar`), because the bar exists to carry this one control and a lone control hard against the left edge reads as a LABEL for the pane rather than as a switch on it. It is a **listbox, not a `<select>`**, and the reason is the ICON: its rows show each template's own `icon.svg` beside the mode name, exactly as the shell's mode menu does (`templateModeIcon`), and an `<option>` renders text in every engine. The icon needs no new server plumbing — stat's `templates` entries already carry the icon's absolute path (PT-11), `/api/fs/raw` serves it, and it is drawn as a mask filled with `currentColor` so one flat glyph follows the row's ink in both themes; a template with no `icon.svg` (and the `_render` sentinel) falls back to the shell's own lettered box. The rows are real `<button>`s inside a `role="listbox"` popup under an `aria-haspopup` trigger — the idiom the `reader` template's voice menu already uses here — so focus, Enter and Space stay the platform's job and only the arrows and Escape are the template's — the same grammar the explorer's own preview pane follows (FS-10). Below the 800px breakpoint there is no persistent left column to hang a bar on, so the *same* element moves back into the shared `#anntools` strip, the one row both narrow views keep; crossing the breakpoint relocates it live, with no reload and no effect on `leftmode` itself. It is hidden entirely when the target offers fewer than two views. An **app folder** (an entry page resolves) → that entry page, via `/render`. **The entry rule is `index.html`, else the FIRST top-level `.html` in name order** (`shared/app_entry.entry_html`, `sorted` so two consumers cannot land on different pages). It used to call several pages without an `index.html` *ambiguous* and resolve to None, which meant every consumer dead-ended on such a folder — this pane drew nothing, the `app` mode drew "no entry page", and a materialised snapshot of one showed that notice instead of the app at that commit. Owner call on the user's own wording ("for multiple html files, just pick the first one"): a deterministic first page is one click from any of the others once the folder is open, and None was one click from nowhere. The consequence here is that a folder with several pages and no index now HAS a pane (and the `app_state` tool with it) where it previously had none. Everything the pane implies rides on those two and nothing else: the annotation layer (§17), the 800px collapse (PT-15) and the `app_state` tool (below). A folder with **no** app entry → **no pane at all**: no `#leftframe`, no `#divider`, no view toggle, no annotate affordance, and the conversation owns the full width. *What this overturns.* D237 framed **`/explorer/embed/<dir>?preview=false&modechip=false`** there — the chrome-free navigable shell (LM-4/D39), a real file browser beside the chat — and it was chosen as the fix for code that used to `throw` (`no app entry…`, a permanent error panel beside a working chat). It fixed the throw and left the real problem untouched: **nothing flowed back from that pane.** The template has no `postMessage` and no message listener, so selecting a file in the browser attached nothing, fed nothing to the composer and changed no agent context; annotate was hard-disabled over it by construction (no element of a file listing is a thing a pin could mean anything about); and the `leftmode` picker was inert for it, since neither directory branch populates `paneEntries`. So it was half the width of a folder chat spent on a view that reported to nobody, for a question the agent's ordinary file tools already answer. **Deliberately given up:** the `state.url` backchannel — embed navigation rewrote the iframe's path, so `app_state` could tell the agent which folder or file the user had walked into. It was the one signal that did flow back, and it goes with the pane. **Both embed params go too, and they go differently:** `modechip=false` loses its only producer in the codebase, so its plumbing is **removed from its consumer** as well (`Preview.tsx` no longer reads it and the corner chip has no opt-out) — a URL param no caller can produce is a branch nothing can test, and if another template ever frames an embed of its own counterpart's target the opt-out returns with that caller; `preview=false` was kept at the time, because the listing wrote it for itself when the user closed the pane (`listing/pane.ts`), and it has since gone the same way — the pane lost its toggle (first to a measurement of the split container, then to nothing at all — D282 deleted the measurement too, so a Listing that has a pane simply has one), and with the toggle went the only writer of that param. What the param MEANT survives it: a framed listing still may not open a pane of its own, and that rule now rides on `snapshot=1` (PT-14 above), the flag the one remaining framer already writes. With the folder embed gone, `/embed` is **no longer used as a pane by any template**, so D235's rejection of it for a FILE target stands unqualified. *The no-pane case is a designed ABSENCE, not a missing element, and the difference is load-bearing.* Shipping the markup without `#leftframe` cannot work: the frame's `load` hook is wired at top level, so with no element to wire that statement throws a `TypeError` and aborts **every declaration after it** — the agent poll loop, the annotate switch, the composer wiring — and the boot `catch` cannot report it either, because its own first statement removes that same missing element, so the throw lands inside the catch and neither the error panel nor `pushAppLog` runs. A blank page with a working-looking composer. So the markup ships the column exactly as it does for a file, every declaration initialises against it, and `enterNoPane()` takes it away **afterwards** — ordering that is guaranteed rather than hoped for, since the template is one `<script>` and the loader reaches that branch only after `await`ing a fetch. Three subtrees are removed (`#left`, `#divider`, and `#anntools` — which is a child of `#chat`, so it survives removing the column and would otherwise sit there as an empty bordered row of controls for a pane that is not there). One `noPane` flag then short-circuits `applySplit`, `applyNarrowView`, `renderAnn` and `annSetMode`, so nothing writes to a detached node or to a param describing a layout this target does not have. **Stale params are ignored SILENTLY and never stripped:** `split`, `paneview`, `leftmode`, `annmode` and `annotations` left on a folder URL by an old bookmark open a full-screen chat with no error — the same forgiving posture PT-9 takes for an unknown `_mode` — and rewriting them would break that bookmark's round trip for the day the folder grows an `index.html` and gets its pane back. **Also enforced, not documented:** `appEntry` (the only field in the app-state payload that distinguishes the user's real app from our own UI) is never set on this path, and the "pane unreadable" sentence no longer names "no app entry" among its causes, because that condition now produces no pane and therefore no tool to ask. **The composer's screenshot BUTTON (D285), which is the second version of this control.** Both composers carry `#viewshot` / `#hviewshot`, a camera pill that **captures on click**: one picture of the entire visible pane (`shotCapturePane`, caps `SHOT_VIEW_EDGE` 1600 / `SHOT_VIEW_BYTES` 900 KB, uploaded into the same shots directory the crops use) which then hangs above the composer as a **chip with a thumbnail**, in the same row as the annotation chips and removable with the same ✕. On send it rides the message as the `<pane-shot>` block (`paneShotBlock`, `composeOutgoing`'s fourth argument), placed after the app-state block and before the annotations so `stripAnnBlock`'s position-zero preamble still matches. The first version was a per-message **toggle**, and it was deleted — "it doesn't make sense": what went out was a picture nobody had seen, of a moment nobody chose, behind a switch that had to be noticed, armed and re-armed every turn, and the capture ran during the send where a failure could only degrade silently. Capturing on click answers each of those in turn — the picture is visible before it goes, the moment is the user's, a failure becomes a chip that says so, and nobody who does not press the button pays a rasterise, an encode or a file. **Seeing the picture (D286), the pass that followed the first user test — which shipped the feature and failed the user: "not obvious that it took a screenshot", "no way to preview it before sending", "not intuitive".** Four answers, all in the template. (1) A **shutter flash** — a white sheet over the photographed pane, 340ms, opacity-only, appended to `annHl.parentNode` (our `#leftview` split, the injected shadow root hosted) and animated with the Web Animations API rather than from either stylesheet; it fires on the click, BEFORE the capture, and cannot reach the capture because `cloneNode` does not clone a shadow tree. The chip's own entrance animation is the second half, for the eye that has already moved to the composer. (2) A **viewer** (`#shotview`): every thumbnail this template draws — the pending chip's and every sent turn's — is a real `<button>` built by one `shotThumbBtn`, and opens the picture full size with the path, the `viewNote` caveat that had ridden the wire since D208 with nowhere to be said, Discard (offered only while the shot is still `paneShot`, by identity) and Close; Escape leads the `escapeAction` precedence because it is modal. Clicking the picture swaps fitted ⇄ natural size with the box scrolling, because `position: fixed` is the TEMPLATE's viewport and in the sidebar that is a ~440px column a 1600px capture fits into at ~330px. (3) A **sent turn keeps its picture**: one `shotReceipt` builds the row for a live send and for a restored one, and `paneShotIn` reads `{view, viewNote}` back out of the `<pane-shot>` block so a reopened session renders the shot from `fused.rawUrl(path)` → `/api/fs/raw` (verified end-to-end), with a pruned temp file saying so in words rather than showing a broken-image glyph. (4) **Discoverability**: the button moved from among the three dropdowns (where it read as a fourth setting) to the seat beside Send, the glyph became a camera rather than a framed landscape, and the tooltip became one verb-first sentence. Shipped with it, because the same wire was leaking one surface over: **`sessionTitle`** — the chat list named conversations "<pane-shot> The user attached a pi…" and "The user annotated 1 element in the l…", since a session preview is the head of a message that BEGINS with a machine-written block and is truncated before the closing tag any strip matches on. **Visibility follows `annCapable()`**, the annotate switch's own question, so the buttons are hidden where the host shows nothing marked and removed outright by `enterNoPane`; the narrow chat view hides the BUTTON (a view showing no preview offers no features of the preview) and keeps the CHIP (it is chat content, about to be sent). **What was already there and was reused rather than reinvented:** the whole capture machinery (`shotPane`, `shotEncode`, the shots directory and its one `Read(//<shots>/**)` grant) is SHARED with the annotation crops (§17); and `stripPaneBlock` / `MARKER_VIEW` / `PANE_SHOT_TAG` never left, because sessions on disk carry those blocks and a restored transcript must show what the user typed rather than a screenful of JSON — so the button rewrote an existing wire format instead of inventing a second one. Reading an old wire format is a permanent obligation; writing one again is a choice this control made deliberately. **A capture that is actually a picture of the screen (D287), and the two ways it was not.** *Scroll.* `cloneNode` copies attributes and `scrollTop`/`scrollLeft` are PROPERTIES, so the clone of a scrolled page was a clone of that page at the top; `shotPane` compensated only for `win.scrollX/Y`, which covers a document that scrolls itself and no app in this repo, all of which scroll an inner `overflow: auto` box. `shotInlineStyles` now records `{clone, x, y}` for every scrolled element as it walks (the same guarded descent that already pairs a live node with its clone — a second walk would pair by index and land one element's offset on another), never for the root (`src` is `<body>`, whose scroll IS the window's), and `shotApplyScroll` puts each box back by prepending `transform: translate(-x, -y)` to each CHILD's inline style — expressible in markup, which is all the serialized SVG carries, layout-neutral so a flex scroller is not rearranged, composed with (never replacing) any transform the walk already wrote, and skipped for `position: sticky`/`fixed` children, which do not move with a scroll. *Images.* An `<svg>` loaded through an `<img>` renders with external resource loading disabled at every origin, so every `<img src="http…">` in the clone drew as a broken glyph — including one served by our own `/api/fs/raw`. `shotInlineImages` fetches each distinct URL (from `img.currentSrc` and from every `url(…)` in the clone's inline styles, which is where the computed `background-image` already sits) and rewrites it to a `data:` URL, capped at `SHOT_IMG_MAX` 30 distinct URLs and re-encoded through a canvas past `SHOT_IMG_MAX_BYTES` 256 KB; the style walk now stops `SHOT_IMG_MS` 1500 ms before the capture's deadline so the fetches have a tail. A cross-origin URL with no CORS headers falls back to drawing the already-loaded element into a canvas; a genuine failure becomes a dashed "image not captured" box the size of the picture (a broken glyph reads as a bug in the page being photographed, and removing the element would redraw the layout around a hole the screen did not have) and is counted into `shotImageNote` — a caveat that rides the pane shot AND every crop, and that is deliberately not one of `shotPaneNote`'s `incomplete` causes, because it is BOUNDED and visible where those are unbounded. `<picture><source>` and the `srcset`/`sizes` attributes are dropped so nothing re-resolves over the rewritten `src`. Order inside `shotPane` is load-bearing: styles → images → `shotRasterise` (whose own data:-URL `<img>`s would otherwise be paired against the source's real images) → scroll (so a canvas swapped for an `<img>` moves with its box). The annotation crops inherit both fixes, being cuts out of that one bitmap. **Pictures the user already HAS (D287): paste and drag-and-drop.** Both textareas take a `paste`, and `#chat` takes a drag (four listeners, all gated on `dataTransfer.types` containing `Files`, with a COUNTED `dropping` class because dragenter/dragleave fire per child element); any image in either is uploaded through the existing `fused.uploadFile` into the SAME shots directory — already the one path `--allowed-tools` pre-approves a `Read` of, already pruned, already served back by `/api/fs/raw` — and becomes a chip beside the camera's. `preventDefault` fires only once a picture has actually been found, so an ordinary text paste still reaches the box. Caps: `SHOT_ATTACH_MAX` 4 pictures per message, `SHOT_ATTACH_MAX_BYTES` 4 MB each; over either, the file still becomes a chip carrying the reason, in the `{view: null, viewNote}` shape a failed capture already wears. `paneShot` is now `shotAttached`, a LIST of `{kind, view, viewNote, thumb, name}` — one list because the chip, the viewer, the ✕, the receipt, the wire block and the restore treat both kinds identically and `kind` decides only the words, with the pane keeping ONE seat inside it (a second camera click replaces it, D285's rule intact). The `<pane-shot>` payload is now always a JSON ARRAY carrying `kind` per entry, so the model can tell a picture of this pane from a photo the user brought in; `paneShotIn` reads that AND the bare object every older session holds, returning a one-element list either way, and `MARKER_IMG` ("🖼 images") joins `MARKER_ANN`/`MARKER_VIEW` in the `MARKERS` set for a wordless send of pasted pictures aloneREMOVED (owner call): the composer's whole-pane screenshot pill.** Both composers carried a per-message toggle (`#viewshot`, `#hviewshot`) that attached one picture of the entire visible pane to the next send, as its own `<pane-shot>` wire block with its own caps and its own receipt row. It is gone — "it doesn't make sense": the question it answered ("the whole layout is wrong") is one the agent can ask about by reading the page, and the cost was a per-send rasterise, encode and uploaded file behind a toggle a user had to notice, arm and re-arm. **What stays, and why the difference matters:** the capture machinery itself (`shotPane`, `shotEncode`, the shots directory) is SHARED with the annotation crops (§17), which are the user's own act of pointing at something, so none of it moves; and `stripPaneBlock` / `MARKER_VIEW` / `PANE_SHOT_TAG` stay although nothing writes one any more, because sessions already on disk carry those blocks and a restored transcript must still show what the user typed rather than a screenful of JSON. Reading an old wire format is a permanent obligation; being able to write it is not. **Rejected:** the `throw` (an error panel for the ordinary case of a folder that is not an app); keeping the embed as a read-only browser (it is the reporting-to-nobody problem, restated as a feature); and hiding the column with CSS while leaving it in the document (the elements would stay live, `shotPane` would still rasterise them and the removed controls would still be focusable — a hidden pane is a pane).
  - **The system prompt** (`_split_system_prompt`) has a shape per pane shape, and is decided **per run, never cached**, so a folder being scaffolded into starts being described as a project the moment it becomes one. An **app folder** keeps the project wording (its HTML is an app fused-render serves through the `runPython` bridge; naming fused-render here rather than leaving it to the user's own `CLAUDE.md`, which we do not own — the D216 reliability argument). A **file** says whose page the pane is and that the viewer is never to be edited. An **ordinary folder** gets the folder-scoping instruction and **nothing about a pane** (D239): the paragraph that used to be here described fused-render's own file browser beside the chat and warned that `app_state` "reports the **browser**, not the folder", and it went with the pane it described — a prompt that tells the model what the user can see beside the conversation, when there is nothing beside the conversation, is a false claim about the screen. The **composer's placeholder** names the same three kinds and is set from the same resolution the pane already performs (stat's `is_dir`, then whether an entry html resolves) — *"Ask Claude about this **project** / **folder** / **file**…"*, with the markup shipping the kind-free *"Ask Claude…"* until stat answers; it was hardcoded to "this project", which was the wrong noun for an ordinary folder and for all 47 file keys, and the rule is the prompt's rule: the UI does not claim a kind the target does not have. That rule is **general, not just the placeholder's** — the footnote under the composer and the annotation block's own preamble both said "project" unconditionally too, so every piece of chrome that names the target reads one writer, and a test asserts no kind noun is hardcoded in the markup. The **app_state disclosure** rides the two shapes that HAVE a pane, for D235's reason (an un-announced tool is a tool that never gets called) — and only those two, since the ordinary folder is not offered the tool at all. Saying "this is a fused-render project" over `~/Downloads` is rejected as a lie that costs something — it invites the agent to hunt for a bridge that is not there and to read a folder of PDFs as a codebase.
  - **The tool roster varies by target kind (D239).** `mcp__fused_approvals__app_state` reads the page beside the chat; a target with no pane has no such page, so the tool is **not offered** there — absent from `tools/list`, absent from the dispatch, and absent from the spawn line's pre-allowance. One switch decides all of it, and it is the **channel's own existence**: `agent.py` spawns `permission_server.py` with the app-state directory only when `_has_pane` (the same `entry_html` predicate), and the server keys both its roster and its dispatch on having that directory. A roster that could vary independently of the channel would advertise a tool the server cannot serve. **Rejected:** offering it and answering with an explanatory error — the model would call it after every edit and spend the 20-second app-state timeout discovering the same thing once per turn; and offering it and answering instantly with "there is no pane" — a tool whose only possible answer is that it does not apply is a tool that should not be in the list. The `Read` rule for the screenshot directory stays unconditional, because it is a rule about a directory and not a claim that this target can annotate.
- **PT-17** **A template never spends a history entry on a value the URL already MEANS, and the guard that decides sits at ONE funnel per template rather than at each call site (D271, D272; generalises PT-15's `annmode`).** **PR-3's gesture gate cannot catch these**, because the gate is read when the write LANDS and not when boot started: a boot path continuing behind an `await` is already post-gesture, so a click anywhere during engine warm-up — the Browse button, the explorer row that chose this file — opens the gate before the seed arrives. **Absent is a value:** a missing param means its default, so writing the default over it is a no-op the runtime's byte comparison cannot see (`set(k, '')` on an absent key appends `k=`), which is what `{default: d}` (PR-3) exists to declare. An audit of **65 `fused.params.set()` call sites across 20 templates** found 39 genuine navigations — left alone, since a working Back is the whole point — and 19 writes that cost an entry for nothing, in six shapes worth recognising: a **re-entrant boot path reached through a gesture** (`latex`'s `startWorkspace` re-running the boot block on Home ▸ open project; `slides`' `openLibrary` ▸ File ▸ New blank deck); an **async callback a click started** (`latex`'s clean compile collapsing an already-collapsed drawer); a **multi-param write whose UNCHANGED half fires first**, takes the push, and leaves the real write to merely coalesce (`las`/`vector`/`pmtiles` writing `dir` before `file` when the file is picked from the folder already being browsed; `xlsx` `offset` before `sheet`; `slides` `slide` before `mode`); a **sweep rewriting every field on every submit** (`api`'s Execute); a **control clamped at its limit** (`pyramid` re-analysing an unchanged path; `pano` comparing the raw param instead of the read-back value, so a drag ending at the default wrote over an absent param); and a **write loop** (`zip`'s `params.onChange` → `openPreview` → `writeLocation`, writing the same pair straight back). The always-live controls D272 closed — a Reset that is not disabled when there is nothing to reset, a Load button over a box seeded from the resolved path, a "back to the list" that is live on a list you never left — are the same rule at the other end of the visit. **The guard goes at the funnel** because a check per call site is a check the next call site forgets — and a key the view leaves OUT of its own repaint comparison (a draft written per keystroke, like `git`'s `msg`) must never push at all, because Back across that entry clears the param while the repaint short-circuits and the stale text stays on screen. **A write's history cost is judged from the state it CAN be reached in, not the state it is normally reached in:** `git` clears `ask` at the end of every mutation and the confirmation has almost always cleared it already, so the no-op guard hides the cost — but `ask` is in the URL precisely so a pending question survives a refresh, and a page reloaded with `?ask=…` arrives with the question live and the entry pristine, so any action clicked from there clears it for real, post-gesture and post-`await`, and takes the push. `history` is outside the audit on the owner's instruction; `git` was too while its rewrite was in flight, and the reworked file has since been reviewed (D271).
- **PT-8** `GET /api/fs/stat` carries the resolved mode list as **`templates`**: an array of `{"mode": <name>, "path": <abs template.html>, "icon": <abs icon.svg|null>}`, in order, first = default. An entry whose folder ships a `condition.py` gate (CT-12) additionally carries **`"conditional": true`** — stat only *marks* it (the gate is **not** evaluated at stat time; it may do real I/O), and the verdict arrives via `GET /api/fs/conditions` (CT-12). A conditional entry is **never the default while an unconditional entry exists**: the default is the first entry *without* `conditional`, falling back to the first (verdict-allowed) entry only when the whole list is conditional. `templates: []` when nothing applies — an unmapped file extension or a `null` binding. A **directory** always resolves at least the universal `/` key's `["_listing"]` (PT-13, D81), so it is empty only when a `null` binding disables it, whereupon the shell falls back to the built-in listing anyway (a folder must always render something). The old singular `template` field is **removed**.
- **PT-9** **`_mode` param (shell URL):** non-default modes are selected via reserved param `_mode=<template name>` on the **shell URL** (bookmarkable, same URL-is-state pattern D40 established for the old HTML `_mode=render|source` toggle — that toggle itself is now the ordinary `["_render", "code"]` mode list, PT-12; old `_mode=source` bookmarks fall to the default, accepted break). Absent `_mode` = default = the first non-`conditional` entry (PT-8; `templates[0]` when none is conditional); selecting the default **deletes** the param (clean URLs); an unknown/stale value falls back to the default with no error. Switching swaps the iframe src to the selected template's `/render?path=<template>&_file=<file>` with a fresh document per switch. A sentinel mode may render a **shell view instead of an iframe**: `_listing` (PT-12) mounts the shell's built-in listing component (no iframe, no `_file`) in place of the preview body, selected by `_mode=_listing` like any other mode (D81). Known accepted quirk: template params (e.g. `offset`) persist on the shell URL across mode switches; a param name used differently by two modes collides — documented, not prevented.
- **PT-10** **Mode switcher (shell, preview header):** rendered only when `templates.length > 1`, right side of the preview header bar. *One qualification, from the listing pane's self target (FS-11): "one mode is not a choice" holds because that one mode is the ACTIVE one — so when the caller's surface has **no** active mode, a single entry still renders, because the trigger is then the only way to pick anything. Nothing is marked active in that state: no checkmark, no accent row, and the trigger names the action ("Choose view") rather than reporting a mode it is not in.* Its contents are exactly the resolved list — the **available width is never an input** to it, so a mode cannot appear or vanish as a divider is dragged (PT-15). **Icon-only buttons**, mode name via native `title` tooltip, active mode in accent color. When an entry's `icon` is `null`, the shell renders a placeholder: the first letter of the mode name in a small rounded box. The `.html` Rendered|Source pair is **not a special case**: it is the ordinary mode list `["_render", "code"]` (PT-12) riding this same switcher — `_render` gets a shell-baked eye icon (sentinels have no folder to ship `icon.svg`); `code` gets its real folder icon. The `_listing` sentinel likewise gets a shell-baked list icon (D81). **No two modes a key BINDS together may be indistinguishable in one list.** This is a constraint on the bindings (PT-7/CT-3), not on the wording — display names live in `platform/lib/mode-name.ts` and are that module's business — and it is easy to break from a distance, because a name that is not in the table falls through to a humanizer: adding a mode to a key, or naming a new template folder into a string the humanizer already produces, is enough. Dispatch keys on `mode`, so a collision breaks nothing and is invisible to every other test, which is why it needs its own: `listing/mode-labels.test.ts` derives every co-offered set from the shipped registry — per key, and per preview-pane list (FS-11/`listing/pane-modes.ts`) across its `isDir` permutations — and fails on any duplicate. *One pair used to be named alike on purpose — the `app` template and the pane-only `_app` sentinel, the same view from two surfaces, of which the pane offered exactly one carrier and never both. D264 deleted both, so the guard has no sanctioned duplicate left and the pane's list is the registry's own.*
- **PT-11** **Icons:** a template folder may ship `icon.svg` — **monochrome** (single fill; the shell tints it via CSS `mask-image` + `currentColor`, so only alpha matters), square viewBox (24×24 suggested), legible at 16px. `icon` in the stat entry is the abs path of the `icon.svg` sitting next to the *resolved* `template.html` (the user folder's icon when a user template resolved), or `null`. The shell loads it through the existing `/api/fs/raw` endpoint — no new routes. Every built-in folder ships one. Sentinel modes (`_render`, `_listing`) have no folder, so the shell bakes their icons in (PT-12).
- **PT-12** **Sentinel modes:** a mode name starting with `_` is a **shell sentinel** — no template folder backs it; the shell knows what it means. Server resolution special-cases sentinels: the stat entry is emitted as `{"mode": "_<name>", "path": null, "icon": null}` without touching the filesystem. The `_` prefix matches the reserved-param convention (`_mode`, `_file`). The sentinel namespace is **shell-owned**; since D73 the server keeps a **known-sentinel set** (`KNOWN_SENTINELS = {"_render", "_listing"}`, D81) and a name in that set is referenceable from **any** registry list, built-in or user — any other `_`-prefixed name is invalid (dropped + `template_error`, CT-6). Two sentinels exist:
  - **`_render`** — "render the file itself" — the default mode of the built-in `.html`/`.htm` list `["_render", "code"]`. Shell handling: iframe src `/render?path=<the file itself>` (no `_file`), shell-baked eye icon.
  - **`_listing`** — "the shell's built-in directory listing" (sortable columns + in-folder search, FS-1/§13.4, plus the optional split preview pane, FS-9..FS-15) — the default of the universal `/` directory key (PT-13, D81), and a peer mode of `.zarr/`'s `["zarr_aoi", "_listing"]`. It backs no folder and takes no `_file`: when it is the active mode the shell **mounts its Listing component in place of the preview iframe** (no iframe at all). Shell-baked list icon.

  Users **can** rebind any registry key — including `.html`/`.htm` (CT-4 revised, D73) and the directory keys (D81) — dropping a sentinel, then listing it explicitly brings it back. Unknown sentinel entries (path `null`, mode not in the set) are filtered out defensively. Non-sentinel entries in the same list (e.g. `code`, `zarr_aoi`) work exactly like any template mode. Future modes are added to the server-side registry and flow through the framework normally.
- **PT-13b** **An explorer folder has no top-bar mode control.** Directories resolve modes like anything else (PT-13), but the shell's title-bar switcher is **not rendered for a directory, full stop** — the folder's mode control is the preview pane's own (FS-10/FS-11), which sits with the thing it changes. It once carried an exception for the app-builder route, which kept the control because under that route the folder was the whole subject rather than a listing beside a preview; D262 deleted the route and the `appChrome` flag that named it, so the rule is one clause with no carve-out and `Preview.tsx` keys on `!stat.is_dir` alone. **The accepted consequence, stated rather than discovered:** nothing in the explorer switches a folder INTO one of its other modes. The pane's menu writes **`_side`** — which of the PANE's three companions is showing (Preview / Claude / Git, FS-10) — never `_mode`, so the folder itself stays on `_listing` while the pane changes. *The menu used to write `_panelMode`, which named which of the SELECTED ROW's templates the pane previewed; that switcher is retired (FS-11) and the param with it.* Two of the folder's own peers are consequently reachable without touching `_mode` at all — `claude` and `git`, which the pane borrows from the folder as companions — while its `graph`/`model_card` views are still **entered** by an explicit `?_mode=` (a typed URL, a bookmark, the file menu's **Open With**), or by a registry default that is not `_listing` (a `.zarr` store opens on `zarr_aoi`). **Getting back out is the BROWSER'S BACK BUTTON, and deliberately nothing else** (owner call). Every one of the ways in is a *navigation* — a typed `?_mode=`, a bookmark, Open With — so the navigation that got the user there is what undoes it, and it is already at the top of the window. This rule briefly shipped a second answer: the `Browse contents` chip (PT-13/D65) revealed in the explorer by an `is-exit` modifier. It is **removed**. Pinned absolutely over the template's iframe it landed on whatever that template drew in its own top-right corner — over the full-width timeline view since removed it sat across that view's own header and the newest commit — so in the folder modes people actually open it read as a stray tooltip rather than as a control. A bespoke affordance that has to be explained is worse than the standard one every user already has, and "the view must supply its own way back" was never the requirement; "the state must not be a dead end" was, and Back satisfies it. **This rule leaves a folder no bar control at all.** It briefly left one — "Open as app", which D262 made the only way into a folder's app view once cards stopped carrying `?_mode=app`, and whose gate therefore had to match the server's exactly. D264 removed the app view instead: there is no destination left for that button to guard, and a folder's modes are entered the way this rule already says every other one is. The chip keeps its **embed** reveal untouched — a different surface, where `.preview-header` and its switcher are hidden outright, so there the chip is the whole affordance rather than a second one. This is an owner call: two mode switchers in one view, a few hundred pixels apart and governing different halves, is not a choice a user should have to work out, and for a folder the pane is the explorer while its peers are opt-in tools rather than other ways of looking at the listing. Files keep the top-bar control; nothing else does.
- **PT-13** **Directory views (D65, revised by D73 and D81):** a preview target may be a **directory**. Directories resolve through the **same registry** as files (PT-7, CT-3): a key with a **trailing `/`** binds a directory's basename, and the **universal `/` key** (zero segments, CT-3) matches *every* directory at lowest specificity. The built-in registry ships `"/": ["claude", "_listing", "git", "graph", "zarr_aoi", "model_card"]` — **`claude` leads it as of D280**, because the listing's preview pane reads this order for its default literally (`activePaneMode` takes the first offered mode), so the lead is what a SELECTED folder row previews as; the FULL-SCREEN folder route resolves "first entry without `conditional`" instead (PT-8), so `_listing` still wins there from second place and opening a folder still lands on its file table. **Dropping `_listing` rather than demoting it is what would break that** — every folder would open on a gated chat with no listing at all — so it stays, and the reorder is the whole of the change (D185 removed `preview` and D264 removed `app` — the mode and its template folder are deleted, so a folder is its listing; `model_card` joined the row with D249/§37; `graph` per MD-2; `git` per §33/D193, directory-only per D235/GT-2 and gated to a folder in a work tree; the per-path timeline mode that used to close this row is deleted (PT-14); `claude` — the one chat template since D237, which deleted the second one this key used to carry — gated only against mount-backed paths, §7.2's `/` row and PT-14/PT-16) and `".zarr/": ["zarr_aoi", "_listing"]` — so **every** directory carries a non-empty `templates` list (≥ `["_listing"]`), and dispatch is uniform: a directory previews its default mode exactly like a file. The built-in **listing is itself a mode** — the `_listing` sentinel (PT-12) — so it rides the ordinary mode switcher (PT-10) and `_mode` selection (PT-9): a plain folder's single-mode `["_listing"]` shows the listing with no switcher; a `.zarr` store shows the listing by default with the `zarr_aoi` map joining as a `condition.py`-gated peer (CT-12) once its background verdict confirms the store (`_mode=zarr_aoi` selects it). This replaces D65's one-way `?listing=1` "Browse contents" escape hatch, which is **removed** (D81) — the only way to the listing is now the `_listing` mode. In **embed** (the preview header, hence the switcher, is hidden), a corner chip toggles the `_listing` mode (writing/deleting `_mode`) so an embedded directory preview can still reach its members. Annotate (§17) is not offered for `_listing` (no iframe to overlay) — moot in the core registry since D235, where `annotate` is bound to nothing at all, but still the rule for a user who re-binds it (§16). A directory resolves to an **empty** list only when a `null` binding disables it (CT-2); the shell then falls back to the built-in listing regardless (a folder must always render something). Users bind directory views like any other key — `"/": ["_listing", "gallery"]` lists the built-in listing plus a gallery mode for every folder (built-in names are listed explicitly — there is no splice, D94); dropping `_listing` from a list forgoes the file listing for those directories (owner call, same "user can shoot themselves" posture as D73's `.html` rebind). Accepted break: old `?listing=1` bookmarks ignore the dropped param — a plain folder still lists (its default), and a `.zarr` bookmark also lists by default now (the `zarr_aoi` map is a gated peer reached via `_mode=zarr_aoi`, not the default). Accepted break (D185): the `preview` folder-preview template is **deleted** and gone from this key, and the two ways a leftover reference surfaces are **different mechanisms** — a **`?_mode=preview` URL or bookmark** is an unknown `_mode` value, so it falls back to the default (`_listing`) **silently, with no error** per PT-9 (and lands on the listing that now carries the split pane, FS-9..FS-15, which is what such a URL was asking for); a **user registry** still listing `"preview"` is instead a dangling name per CT-6/D95 — dropped from the mode list with `template_error` naming it on the stat payload and a broken (`exists:false`) row in the Templates view (§23).
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
- **DM-2** **DECIDED:** user `runPython` code executes on the **bundled interpreter only**. `[bundled]` is the dev-install list and the Linux/Windows shipping list; on macOS py2app **copies** only what `scripts/setup_py2app.py` names — which now DERIVES that list from the installed distributions and excludes nothing, so all three platforms ship the whole extra (D176). `BUNDLED_EXCLUDED` is empty but stays as the mechanism: a `[bundled]` distribution the bundle does not carry must be named there with its measured cost, never merely absent. "Is this dependency available?" therefore has one answer today, and `tests/test_bundle_contents.py` is what keeps it that way — the templates that genuinely need an install declare dependencies **outside** `[bundled]` (`pyproj`, `imagecodecs`, `py360convert`, `pypandoc-binary`, and since D276 the geo/PDF stacks named below), which is what exercises the install loader on a shipped build. **The extra is a size budget, not a wish list (D276).** It ships preinstalled: numpy, pandas, pyarrow, duckdb, pillow, openpyxl, requests, httpx, msgpack, python-pptx, drain3, botocore, google-auth, the `fused` engine + the core `dependencies`. It deliberately does NOT ship polars (197.0 MB, imported by nothing in the product), scipy (70.3 MB), matplotlib (25.0 MB), pymupdf + pikepdf (68.9 MB) or the geo stack geopandas/rasterio/rio-tiler/shapely/zarr and their exclusive transitives (180.1 MB) — 541.9 MB removed, taking the installed set from 954.3 MB to 412.4 MB (D276 states the measurement method; absolutes are only comparable against it, deltas against anything). Those live in the `pyproject.toml` of each template that imports them (`map`, `vector`, `geometry_editor`, `pdf_studio`) or in the venv a daemon manages itself (`geotiff`, `netcdf`, `zarr_aoi`, `pyramid`, D174), and are installed on first render through PY-18 — `map`'s environment resolves to 472.8 MB on that same measure, since a declaration is the complete list (D172) and it additionally carries duckdb + requests for the user-supplied Python targets `worker.py` executes in-process. **The unit of that decision is the FOLDER, not the wheel** (PY-16): `fpdf2` stays in the extra at a measured 14.1 MB precisely because moving it would have put all of `excel` and `slides` behind a project venv, gating every `.xlsx`/`.csv`/`.pptx` on a first-render install of packages the app already ships. **The built-in executor cannot honour any of this** — it owns no venv machinery (D174) — so `executor.explain_missing_module` replaces a bare `ModuleNotFoundError` with one naming the folder, its manifest, the missing distributions and both fixes, whenever the failed import resolves to something that folder declares. At FAILURE time, never before the run: a pre-flight refusal keyed on the folder's state breaks every stdlib-only entry point in a folder that declares one heavy optional dependency (`geotiff`'s `ensure()`, `model_card`'s `inspect_model.py`, `pano`, `docs`, `latex`), and an AST pre-scan would refuse the lazy imports that make `pdf_studio`'s `health` action answerable while its venv builds. That obligation is enforced in both directions: a template may not declare what the bundle already ships (`test_a_declaration_is_needed_for_what_the_MACOS_BUNDLE_lacks`) and MUST declare what it does not (`test_a_template_declares_whatever_the_app_does_not_ship`), and the Learn page may not promise a library the app lacks (`test_the_learn_page_only_promises_what_the_app_ships`). Removing from the extra rather than excluding from the bundle is the deliberate choice: `BUNDLED_EXCLUDED` would have shrunk macOS alone and left Linux and Windows carrying what the extra still promised — D176's defect in the other direction. py2app note: these are force-copied via `packages` — the executor imports them only in child processes, so import tracing can't see them. **The standard library ships WHOLE** (D305): py2app freezes only the stdlib its modulegraph reaches from `app_entry.py`, and that subset is inherited by every environment built on the bundled interpreter (PY-18) — a DMG shipped without `filecmp`, and an MLX load died inside transformers with a message about the model. `setup_py2app.STDLIB_EXCLUDED` names the few omissions with reasons (tkinter and turtle, idlelib, turtledemo, ensurepip, lib2to3, antigravity, this), and `build_dmg.sh` §4b-ter fails the build when either the bundled interpreter OR a venv built on it cannot import what that list says ships. This holds under the fused engine too: a script whose folder declares no `pyproject.toml` runs on that same interpreter (PY-17), and only a folder that declares one gets an environment of its own (PY-16/PY-18).
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

- **LM-1** Route: `/explorer/view/_panel?...` and `/explorer/embed/_panel?...`. `_panel` is a **sentinel pathname**, not a real file: the shell's route dispatch (`shell/App.tsx`) intercepts it under both prefixes before calling `stat`. Zero server changes (the server already serves the shell for any view/embed path). The pane tree lives in the reserved `_layout` query param (LM-2). *The prefixes were plain `/view/` and `/embed/` when this section was written; `platform/lib/router.ts` rewrites those shapes once at module init (`rewriteLegacyUrl`), so an old `/view/_panel` bookmark still opens — which is why the shorthand `/embed/<path>` below still names the right thing.*
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

- **LM-10** Entry: **split-right and split-down items in the path `⋮` menu** (`BarMenu`'s PathOverflow), beside "Open in Finder" / "Copy path", carrying their names and travelling with the path they act on — and offered for a **file preview only**, since the splits make least sense over a view that already IS a split. *They were unlabelled glyph buttons in the breadcrumb's own layout zone, pinned to the far right of the window an inch of empty bar away from the path; that zone is deleted, hairline and all. `enterPanel` is unchanged and is still what they call.* Click → navigate to `<prefix>/_panel?_layout=(<seg>,<seg>)` (split right) or `(<seg>;<seg>)` (split down) (D51 grammar) where `<seg>` is the current fs path + its **whole** current query (D72 — nothing is promoted to the top level) — two panes side by side or stacked, both the current view with its params carried over (a single pane on entry looked like nothing happened).
- **LM-11** In layout mode the sidebar stays visible (bookmarks reachable, ★ button works on the layout URL — bookmarking a layout needs zero bookmark-layer changes, D20). Breadcrumb shows a static "Panel" label. The armed-bookmark "Update bookmark" flow (D38) works unchanged: pane/param drift rewrites the shell URL via replaceState → `fused:urlchange` → `syncUpdateButton`.
- **LM-12** Module: **`apps/explorer/Panel.tsx`** — tree ops (split/close/collapse), pane DOM + bar, URL sync; the tree codec is the shared `platform/lib/layout-codec.ts` (TM-10). Imports `platform/lib/router` only (one-way deps, ARCHITECTURE §6). `shell/App.tsx` carries the sentinel branch; the styles are a `.layout-*` section; sidebar/bookmarks/api untouched. *Written as `views/panel.js` + a `main.js` branch, against the vanilla shell this predates (D52 moved the shell to React).*

## 15. Tab Mode — Tabbed Views (M6)

Goal: the same URL-is-state model as §14, but as **tabs instead of a grid**: one page visible at a time, a tab bar to switch. Primary use: a **bookmark folder rendered as one view** — click the folder, get its bookmarks as tabs, bookmark the result as a dashboard.

### 15.1 URL & route

- **TM-1** Route: `/explorer/view/_tab?...` and `/explorer/embed/_tab?...` — a sentinel pathname exactly like `_panel` (LM-1), intercepted by the same route dispatch under both prefixes (and reached by the same legacy rewrite from `/view/_tab`). Zero server changes.
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

- **TM-10** The §14 codec (escape/parse/encode/segment helpers) lives in a shared **`platform/lib/layout-codec.ts`**; `Panel.tsx`, **`Tabs.tsx`** and `Breadcrumb.tsx` import it. `Tabs.tsx` owns the tab bar, lazy iframes, URL sync and the folder-entry URL (`composeFolderTabsUrl`, TM-8); `shell/App.tsx` carries the `_tab` sentinel branch; the styles are a `.tabs-*` section; the sidebar (`sidebar/BookmarksSection.tsx`) changes only the folder-row click wiring, importing that one function. *Specced as `views/layout-codec.js` / `views/tabs.js` / `main.js` / `sidebar.js` against the vanilla shell (D52).*

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
id-only deep link (the sidecar-inspector→annotate contract, §24 HV-8 —
that caller is removed, this param is not; mirrors the claude
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
recoverable through the sidecar itself; what it leaves is the **live** store,
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
annotate-agnostic so other hosts (`claude` has) can adopt the same reader. Its own
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
  fused_available}, deploy: {enabled}, reader: {enabled}, model: {default,
  choices}, calls: {…}}` — and no
  `log` block (PF-5). `PUT /api/prefs`
  (X-Fused) applies a **partial** update — any of `engine`, `deploy_enabled`,
  `reader_enabled`, `default_model`, `calls_enabled`, `calls_params` or
  `calls_retention_days` present, so each control PUTs only its own field — and
  returns the same shape. An unknown engine value, a non-boolean
  `deploy_enabled`, or a body naming no known preference → 400; the file merges
  (future prefs are new keys, not new files).
- **PF-1b** `default_model` is the user's preferred Claude model as a **short
  name** — `""` (unset), `fable`, `opus`, `sonnet` or `haiku`, the claude
  template's own selector vocabulary; anything else → 400, and a hand-edited
  unknown value in the file reads as unset. Two consumers rank it identically:
  an **explicit** choice always wins, the preference is next, and each keeps its
  own hardcoded fallback beneath it. For the fused.ai relay (`server/ai.py`)
  that is a caller-supplied `model` > the pref > `claude-haiku-4-5-20251001`,
  with the short→full-id mapping (`fable`→`claude-fable-5`,
  `opus`→`claude-opus-5`, `sonnet`→`claude-sonnet-5`,
  `haiku`→`claude-haiku-4-5`) living in that one module. For the claude chat
  template it is the `model` pane param > the config detected from the
  project's sessions/settings > the pref > `sonnet`; the template reads it with
  a plain `GET /api/prefs`, like its other `/api/…` reads. Read per request, so
  a change applies without a restart.
- **PF-1a** The page renders its sections in this order: **Appearance**,
  **Default model**, **Call log**, **Deploy to Fused account**,
  **Accessibility**, and last
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

- **SR-12** **A search selects its TOP HIT**, so the preview pane has something in
  it and Enter has a target without the user reaching for a row first. Typing a
  query is itself a request to look at something — which is why this survived the
  removal of the folder auto-select (FS-16, D278) and is now the listing's only
  automatic selection. Unlike that one it is **repeated, not one-shot**: results
  re-rank on every keystroke, every stream flush and every published slice, so
  the selection FOLLOWS the ranking as it refines rather than pinning row one of a
  ranking the user has typed past. Three situations, three answers
  (`searchAutoSelectPath` / `nextSearchSelection`): nobody has claimed the
  selection → the top hit; **the user moved it → leave it, and that claim
  OUTLIVES a query change** (a new query re-ranks the rows, it does not revoke a
  choice; a deliberate Escape is equally theirs); the user's row dropped out of
  the results → the top hit again, since keeping the selection on a path that is
  not on screen previews something the user cannot see. Whose selection it is has
  to be REMEMBERED (`SearchSelectState`: the path this rule last wrote, plus a
  sticky user-claimed flag) rather than recomputed per query — as a pair of refs
  reset on every query change it reclassified the user's own selection as the
  app's guess and let the next re-rank overwrite it.

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
## 24. History View — Sidecar Inspector Template (D96) — **REMOVED (D243)**

**This template is deleted and the section is kept only as a tombstone, so the
HV-* ids other sections still cite resolve to an explanation rather than to
nothing.** `fused_render/templates/history/` used to be a read-only inspector
over a file's `<ext>.json` sidecar (§21, SB-7, D82–D84): it parsed the sidecar
in the browser (no `.py`), validated each top-level key against an inline
per-key schema, and drew claude sessions, bookmark history, the last-session
restore card and annotate comments as one merged timeline, with a
`_mode=claude&session_id=<id>` resume link off a session row. It was bound to
the wildcard key `".*.json"` (where it was the default) and appended to
`".html"` and `".parquet"`.

It went away because the word it occupied was wanted by the mode users actually
meant by "history": the git commit spine that took over the name (D243). Two
modes both reading "History" on the same key was a real
defect — `frontend/src/apps/explorer/listing/mode-labels.test.ts` exists
because of it — and the sidecar inspector was the half nobody reached for. **That
successor has since been deleted too**, folded into the folder-only `git` view
(§33, PT-14), so neither timeline mode ships and no template folder answers to
`history` at all. The
sidecar itself is unchanged and still written and read by the surfaces that own
it; what is gone is the merged read-only view of it. `".*.json"` now falls
through to `["tree", "code"]`, which renders the same bytes without pretending
to be a history.

The behaviours other sections cite by id: **HV-1** — the "ordinary view
template, no shell code, forkable" shape (that is D78's rule, not this
template's). **HV-6** — the sidecar's records grow **additively**: a writer may
add fields, and a reader must require only what it renders. **HV-8** — the
`comment=<id>` deep link into `annotate` (§17), removed by D237 before the
template itself was.

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
  "reader"]` — canvas listed first, then the ordinary tail every
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
  `vector`, `structure`, `tree`, `log_studio`, `claude`, `annotate`,
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
  (`annotate` and `zip`, and the since-deleted timeline view, left this list in D157 — they are ordinary DOM chrome
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
  "claude", "reader"]` — `markdown` now supersedes `code` for
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
  `["_listing", "app", "claude", "git", "graph",
  "zarr_aoi"]` (as it then stood) (as of D185, which removed `preview`, D193, which added `git` —
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
default. **`git` is offered on FOLDERS ONLY** (GT-2) — staging, stashing,
committing and pushing are things done to a tree, and the tree a file sits in is
its folder's, so the folder is the target that has the question. A file's reader
is not left out: the preview sidebar BORROWS the parent folder's entry
(`apps/explorer/lib/dir-mode.ts`), and the view is scoped to the open path
either way. The path-scoping machinery below is unchanged and still the
point: a folder deep inside a monorepo asks about itself, not about the
repository.

The view was read-only through D193 (the original GT-11) and is not any more:
D229 replaced that item with a **VSCode Source-Control-style GUI** — branch
management, staging, stashing, committing and pull/push — rebuilt **in place**,
as one mode in the switcher rather than a second template. GT-12..GT-17 below
are that surface, and everything GT-1..GT-10 says about bounds, refusals,
pathspec hygiene and the theme still holds for it unchanged.

**History is a section of this view, and it is the only one.** It was pulled
out for a while, on the grounds that a separate per-path timeline mode
(`history`) rendered the same commits with a spine this view never had, and that
two modes on every target told one story twice. That mode is **deleted**, so the
argument for the removal went with it: the commit log is back as one section of
this page, scoped by the same pathspec the working-tree lists use, and its
`overview` read carries it (`log.py`'s `history` flag defaults to True and the
view does not opt out). `log.py`'s `op="log"` and `op="commit"` remain for a
caller that wants the log on purpose.

- **GT-1** An ordinary view template (`fused_render/templates/git/`) —
  `template.html`, `log.py` (the reader), `ops.py` (the mutations),
  `condition.py` (the gate) and `icon.svg`. No shell or server code: everything
  is the ordinary template contract (`_file`, `fused.runPython`,
  params-as-state). The read and write halves are **two modules, not one**: a
  reader that also mutates has no honest place to draw its validation line, and
  "what can this template DO to my repository" must be one file to audit rather
  than a grep for verbs across an 800-line reader.
- **GT-2** **Registry binding — the universal `/` directory key, and NO file key.
  `git` is a FOLDER-ONLY mode.** It sits among the gated peers of
  `"/": ["_listing", "claude", "git", "graph", "zarr_aoi", "model_card"]`
  and appears in none of the ~47 **authored-file** keys, which keep `claude`
  exactly as it was — code, config, prose, notebooks, tabular,
  geo, images, record streams. The reason is what the mode DOES: staging,
  discarding, stashing, committing, branches, push/pull are **repository-level**
  acts, not things done to one file, and the working tree a file sits in belongs
  to its FOLDER. So the folder is the target that has the question. The per-file
  question — what happened to this file — is answered by this same view rather
  than by a mode of its own: its commit list is scoped to whatever target it was
  opened on, and selecting a commit renders the open file as of it (GT-17).
  `_listing` stays the directory default (PT-8) and `git` is never a
  default anywhere. The authored-file set is still deliberately **withheld** from
  spreadsheets, media, 3D, archives, PDFs and generated tool files — "did a human
  write or analyse these bytes" is unchanged as the question for
  `claude`; it was never the question for `git`.

  *What this is, historically.* This is the D235 binding restored, with a
  different reason and a fixed consequence. D235 bound `git` to `/` alone because
  a file's commit story belonged to the separate `history` mode and a file
  offering both would be two commit-log modes for one story. That premise expired
  when `git` dropped its commit log (see the §33 preamble), and the binding was
  widened to all 48 keys — not because a file wanted a working tree, but because a
  folder had no way to reach one: the explorer gave a FOLDER no mode switcher of
  its own, the only mode surface a browsing user had was the preview pane's, and
  the pane acted on the **selected row**, always a file. Riding the file keys was
  how an unreachable mode got reached. The preview pane now selects and previews
  FOLDER rows too (the folder peek, FS-10/FS-11), so the folder has its own mode
  surface, the workaround has nothing left to buy, and the binding goes back to
  the target the mode is actually about. The `.jsonl`/`.ndjson` key is not a
  carve-out either way any more: like every other file key it simply has no
  `git`.
- **GT-3** **The gate (`condition.py`, CT-12) is `git rev-parse
  --is-inside-work-tree`, one bounded subprocess — never a search of the tree.**
  **It passes `close_fds=False`, as every subprocess spawned from the SERVER
  process must, and that is not a tuning knob.** This sentence used to say "like
  every other subprocess in this codebase", which was never true: an AST sweep
  counts ~110 call sites in `fused_render/` that omit the flag. Most are worker
  or daemon processes, where PROJ is not resident and the hazard does not exist,
  so the rule is stated by its actual scope rather than as a codebase-wide
  habit — and a reader must not infer from a nearby spawn's silence that the
  flag is unnecessary here. Without it CPython takes the `fork()` path
  instead of `posix_spawn`, and in the SERVER process — which has PROJ loaded —
  the child dies in PROJ's SQLite atfork handler (SIGSEGV, `returncode == -11`).
  The gate reads that as "not a work tree" and fails closed, so `git` (and, at the
  time, its per-path timeline peer) was silently missing from every mode list in
  the UI while the gates passed their own tests and answered correctly from a
  plain shell. The
  same fork hazard is documented for the pyramid worker; the fix is the same
  one.  Any gate that grows a subprocess inherits this rule — and so did the two
  interpreter probes in `engine.py`, which spawn from this same process, omitted
  the flag, and produced the identical symptom one layer up (PY-17).
  **A path that is not a directory is False, before the subprocess.** One
  `os.path.isdir`, and the gate is over: this is GT-2's folder-only binding said
  a second time, in the place a hand-written `?_mode=git` has to pass, so a file
  is never answered for even though a user may re-bind the mode (§16). The gate
  used to fall back to a file's PARENT directory instead (handing git a file as
  `-C` is an ENOTDIR, not an answer) — a workaround for the file keys the mode no
  longer has, and one that answered a folder's question while pointing at a file.
  The **runtime** modules are the other half of MD-11 and deliberately did NOT
  follow: `log.py` and `ops.py` still resolve a file target to the work tree
  around it, so a hand-written `?_mode=git` on a file degrades to a working-tree
  view rather than crashing. The gate is the UX, the module is the guarantee.
  It **never enumerates**
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
  git-backed registered linked app — because that history belonged to the
  separate `history` mode. Both are gone, along with that mode and its mirror
  rule (which refused every directory that was not a fused app). One gate asks
  git one question, which is also why the instruction to keep it and
  `app_git.app_dir_for` "in step" is no longer needed: there is one rule, not
  three. Nothing about
  WRITE authority moved — only real app folders are ever written to by `app_git.py` at all
  (their scaffold commit; a Claude turn's commit through the template's mirror
  of it — manual edits commit nowhere since D245); being offered a
  timeline never implied being given one (MD-11: the gate is the UX, the module
  is the guarantee).
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
  stash ▾) → commit message box with the ✓ Commit button, the GT-18 AI sparkle
  and the GT-14 warning →
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
- **GT-18** **The commit message can be WRITTEN for you, and the writer is an
  offer rather than a step (D241).** A sparkle button sits in the commit-actions
  row beside ✓ Commit; it fills the textarea with a draft and stops there. It
  never commits: the user reads the draft, edits it, and sends it with the same
  ✓ Commit / Ctrl+Enter as a typed one — an AI that both wrote and committed
  would remove the only review step this box has.
  **What it reads is a new READ op**, `log.py` `op="pending"` — not an `ops.py`
  op, because it forks `git diff` and touches no ref, no index and no file, and
  routing a read through the write module's confirmation machinery would say it
  can lose work. Its shape mirrors GT-14: the **staged** diff (`git diff
  --cached`), deliberately **unscoped**, because `git commit` records the whole
  index wherever it lives and a message written from a scope-filtered diff would
  describe less than the commit is about to make; with **nothing staged at all**
  it falls back to the uncommitted changes under the open scope (what the panel
  is showing), with untracked files NAMED rather than diffed — an untracked file
  has no `git diff`, and per-file `--no-index` would be a fan-out this one
  bounded call will not do. `empty` is a first-class answer (GT-9): the view says
  *"there is nothing to describe"* rather than prompting a model with no change.
  **Its own budget, smaller than the pane's** (GT-8 still applies, with different
  numbers): `MAX_PROMPT_DIFF_BYTES` 80 KB and `MAX_PROMPT_DIFF_LINES` 1 500
  against the diff pane's 400 KB / 3 000, plus `MAX_PROMPT_FILES` 100 on the name
  list, which is streamed under the status byte cap and cut back to whole
  NUL-terminated fields exactly like `git status`. The consumer is a PROMPT, not
  a reader: it is billed per token and summarises worse the longer it gets. The
  truncation flag is **carried into the prompt** ("the diff was cut off — do not
  guess at the rest") rather than dropped, so the model cannot describe a change
  it only half saw.
  **The call** is `fused.ai` at `effort: "low"` (a commit message is not a
  thinking problem) with a system prompt fixing the format — imperative subject
  ≤72 characters, conventional prefix where one applies, a body only when the
  change genuinely needs one, and no preamble, fences or quotes; a stray fence is
  stripped anyway. It **streams** through `onChunk` into the textarea, clearing
  it first (VS Code's behaviour: the button replaces the draft rather than
  appending to it) and keeping the previous text for the failure path, so a
  server with no AI configured cannot cost the user a message they typed. Each
  chunk is written to `msg` as well as to the node, and the node is re-found on
  every write rather than closed over — a mutation elsewhere in the view repaints
  between two chunks, and the rebuilt textarea is filled from the param, so the
  stream picks up where it left off instead of disappearing into a detached DOM.
  The button's disabled state is read from module state at BUILD time for the
  same reason: a repaint mid-generation must not hand back a second click into
  the same stream. Failure is the ordinary `flash` a failed mutation gets —
  never the traceback overlay — and `ai_unavailable` reads as *"AI is not
  available on this server."*

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
`claude` (and, at the time, other hosts) can adopt it — and **`claude` now
has** (FH-18 below).

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
history you are asking about. (This panel sits in a FILE's own mode list, since
the agent's checkpoints are about that one file; `git` is offered on the FOLDER,
because a working tree is a repository-level thing (GT-2), and a file reaches it
through the sidebar's borrowed entry. The comparison below is
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
  would silently find nothing rather than fail. **It is also not the cost anyone
  assumes it is:** the whole walk — `listdir` over every session dir plus a
  `getmtime` per match — measures **0.2 ms** against a 44-session store, which is
  why it is not worth deferring, caching, or answering from the file index
  (FH-19). What costs is `_delta`, and that is the knob.
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
  not derivable from it. Another template adopting the reader
  is later work (`claude` has, FH-18); nothing here is annotate-specific except the UI.
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
  panel is a section of the chat template's LANDING page (`#snaps`,
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
  - **LISTED on arrival, with no control to open it, because the read got cheap
    enough to stop hiding.** The heading is a quiet static label over a stack of
    pressable rows, exactly like "Recent chats" above it, and `mountSnapshots()`
    reveals the section AND calls `loadSnapshots()`. Two earlier revisions put a
    door in front of this list and both are gone: first the HEADING was the
    button (a full-width bordered pill wearing 11px uppercase letter-spaced
    micro-type with a lowercase "show" hint beside an uppercase label — neither a
    heading nor a button, rejected on sight), then a placeholder `.snap-row`
    stood in for the section with `hide ▾` beside the label. The deferral they
    existed to serve was aimed at the wrong cost. **Measured** on `claude`'s own
    `template.html` (453 KB, 12 checkpoints): 292 ms for the timeline, of which
    `difflib` inside `_delta` is **290** — two numbers per row — while reading
    all twelve versions off disk is 7 ms and enumerating them out of the store
    (FH-2) is **0.2 ms**. So the call declines the exact deltas
    (`deltas: "0"`, FH-19) as well as enrichment, lands in **~6 ms**, and a list
    that costs six milliseconds does not need a click, a caret or a hide. The
    rows still open into a real diff — `snapshot_plan`, for that one version.
    While a FIRST read is in flight the section holds a `snap-note` where the rows
    will be ("reading the version history…"); a refetch leaves the rows it already
    has up, because swapping a good list for that sentence reads as the panel
    losing the history it is about to reprint. A FAILED read empties the list, says
    why in `#snapsnote`, and grows a `try again` on the heading line — the retry is
    now a control rather than "press the thing you pressed before".
    A loaded timeline is **cached until something appends to the chain**, so a trip
    into a chat and back repaints rather than re-asking (`mountSnapshots` renders
    `snapTimeline` directly when `snapLoaded`); a FAILED read caches nothing, so
    the retry and the next landing ask again rather than leaving the section stuck
    on the failure. **Two** things append. A **revert** repaints from the
    post-revert timeline the write returned (falling back to a refetch). And a
    **finished chat turn** — Claude edits the file in this very page and Claude
    Code checkpoints what it edits, which the panel has no other way to see; a
    landing page reached after a turn showed the pre-turn position, stale deltas
    and none of the new versions. So `snapInvalidate()` runs wherever a run ENDS
    (beside `annResolveSent`, the template's existing "that turn is over" moment:
    the poll loop's `done` branch and `resumeRun`'s), drops the cache and
    **refetches immediately** — the list is on screen from the moment the panel
    mounts, so leaving a stale one up is worse than spending the round trip, which
    is the argument the revert path already made. (While the panel was collapsed
    this was conditional on its being open; with nothing on screen to go stale,
    reading nothing was the cheaper half.) It is gated on `snapMounted`, because
    that same hook fires for FOLDER targets where the panel never mounted, and a
    hidden section would still have spent a round trip per turn for ever, for an
    answer `_snapshots` refuses. The expanded ROW stays open: `snapshot_plan` is
    re-fetched on every render, so it re-reads against the new bytes instead of
    going stale.
  - **The list is GROUPED INTO PER-SESSION RUNS, because `vN` restarts in every
    one.** FH-4 is a fact about the store; listing on arrival made it a fact on
    screen. Five chats that edited a file give five chains each beginning at v1,
    and the flat merged list therefore showed "v2" three times — *"why do we have
    multiple snapshots of the same version"*. The number is real and per-chain, so
    the chain is drawn: one `.snap-runbox` per run, a rule down its left, and a
    heading naming it.
    - **Contiguous RUNS, not a `group by session`.** The row order is
      load-bearing — `_locate` walks the merged timeline positionally to decide
      where a revert lands — so `snapRuns` may only insert boundaries, never
      reorder or merge. A session that edited the file, left, and came back gets
      TWO headings, which is what happened.
    - **The heading is named from the SIDECAR**, using the same `preview` string
      the "Recent chats" rows above it are labelled with, so the two sections of
      the landing page agree about what a chat is called. `loadRecent()` fills that
      map and **repaints the headings when it finishes**, because the two reads
      RACE rather than being ordered: "Back to chats" fires both at once, and on a
      page opened straight into a resumed chat (`?session_id=`) that is the panel's
      first read, so whichever round trip lands first would otherwise decide
      whether the headings have names — with nothing coming back to fix them.
      Ordering the call sites would have worked today and broken on the next path
      added onto the landing page. A MISS is ordinary, not a
      degraded state: this store records every Claude Code session that touched the
      file, terminal ones included, and those were never in this file's sidecar —
      so the fallback claims only "chat" and puts the session's short id beside the
      count, that id being the one handle that separates two unnamed chains. The
      full id stays on the heading's `title`, and on each row's, so a row read out
      of context can still say which "v2" it is.
  - **The read declines BOTH of the reader's costs.** Enrichment reads session
    transcripts (5 MB+), so the panel takes the unenriched timeline — which is why
    it never claims a chain is complete (FH-3). The exact deltas are `difflib` per
    version, so it takes the undiffed one too (FH-19) — which is why its counts
    wear a `~`. (A `snapshot_plan`/`snapshot_revert` does both for real: paid once,
    on an explicit click, and an unenriched plan cannot see the did-not-exist
    boundary while an undiffed one has no diff to show.)
  - **The panel is offered on EVERY path onto the landing page**, the boot with
    no session and "Back to chats" alike. A page opened straight into a resumed
    chat (`?session_id=`) never runs the boot's landing branch, so a panel
    mounted only there could never appear for the rest of that page's life.
    Returning to the landing page does **not** drop the cache: it is the same
    file's history either way.
  - **Every absence is a LINE OF TEXT inside the section**, the reader's
    own `note`: "no store on this machine" (Claude Code has never run here) and
    "a store with nothing for this file" are distinguished by that sentence
    rather than by whether a panel exists. *This overturns the earlier "no store
    at all → no panel" rule, which `annotate` also drew:* a heading that vanishes
    once its own read comes back reads as a bug, not as "this feature does not
    apply".
  - **A worktree is a different file, and the panel will say so by saying
    nothing.** The store key is `sha256(abspath)[:16]` (FH-2), so the same file
    checked out at two paths — a git worktree, a second clone — has two unrelated
    chains, and the copy that was never edited under *this* path correctly reports
    "Claude has no recorded versions of this file." Known and not worked around:
    keying on anything but the absolute path would mean guessing which of two
    files a checkpoint belongs to.
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
- **FH-19** **The exact line delta is opt-out, because it is the entire cost of a
  timeline.** `_delta` takes `exact=`, threaded through `_scan`, `list_versions`
  and `timeline` as `deltas=`, and **defaults to True** — the opposite direction
  from `enrich`, because the complete answer is what a caller who did not think
  about it should get. `deltas=False` takes the O(1) net line counts the byte cap
  (`DIFF_BYTE_CAP`) already produces, flagged `exact: False`, and the flag only
  ever NARROWS: above the cap the answer stays inexact however it was asked for,
  or `deltas=True` would be a way back into the quadratic diff the cap exists to
  prevent.
  - **What it buys.** Profiled on a 453 KB file with 12 checkpoints: 292 ms total,
    `difflib.find_longest_match` **290 ms** of it across 4.66M calls, versus 7 ms
    to read all twelve versions and 0.2 ms to enumerate them. Declining it is
    **~49×** on that file and is what lets FH-18 paint its list on arrival.
  - **What it cannot change, and this is the guarantee that makes it safe.**
    `differs` is a BYTE comparison, so `_locate`, `position`, `revert`, `offer`,
    `at_earliest` and `unique_current` are bit-identical either way (FH-3). The
    pair is row decoration; the diff a user actually confirms comes from
    `revert_plan`, which has no such knob.
  - **An inexact delta is a SINGLE net term, not a softened pair, and a renderer
    that prints it as a pair is wrong.** `_delta`'s cheap branch is
    `max(0, ver − cur)` against `max(0, cur − ver)`, so at most one side can ever
    be non-zero: `claude`'s `snapDelta` prints `~−43` or `~+12`, and the word
    "changed" when both are zero (an edit that replaced as many lines as it
    removed — most of them), after the `differs` check that already owns "on disk
    now". Printing the pair was the first attempt and it put `~+0 −43` down every
    row of a file that has only grown, the `+0` being the shape of the arithmetic
    rather than a measurement. **It took looking at the running app to see it** —
    every test passed, and the panel was worse than before the change.
  - **The enumeration is NOT a candidate for the same treatment, and not a
    candidate for the file index either.** FH-2's filesystem walk is 0.2 ms —
    0.07% of the read it was assumed to dominate. Answering it from
    `/api/index/*` instead (§30) measured **8.6 ms** cold and 3.2 ms warm
    *in-process*, before the HTTP hop and the `/api/index/status` probe the JS
    bridge adds, and it would import a staleness this panel cannot afford: the
    index is a background scan, `snapInvalidate` fires the instant a turn ends,
    and the checkpoint written seconds ago is exactly the row the user is looking
    for. The index is the right answer for machine-wide questions; a 44-entry
    `listdir` is already faster than asking anything.

---

## 36. Activity — Background Jobs & the Download Manager (D244)

Goal: work that outlives the call that started it — a model download, a
checkpoint pull, a generation that runs for minutes — is visible from anywhere
in the app, not only from the page that started it.

Before this, every page that started such work drew its own progress bar inside
itself. The shell tears a page's frame down on every navigation, so browsing to
another file while an 8GB model downloaded left multi-GB of traffic happening
with nothing on screen to say so, no way to tell it from a hang, and no way to
stop it short of quitting the app.

- **BG-1** A **background-job registry** (`fused_render/jobs.py`) holds one
  record per long-running operation: `{id, title, detail, kind, state, done,
  total, unit, message, page, cancellable, cancel_requested, started_at,
  updated_at, finished_at}`. **In memory** — the records describe work
  happening in THIS app session, and the durable record of what a page did is
  the call log (§31), not this.
- **BG-2** **The server holds it, not the page**, because the reporter and the
  viewer are different documents: a rendered page lives in a same-origin iframe
  the shell replaces on every navigation, and may not even be in the same
  browser tab as the shell chrome. The record therefore outlives the document
  that reports it, and the same registry answers a detached Python worker
  POSTing to `/api/jobs` directly.
- **BG-2a** **Observing is the other half, and it arrived with a sibling name.**
  `trackJob` writes: a page reports work IT runs. A page can now also start work
  the SERVER runs (§40) and wants to show that progress inline with a ✕ — which
  is a read, of a row it did not create. `fused.watchJob(id)` is that half.
  Named as trackJob's sibling deliberately: D244 called it `trackJob` rather than
  `job` because a bare `fused.job(...)` reads as the job itself rather than as a
  handle, and that still holds — TRACK takes a spec and creates a row, WATCH
  takes an id and looks at one.
- **BG-3** **Reporting bridge** (`static/runtime.js`): `fused.trackJob(spec)` returns
  a handle — `update(fields)`, `finish(detail)`, `fail(message)`, `cancelled()`,
  plus the read-only `cancelRequested` / `state`. Reporting is **decoration**:
  every method is fire-and-forget, no rejection escapes, and a page whose
  reports all fail still does its work. Reports are **serialized** per handle —
  they are deltas, so two in flight can land out of order and walk a bar
  backwards. A **rejected** report (bad id, missing title) warns once to the
  console: silence would leave an author with a page that works and a manager
  that never shows it.
- **BG-4** **Cancel is a REQUEST for a PAGE-owned job, and an ACTION for a
  SERVER-owned one.** For work a page runs, the server does not know what the
  work is or which process is doing it, so `POST /api/jobs/{id}/cancel` only sets
  `cancel_requested`; the reporting page reads it back in the reply to the
  progress tick it was going to send anyway, and stops the way it knows how. The
  row stays "running / Cancelling…" until the work actually stops — a row that
  flipped to "cancelled" while the download carried on underneath would be a lie
  the UI told to look responsive. A job that never declared itself `cancellable`
  shows no ✕ at all rather than a dead one. Since local inference (§40) the
  server ALSO runs work of its own — a model download, a generation — and there
  it owns the process and really can stop it. Same ✕, two meanings, so every
  record carries an `owner` (`"page"` / `"server"`).
- **BG-4a** **Server-owned ids are reserved.** They are deterministic
  (`sys:ai-model:<repo>`), so without a rule a page could post `state: "done"` for
  a download that is still running and the manager would have no way to catch the
  lie. `POST /api/jobs` REFUSES any id under the `sys:` prefix; only this process
  writes those. `owner` follows from the id at creation and is never settable
  from a report body — a page cannot claim to be the server by saying so.
- **BG-5** **Stalled, not frozen.** A `running` record with no update for
  `STALE_AFTER_S` (30s) is reported `stalled: true` — computed on read, so a
  late tick un-stalls it with no timer involved. The UI dims the row and says
  *"No longer reporting"* — plus **which reporter went quiet**, which follows
  from `owner`: *"the page that started it was closed"* for a page-owned row,
  *"the process running it stopped reporting"* for a server-owned one. Blaming a
  page for a model download nobody's page started sends the user to look in the
  wrong place. Either way it is the truth: the reporter is gone, the work very
  likely is not. It is dropped
  entirely after `STALE_DROP_S` (10 min) so a dead reporter cannot wedge the
  list for the session.
- **BG-6** **Retention.** A finished record stays `FINISHED_TTL_S` (30s) — long
  enough to be noticed by someone who was not watching the corner, short enough
  that the manager stays a picture of *now*. An **error is exempt** and stays
  until dismissed (the persistent-error toast's rule, §3). `MAX_JOBS` (64) caps
  the list; over the cap, finished rows are evicted before running ones and
  least-recently-updated first, so a live download is the last thing to go.
  **Live SERVER work is never evicted by the cap at all** (D288), so the list
  can exceed `MAX_JOBS`. For work the app itself runs, the row is not a view of
  the work but its only channel — the ✕'s route to the process, and the
  completion signal `fused.watchJob` polls, which reads a missing row as work
  that stopped and settles a promise that cannot then be un-settled. The cap
  was written for a pathological reporter; a queue of transcriptions (AI-10a)
  made "more than 64 rows of live work" ordinary, and evicting there rejected
  transcriptions that went on to succeed. Page-owned rows stay capped, which is
  where the unbounded risk is (`fused.trackJob()` can mint rows a page never
  finishes); finished server rows stay evictable; and the age sweep still drops
  a running row whose reporter has gone silent, so a crashed worker cannot pin
  one for the session. **The cap is measured against the rows it can actually
  shed**, not against the whole list: counting exempt rows while refusing to
  evict them bounds nothing and merely moves which row pays — over the cap the
  only candidate left was whatever had JUST finished, so it was deleted on the
  next read and a watcher never saw the outcome. A success survives that through
  its artefact; a failure or a cancel has none, so the page reported "no longer
  being reported" instead of the reason. It also silently shortened this
  bullet's own retention promise to zero under pressure.
- **BG-7** **Dismiss is for rows nobody is reporting on** — finished, or
  `stalled`. A live row is refused (409): the only honest way to make it go away
  is to stop the work, and a dismiss that hid a live download would restore
  exactly the state this feature exists to fix. A *stalled* row is not an
  exception to that but the same rule — nothing is reporting, so the row hides
  nothing the app could otherwise say. A dismissed id is remembered (bounded)
  and its **late ticks stay refused**, so a poll loop that runs on after its job
  ended cannot resurrect the row the user just closed; an **opening report**
  (the only report that states `running` outright, which `fused.trackJob()` sends
  once per handle) clears the dismissal and re-opens the row, so a page reusing
  a stable id still gets one for its next real run.
- **BG-8** **API** (`server/routers/jobs.py`): `GET /api/jobs` (unguarded read,
  answering `{jobs, now}` — ages are measured against the SERVER's clock, since
  a throttled tab's `Date.now()` disagrees enough to show a job finishing in the
  future); `POST /api/jobs` (upsert, applying only the keys present, so a tick
  carrying `done` cannot blank the title); `POST /api/jobs/{id}/cancel`;
  `POST /api/jobs/{id}/dismiss`; `POST /api/jobs/clear`. Writes carry the
  `X-Fused` guard (D36). Attribution comes from the `X-Fused-Page` header, the
  same rule the call log uses.
- **BG-9** **Progress ticks are not call-log records** — `/api/jobs` joins
  `/api/calls` in `calls.SKIP_PREFIXES`. A four-minute download reporting at
  1.5s is ~160 records describing nothing that happened, and they would spend
  the rate budget the calls they annotate need.
- **BG-10** **The download manager** (`platform/ui/DownloadManager.tsx`) renders
  the list as one card in the shared bottom-right notification stack (§3),
  between the toasts and the server card — the column is ordered by lifetime
  (seconds / minutes / the session), so nothing long-lived shifts under the
  pointer when a short-lived neighbour expires. Top-level document only
  (`!IS_EMBED`): the list is global, so a copy per pane would say the same thing
  N times. Hidden entirely when there are no records.
- **BG-11** **Indeterminate is a first-class state.** A running job with no
  `total` (or a `total` of 0 — a size not learned yet) draws a travelling fill,
  never a bar parked at an invented percentage: parking is what makes live work
  read as frozen (the install loader's D213 lesson). Under
  `prefers-reduced-motion` the sweep is replaced by a dimmed full-width bar
  rather than left as a stub the blanket rule stopped mid-travel.
- **BG-12** **Overall progress averages the running jobs**, it does not sum
  their bytes: a sum lets one 8GB download swallow a 40MB one, so the header bar
  would sit still while a whole other job ran start to finish. Any running job
  with no numbers makes the overall bar indeterminate rather than optimistic.
- **BG-13** **Poll cadence** is 1s while anything runs, 5s otherwise, and paused
  while the document is hidden. A same-origin `localStorage` ping
  (`fused-render:jobs-ping`, written by the runtime on every report and heard
  through the `storage` event — the mechanism the appearance theme already
  converges through) makes a new job appear instantly. The ping is an
  optimisation only: a Python worker reporting straight to the API runs no JS
  and writes none, so the idle poll is the floor that guarantees its row shows
  up either way.
- **BG-14** **Portable.** `fused.trackJob` **and `fused.watchJob`** are no-op
  stubs in the hosted runtime (the `fused` wheel's copy of the bridge) rather
  than export-blocking calls like `fused.ai` (RH-11): progress reporting is
  decoration, and a page that reports or observes it should still deploy — it
  simply has no manager to talk to. `watchJob`'s stub resolves its `watch` with
  null, the same answer the local one gives for a row that is gone, so a page
  written against it needs no hosted-only branch. **This is an obligation on a
  DIFFERENT repo**: adding to the bridge here is not done until that copy has
  the same name.

---

## 37. AI Models — What the Hugging Face Cache Holds (D249)

Goal: the models, datasets and Spaces this machine has downloaded from the
Hugging Face Hub are visible and accounted for, from a sidebar entry, without
anyone having to remember where the cache lives or run `du` on it.

The cache is shared and invisible: a `transformers` import in a page's Python,
a `diffusers` pipeline, a template someone pasted in, or an `hf download` in a
terminal all write into the same tree, and nothing in the app has ever named
it. It grows in multi-GB steps and the app is the thing that grew it — a
checkpoint pulled by a page the user opened once is still on their disk a month
later with nothing on screen to say so.

- **HF-1** **An inventory that can also clear space** (D250 revisited the
  original read-only posture, which shipped first). The page's first job is to
  show what is cached and what it costs; on top of that it offers exactly two
  deletions — a repo, or one revision of a repo. It never downloads,
  re-downloads, or repairs anything, and no deletion happens without a
  confirmation that names what goes and what it frees. **Bulk age-based pruning
  was offered and withdrawn (D256):** a dialog that selects models by a
  threshold is one wrong click from a multi-GB re-download, and `lastUsed` rests
  on an atime that `noatime` volumes never write — a caveat printed inside the
  dialog cannot make a list built on it safe to confirm in one action.
- **HF-2** **Where the cache is** follows `huggingface_hub`'s own resolution
  order, not a hardcoded `~/.cache`: `HF_HUB_CACHE`, else the deprecated
  `HUGGINGFACE_HUB_CACHE`, else `$HF_HOME/hub`, else
  `$XDG_CACHE_HOME/huggingface/hub`, else `~/.cache/huggingface/hub`. Any other
  order reports "nothing cached" on precisely the machines that care most — the
  ones with a shared model disk pinned by `HF_HOME`. Resolved per request, so
  the answer is the environment the server is actually running in.
- **HF-3** **One row per repo**, decoded from the cache's own directory
  encoding: `models--openai--whisper-small` → `openai/whisper-small`, kind
  `model`; `datasets--` → dataset, `spaces--` → space. A directory carrying
  none of those three prefixes is not a repo folder and is skipped, which is
  also what keeps `.locks/`, `version.txt` and half-written `tmp*` downloads
  out of the list.
- **HF-4** **Size is bytes on disk, measured with `lstat`.** Every file under
  `snapshots/` is a symlink back into the same repo's `blobs/`, so a walk that
  followed them would multiply a repo by its revision count — on a real cache
  that is a page about disk usage being wrong by hundreds of GB. Hardlinks (one
  blob shared by two entries, and what Windows falls back to when it cannot
  symlink) are de-duplicated by `(st_dev, st_ino)` for the same reason. The
  per-row sizes therefore sum to the reported total.
- **HF-5** **Newest-file time, directories excluded.** A repo's stamp is the
  newest mtime among its files *and its snapshot symlinks* — a blob is written
  once, but materialising a revision creates its links, so the links are what
  "last pulled" looks like on disk. Directory mtimes are left out because they
  also move on deletion, which would report a repo someone just emptied as
  freshly used. A repo with no files at all reports no time rather than "now".
- **HF-6** **Biggest first.** The page exists to answer "what is this costing
  me", and a name sort buries the 8GB checkpoint among forty 2MB tokenizer
  repos. Each row also carries its file count, its revision count when it holds
  more than one, and the refs (`main`, a tag) pointing into it.
- **HF-7** **A card has TWO doors, and they lead to different places (D256).**
  The **name** goes to the model's page on the **Hub** — a repo id is a Hub
  address, and the licence, the full model card, the discussions and every
  revision live there, none of it on this disk. **"Explore"** opens it *here*,
  in the model card view (§38), which reads this folder's own files. Both are
  real `<a href>`s, so middle-click and copy-link behave. One control cannot
  serve both: a name that sometimes meant "read about this" and sometimes meant
  "open the local copy" would be a coin flip, and the two destinations answer
  different questions. Explore is visible without hovering — unlike the delete
  controls beside it, which stay quiet until the card is hovered.
- **HF-8** **REVISED (D265): the sidebar entry is UNCONDITIONAL — no gate, no
  probe.** It used to be gated on the cache directory existing (`GET
  /api/ai-models/status`, one `isdir`, published to whatever sidebar was
  mounted because the answer can flip mid-session), on the argument that a
  machine which has never pulled from the Hub should not be offered a page that
  can only say "nothing here". Discover (§39) retired that argument: the page
  DOWNLOADS now — the suggested models, through the local runner (HS-1 as
  amended by D258) — so on exactly the machine the gate was hiding it from, it
  is the way to get a first model. Hiding the only door because the room is
  empty is worse than an empty room with a door — and the gate's own cost was
  real: a probe endpoint, a session cache with two TTLs, and a publish channel
  so the page could tell a sidebar what it had just learned, all of it in
  service of removing a row. Both nothings still get named on arrival — no
  cache directory at all, or a cache that is empty — and each ends in the same
  offer, a control that opens Discover.
- **HF-9** **Scanning happens once per visit.** The walk touches every blob in
  the cache, so it runs **on mount and nowhere else** — never on a focus/return
  tick, which would re-walk tens of thousands of files each time the window came
  back, and no longer behind a Refresh button (D256), which asked the user to
  know when a re-walk was worth paying for. A delete answers with the fresh
  listing it just measured, so the one thing that changes the cache from here
  refreshes it without being asked.
  It runs in the threadpool (a sync endpoint), so a big cache cannot stall the
  requests the rest of the page is making. And it **never fails because the
  cache changed under it**: a download finalising or another window's delete can
  take an entry away between the listing that found it and the stat that asks
  about it, so every such read treats the race as "report what was there" — a
  row fewer, never an error page.

**Managing it** (D250, narrowed by D256). Two deletions — one repo, or one
revision of one — each behind a confirmation naming what goes and what it frees:

- **HF-10** **Deleting a repo** removes its cache folder and the `.locks/` entry
  that mirrors its name, and reports the bytes it held. Nothing else in the
  cache is touched.
- **HF-11** **Deleting a revision removes only what that revision alone owns.**
  Its snapshot directory goes, and with it the blobs no *other* revision
  references — a blob two revisions share stays, because taking it would corrupt
  the revision left behind, which is a worse outcome than any amount of wasted
  disk. Refs pointing at the deleted commit go too (a ref to a revision that no
  longer exists is dangling, and would make the next `from_pretrained` resolve
  to nothing). If it was the **last** revision, the whole repo folder goes: a
  shell of refs and unreferenced blobs is not something to leave behind, and it
  is what `huggingface_hub`'s own delete does with a last revision. The number
  shown against a revision is therefore its **exclusive** bytes, with what it
  shares stated separately — two revisions of a 7GB model that differ in a
  config file are 7GB shared and a few KB each, and a row claiming 7GB apiece
  would be a lie in the one column this page exists for.
- **HF-12** **RETIRED (D256).** Bulk age-based pruning is gone. What it stood
  on remains true and still shapes the page: `lastUsed` is filesystem atime,
  `noatime` volumes never write it, and a repo with no readable timestamp proves
  nothing about being cold — which is exactly why "used 4 months ago" is a fact
  on a card someone reads before deleting one model, and not a threshold that
  selects twenty. Deleting still names its targets one at a time (HF-13), and
  the multi-target request shape survives because a revision delete uses it.
- **HF-13** **A delete request names a cache FOLDER, never a path.** The name
  must be a single path segment carrying a known kind prefix; the path is built
  server-side from the cache dir the server resolved. A repo folder that is a
  **symlink** is refused rather than followed — those point at another disk, and
  deleting through one would reach outside the directory this endpoint is scoped
  to. The POST carries the `X-Fused` guard (D3) like every mutating endpoint:
  it removes multi-GB directories, and a blind cross-origin POST must not reach
  it. A malformed revision is an error, never a fallback to "delete the whole
  repo" — only an *absent* revision means the repo.
- **HF-14** **Every target is reported.** One stale row must not lose the rest
  of a multi-target request, so each target succeeds or fails on its own and the
  failures come back named. The reply is the **fresh listing**, re-read from
  disk after the deletions, so the page swaps in state it just measured instead
  of patching rows it hopes are still true.
- **HF-15** **Reading the page does not count as using a model.** Resolving
  revisions means opening ref files, which bumps their atime — the very signal
  `lastUsed` reports. Their atime is put back after the read, so the page cannot
  quietly rewrite the "last used" of everything it just looked at into now,
  which would make the one number a person deletes by say the same thing about
  every model they own.

**Naming what a model is** (D251). A repo id and a size do not say what a thing
is for, and the cache states it nowhere:

- **HF-16** **The purpose is read from whatever evidence the download brought**,
  best first: the model card's `pipeline_tag` (the Hub's own answer, so nothing
  is inferred when the card came down); a diffusers `model_index.json`
  `_class_name`; a sentence-transformers marker; the `architectures` head in
  `config.json` (…`ForCausalLM` → text generation, …`ForImageClassification` →
  image classification, with `model_type` separating whisper's
  …`ForConditionalGeneration` from t5's); a `.gguf` weights file. Tags render by
  turning hyphens into spaces, which needs no table to stay current — except
  where that produces something unreadable: `image-text-to-text` becomes
  "image + text to text" (an image AND a prompt in, text out) rather than the
  unparseable "image text to text". **The hover explains the label, not just its
  provenance**: one sentence saying what goes in and what comes out ("takes an
  image AND a prompt, answers in text"), because the Hub's vocabulary is jargon
  to anyone who has not met it, and a card that shows a term without defining it
  has told the user only that they don't know it. The glossary is keyed by the
  LABEL, so one table serves the model-card path and the architecture path; a
  tag it has no sentence for still shows its label and its source, which is what
  an open vocabulary degrades to gracefully. **Every answer
  carries where it came from**, and the card shows that on hover: a
  `pipeline_tag` is a fact and an architecture is a reading of one, and a UI
  that rendered them identically would be overclaiming. A repo with none of that
  evidence says nothing rather than guessing from its name.
- **HF-17** **Parameter count is exact or absent.** It is summed from the
  **shapes in the safetensors headers** — a little-endian u64 length then that
  many bytes of JSON, so it costs one small read per shard rather than the
  multi-GB file. `.bin` pickles and `.gguf` carry no equivalent cheap header, so
  those repos report no count; a figure derived from file size ÷ assumed dtype
  would be a guess dressed as a measurement, in a page whose credibility rests
  on its numbers being real. The revision is **walked**, not listed: a diffusers
  pipeline keeps its weights per component (`transformer/`, `vae/`,
  `text_encoder/`), which is the very layout behind the pipelines HF-16 detects,
  so a top-level-only look would report nothing for the models people most want
  the number for. What is summed is every component — the parameters the repo
  actually holds, not a curated idea of which component is "the model" — with a
  precision variant skipped when its plain counterpart is present
  (`…fp16.safetensors` beside `….safetensors` is the same tensors twice) and a
  blob counted once however many names link to it.
- **HF-17a** **A quantized checkpoint stores several weights per element**, so
  summing shapes counts storage slots rather than parameters: a 4-bit MLX or
  GPTQ checkpoint packs eight weights into each `U32`, and a 12B model reported
  2.4B — not a small error but a different number. When `config.json` declares a
  width (`quantization.bits`, `quantization_config.bits`/`w_bit`/`load_in_4bit`),
  integer tensors are expanded by how many weights their word holds and the
  count is shown with a **≈**: it is recovered from the declared width, not read
  off unpacked shapes, and the card must not present the two identically. The
  width is read from the checkpoint, never from the repo name —
  `mlx-community/…-4bit` is a naming convention, and no number this page prints
  may rest on one. The declared width is also shown in its own right ("4-bit"):
  it is the difference between a 7GB download and a 24GB one of the same model.
- **HF-18** **The date shown is `added`, not "released".** The Hub's release
  date is not on this disk and this page never goes to the network, so what it
  states is the oldest file in the repo — when this machine first got it. The
  distinction is the honest one: a model published in 2023 and pulled here last
  week is a week old *to this cache*, and that is the fact the page can back up.
- **HF-19** **Metadata is read through the same atime-preserving read as the
  refs** (HF-15) and cached per snapshot directory, keyed by its mtime — a
  snapshot's contents are immutable once written, so a Refresh over forty repos
  re-reads nothing.

---

## 38. The Model View — Opening a Cached Model (D252, D254)

Goal: a cached model is a folder full of opaque names — `model-00001-of-00004.safetensors`,
`config.json`, a 40MB `tokenizer.json` — and opening it showed exactly that. One
template makes the folder answer for itself, and it reads only: nothing here
loads weights, imports a framework, or touches the network.

- **MV-0** **A link back out to the Hub (D256).** The view reads the folder on
  THIS disk; the licence, the discussions and every revision live on the model's
  Hub page and nothing here can show them, so the header carries a link to it —
  built from the KIND as well as the id, since a dataset linked as
  `huggingface.co/<id>` is a 404 dressed up as a link, and absent entirely for a
  folder that is not a cache repo (someone's own checkout has no Hub page, and a
  link built from a local directory name would point at a stranger's). The id
  goes through `quote()` on its way into the URL, not an f-string.
- **MV-1** **`model_card` — what this model IS.** Name (decoded from the cache
  folder, or from the repo folder when a snapshot is opened directly, because a
  commit sha is not a model's name), parameters, disk, the model card's own
  summary and tags, the configuration a person actually reads (hidden size,
  layers, heads, context length, vocabulary), a per-file weights table, the
  largest tensors, and the file list. Instant on a 40GB checkpoint, because the
  parameter counts come from the **safetensors headers** rather than the weights
  (SPEC HF-17's rule, and its quantization arithmetic with it — a 4-bit
  checkpoint's count is unpacked from its declared width and marked `≈`).
- **MV-2** **A tokenizer section on the same page — how it splits text.** A
  textarea, a token count, chars-per-token, and the text painted token by token,
  below the card rather than beside it in a second mode (D254): they describe one
  model, and one page beats two. Two halves that fail separately, deliberately:
  the **facts** (vocabulary size, model kind, merges, special tokens) are read
  from `tokenizer.json` itself and always work; **encoding** needs the
  `tokenizers` library, which the template declares in its own `pyproject.toml`
  (PY-16) and which therefore arrives only under the fused engine. A missing
  library is a state the page explains, not an error — and so is a
  `tokenizer.json` this build of the library refuses, which keeps its facts and
  says why it could not load. Tokens are highlighted by **offsets into the
  original text**, never by the decoded piece: every BPE tokenizer rewrites
  whitespace (`Ġ`, `▁`), and showing that instead of what was typed makes the
  highlighting unreadable. The section is **inert until someone types** — the
  card stays instant because parsing a 40MB vocabulary is work it refuses to do
  before anyone has asked to tokenize anything. The older `vocab.txt`+`merges.txt`
  pair is deliberately not served: loading those needs the model class that owns
  them, and a playground that cannot tokenize is worse than none.
- **MV-3** **The view is gated, and the gate runs on every folder you open** —
  it is bound to the universal `/` registry key beside `zarr_aoi`, the existing
  view for one kind of directory content. So it obeys that gate's discipline: no
  listing, no walking, constant-time probes, cheapest first. It accepts a cache
  repo folder (a name check plus one `isdir`), a folder carrying a decisive
  marker (`model_index.json`, `config_sentence_transformers.json`, or
  `tokenizer.json` — a tokenizer-only repo is a real shape and the view now has
  something to say about it), or a `config.json` — and that last case only after
  ONE bounded read confirming the file is a model config, since `config.json` is
  among the most common filenames there is and a folder of application settings
  must not sprout a model view. That read is reached through `isfile` **before**
  `os.stat`, and the order is load-bearing: over a mount `isfile`/`isdir`/`exists`
  can be answered from the listing the endpoint already took while `stat` cannot,
  so probing with `stat` would make every ordinary folder pay a remote round trip
  for a name already known to be absent.
- **MV-4** **The AI Models cards open the view by name.** A gated template can
  never be a folder's default mode (CT-12), so a model folder still lists like
  any other folder for anyone who browses to it — but from the AI Models page the
  repo IS a model, so its card navigates with `_mode=model_card`. The switcher
  then offers that mode beside the folder's own.
- **MV-5** **Looking at a model is not using it.** Every read in the template
  restores the file's atime, for the same reason the AI Models page does
  (HF-15): "last used" is the number a person weighs before deleting a model,
  and looking at one must not rewrite that number into now. **Including the
  gates** — they read too (`config.json`, `refs/main`), they run on every folder
  the user opens, and a gate is the last thing that should mark a model as
  recently used. That `os.utime` is the one WRITE a gate makes, so the mount
  shim (CT-12's routing) **drops it on a mount path** rather than routing it: a
  mount has no atime worth preserving, and a kernel SETATTR there is exactly the
  class of call the shim exists to keep gates from making.
- **MV-6** **The folder the AI Models page opens is a cache REPO, and the
  layout has exactly ONE owner.** A repo folder (`models--org--name`) holds
  no model files itself — they live under `snapshots/<commit>/`, at the revision
  **`refs/main`** names, which is the one a load would get. `inspect_model.py`
  resolves that once when the card is drawn and reports it as `root`; the page
  hands `root` straight to `tokenize_text.py`, which therefore resolves nothing
  and reads one file in one folder. That hand-off is the point: a template is
  scripts the engine runs, not a package, so two scripts cannot share a module
  (PY-15/D166) — and a second copy of the cache layout is a second thing to drift
  from the first. Passing the answer instead of re-deriving it removes the drift
  rather than testing for it.
- **MV-7** **Nothing survives between calls, so the playground is built not to
  need it.** Each `runPython` is a fresh subprocess (PY-6); a tokenizer held in a
  module-level dict is dead code that reads as an optimisation. What keeps typing
  responsive instead: the **facts are requested on the section's FIRST call and
  never again**, so every keystroke after it skips the whole-file parse of a
  `tokenizer.json` that is routinely tens of MB. Facts and encoding travel on that
  one call rather than two, because loading the file to encode and parsing it for
  facts are one visit to one file — and a facts-only call (no text yet) never
  loads at all, since there is nothing to encode and the load would be paid for
  and thrown away. The per-call cost that remains is the library's own load, which
  is Rust and measured in tens of milliseconds; holding one tokenizer open across
  keystrokes would need a resident process, which is a different design from a
  script.

---

## 39. Discover — Searching the Hub From the AI Models Page (D255)

Goal: §37 answers "what did I already download". This answers the other half —
"what is out there" — and the two are only worth anything **together**, because
the Hub does not know your disk and a browser tab open on huggingface.co cannot
tell you that the model you are reading about is already cached, was last read
three weeks ago, and would cost nothing to open.

- **HS-1** **AMENDED (D258): downloading is offered, and the reasoning is
  unchanged.** The original rule was that a download needs a progress surface, a
  cancel and an answer for a half-finished pull — none of which existed, so the
  button did not either. Local inference (§40) built exactly that, so Discover
  now downloads through it. What still holds: search, filter and sort tell you
  what a result would COST before the click, and this module still never writes
  to the cache itself — it asks the runner's worker to, and the job registry
  shows it. Downloading gigabytes onto someone's
  disk is a separate decision with a separate cost (free space, a progress
  surface, a resumable transfer, a half-written cache to clean up) and is
  deliberately not part of this. Every route is a GET, so none carries the D3
  `X-Fused` guard: there is nothing to guard.
- **HS-1a** **Search is a guarded POST; the rest of Discover is an ordinary
  read.** The app's rule is that reads are unguarded GETs (WF-5), and the reason
  is D36's: a foreign page can fire a request but the browser will not let it
  read the reply. That protects the RESPONSE and says nothing about the REQUEST
  — and search is the one read in this app that LEAVES the machine, calling the
  Hub with the user's token attached (HS-3). Unguarded, a blind cross-origin GET
  could spend someone's credential and their rate limit while learning nothing,
  which the same-origin policy does not prevent. Rather than bolt a guard onto a
  GET — leaving a shape that contradicts the stated rule and invites the next
  reader to copy it — the route takes the shape its effect deserves: an outward
  effect is a POST, and POSTs carry `X-Fused`. `hub/tasks` stays a GET beside
  it, a static glossary that touches nothing. **What earns the guard is the
  outbound call, not the router**, and the asymmetry inside one module is the
  clearest possible statement of that. Still not authentication (D3 stands).
- **HS-2** **The join is the feature.** Every result is cross-referenced against
  the local scan before it is returned, so a card says **downloaded** (with what
  it costs on disk and when it was last read), **partly downloaded**, or
  **not downloaded**. `partial` is a real state and not a rounding of the other
  two: an interrupted pull leaves a repo folder holding blobs and no
  materialised snapshot, and calling that "downloaded" sends someone to a model
  that cannot load. The line is "has at least one snapshot" — and the page holds
  the same line, so only a **downloaded** result opens its model card. A partial
  one links to the Hub like an absent one does, because there is no revision for
  the card to describe and linking there would hand someone the very failure the
  distinction exists to prevent.
- **HS-3** **The server fetches; the page never does.** One place holds the
  token, bounds the timeout, caches, and can be audited for what this app sends
  to a third party. The **host is fixed** — only the query string varies, so no
  request can point this at another server — with `HF_ENDPOINT` (the standard
  mirror override `huggingface_hub` honours) the one exception, and it is still
  checked to be an http(s) URL before it is used. The query is **encoded**, never
  concatenated: a search for `a&b=c` is a search, not a second parameter. The
  sort is a **fixed set** of names, so a client can never pass a raw field
  through to the Hub.
- **HS-4** **Nothing reaches the network until Discover is opened.** The app is
  a local file explorer; a page that quietly queried a third party on mount
  would be a surprise. Selecting the tab is the consent, the caption names the
  host being asked, and the query is debounced — a burst of typing is one
  request — with identical queries inside a short TTL answered from memory,
  because search-as-you-type would otherwise put one request per keystroke on a
  public API. **Errors are never cached:** the network comes back, and the next
  keystroke has to be allowed to find out.
- **HS-5** **The local half of a result is never served stale, and is scoped to
  the results.** The Hub's answer holds for the TTL; what is on this disk does
  not, so the join runs on every request, outside the cache — a model deleted a
  second ago must stop claiming to be downloaded, or its card links to a folder
  that is no longer there. It can afford that because it costs what the RESULTS
  cost rather than what the cache costs: one `scandir` of the cache root to map
  ids to folder names, then a measure of only the handful of rows that turned
  out to be present. The §37 listing would answer this too, but it also reads
  every repo's model card, config and safetensors headers to say what each model
  is FOR — work no row here needs, and work a debounced keystroke must not pay
  for across a cache of hundreds of repos.
- **HS-6** **Sizes are recovered, and say so.** `safetensors.parameters` is a
  dtype → count map, so bytes come from summing `count * bits / 8` — the same
  arithmetic and the same `≈` the model card uses on local files (HF-17), so one
  model cannot be 16GB on one tab and 8GB on the other. A repo with no
  safetensors metadata reports **no size** rather than a guessed one: a number
  someone plans a 16GB download around must not be invented. `gated` is surfaced
  before anyone tries, because "accept the licence on the Hub first" is worth
  knowing in advance rather than as a 403.
- **HS-7** **One vocabulary across both tabs.** A result's task label and its
  hover sentence come from the same glossary the cached cards use (HF-18), so
  "image + text to text" means the same thing wherever it appears. The task
  FILTERS, though, are the Hub's own `pipeline_tag` values, listed explicitly in
  the module that talks to the Hub — deriving them by reversing the glossary is
  the tempting version and it is wrong, because several labels there ("image
  generation", "video generation") are this app's reading of a diffusers
  `_class_name` rather than tags anyone publishes under, and a filter built from
  one would quietly return nothing. Every offered filter resolves to a label the
  glossary explains, and a test pins that.
- **HS-8** **A far side that is unhappy is a sentence, not a 500.** Unreachable,
  rate-limiting, refusing without a token, answering with HTML — each produces a
  200 carrying an empty result and an explanation the page can show, because the
  request this server received was fine and the distinction matters to whoever
  is looking at it. Every field of a result is optional: the Hub returns what it
  returns, an older deployment may refuse an `expand[]` field entirely, and a
  missing field is one a card leaves out rather than an exception.

---

## 40. Local Inference — Running a Model on This Machine (D257)

Goal: `fused.ai(prompt)` meant exactly one thing — a completion from the Claude
Code CLI — while local models lived inside individual apps. `local_chat` shipped
its own MLX server, its own dependency declaration, its own download reporter and
its own curated model list; the image app shipped the same apparatus again for
diffusers. Two copies of "how do I run a model", neither reusable by a third, and
an AI Models page that could say what was on disk but not what was *running*.

- **AI-1** **One door, two tiers.** `POST /api/ai` is extended, not replaced: its
  `model` parameter already existed, and a value containing a **slash** is a
  Hugging Face repo id and therefore local, while one without is a Claude alias.
  That is not a heuristic — a Hub id is always `org/name` and no Claude alias
  contains a slash. So `fused.ai(prompt, {model})` reaches a local model with no
  new parameter, the streaming shape is byte-identical (`{"type":"chunk"}` lines
  closed by `{"type":"done"}`), and **a call with no `model` still means Claude**,
  which is what keeps every page written before this working.
- **AI-1a** **A conversation and a stop, because a chat client needs both.**
  `prompt` stays the thing being asked NOW, and `history` carries the turns
  before it — so adding it changes no existing call, and the turns reach the
  worker as MESSAGES for the model's own chat template rather than flattened
  into one string (flattening is how you get output that looks almost right).
  Local models only: the Claude path is one invocation with no conversation to
  resume, and it **refuses** history rather than dropping it, because silently
  ignoring it answers a follow-up as if it were the first question — which reads
  as the model having forgotten, not as the API having declined. `POST
  /api/ai/cancel` stops the generation in flight WITHOUT unloading, so the next
  message answers immediately; it returns false when there was nothing to stop,
  which is not an error (a Stop pressed as the last token lands is a no-op).
  **`raw` is refused on the Claude path for the same reason as `history`**: it
  means "no chat template", which only something owning the template can honour,
  and the CLI does not expose one — dropping it would answer a raw continuation
  as a chat turn, which is plausible text that is silently not what was asked.
- **AI-1b** **The terminal frame carries the RESULT, on both tiers and in both
  shapes.** `fused.ai()` resolves with `{text, model, usage}` whether or not the
  caller passed `onChunk`, so a page can stream and still use the return value —
  and a streamed local reply that closed with a bare `{"type":"done","ok":true}`
  is why this is a written rule rather than an obvious one: every token had
  already been delivered, the answer was on screen, and the caller crashed on
  `result.text` at the end of a visibly successful generation. The chunks are
  a VIEW of the completion, never the only copy of it, so the relay accumulates
  what it forwarded and states it at the end; a failed stream closes with
  `error` in the same frame, so a caller has exactly one place to look either
  way. Pinned by contract tests over both the streaming and non-streaming
  relays, because this is an agreement between the worker, the relay and
  `runtime.js`, and the failure mode is silent in all three.
- **AI-2** **A runner is a folder, and its environment is `envinstall`'s.** Each
  backend is a folder holding a `pyproject.toml` and a `worker.py`. The
  declaration is the ONLY place mlx/torch are named — fused-render's own venv
  must stay a file explorer's, and must not carry a Metal-only dependency into a
  Windows wheel — and it is built by the existing detached `uv sync` loader
  (PY-18), with the same progress record and the same verbatim uv errors any
  declaring folder gets. No second install mechanism exists for AI.
- **AI-2a** **A runner declares WHEELS, and uv's children do not inherit this
  process's Python environment** (D266). Two halves of one failure. A dependency
  uv cannot download as a wheel is compiled by a build backend in an interpreter
  uv creates — and that interpreter inherits the installer's environment, which
  inside the macOS .app carries py2app's `PYTHONHOME` pointing into the bundle.
  Build interpreters therefore loaded the app's own frozen `_distutils_hack`
  over the setuptools doing the build, and every source build in the packaged
  app died on `No module named 'jaraco.text'` — reported to the user as a runner
  environment that would not build, on the one runner that had a `git+` pin.
  `_env_install_worker` scrubs `PYTHON*`/`VIRTUAL_ENV` from every uv invocation
  now, the same names every other child spawn in this app already strips. The
  wheels-only rule stands on its own merit beside it: a runner environment is
  built on a user's laptop the first time they press Download, and compiling
  from source there is minutes of their battery for something a release already
  answers. Held by a test over every runner's declaration.
- **AI-3** **Four routes, and that is the whole worker contract.** `GET /health`
  (state, resident bytes), `POST /generate` (NDJSON for text), `POST /cancel`,
  `POST /quit`. Adding a capability is writing a worker, not extending the
  supervisor. The port is **ephemeral and published by the child** — anything the
  parent reserved could be taken between its bind and the exec — and every
  request carries a per-worker token in a header, so a foreign page that guessed
  the port still cannot drive the model.
- **AI-4** **One resident model per capability, auto-evicting.** Loading a second
  text model stops the first BEFORE the new one loads. Arithmetic, not taste: two
  8GB models on a 16GB machine is a swap storm, and a swap storm reads to the user
  as "the app hung". A text model and an image model coexist — different
  capabilities, and the user asked for both.
- **AI-5** **Load is a job, never an awaited request.** A cold load is a multi-GB
  download and a minutes-long weight load, so `load()` returns a **job id** and the
  work continues on a thread. Progress goes to the existing download manager
  (§36) under the deterministic id `sys:ai-model:<repo>`, which is also what lets
  the AI Models page join a job row onto a repo card without a second index.
  Generating with a model that is not resident **starts the load and fails with
  that job id** (409): a caller should not have to orchestrate load-then-wait
  before its first call, and generation must not block for minutes either.
- **AI-5a** **A download is part of the runtime, even though it is not
  residency.** `GET /api/ai/runtime` reports `downloading` beside `loaded`: a
  weights-only pull holds no memory and evicts nothing, but it is work this
  machine is doing, and a runtime that omitted it made an 8GB fetch invisible —
  the page polls job rows only while the runtime says something is happening, so
  a Download reported progress nothing was reading, and the card that started it
  went on saying "not downloaded". The BYTES stay in the job row; this list only
  says which models have a pull in flight. A second Download of a model already
  being fetched **joins the first** rather than starting a second
  `snapshot_download` over the same `.incomplete` files.
- **AI-5b** **Download progress is measured from the DISK.**
  `snapshot_download` exposes only its outer "Fetching N files" counter through
  `tqdm_class`; the per-file byte bars are internal. Reporting that counter as
  bytes is how a 4.6GB pull came to read **"10 / 11 B"**, and during a single
  large shard it does not move at all — so the row also went stale mid-download
  and the manager declared nobody was reporting. The runner instead walks its own
  repo folder (counting the partial files, skipping the snapshot symlinks) on a
  **one-second poll that doubles as the heartbeat**, and the repo's total comes
  from one Hub metadata call. No total means an indeterminate bar, which is
  honest; a wrong total is not. Since AI-5i the partial file being counted is
  usually **ours**, and it is measured by allocated BLOCKS rather than by length:
  segments write out of order, so the file is created at its final size and
  filled sparsely, and `st_size` would report a 4.6GB download as complete before
  a byte had arrived.
- **AI-5c** **The port handshake file is per BRING-UP, never per capability.**
  Two workers for one capability really do overlap — an eviction's replacement
  starts while the old one is still being killed, a Download runs beside a Load —
  and when they shared `<capability>.json` the second one's `unlink` deleted the
  port the first had just published, so the first sat out its entire bootstrap
  timeout waiting for a file that was never coming back. The name carries a
  random per-worker id (never the token: a secret must not become a filename),
  and both the status and log files are removed once the process is gone.
- **AI-5d** **A bring-up thread reports its own death, and the environment
  wait polls the key the INSTALLER named.** Two failures of the same kind — work
  that stops without saying so — seen as one card reading "Preparing Diffusers
  (PyTorch)…" beside a manager row reading "no longer reporting". (a) `_bring_up`
  and `_fetch_only` run on threads, so an exception that is not a
  `SupervisorError` is raised to NOBODY: it kills the only thing reporting and
  the row sits at its last detail until the manager gives up and blames the
  process — a lie in the one direction that matters, since the server is fine
  and nothing says the load is gone. Both catch **everything**, name the
  exception class on the row (the only part a user can act on) and log the
  traceback, which is otherwise the sole copy. (b) The venv wait takes its key
  from `envinstall.start()`'s reply, never from a second derivation of its own:
  with no pinned interpreter yet (D214) the first round installs the PYTHON
  under `PYTHON_BOOTSTRAP_KEY`, and a re-derived venv key polls a record nobody
  is writing — an infinite "Preparing…" over an install running fine.
  `envinstall._reported` exists to hand a caller the right key and its docstring
  names this exact failure; this was a caller that recomputed it anyway. It
  therefore also runs **rounds**: every other caller of that loader is a page
  that re-POSTs for the second round, and there is no page here.
- **AI-5e** **The ✕ reaches every phase, and shutdown reaches every process.**
  Two halves of "the supervisor owns it, so the supervisor can really stop it"
  (D258), each of which had a hole. **Cancel**: `stopping` is set by an eviction
  or an explicit unload — things the SERVER decided — while what a user presses
  sets `cancel_requested` on the JOB. The env-build loop honoured that and the
  post-spawn loop did not, so a ✕ during the phase that actually takes the time
  (the multi-GB fetch the worker is doing) did nothing at all and the download
  ran to completion under a row that said cancelled. Both loops read both.
  **Shutdown**: `unload_all()` walked the RESIDENT table, and a weights-only
  fetch is deliberately not in it — it evicts nothing and holds no memory — so
  quitting the app left a detached `snapshot_download` pulling gigabytes with
  the only thing that could stop it gone. The fetch's process handle is kept in
  its own table, published nowhere, and shutdown terminates those too — from
  the moment the work starts, not from the moment there is a download PROCESS,
  because the first phase may itself be a multi-GB `uv` install and registering
  after it left the fetch invisible for exactly those minutes. What "stop this
  worker" means therefore depends on its phase, and `_terminate` knows both: an
  environment build is cancelled by KEY (it belongs to a detached installer,
  and there is no process of ours to kill yet), a running worker is killed.
- **AI-5f** **Deleting a model's files asks the supervisor first.** The cache
  endpoint owns the bytes and the supervisor owns the processes, and neither
  could see the other. Deleting a repo mid-load removes the shards
  `from_pretrained` is still reading and the error surfaces minutes later
  looking like a corrupt model; deleting a RESIDENT one is quieter and worse,
  because the weights are already mapped — on POSIX the delete succeeds, the
  page says the model is gone, and it goes on answering until an unload makes
  the bytes vanish for real. `busy_reason(model)` returns a SENTENCE, not a
  bool, because the instructions differ ("unload it first" vs "wait for the
  download"), and a revision is refused on the same grounds as a repo: the
  revision a resident worker holds open is not the safer target it looks like.
  The button is disabled for the same reason and is the courtesy; the endpoint
  is the guarantee (MD-11).
- **AI-5g** **A prerequisite this machine lacks is stated at the REQUEST.**
  `uv`, and — less obviously — the **`fused` package**: `envinstall` is the
  loader for the fused engine (PY-18) and reads the base interpreter off that
  engine's live backend, so a machine running the builtin engine cannot build
  any runner venv at all. Unchecked, that surfaced as a bare
  "ModuleNotFoundError: No module named 'fused'" on a model download card, which
  names neither what to install nor why a download wanted it. Both are knowable
  before a job row exists, so both are a 409 on the request with a sentence the
  page can show.
- **AI-5h** **Every reporter heartbeats, because "no longer reporting" must
  never be said about work that is simply slow.** A row untouched for
  `STALE_AFTER_S` (30s) is reported as stalled — true of a page that was closed,
  a LIE about a worker mid-step. AI-5b made a download's disk poll double as its
  heartbeat; the image runner reports once per DENOISING STEP, and a FLUX step on
  a laptop routinely takes longer than the whole stale window, so a render that
  was progressing perfectly announced at **step 1 of 3** that nobody was
  reporting it. The heartbeat therefore lives in `worker_base` and wraps every
  generation, because the property that causes this — progress whose natural
  granularity is coarser than the stale window — belongs to the CONTRACT rather
  than to one denoiser. It re-sends the LAST payload, never an invented one (a
  tick that learned nothing must not move the bar), it is plain `report` and
  never `report_or_cancel` (a `Cancelled` raised on a timer thread is raised at
  nobody — the ✕ is still honoured in the generating thread's own tick), and it
  never repeats a TERMINAL state, which would revive a row the manager had
  already retired. Two consequences of it being a THREAD, both found in review:
  it sends through the half of `report` that does NOT re-record the payload (a
  beat that re-recorded what it had just read clobbered any real tick landing in
  between, so the bar went BACKWARDS while the model progressed — a worse lie
  than the stall), and the context manager JOINS it rather than only signalling
  it, because `stop` cannot reach a beat already inside its POST and that tick
  would land after the work finished, flipping a row the supervisor had just
  marked done back to running.
- **AI-5i** **The weights are fetched on several connections, and an interrupted
  fetch resumes.** `snapshot_download` opens one connection per file and one file
  at a time, so a model whose bytes are a single 4.6GB shard downloaded at
  exactly one connection's speed — and a cancel, a crash or a quit threw all of
  it away, which is not a corner case: the supervisor KILLS the fetch on quit
  (AI-5e). `worker_base` therefore fetches the repo itself, stdlib only. **One
  Hub listing decides three things at once — the bar's total, the files to fetch
  and the REVISION to fetch them at** — because deciding the revision separately
  is how a list taken from a repo's default branch came to be fetched at a
  hardcoded `main`: a different set of bytes, recorded under a ref for a revision
  nobody read, internally consistent the whole way down since every etag still
  matches its content. The revision is asked for by name (`main`, the same
  default hf's own `snapshot_download` uses, so the fast path and the fallback
  cannot land on different revisions of one model) and the fetch is pinned to the
  COMMIT that name resolved to, which also settles the repo moving between the
  listing and the last byte. Then `get_hf_file_metadata` per file at that commit,
  for the CDN location, the etag and the commit,
  then — **carrying the Hub token only when the blob is served by the Hub
  itself**, since a presigned URL already holds its credentials in the query
  string and S3 refuses a request bearing two of them, which made every download
  by a token-holding user fail over to the slow path — up to
  **4 `Range` segments per file** with **segments across all files** as
  the units of work in one pool capped at **8 connections** — the single number
  that bounds how many sockets a download opens, which a pool per file would
  multiply out. Segments share one fd and write with `os.pwrite`, and per-segment
  offsets go to a sidecar in the order that makes them true: snapshot the
  cursors, **fsync the data, then write the sidecar** — a recorded byte is always
  a durable byte, so a resume never skips one that was still in flight. The
  partial file is `<blob>.fusedpart`, deliberately **not** hf's `.incomplete`: hf
  resumes one of those by seeking to its length, ours are written out of order,
  and handing it one would produce a silently corrupt blob. Resume demands that
  etag and size still agree and that the recorded LAYOUT is the one resumed with;
  anything that does not agree starts clean, never a guess. **The range probe is
  therefore three-valued**, because two rules turn on the difference between a
  server that says no and a server that does not answer: a probe that FAILS is a
  network condition and leaves both the recorded layout and the host's cached
  answer alone, while a probe that answers NO is a fact — it caps that file at
  one connection, and on a resume it discards the multi-segment layout so the
  file restarts whole. Without that second half, a CDN that stopped honouring
  ranges hands byte 0 to every segment past the first, the refusal takes down the
  entire repo, and the fallback deletes the progress the un-probed resume existed
  to protect. A segment reconnects on an exception AND on a body that simply ends
  early (a server closing mid-stream raises nothing), and treats a failed
  re-resolve as a retry rather than the end of the download. **Its budget resets
  on the CURSOR MOVING across an attempt, not on bytes arriving** — a distinction
  that is a hang in one direction (a server ignoring `Range` and truncating
  re-sends the same prefix forever, and the safe reading of that body rewinds the
  cursor, so a byte-counting budget never expires) and an abort in the other
  (bytes written before a `read()` raised are real progress, and counting them as
  a failed attempt exhausted the budget on a link that reset reliably).
  **Three rules exist because a wrong-content blob under a correct etag is the
  worst failure available here** — hf then serves it from cache forever. (a) A
  200 answering a ranged request at a non-zero offset is refused, and so is a
  **206 whose `Content-Range` does not start where we asked** (a clamping proxy):
  either body written at four segment offsets gives a file of exactly the right
  length and entirely wrong content. (b) The 401/403 re-resolve may replace only
  the LOCATION; a changed etag, size or commit aborts, because the blob path and
  every offset were derived from the first answer and a repo updated mid-download
  would publish a mixture of two revisions. (c) Publishing is gated on the
  per-segment CURSORS, never on the part file's length — the file is pre-sized
  before a byte arrives, so a sparse file of pure holes measures exactly right.
  No hash, like huggingface_hub itself, which relies on TLS and `Content-Length`.
  **Every failure and every incapability falls back to `snapshot_download` /
  `hf_hub_download`** — no range support, a Hub API that moved, a platform with
  no `os.pwrite`, a cache filesystem that allocates rather than holding a sparse
  file, an argument ours does not understand — logging the reason to stderr and
  clearing our part files first, because a download that got faster and sometimes
  broken would be a bad trade. Resume therefore covers the app being killed, quit
  or crashed — the case that motivated it — and not a fetch that fell back, which
  hf re-downloads. Explicitly out of scope: no bandwidth limit, no detached
  daemon (quitting still stops it; the on-disk state is what makes that cheap
  rather than destructive), no per-segment UI, and no cache lock — the etag names
  the content, so two instances write identical bytes at identical offsets and
  the loser of a rename race falls back rather than corrupting anything.
- **AI-8b** **A runner whose weights live outside RSS supplies its own memory
  probe.** AI-8a made the hook for MLX's memory-mapped, lazily-materialised
  arrays; the image runner needs it for an unrelated reason and the number was
  just as wrong — torch keeps the weights in a GPU allocator's pool, which on
  MPS is not counted in the process's resident set, so an 11.9B pipeline
  reported **33 MB in memory**. Both runners now answer for themselves, and the
  test asks it of BOTH with the reason each one needs it, because "supplies a
  probe" is a property of a runner rather than a fact about MLX.
- **AI-6** **Availability is answered with a REASON.** MLX is Apple-Silicon-only,
  so `available()` returns "needs Apple Silicon — MLX runs on Metal only (this is
  linux/x86_64)", and resolution SKIPS an unavailable runner rather than picking
  it and failing at load time — which would report "the model failed to load" for
  a machine that was never going to load it. A capability this machine cannot
  serve is still listed, with its reason: hiding it leaves a user hunting for a
  feature that was never there.
- **AI-7** **Liveness is `poll()`, and stopping is platform-specific.** Never
  `os.kill(pid, 0)`: on POSIX an unreaped child is a zombie and signal 0 to a
  zombie succeeds, so the check answers "alive" for a model that crashed; on
  Windows `os.kill` maps onto `TerminateProcess`, so the *liveness probe kills the
  process it asks about*. Stopping takes the whole process group — a worker
  spawns children that would otherwise keep the weights — through `killpg` on
  POSIX **only when the pid is its own group leader** (the guard `envinstall`
  carries for the same reason: a stale pid in the server's group once shut down a
  test session) and `CTRL_BREAK` + `taskkill /T /F` on Windows, which has no
  `killpg` at all.
- **AI-7a** **The AI Models page shows what is RUNNING, not only what is on
  disk.** A cached card carries a live state row — downloading (with bytes, from
  the job row, because that is the only place byte counts exist), loading (text
  and a pulse, **no bar**: weights going into memory is one opaque step and an
  invented percentage reads as frozen), loaded (with its resident memory), or
  failed (with the reason). **Loaded is said loudly**: a filled badge beside the
  name and a colour change over the whole card, in the same green as the
  sidebar's live dot. A small bullet was the wrong instrument — a grid is read by
  sweeping before it is read by reading, and the one state that costs gigabytes
  continuously has to survive the sweep. **Load / Unload** is a word rather than a glyph and
  is always visible: it is the one control on the page that spends MEMORY rather
  than disk, and it is not offered at all for a capability no runner here serves
  — a button that always fails is worse than no button. **Which capability
  could load a repo is answered by the SERVER** (`capability` on each listed
  repo, or null): the task vocabulary and the capability vocabulary both live
  there, and a page deciding for itself needs a second copy of the mapping — the
  first version of it guessed text generation for every cached repo and offered
  to load a dataset as a chat model. **Every task label is CLASSIFIED, never
  merely absent**: it maps to a capability or it is listed as served by nothing
  yet. A label nobody has thought about and a label that has been ruled out both
  answer null, so they are indistinguishable from the page — which is how
  "image + text to text" lost its Load button while Discover went on
  recommending `gemma-3-12b-it-4bit`, a model carrying exactly that label, as a
  chat model. (A vision-language checkpoint IS the causal LM the text runner
  loads when you only give it text; the image half goes unused until a runner
  wants it.) A **dot on the sidebar
  entry** whenever anything is resident, naming it on hover: gigabytes held by
  something you have forgotten about is exactly what an indicator is for, and it
  is the same treatment being signed in already gets (AC-1).
- **AI-7c** **The tab is URL state, and the cache path and the Hub host are
  links.** The two tabs are **Local** and **Discover** — "cached" names the
  mechanism (a Hugging Face cache directory) where "local" names the thing, and
  local-vs-discover is the pair that reads. Which one is showing lives in the
  URL (`?tab=discover`, the default omitted), the shape Preferences already
  uses: bookmarkable, and — the reason it is worth doing on a page with two
  tabs — **on the back button**, which is where a user reaches for "put it
  back". The page therefore **must not be remounted by its own toggle**: it is
  the one shell route not keyed on the navigation epoch, because remounting to
  change a page's own view state would re-walk every blob in the cache and
  discard whatever was typed into Discover's search. An unrecognised `tab=`
  value falls back to the default silently (PT-9's posture for `_mode`).
  Each tab's caption names a DESTINATION and makes it reachable: the cache
  directory opens in the explorer, the Hub host opens in a new tab. This is a
  file explorer — leaving the path as text asks the user to copy it into the
  very thing they are looking at — and the host is the one place the app
  discloses who it queries, which is worth being able to go and check.
- **AI-7b** **Discover suggests, and the suggestions know what you have.** A
  short curated list per capability — moved out of the apps that used to carry
  it privately — with size, the reason you would pick each one, and a **✓** on
  the ones already downloaded. It shows only when the search box is empty,
  because it answers "what should I even get", which is the question you have
  *before* you know what to type. A capability this machine cannot serve is
  still listed, with its reason. **Downloading is offered here** (D258,
  superseding HS-1's read-only posture): the job-backed machinery HS-1 named as
  the prerequisite now exists, so the ✕ in the manager really stops a pull.
  The ✓ means a **materialised snapshot**, never merely a repo folder:
  `huggingface_hub` creates `models--org--name/` on the first byte, so a set
  built from folder names flipped a suggestion to "✓ downloaded" seconds
  after Download was pressed, over a 4.6GB pull that had barely started. While
  the pull runs the card shows that pull's progress instead — the same three
  states the Hub result cards already draw. And the cache answer is the PAGE's
  one walk, handed down, not a second walk Discover runs for itself: two walks
  meant two definitions of "on this machine" and a window where the tabs
  disagreed about the same repo.
- **AI-9** **Image generation is job-backed, and the reply decides everything
  but the pixels.** `POST /api/ai/image` answers immediately with a `jobId` to
  watch AND with the **path** and the **seed** already settled — so no second
  endpoint exists for "what did I get", and the job record needs no result
  field. The server picks both because it owns where user files go
  (`<home>/ai/images/`, not beside a page that may sit in a read-only folder)
  and because a seed invented inside the worker and never surfaced would make
  every unseeded render unrepeatable. The reply describes the render that will
  actually happen, not the one that was asked for: sides are clamped to
  256–2048 and snapped to a multiple of 16, steps to 100, guidance to 20 — a
  4096² render at 500 steps is an OOM, and a caller echoing its own request
  would mislabel the picture it got. **Unlike text, an image WAITS for its
  model.** `fused.ai` fails fast with a job id because a chat box must not hang
  for a cold multi-GB load; an image caller is already watching a progress row
  for work that takes minutes either way, so the wait belongs inside the job
  rather than being a second failure to orchestrate around. The load keeps its
  own row (`sys:ai-model:<repo>`, with the bytes); the image row says only that
  it is waiting. One row per RENDER (`sys:ai-image:<uid>`), never per model — a
  shared id would have a second render overwrite the first's progress. The PNG
  is read back through `/api/fs/raw` like every other local file, and
  `fused.ai.image()` hands the page a ready-made URL for it.
- **AI-9c** **Both rows say the same failure** (D266). Two rows, two truths is
  the rule; two rows where one of them lies is not. A waiting render watches the
  worker RECORD it started, never the `_workers` table — a failed bring-up drops
  itself from that table inside the same locked block that stamps the error, so
  a waiter polling the table finds only that the model is gone and reports "was
  unloaded before it could be used" for a load that failed with a real message.
  That message is what a user reads and retries against, and it made a permanent
  environment failure look like a transient race. "Unloaded" survives as the
  answer for what it actually describes: a record that never errored and was
  taken away — evicted by another model, or unloaded from the AI Models page.
- **AI-9a** **The worker contract is written once, in `runners/worker_base.py`.**
  A runner is still a folder, but the half that is the SUPERVISOR'S contract —
  the auth header's name, the status file's shape, the state vocabulary it
  polls, the way download bytes are measured — lives in one stdlib-only module
  both runners import, and a concrete runner supplies only `download`, `load`
  and `generate`. Copying it per folder would have put that contract in two
  places, which is the failure mode every bug in this feature has had. It is
  stdlib-only for two reasons: anything imported there becomes a dependency of
  every backend forever, and it makes the contract **testable on CI**, which
  neither concrete worker is (one needs Metal, the other several GB of torch).
  `load` is handed what `download` returned rather than resolving the files
  again — doing it twice re-ran the Hub metadata call and re-reported a finished
  download on every load of a cached model.
- **AI-9b** **A quantized single-file component is a RECIPE, not a heuristic.**
  The image runner keeps an explicit table of which quantized checkpoint
  replaces which component of which model (FLUX.2 klein's ~8GB bf16 transformer
  → a ~2.6GB Q4_K_M GGUF), because "which quantization of which part is safe" is
  the same editorial judgement `catalog.py` makes about what to suggest, not
  something to infer from a file listing. A model absent from the table is not
  unsupported — it loads the ordinary way. The recipe also decides what NOT to
  download: the base repo's own `transformer/` is exactly the weights the
  quantized file replaces, and fetching it would cost several GB for components
  that are then ignored — but it skips the WEIGHT files, never the subfolder,
  because `from_single_file` reads that subfolder's config and a "download" that
  leaves a cache which cannot load offline has not done what the button said.
  **A scoped download measures itself against a scoped total**: one file out of
  a repo that publishes a dozen quantizations counts that file, and a pull that
  ignores a subfolder does not count it. Summing the whole repo either way is
  how a 2.6GB fetch came to read as a fraction of 30GB and then jump to
  complete; the reported figure is also capped at the total, since the disk walk
  sees siblings the download was never fetching.
- **AI-8** **The worker measures its own memory.** Only the process holding the
  weights can; on Apple Silicon the GPU pool IS system memory, so RSS is one
  honest number rather than two that need reconciling. What the supervisor knows
  better is whether the process is ALIVE — a worker that stops answering becomes
  `error`, never a `ready` row that lies. The figure is **resident bytes**, and it
  is not the model's size: it overcounts shared pages and moves during a
  generation.
- **AI-8a** **Measured at every `/health`, and by the FRAMEWORK when it knows
  better than RSS.** Both halves come from one card reading **379 MB in memory
  for a 6.1 GB model**. The figure was taken once, immediately after `load()`
  returned, and then served from the state dict forever — so it was a snapshot of
  the worst possible instant, and it never moved again however much the model
  went on to use. And the instant is worst *because of what the number is*: MLX
  memory-maps the weight files and its arrays are lazy, so right after a load the
  process has genuinely touched almost none of them and RSS is reporting the
  interpreter. So residency is measured **when it is asked for**, and a runner
  that has a better probe than RSS supplies `memory=` to `serve()` — MLX's
  allocator, which knows what it reserved whether or not the pages have faulted
  in. The **larger** of the two wins: neither is a superset (RSS carries the
  interpreter and framework; the allocator carries buffers RSS has not seen yet),
  so the honest claim is "at least this much". A runner's probe that raises is
  worth no memory figure, never a broken `/health`.
- **AI-10** **Speech to text is the third capability, and it is the first one
  that works EVERYWHERE** (D288). `automatic-speech-recognition` — the Hub's own
  tag — served by a `faster_whisper` runner folder built on CTranslate2, which
  publishes wheels for macOS on both architectures, Linux and Windows. That
  choice is the point of the bullet: mlx-whisper would be quicker on Apple
  Silicon and would have made ASR a *third* Apple-Silicon-only feature, and an
  app whose local AI is something most users read about is not the app this is
  meant to be. An `mlx_whisper` runner may be added later ABOVE this one — the
  registry's first-match-wins ordering (AI-2) exists for exactly that, and this
  would be the first capability to use it. Both Whisper directions ship:
  `task: "transcribe"` (same language) and `task: "translate"` (into English)
  are one flag to the model, so omitting either would only buy a second change
  later — but the value is **named, never silently defaulted**, since
  "translation" instead of "translate" would transcribe in the original language
  and read as the model ignoring the request. `language` omitted means Whisper's
  own auto-detect. **The whole audio dependency stays inside the runner folder**:
  faster-whisper decodes through PyAV, whose wheels carry the ffmpeg libraries,
  so nothing shells out to an `ffmpeg` binary this app does not ship — the rule
  AI-2 states about mlx and torch, applied to a system tool. **The format
  constraint is surfaced rather than hidden**: the runner loads CTranslate2
  conversions, so a transformers-format repo like `openai/whisper-large-v3` will
  not load however happily the AI Models page offers it a Load button (the same
  situation text generation has with GGUF and AWQ). The load error names the
  cause and a repo that works, because a user who picked the wrong one should
  learn it from the error rather than from a web search. Text-to-speech and
  audio generation stay in `NO_RUNNER_YET` as SEPARATE future capabilities, not
  as a direction flag on one "audio" capability: AI-4 keeps one resident model
  per capability, so sharing one would have a synthesis model and a Whisper
  model evict each other on every alternation.
- **AI-10a** **A transcription is job-backed like an image, and its result is a
  FILE.** `POST /api/ai/transcribe` answers immediately with a `jobId` and with
  the output paths already settled, exactly as AI-9 does — a 90-minute recording
  is minutes of decoding, so nothing waits on it, and the wait for a cold model
  belongs inside the job for the same reason it does for a render. Progress is
  **seconds of audio** (`unit: "s"`, `done` = the last segment's end timestamp,
  `total` = the decoder's reported duration), which is the unit the person
  watching is thinking in, and the download manager renders that unit as a
  CLOCK (`12:00 / 1:30:00`) rather than a bare pair, since `720 / 5400` is a
  number a reader takes for segments or steps. **The ✕ has to reach two
  phases, not one.** The per-segment loop is a real interruption point because
  `transcribe()` hands back a generator that decodes as it is consumed — but
  before it returns that generator it has already decoded the entire file and
  run the VAD over it, which on a long recording is minutes. That eager phase
  is therefore ticked from a thread (the poll IS the progress and the
  cancellation point, as it is for downloads); leaving it behind a single plain
  `report` left a window where the row sat at zero and a ✕ was not honoured
  until the first segment landed. **And it gets its OWN socket timeout**
  (`TRANSCRIBE_TIMEOUT_S`, four hours) rather than the 900s an image uses: the
  worker sends nothing until the decode finishes, so that timeout covers the
  entire run, and at 900s this feature's own motivating case — a 90-minute
  recording, ~18 minutes of decoding — died on the socket while the worker went
  on to write a transcript nobody was told about, still holding `GENERATE_LOCK`
  so every queued request repeated it. It is a backstop, not the stop: the ✕
  makes the worker reply and an unload closes the socket, both in seconds.
  **A second transcription waits on the SUPERVISOR's side, not inside the
  worker.** The worker serializes generations anyway, but by parking the second
  request before its handler reaches `heartbeat()` — so with a four-hour
  timeout that row got no ticks for hours and hit every timer in §36: stalled
  at 30s ("no longer reporting" about work that is merely queued), swept at
  600s, after which the bridge is told a still-running transcription failed.
  Holding a supervisor-side lock instead is what makes the wait describable —
  the row says it is queued, keeps saying so, and its ✕ is honoured, none of
  which is reachable from inside a blocked `urlopen` that has sent the worker
  nothing to cancel. **Every reporter on a transcription's lifecycle restates
  the ROW'S IDENTITY on every tick** — title, kind, unit, cancellable, and
  `state: "running"` — title, kind, unit, cancellable, `state` — so that a row
  which had to be re-created comes back as the SAME row rather than one with
  the same id. A report without a title is refused outright and the row never
  returns at all; without `cancellable` it is drawn with a dismiss cross
  instead of a cancel one, operable-looking and inert; without `unit` the
  seconds clock reverts to a bare pair of numbers. The identity is defined ONCE
  and handed to the worker in the request body rather than re-spelled in that
  process, so the supervisor's reports and the worker's cannot disagree about
  what the row is — the cold-model wait included, since it is the longest
  reporter of the lot.
  **Why the row can no longer vanish under a live transcription is BG-6's rule,
  not a cadence here** (D288): the cap does not evict live server work. That is
  the fix nine rounds of this feature had been compensating for from the
  reporting side — restate the row harder, tick faster, rebuild on detection —
  and none of it could reach the actual consequence, because `fused.watchJob`
  resolves null after five consecutive misses and a promise that has settled
  cannot be un-settled by a row that comes back. Maximum absence for a live
  transcription is therefore ZERO, and the write cadence went back to being
  what it sounds like: a display heartbeat, sized only so a row waiting its
  turn is not shown as "no longer reporting", with the ✕ polled on its own
  faster cadence because a cancel must not wait on a display. The restatement
  above and the rebuild-on-detection stay as backstops for the one absence a
  user can still cause — dismissing the row — and `fused.ai.transcribe`'s
  absent-row branch is the same two lines `fused.ai.image` has: read the
  artefact, and if it is not there, reject. It briefly grew a retry loop
  instead, which could hang forever; that machinery is gone, because the fix
  belonged in the manager.
  **The turn is taken before the MODEL is resolved**, and
  that ordering is load-bearing: resolving first put the one destructive step —
  `_start_resident`, which EVICTS the resident model when the requested one
  differs — outside the very lock that serializes this path, so a page asking
  for a different Whisper model killed a 90-minute run mid-decode and lost its
  transcript. One row per RECORDING (`sys:ai-transcribe:<uid>`). The
  transcript is written under `<home>/ai/transcripts/` as a `.json` (segments
  with timestamps, language, duration, model) and a `.txt` (plain words) —
  **segments, not a flat token stream**, because the timestamps are most of what
  Whisper produced and a transcript beside a player needs them. Writing a file
  rather than streaming is the same argument the PNG makes: work that took four
  minutes should outlive the tab that asked, and a page that navigated away
  mid-run should still find it. **The input is an absolute path and there is no
  allowlist**, deliberately: the worker is a process on this machine and opens
  the file itself (nothing is uploaded), `/api/fs/raw` already serves any
  absolute path because this app IS a local file explorer, and the protection is
  D3/D36's `X-Fused` guard plus the worker's own token. The route normalizes the
  path and refuses one that is missing or is not a regular file with a 400
  before a job row opens — a typo deserves an error the caller can show, not a
  progress bar that dies. A RELATIVE path resolves against the calling page's
  directory via `base`, the same page-relative rule `/api/fs/raw` follows
  (RH-1): `fused.readFile("clip.m4a")` already means "beside this page", so
  resolving here against the server's cwd would 400 on a path the author never
  wrote — or, if a same-named file happened to sit under whatever directory the
  app was launched from, silently transcribe the wrong recording. Relative with
  no `base` is refused rather than guessed. `fused.ai.transcribe({path, …})`
  resolves with the
  text, the segments and a ready-made `/api/fs/raw` url, and falls back to the
  file when the row has aged out from under a backgrounded tab.
- **AI-10b** **No audio has ever been transcribed by this code, and the two
  bullets above should be read as a design that is tested only down to the
  worker's door.** faster-whisper cannot run on CI — CTranslate2 plus a model
  download — so the route, the supervisor, the job row, the glossary, the
  catalog and the bridge are all exercised against a FAKE worker speaking the
  AI-3 contract with canned segments, exactly as the image path is. The
  runner's OWN logic is tested a level down — `faster_whisper/worker.py` is
  stdlib-only at import time (`faster_whisper` and `ctranslate2` are imported
  inside the functions that need them), so its decode loop, its
  seconds-of-audio arithmetic, both cancel paths, the two Whisper directions,
  the files it writes and the CTranslate2-format check are all driven on CI
  against a stub model. **What no test touches is Whisper itself**: no audio is
  decoded anywhere in this suite, so the numbers a real `info.duration` and a
  real `segment.end` supply, the CPU-vs-CUDA placement, the int8 quality trade
  and the actual transcription quality are unverified — as is the assumption
  that PyAV opens the container formats users will point at it. A first real
  transcription is the outstanding verification, and until it happens this
  section describes a design that is proven only down to the model's door.
- **AI-11** **Text generation runs on every supported desktop platform, on the
  backend that suits the machine — and TWO runners share one capability for the
  first time** (D293).
  MLX is Metal-only, so the app's flagship local capability was something a
  Windows or Linux user could read about and not use: the exact complaint AI-10
  answers for transcription, still standing for chat. A `transformers_text`
  runner folder (torch + transformers) is registered BELOW `mlx_text`, and
  AI-2's first-match-wins ordering does the rest — Apple Silicon prefers MLX
  (faster on Metal, and its 4-bit catalog is sized for a 16GB laptop) but can
  fall through to torch when MLX is unavailable, while Windows and Linux reach
  torch directly. Intel macOS is not a distribution target and is not
  advertised by the runner. Nothing else in the app learned that a capability
  can have two runners, which is the claim AI-2 made and this is the test of it.
  **The backend was chosen on packaging, not on benchmarks.** llama.cpp would be
  the obvious pick and is refused by AI-2a: `llama-cpp-python` publishes an sdist
  and no wheels at all, so declaring it would put cmake and a C++ toolchain —
  MSVC, on Windows — between a user and the Download button, with its prebuilt
  wheels on a private index that is a second thing to trust. torch is the
  runtime this app already builds on users' machines for the image runner, so
  its install path and its failure modes are known rather than guessed at.
  `onnxruntime-genai` is the credible alternative (tiny, fast int4 on CPU,
  DirectML reaching every Windows GPU) and was deferred rather than dismissed:
  it only loads pre-converted ONNX repos, so the Hub models the page already
  offers a Load button for would refuse — and as a SECOND text runner it would
  break the rule that a model id never picks the runner.
- **AI-11a** **The CATALOG is keyed by runner, and the page says which one it
  resolved.** This is the part a second runner really did change. A suggestion
  is only meaningful for the backend that will load it: `mlx-community/…` is
  packed for Metal kernels and is an unusable download on a PC, while an
  ordinary safetensors repo is the right answer there and the wrong one on a Mac
  that has MLX. So `catalog.SUGGESTIONS` moved from capability keys to RUNNER
  keys, and `catalog.describe()` resolves the runner the way a LOAD resolves it
  — it used to take the first runner REGISTERED for a capability whatever its
  availability, which with two rows would have told a Windows machine that text
  generation "needs Apple Silicon" while a runner sat ready to serve it, under a
  heading whose four suggestions it could not load. The curation rules for the
  cross-platform list are three, each a failure this app has already shipped
  once: unquantized safetensors only (every other format needs a package the
  runner does not ship — the CTranslate2 trap AI-10 describes), ungated only (a
  licence-gated repo 401s partway through a download for a user who did nothing
  wrong), and sized for a machine with no GPU. One consequence had to be fixed
  where it surfaced rather than where it started: an unavailable runner also has
  no curated default, so `POST /api/ai/image` began answering "no image model is
  configured" — true, useless, and hiding the actionable "the Diffusers runner is
  not built yet" underneath. `registry.unavailable_reason()` tells the two apart,
  because no runner is a fact about the MACHINE and no suggestion is a fact about
  the catalog. **It reports EVERY runner's reason, not the first one's**, which
  only became a distinction when a capability grew a second runner: three places
  independently took "the first runner registered for this capability" — the
  registry, `_runner_or_raise` and `start_image` — so a Linux machine whose
  transformers worker was missing was told text generation "needs Apple
  Silicon", naming the one backend that was never going to serve it. The three
  copies are now one, which is the actual fix; joining rather than picking is
  the answer because there is no rule for choosing between two reasons that is
  not a guess about which the reader meant.
- **AI-11d** **Reasoning is OFF by default, because it is invisible and the CPU
  path cannot afford it.** Qwen3's chat template defaults `enable_thinking` to
  true and three of the four curated models are Qwen3, so an ordinary question
  emits a `<think>` block first — hundreds of tokens the caller cannot tell
  apart from the answer, since `/generate` streams whatever the model produces.
  At a few tokens a second on the CPU this runner exists to serve, that is
  minutes of apparent silence on a machine already suspected of being slow. The
  flag is passed to every model rather than to a list of known ones: kwargs land
  in the Jinja render context, so a template that never mentions it does not
  read it, and a tokenizer whose signature rejects it outright retries without —
  a model that will not take the hint should still answer, just verbosely. The
  same class of trap as the version floor beside it: `transformers>=4.51` is
  what knows a `qwen3` exists, and an older resolution installs perfectly and
  then fails every Qwen3 Download with `KeyError: 'qwen3'`, which reads as a
  broken model rather than an environment one version too old.
- **AI-11b** **The device is reported, because a model on a CPU works and looks
  broken.** torch runs on whatever it can see, and what it can see is not
  knowable from outside the process: **the PyPI torch wheel is CPU-only on
  Windows** (its `nvidia-*` dependencies are all marked `platform_system ==
  "Linux"`), so the ordinary outcome on a Windows machine with a graphics card
  is a perfectly healthy model answering at a few words a second, with a green
  LOADED card and a healthy memory figure and nothing on screen to explain the
  speed. `worker_base.STATE` therefore carries a `device` that each runner sets
  in its own `load()` — the same argument AI-8 makes about resident bytes: only
  the process holding the weights knows. It surfaces twice, and the two are
  different KINDS of statement: the loaded card shows a measurement (**on CPU**,
  warning-coloured, beside the memory figure), while Discover shows a standing
  fact about the backend above the cards, before any download, since that is
  when it can still change a decision. All three runners report it — the image
  runner has had the same Windows CPU-only problem since D257 and never said so.
  **Windows CUDA was deferred, deliberately**: reaching it means pulling torch
  from `download.pytorch.org` through a `[[tool.uv.index]]`, which costs EVERY
  Windows user a ~3GB CUDA runtime to serve the ones with an NVIDIA card. The
  trade is stated rather than hidden, which is what the device reporting is for.
- **AI-11c** **No text has ever been generated by this runner, and AI-10b's
  disclaimer applies verbatim.** torch cannot run on CI, so the registry, the
  catalog, the resolution across four platforms and the API are exercised
  against fakes, and the runner's OWN logic is tested a level down —
  `transformers_text/worker.py` is stdlib-only at import time, so its format
  refusals, its dtype-keyword choice, its device placement and its two
  prompt-encoding paths are all driven on CI with stubs. What no test touches is
  torch itself: the actual generation, the streaming, the real speed on a CPU,
  and whether the four suggested repos load as expected. Their `size_gb` values
  are full-snapshot download estimates from the Hub's per-file byte metadata,
  not claims about measured filesystem usage (D295). A first real load is the
  outstanding verification.

## 41. Scheduled Messages — Sending Claude a Message Later (D289, D290, D291)

Goal: the app could start a Claude Code session on demand — the split-view chat,
and the apps API's scaffolding turn — but had no way to say *later*. Scheduling
a message ("update the changelog at 6pm", "re-run the check tomorrow morning")
had to be done outside the app, by a crontab line or a Task Scheduler entry
invoking `agent.py` directly, and that turns out not to work in the way that
matters: a scheduled turn launched from outside the app runs in a different
world from one the user typed.

- **SCH-1** **A durable schedule** (`fused_render/schedule.py`,
  `<home>/scheduled_messages.json`). One entry per message:
  `{id, target, message, due, session_id, permission_mode, state, created,
  fired, run_id, error}`. On disk, not in memory (unlike the job registry, §36)
  — the whole point is to outlive the app session that scheduled it. Branch-aware
  through `storage.home_dir()`, so a dev checkout on a branch ref never fires the
  baseline install's messages. A missing or corrupt store reads as "nothing
  scheduled", the same degradation as every other registry here.
- **SCH-2** **The app sends it, not the OS.** `supervisor/paths.py`'s
  `child_environment` injects ~20 variables into every child the app spawns
  (state/cache/runtime/temp/log dirs, the bundled rclone and uv, `TMPDIR`, the
  `CLAUDE_CONFIG_DIR` passthrough), and `_plugin_argv` hands a session
  fused-render's skills only when that contract is present. A cron line
  reproduces none of it, so its turn silently becomes a *different install*:
  other state dir, no skills. On macOS it is worse than different — per D72 a
  process that is not the app does not inherit the app's Documents/Desktop TCC
  grants, and the CLI's credentials live in the login Keychain of a GUI session
  cron is not in; both spawn paths run headless (`claude -p`) where the `/login`
  the CLI prints can never be actioned. Firing inside the server process makes a
  scheduled turn environmentally identical to a typed one.
- **SCH-3** **The cost, stated: nothing fires while the app is closed.** The page
  says this where it is relevant rather than implying a guarantee it does not
  have. Two mechanisms make it survivable:
  - **SCH-3a** **Wall-clock comparison, never tick-counting.** Every tick asks
    what is due *now*. A laptop that slept through a due time fires on the tick
    after it wakes; an app that was quit fires on the tick after it starts.
    Catch-up is not a feature — it is what the absence of tick-counting gets for
    free, which is why the loop is a **startup event** (not the `create_app`
    body: it sends things, and every test that builds an app would otherwise
    spawn whatever the developer's store held) and does not sleep before its
    first pass.
  - **SCH-3b** **Missed work QUEUES and runs when the app next opens; there is no
    bound by default.** The earlier design bounded catch-up at 24h on the reasoning
    that a message meant for Tuesday's standup, fired unattended on Friday, is worse
    than one that never fired. That reasoning was sound about *unattended* firing and
    is answered directly now rather than by a timer: the queue is visible before it
    runs (the Calendar's Queued strip), every entry in it can be cancelled, and a
    running one carries its own cancel. Given a cancel affordance, a bound only
    throws work away silently. `FUSED_RENDER_SCHEDULE_MAX_LATE` still reinstates a
    bound where an operator wants one, and only then does an entry become `missed`.
    `GET /api/schedule` reports `max_late_seconds: null` when unbounded — the page
    must read null as "no bound", never as zero.
- **SCH-4** **The claim is written before the spawn.** An entry becomes `sending`
  *before* the helper is launched, so a process that dies mid-spawn leaves it
  `sending` rather than `pending` and the next boot does not resend it; a sweep
  later reports it as interrupted. That is the safe direction to fail — an unsent
  message is a disappointment, a message sent five times over five crash-restarts
  is an agent running unattended five times.
- **SCH-5** **Permission mode is per-entry, default `auto`.** Same reasoning as
  the apps API (`_APP_SESSION_PERMISSION_MODE`): nobody polls `decide`, so under
  the strict default the first tool call parks a request until `PERMISSION_WAIT`
  denies it — a message that "sent" and did nothing. The extra wrinkle here is
  that the turn is unattended *by definition*, so the mode is recorded ON the
  entry: "auto" is a choice made per message, not a property of scheduling.
- **SCH-6** **A mount-backed target is refused**, in the router rather than the
  model (the mounts registry lives above it). A scheduled turn is an agent turned
  loose on a path; scheduling one against a FUSE mount would route around the
  gate `templates/claude/condition.py` exists solely to be.
- **SCH-7** **Routes.** `GET /api/schedule` lists and `GET /api/schedule/events`
  reads undelivered outcomes (both unguarded, like every read);
  `POST /api/schedule` schedules, `POST /api/schedule/cancel` withdraws, and
  `POST /api/schedule/events/ack` confirms narration — all three
  behind the D3 X-Fused guard — one schedules code execution and the other stops
  it. Create takes **exactly one** of `due` (ISO 8601) or `delay_seconds`, so a
  caller offering "in 30 minutes" never does timezone arithmetic; a **naive**
  `due` is read as LOCAL time, because it came from a human writing the time on
  their own clock. Only a `pending` entry is cancellable: the helper for a
  `sending` one is already away.
- **SCH-8** **The OS half launches the app and nothing else**
  (`fused_render/schedule_wake.py`). It sends no messages and does not know what
  one is; it asks the platform to have the app *running* at the times something
  is due, and the app's own first tick (SCH-3a) does the rest. **macOS: a
  LaunchAgent** — `launchd`, not cron, because it runs in the Aqua session (so
  the app it starts has the Keychain and can prompt for consent) and it runs a
  missed `StartCalendarInterval` when the machine next wakes, which cron does not
  do at all. Intervals are written in **local time** (what launchd evaluates
  against), capped to the soonest few (the plist is a wake-up list, not the
  schedule), and `open -g -a` launches **without stealing focus** from whatever a
  3am wake interrupted. **Windows and Linux get nothing new, deliberately:** both
  already have a start-at-login toggle the supervisor owns (`_win32/startup.py`'s
  Run key, `_linux/startup.py`'s autostart entry), and a schedule-specific timer
  would be a third mechanism that can disagree with those two about whether the
  app should be running. Everything here is best-effort: a failed plist write
  makes messages fire less reliably, never lost, and must not fail the store
  write that triggered it.
  - **SCH-8a** **The wake stub reads the store; nobody hands it a view of it.**
    `_sync_wake()` takes no argument. It cannot run under `_lock` — that would hold
    the store across two `launchctl` subprocesses, letting one tick stall a
    `GET /api/schedule` for as long as launchd takes — so it takes `_wake_lock`,
    re-reads the pending times under `_lock`, releases, and only then shells out.
    Callers used to snapshot the times inside their own locked block and pass them
    in, and that lost writes: two mutations racing could reach `launchctl` in the
    opposite order, and the older snapshot then overwrote the plist and **dropped
    the newer message's wake time**, with nothing to resync until some later store
    write happened along. Serialising the shell-out and re-reading inside it is
    what makes "last to write the plist is last to read the store" true. Lock order
    is `_wake_lock` then `_lock`, never the reverse — a caller still holding `_lock`
    here inverts it, and two such callers deadlock, which is what the
    "outside `_lock`, always" rule is really protecting.
- **SCH-9** **One copy of the spawn discipline** (`fused_render/claude_spawn.py`).
  The apps API and the scheduler need the identical posix_spawn posture — calling
  `agent._start` in the server process fork()s with libproj resident and SIGSEGVs
  the child before exec — plus the same poll that gets a run into its sidecar.
  Extracted rather than duplicated, because that reasoning is the kind that gets
  paraphrased into something false on the second telling.
- **SCH-10** **Two surfaces, because nobody is looking when it happens.** This is
  the one kind of work in the app with no audience at the moment it runs, so
  "what did it do" cannot be left to a page the user has to think to visit.
  - **SCH-10a** **A job row per send** (§36's registry, id `sys:schedule:<entry>`,
    kind `task`). Opened `running` when the send succeeds and held for the whole
    **turn**, not just the spawn — the spawn takes a moment, the turn can take
    minutes, and the minutes are what is worth seeing. Its `detail` carries the
    turn's phase and token count, and — the one worth having — reports **waiting
    for permission** when `_poll` shows a parked card, because from the outside a
    turn nobody has approved looks exactly like a slow one, and for an unattended
    session that is the likeliest way to be stuck. `cancellable` is honest here in
    a way it is not for most reporters: this process owns the run, so the
    manager's ✕ calls `agent._cancel` and is an ACTION (`jobs.OWNER_SERVER`).
  - **SCH-10b** **An event log the shell toasts** (`schedule.event_log()`,
    `GET /api/schedule/events`) — append-only, monotonically ided, bounded. A
    separate endpoint from the listing for the reason the mount-health log is
    separate: this poll runs app-wide in every shell forever and must not carry
    the page's payload. **The SERVER decides what is undelivered**
    (`undelivered_events` + a guarded `ack` the shell POSTs after narrating), and
    that is the correction to the first shape, which copied `useMountHealth`'s
    "first successful poll is a silent baseline". That rule is right for mounts —
    which emit nothing at startup by design — and exactly wrong here: the catch-up
    pass emits its `missed` verdicts on the scheduler's FIRST tick, long before a
    shell has loaded, so a client-side baseline swallowed precisely the events the
    log exists to deliver. Acking after narrating means a client that dies in
    between gets a duplicate toast rather than a silent miss, and a reload is quiet
    without the client having to guess. It is an ack POST rather than a
    drain-on-read because a GET with that side effect would let any page the user
    visits silently consume their notifications. Kinds: `done` → info, auto-dismissing; `failed`
    and `missed` → persistent errors with an action onto `/scheduled`. `missed` is
    worded differently from `failed` on purpose — nothing went wrong, the app was
    not running — but it still needs a person, because the user asked for
    something that did not happen. The rules are a pure module
    (`schedule-toast.ts`, bun-tested) with the polling left in the hook, the same
    split `server-status.ts` uses.
  - **SCH-10c** **`state` and `turn` are two facts, not one.** `state` says
    whether the message was SENT; `turn` says how the session it started then went
    (`""` until it ends, else `ok`/`failed`/`cancelled`). They fail
    independently — a message can send perfectly and its turn still die on the
    first tool call — and reporting a dead turn as a send failure would send the
    user looking in the wrong place. The page labels a sent row by its turn
    ("Running…", "Ran", "Turn failed") and counts a still-running turn as live.
  - **SCH-10d** **The watcher wraps the recorder, it does not replace it.**
    `record_session_when_ready` gained an optional `on_tick` observer (called
    before its `done` check, since that final tick is where the outcome is), whose
    exceptions are swallowed: the sidecar write and the commit must happen whether
    or not anything is watching, so an observer is never allowed to abandon a run.
    Every report is best-effort — a registry that refuses a field must not cost a
    message its send.
  - **SCH-10e** **A turn abandoned by a dead process is closed by the sweep.**
    `sent` with an empty `turn` means two different things — a turn running
    normally, and one whose watcher died with the app mid-turn — and the store
    cannot tell them apart, because the difference is only knowable by a live
    process. `schedule._watched` holds the entry ids this process is watching
    (registered BEFORE the store says `sent`, so a concurrent sweep cannot close a
    turn that is about to be watched; deregistered in a `finally`, so a finished
    turn does not stay invisible to the sweep instead). The sweep closes any `sent`
    entry with no `turn` that nothing is watching: `state` stays `sent` because the
    message did go, `turn` becomes `unknown` — the same word SCH-10d's
    `_close_unwatched` uses, which is the in-process floor under a watch that ENDS
    and by construction cannot cover a process that DIES. Left unclosed the entry
    cost three separate things: the page read `Running…` for ever, no toast ever
    said what happened, and — the one that costs a future message rather than a
    label — its session stayed in `_busy_sessions`, so the next scheduled message to
    that conversation was held back tick after tick until the catch-up bound gave up
    and called it missed.
- **SCH-11** **Scheduling happens in the claude template's composer, not on a
  settings page** (`templates/claude/template.html`, the **Send now** pill beside
  the model/effort/approvals pills). The composer already holds the two hard
  parts — WHICH FOLDER (the template is bound to one target, `FILE`) and WHAT TO
  SAY — so the only thing it was missing is *when*. A settings page asking for a
  path and a message again was making the user do twice what they had already
  done once, and typing an absolute path by hand was the worst affordance in the
  feature.
  - **SCH-11a** **Presets, not a date picker.** The pill row is a fixed
    vocabulary and a `datetime-local` field wedged between the pills would read as
    a different app; "in an hour / this evening / tomorrow 9am / Monday 9am" is
    what a deferred prompt actually wants. `POST /api/schedule` still takes an
    exact `due`, so nothing is lost for a caller that needs 03:14. Presets resolve
    at SEND time — "tomorrow 9am" means tomorrow from the moment the user commits,
    not from whenever the pill was touched — and a preset already past today (the
    evening case) is reported rather than silently shifted a day.
  - **SCH-11b** **The choice does not outlive its message.** Model, effort and
    approvals persist in `fused.params` because they describe how the chat
    behaves; "send it at 6pm" describes ONE message, so the pill resets to
    **Send now** after every send. A deferral that survived its own send would
    silently defer whatever the user typed next.
  - **SCH-11c** **The approvals pill applies to the scheduled turn**, which is the
    same question asked about the case where the answer matters most. The
    composer's four modes and `schedule.PERMISSION_MODES` are therefore held
    together by a test (`test_claude_schedule_pill.py`): the first version of that
    copied tuple omitted `acceptEdits`, so a composer sitting on that mode had its
    schedule refused with a 400 naming modes the user had never chosen.
  - **SCH-11d** **Annotations are not folded into a scheduled message**, unlike a
    sent one. A note is a crop of what is on screen *now* plus a pointer into this
    render of the pane, and by the time the message runs the pane may show
    something else. A scheduled message is the words; the pending notes stay
    pending. An annotation-only send is therefore refused with a reason rather
    than deferred.
  - **SCH-11i** **The two list groups run in OPPOSITE directions**, because "most
    relevant first" means opposite things about the future and the past: live
    entries ascending (the next thing that will happen, at the top), handled ones
    DESCENDING (the latest news, at the top). One direction for both was a straight
    bug, found by hand: it buried what had just run under every message ever
    scheduled, and got worse the longer the feature was used. A handled entry sorts
    on when it ACTED (`fired`), falling back to `due` for one that never did —
    `missed` and `cancelled` carry no fired stamp — which is also the stamp its row
    displays, so the order matches what the reader is reading. Ordered by
    `list_entries`, not the page: the page filters the one list into its two
    sections and must not re-sort or reverse what it is handed.
  - **SCH-11j** **The page's URL is served by `routers/shell.py`**, like every
    other shell route. Omitting it is invisible to whoever built the page — in-app
    navigation is a client-side pushState that never asks the server — and 404s for
    anyone who refreshes or bookmarks. `/scheduled` shipped that way and was found
    by hand. `test_shell_routes.py` now DERIVES the list from the shell's own route
    table (the `pathname === "…"` comparisons in App.tsx) and requests each for
    real, so the next page added without a server entry fails a test rather than
    waiting for someone to press ⌘R.
  - **SCH-11k** **A scheduled message is a CARD**, in the grid and shell the apps
    hub already uses (`.apps-cards` / `.app-pcard`: auto-fill columns, `--bg-alt`
    on a 12px radius, a hover lift) so it reads as the same kind of object as the
    rest of the app's cards. Borrowed, not re-invented — these carry no thumbnail,
    so only the shell comes across, and the columns are narrower (280 vs 300)
    because a card here holds a few lines of text rather than a 16:10 preview. The
    prompt is the card's subject and gets the body colour, clamped to four lines so
    a grid keeps an even baseline with the full text in the title. Actions are
    pinned to the foot (`margin-top: auto`) so buttons line up across a row however
    long each prompt is. **Only `error` and `missed` tint their border** — the two
    states that need a person — because a pill alone is easy to miss across twenty
    cards, and if every card had an accent the accent would mean "this is a card"
    rather than "look here". Those two **restate their tint on `:hover`**, without
    which the generic card-hover border (two selectors to the modifier's one) wins
    and pointing at a failed card is what erases the mark saying it failed.
    The page runs at **two widths**, not one: it is built out of the settings
    vocabulary, whose `.prefs-page > *` caps children at 760px, so a
    `minmax(280px, …)` grid needing 868px for a third track sat silently two-up.
    The sections holding cards get the ~1120px the app's other card grids use
    (`.apps-cards`, `.fhb-grid`) while the prose inside them keeps the narrow
    measure — two kinds of content in one page: text read a line at a time, and
    objects scanned in a grid. Both invariants are arithmetic between rules in two
    files, which nobody re-checks after changing one, so
    `tests/test_schedule_css.py` reads the stylesheet's own numbers and pins them.
  - **SCH-11e** **The page keeps the list and loses the form.** Every folder's
    schedule in one place, with cancel and the outcomes, is the part that has
    nowhere else to live; it points at the composer for the scheduling itself.
  - **SCH-11f** **A chat left open picks up its own scheduled send.** The gap this
    closes was reported from use and it was the composer's worst failure: a
    scheduled message is spawned by the SERVER, so nothing in the page ever set
    `activeRun`, and a chat left open past its own scheduled time sat on
    "Scheduled for 12:20" while the session ran, finished, and edited files — the
    page's only route to the truth being a reload. That made the composer a worse
    place to schedule from than the settings page it replaced, where at least
    nobody expected the conversation on screen. `pollScheduledRuns` reads
    `/api/schedule` every 15s while the chat view is showing and hands any newly
    fired run for this target to **`resumeRun`**, which is already written for a
    run this frame did not start: live, it renders the user line and streams the
    rest through `pollLoop`; already finished, it repairs the transcript from the
    poll payload. Three guards, each earning its place: never over a live turn
    (`resumeRun` would fight `pollLoop` for the frame); the first pass is a silent
    baseline — taken AT LOAD, not one interval later, since baselining on the first
    interval wrote off anything firing in the opening 15s as predating a frame it
    had fired inside, which is exactly when a reader opens the chat because the
    note told them to; and a run only attaches when it BELONGS on this screen — the same session by either id, or, with no
    session yet, one that resumed nothing and so created a session this frame can
    adopt. Splicing another conversation's turn into this transcript would be the
    page lying about what was said where, which is worse than not attaching. The
    confirmation note promises the turn will appear here, which is true only
    because this exists; the two move together and a test says so.
  - **SCH-11g** **A finished scheduled turn is APPENDED, which needed an opt-in**
    (`resumeRun(run_id, {neverShown: true})`). `resumeRun`'s done path repairs
    only what it can prove is missing — an empty log, or a last user bubble that IS
    this run's message — because on the reload path it was written for, the restored
    transcript may already hold the turn and appending would duplicate it. A
    scheduled send is the opposite case: it fired *after* this frame rendered, so
    the turn cannot be on screen and the caller knows it. Without the opt-in, a
    scheduled turn that finished between two polls fell through both branches and
    appeared nowhere — which is most of them, since a short turn beats a 15s poll,
    so the first cut of SCH-11f fixed only the live case while the note promised
    otherwise. A failed run appended this way gets its own user line first, or the
    error reads as belonging to whatever the reader last said. **The flag suppresses
    `matches` outright rather than adding a branch beside it**, which is the second
    bug in this area: with matching still preferred, the same prompt sent twice ("run
    the tests" now, those words scheduled for later) let the earlier identical bubble
    match, and the repair stripped everything after it — DELETING that turn's real
    reply to hang the scheduled answer there. The live path strips partial rows on a
    match too, so the suppression is folded into `matches` itself, in one place,
    covering both. **And `neverShown` is only sound while the visible transcript
    postdates every already-fired run**, which a session SWITCH breaks: `loadHistory`
    restores the new session's history, already containing any scheduled turn that
    ran in it, so attaching appended a second copy. `scheduleResetForNewTranscript`
    therefore clears both sets and re-baselines wherever the transcript is replaced,
    beside the other per-transcript clears — which is what makes `neverShown` true by
    construction for the attaches that survive, and which supersedes the earlier
    reasoning that a foreign-session run was left unmarked so a later switch could
    adopt it. Adoption is history's job, not the poller's.
  - **SCH-11h** **A run is written off only once it is really handled.** Two sets,
    not one: `SCHEDULE_ATTACHED` blocks a second attach, `SCHEDULE_NOTED` blocks a
    repeated mention of a run that belongs to another session — which is
    deliberately NOT marked attached, because this frame can switch sessions without
    remounting and the turn would then belong here after all. The live-turn guard
    sits adjacent to the `resumeRun` call with nothing awaited between: checked
    before the fetch it could go stale, and `resumeRun` returning early on `sending`
    while the id had already been written off lost the turn entirely. The attached
    run also goes on the URL as `run` (a `replace` write, PR-3), for the same reason
    `sendMessage` does it — a reload or a remounting mode switch re-attaches from
    the param, and without it the stream was lost and the next frame's baseline then
    wrote the same run off as predating it.

### 41.b Recurring schedules and the calendar (D296)

- **SCH-12** **A recurring job is a `recurring` TEMPLATE plus materialized
  one-shot occurrences** (`repeats`: a 5-field cron line; occurrences carry
  `template_id`). The template is never claimed or sent; each tick,
  `_materialize` guarantees exactly one `pending` occurrence per live template,
  computed from the latest occurrence ever created (never earlier than now).
  Everything downstream — claim-before-spawn, job rows, the event log, the
  watcher — handles only one-shots, unchanged.
- **SCH-13** **A recurring backlog COALESCES to its latest run, never replays.**
  Where a one-shot missed by a week still runs (SCH-3b), a repeat missed five times
  runs **once** — replaying "daily at 9am" five times into one thread is not what
  the words meant. `_coalesce` walks the recurrence before `_materialize`, keeps the
  most recent missed occurrence and counts the rest onto it as `skipped` /
  `skipped_note`, announced as a single event ("4 earlier runs skipped") rather than
  four. Occurrences no longer carry `max_late: 120`; a legacy one is cleared by the
  coalescer. `count`/`until` budgets are spent by skipped runs, following the same
  rule `create` already applies.
- **SCH-13a** **A repeating template LEARNS its thread on the first run.** A task is
  a Claude session, so a repeat appends into one thread and `new_task_each_run` is
  the opt-out — but that does not fall out for free, and assuming it did was a real
  bug. A template is created with **no** session, because none exists yet: Claude
  Code mints the id on the first turn. So when a run reports the session it actually
  ran in, `_chain_session` writes that id back onto the TEMPLATE's `session_id`, and
  every later occurrence inherits it. Three guards, each of which is a bug if
  dropped: write only while the field is empty, so a re-report cannot thrash it and a
  session the user chose deliberately is never overwritten; never write when the
  template is set to fork; and fix up any ALREADY-materialized pending occurrence in
  the same pass, because `tick` materializes the next run before the previous turn
  reports, so run 2 normally already exists carrying `""`. `_busy_sessions` must also
  union `session_id` with `claude_session_id`, or run 2 resumes run 1's thread while
  its turn is still open. The `session_id` (input, "resume this") and
  `claude_session_id` (answer, "what it ran in") split is preserved throughout: this
  propagates run 1's ANSWER into run 2's INPUT, and never conflates the two on one
  entry. The writeback also stamps **`session_learned`** on the entry, because a
  `session_id` has two possible authors — a chat handoff the user supplied, which a
  repeat must refuse, and this — and only the moment of writing knows which. It rides
  with the id everywhere the id goes (`_materialize` copies both; `create` accepts it
  so an edit, which is cancel + re-create, can re-state it) and is never invented for
  a supplied id. Absent means NOT learned, which is what every entry stored before it
  existed is.
- **SCH-13b** **A rule anchored in the PAST runs once, on its most recent slot.**
  A past-anchored template used to run nothing until its next future slot, because
  `_materialize` computes from `base = now` and the anchor only sets the pattern
  (time of day, weekday, nth-weekday). That is right for "monthly on the second
  Wednesday" but reads as broken beside a past one-off, which fires immediately.
  So a rule template with no occurrences yet walks anchor → now and materializes
  **the latest slot at or before now**, at that slot's own time, marked `catch_up`.
  The latest, not the anchor: "the oldest thing you missed" is rarely what anyone
  wants re-run, and this is the rule `_coalesce` already applies to a repeat missed
  while the app was shut — the same walk, extracted as `_walk_latest` and shared,
  with `spend=True` for the coalescer (it bills `made` and counts `skipped`) and
  `spend=False` here. Intervening slots are never materialized and never reported:
  nothing happened at those times, so nothing is drawn at them either.
- **SCH-13c** **A due message DEFERS while that conversation has a live turn.**
  `_busy_sessions` only ever knew about scheduled sends, so a message due while the
  user was mid-turn in the same session spawned a second `claude --resume` against
  one transcript. Liveness is now asked of the same rule the session badge uses —
  extracted to `fused_render/session_liveness.py` rather than duplicated, because it
  is a 16KB tail read plus a housekeeping filter plus a `turn_duration` end-marker
  plus two windows, and a scheduler that disagreed with the badge on any one of them
  would hold a message the page calls safe. **Deferred, never dropped:** nothing is
  written, so the entry stays `pending` and cannot be swept `missed` (catch-up is
  unbounded by default). An unreadable or absent transcript answers "not live", so a
  bad read can never park a message for ever. The user is never blocked from their
  own chat — the machine waits, not the person. **That rule holds at the OTHER end
  too, and it cost a feature to keep.** The claude template shows a banner directly
  above the composer while a pending message names the session on screen (the Tasks
  list view's own row — ring, `TASK-nnn`, name, state and time — clicking through to
  that task on the calendar, with `Stop the repeat` / `Cancel this message` as its
  one action, two presses for a repeat since that spends every future run). For half
  of 2026-08-17 that banner also **disabled the composer** — a greyed textarea, a
  "Waiting on a scheduled message…" placeholder and an early return in `submitChat`
  that caught annotation-only sends too — on the CONTEXT POLLUTION argument: a task
  is a session, so anything sent first is inside the context the scheduled run
  reads. **The block is WITHDRAWN** (Akshil, 2026-08-17 — "when there is a scheduled
  message, we block the chat. Let's not do that. Let's keep the banner, but let's not
  block the chat for now"). The argument was sound and the remedy was not: pendency
  has no ceiling, so a message due next Tuesday shut the box for six days, and the
  person typing is the person who scheduled the message. **The banner now WARNS** —
  "A scheduled message runs in this chat. Anything you send now joins its context."
  — and the composer types, queues, attaches and sends throughout.
- **SCH-13d** **`POST /api/schedule/run-now` sends a pending message early and
  leaves its `due` alone.** The Board's Upcoming → In Progress drag means "run it
  now", and the schedule time is a fact about what was asked for, so history keeps
  it and the row reads as having run early (`at` is scheduled-for, `ran_at` is when
  it went; see SCH-15). Reuses `_claim`, the single `pending → sending` transition,
  so claim-before-spawn is unchanged and run-now races a tick exactly as two ticks
  race each other. Anything not pending is refused 409 with its own sentence.
  Running one occurrence early leaves its template's rule, `made` and `due` alone.
  A conversation with a live turn is refused rather than forced — a drag gesture
  cannot consent to two processes on one transcript (SCH-13c).
- **SCH-15** **`at` is when a message was scheduled for and never moves; `ran_at`
  is when it actually went.** They were one field, and matching a run to its
  transcript prompt overwrote the due time — so a task scheduled two days ago and
  caught up today jumped to today's column. A calendar places chips by `at`, so the
  chip stays on the day the work was asked for and the row says it ran late; the
  Board's run-now case is the same field pair read the other way round. `last_active`
  maxes over both, so a caught-up task still sorts as today's news while its chip
  stays put.
- **SCH-14** **Cron is parsed in-house** (`fused_render/cron.py`): `*`, numbers,
  ranges, lists, `/n` steps, dow 0–7 with both 0 and 7 as Sunday, and the
  standard dom-OR-dow rule; all arithmetic in naive LOCAL time because "daily at
  9am" is a promise about the reader's wall clock. One question is ever asked of
  it (next occurrence strictly after t), so a dependency was not bought for it.
  Bad lines fail at `create` with the field named; a template whose stored line
  stops parsing (hand-edited store) goes to `error` and is announced — silently
  never firing again is the one outcome the feature must not have.
- **SCH-15** **The page grew a week calendar (default) beside the card list**,
  one toggle apart (persisted). Same entries, two questions: the calendar
  answers "when", the list "what exactly happened". Future runs of a recurring
  job render as dashed GHOST boxes from `upcoming` — a projection the SERVER
  computes on `GET /api/schedule` (next 14 days) precisely so the client needs
  no cron parser; ghosts that collide with the stored next occurrence to the
  minute are not drawn twice. A box's popover reuses the card vocabulary, and a
  ghost's cancel is honestly the TEMPLATE's cancel — a projection has no id.
- **SCH-16** **Creation lives in BOTH places now**: the composer pill keeps the
  convenient path (it knows the folder, holds the message) and gained an exact
  `datetime-local` pick plus repeat presets; the page's New job modal serves the
  calendar-first direction ("what should run Monday 9am?"), where no chat exists
  to borrow from — an empty-slot click opens it with the time filled in. Preset
  UI is a cron BUILDER: the generated line is always shown, so "Custom" is a
  continuation, not a cliff. Cancelling a template cascades to its pending
  occurrence; cancelling just the occurrence means "skip this one" and the
  schedule continues.
