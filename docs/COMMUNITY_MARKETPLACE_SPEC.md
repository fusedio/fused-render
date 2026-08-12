# Community Marketplace — Design Proposal

**Status:** Draft for discussion — 2026-08-12.
A git-backed community marketplace for fused-apps: a public GitHub repo
(`fusedio/fused-render-community-apps`) holding shared apps, plus a bundled
**Community** sub-app in fused-render that lists them, renders their readmes,
and installs them into the user's workspace with one click. The marketplace
experience is itself a fused-app — plain HTML + `.py` helpers running through
the existing runtime, dogfooding the authoring model it showcases.

---

## 1. Goals and non-goals

**Goals (v1)**

- Anyone can publish an app by opening a PR against the community repo.
- Users can browse, search, and read about community apps inside fused-render
  without leaving the app.
- Install = copy the app folder into the workspace under a `community` tag,
  where the existing app hub picks it up with zero registration.
- Installed apps can be updated when upstream changes, with a guard against
  clobbering local edits.
- No new hosted infrastructure: GitHub is the registry, CDN, and moderation
  queue.

**Non-goals (v1)** — listed to prevent scope creep, not because they're bad ideas:

- In-app publishing ("Share this app" button that opens a PR). Submission is a
  documented PR flow in the community repo.
- Ratings, comments, download counts, screenshots galleries.
- Server-side dependency/version resolution between apps.
- Any signing or sandboxing story beyond trust-on-confirm (§7).

---

## 2. The community repo

`fused-render-community-apps` — flat directory of apps, one folder per app.
Folder name is the app's **slug**: `^[a-z0-9][a-z0-9-]{1,63}$`, globally unique
by construction (it's a directory name).

```
fused-render-community-apps/
├── README.md                  # what this repo is, link to CONTRIBUTING
├── CONTRIBUTING.md            # submission checklist (the publishing docs)
├── .github/workflows/ci.yml   # validation + index build (§3)
├── index.json                 # generated on merge to main — never hand-edited
└── <slug>/
    ├── index.html             # entry point (required)
    ├── readme.md              # shown in the marketplace detail view (required)
    ├── icon.svg               # square icon (required)
    ├── metadata.json          # manifest (required, schema below)
    └── ...                    # anything else: .py helpers, assets/, etc.
```

### `metadata.json` schema

This is net-new — nothing in fused-render reads any per-app manifest today
(the app hub only scrapes `<title>`), so the schema is owned entirely by the
community repo and the marketplace app. Versioned and forward-lenient like the
`application/fused-bundle` declaration: unknown keys are ignored.

```json
{
  "schema": 1,
  "name": "Trip Explorer",
  "description": "Browse and filter GPS trip parquet files on a map.",
  "author": { "name": "Ada L.", "github": "adal" },
  "tags": ["maps", "parquet"],
  "version": "1.2.0",
  "min_fused_render": "0.4.0",
  "requires_python": false
}
```

- `schema` — manifest format version, integer, starts at 1.
- `name` — display name, ≤ 60 chars. The slug stays the identity; `name` is
  presentation only.
- `description` — one-liner for the card, ≤ 200 chars.
- `author.name` required; `author.github` optional but recommended (links the
  card to a profile; CI can verify it matches the PR author).
- `tags` — free-form lowercase strings, ≤ 5; the marketplace builds its filter
  chips from the union of tags in the index.
- `version` — semver string, bumped by the author on changes. Used for display
  and "update available" copy; the *mechanical* update check uses git commit
  shas (§6), so a forgotten bump degrades UX, not correctness.
- `min_fused_render` — optional; the marketplace greys out Install with an
  upgrade hint when the running version is older.
- `requires_python` — whether the app calls `fused.runPython`. Shown as a
  badge; feeds the trust-on-confirm copy (§7).

### CI validation (publishing gate)

The repo's CI runs on every PR and blocks merge unless every changed app:

- has exactly one top-level `.html` and it is `index.html` — this is what
  makes the installed copy conform to `app_listing.app_entry`'s single-entry
  convention so the app hub lists it;
- has `readme.md`, `icon.svg`, schema-valid `metadata.json`;
- slug matches the pattern and the folder contains no symlinks;
- stays under a size cap (proposed: 20 MB per app, no single file over 10 MB) —
  big data belongs behind a download-on-first-run `.py` helper, not in git;
- contains no absolute paths in HTML/py that escape the app folder (lint-level
  grep, not a security boundary — see §7).

On merge to `main`, CI regenerates **`index.json`** and commits it:

```json
{
  "schema": 1,
  "generated_at": "2026-08-12T10:00:00Z",
  "commit": "<sha of main at generation>",
  "apps": [
    {
      "slug": "trip-explorer",
      "commit": "<sha of last commit touching this folder>",
      "size_bytes": 48210,
      ...metadata.json contents...
    }
  ]
}
```

`index.json` is the *entire* listing payload — one raw-file fetch serves the
whole browse experience. The marketplace never enumerates the repo via the
GitHub API (unauthenticated rate limit is 60 req/hr; a listing built from API
calls dies the moment two users share an office IP). Per-app `commit` is what
installs record and updates compare against.

---

## 3. The marketplace sub-app

A bundled core app, following the learn/sessions pattern exactly (D227):
`core_apps/community/` with `index.html` + `.py` helpers, shipped as
`community.zip`, mounted read-only via `BUILTIN_MOUNTS`
(`fused_render/shell/mounts/automount.py`), all mutable state under
`~/.fused-render/community/` so it survives app upgrades replacing the zip.

**No new backend routes.** The page has `fused.runPython`, and the app's own
`.py` helpers do everything server-side work would have done:

- `repo.py` — clone/pull the community repo cache (§4) via subprocess git,
  borrowing `deeplink.py`'s hardening: `GIT_TERMINAL_PROMPT=0`, `--` before
  URL-derived values, blobless clone, ff-only pull.
- `catalog.py` — return the parsed `index.json` from the cache, plus install
  state joined from `installs.json` (§5).
- `install.py` — copy an app folder from the cache into the workspace,
  git-init it, record the install (§5).

One constraint the helpers live with: `fused.runPython` user code runs in a
subprocess that deliberately cannot `import fused_render` (the PYTHONPATH
injection that once allowed it was removed — `executor.py` spawn comments —
and the hosted execution backend strips PYTHONPATH anyway). So the helpers
**vendor** the two small pieces they'd otherwise import: the atomic
rename-claim loop (the `os.rename` + collision-retry idiom of
`zip_import.move_into_new_dir`) and the git-init-with-first-commit sequence
(the subprocess-git calls of `app_git.init_repo`). Both are ~20 lines of
stdlib + subprocess git; duplicating the idiom is cheaper than adding an
import channel or new routes.
- `readme.py` — return a given app's `readme.md` and `icon.svg` from the cache
  for the detail view.

Rejected alternative: new backend routes à la `deeplink.py` / `app_clone.py`
(`/api/community/index`, `/api/community/install`, …). It would work, and the
X-Fused header pattern is well-trodden — but it adds server surface for
something the runtime bridge already supports, couples marketplace release
cadence to fused-render releases, and misses the point of building the
marketplace *as a fused-app*. If the helpers outgrow subprocess-git (e.g. we
want the download manager's cancel semantics on slow clones), `fused.trackJob`
already covers progress/cancel reporting without new routes.

Long-running steps (first clone, pulls on slow networks) report through
`fused.trackJob` so they show in the download manager and survive the page
navigating away.

### UI

Single page, three states, no build step (plain ES2020 like every template):

- **Browse** — card grid from `index.json`: icon, name, description, author,
  tags, installed/update-available badge. Client-side search over
  name/description/tags; tag filter chips. Card colors/theme follow the
  sessions app's shell-theme CSS variable pattern.
- **Detail** — rendered `readme.md`, metadata, and the Install / Open /
  Update / Uninstall actions. Readme rendering reuses the repo's existing
  dependency-free markdown approach (same constraint as templates: no JS deps,
  no build). A "Preview" link renders the app straight out of the read-only
  cache via `/render?path=<cache>/<slug>/index.html` — try before install.
- **Installed** — the subset of cards with installs, with update-all.

### Shell wiring

The standard six steps for a content sub-app:

1. `@router.get("/community")` in `fused_render/server/routers/shell.py`'s
   flat page list.
2. `community_mount_ready` flag in `/api/config`
   (`routers/config.py`, next to `learn_mount_ready`).
3. `frontend/src/apps/community/index.ts` exporting a
   `communityEntryPath(config)` codec (mirror `apps/learn/index.ts`).
4. Route branch in `frontend/src/shell/App.tsx` rendering
   `<StatView variant="learn">` (chrome-free).
5. `NavItem` in `frontend/src/shell/ShellSidebar.tsx`, gated on the readiness
   flag.
6. `community.zip` + `FUSED_RENDER_COMMUNITY_ZIP` override in `BUILTIN_MOUNTS`;
   zip added to the DMG/wheel payload builds.

---

## 4. The local cache: one clone, not one per app

**Layout under `~/.fused-render/community/`:**

```
~/.fused-render/community/
├── repo/            # blobless clone of fused-render-community-apps
└── installs.json    # install registry (§5)
```

The marketplace maintains a **single managed clone** of the community repo.
GitHub is the database, git is the sync protocol, the disk is the cache: the
UI never talks to GitHub directly — it only reads local files that a
background git process keeps fresh, which keeps the network off the critical
path everywhere except first run.

The clone is `git clone --filter=blob:none --no-checkout`:
`--filter=blob:none` downloads the commit graph and directory trees but no
file contents (blobs are fetched from GitHub lazily, in one batched pack
request per checkout, the first time something materializes them — and are on
disk forever after); `--no-checkout` skips materializing a working tree, so
nothing is checked out until asked for. After the clone, the helper sparse-
checks-out exactly what the browse grid needs: `index.json` plus every app's
`icon.svg` and `metadata.json`. An app's full folder is materialized only when
its detail view, preview, or install first touches it
(`git sparse-checkout add <slug>`; the readme alone can come from
`git show HEAD:<slug>/readme.md` without widening the checkout).

### Lifecycle and expected latency

| Step | Network | Expected latency |
|---|---|---|
| First launch ever: clone + sparse checkout of index/icons/metadata | yes | 2–6 s, behind a "fetching catalog…" state; once per machine |
| Opening the marketplace later: read cached `index.json` from disk | no | 50–150 ms (one runPython spawn + small file read) |
| Background refresh on open: `git fetch` + ff + re-checkout sparse set | yes | 0.3–1.5 s when unchanged, a few s when apps landed; user never waits on it |
| First open of one app's detail view: materialize `<slug>/` (blob pack for that folder) | yes | 0.5–2 s (size-capped 20 MB/app, typically ≪ 1 MB) |
| Later opens of the same app | no | ~50 ms, disk read |
| Preview-before-install (`/render` from the materialized cache folder) | no | local page render; folder already materialized by the detail view |
| Install: copy from cache + atomic rename + git-init | no | 200–800 ms, all local disk + two git subprocesses |
| Update check: cached `index.json` shas vs `installs.json` | no | 0 — pure local JSON comparison during the browse render |
| Applying an update | no | sub-second, same shape as install |

Refresh strategy is stale-while-revalidate: serve the cached `index.json`
immediately, kick a fetch in the background, re-render if the catalog moved. A
"Refresh" button forces a fetch. Readme rendering happens client-side from
the cached markdown; relative image links resolve against the materialized
folder via `fused.rawUrl` — local files, no network. The only moments a user
perceives GitHub's existence are the one-time first-run catalog fetch and the
first open of each app's detail view.

Rejected alternative: **sparse-clone per app via the existing `/clone`
deep-link machinery** (`fused_render/deeplink.py`). Superficially a 1:1 fit —
but a sparse clone of `<repo>/tree/main/<slug>` lands the app at
`<dest>/<slug>/index.html` with `index.html` one level *below* the app folder,
so `app_listing`'s "entry = single direct-child `.html`" convention never
matches and the app hub won't list it; and every installed app would drag a
full `.git` of the whole community repo. The user's mental model — "copy that
app into their Fused folder" — is the copy semantics of §5, not N clones. The
deep-link flow stays valuable as a *sharing* channel (a `?git=` link to one
community app from a blog post still works today), but it is not the install
mechanism.

Rejected alternative: **full clone (no blob filter)** — simplest, but
downloads every app's content up front; at 500 apps × 2 MB average that's a
~1 GB first run for a user who installs two apps. The blobless clone is
pay-for-what-you-touch with identical code paths afterward.

If the repo grows huge, the escape hatch is fetching `index.json` +
per-app tarballs over raw HTTP instead of a clone — the `index.json` contract
(§2) is deliberately clone-agnostic, so this swaps out inside `repo.py`
without touching the UI or install registry.

---

## 5. Install

Flow, all inside `install.py` via `fused.runPython`:

1. **Confirm dialog first** (in the page, before the runPython call), reusing
   the trust-on-confirm language of the `/clone` page (DL-3/D110): the app
   will render same-origin and — when `requires_python` — can run Python on
   this machine. Author, source link, and size shown.
2. Ensure the cache is fresh enough (pull if the user hit Install from a
   stale listing and the slug is missing).
3. Copy `repo/<slug>/` to a staging dir, drop repo-plumbing files if any, then
   claim `<workspace>/community/<slug>/` atomically by rename (the vendored
   `move_into_new_dir` idiom from §3 — `os.rename`, collision retries to
   `<slug>-2` etc., same behavior as clone-a-deployed-page).
4. Git-init the destination with a first commit = pristine upstream (the
   vendored `init_repo` sequence from §3), exactly like `POST /api/apps/new`
   does for scaffolded apps. This is what makes update-time dirty-detection free
   (§6) and gives users the history/claude modes on their copy.
5. Record in `installs.json`:

```json
{
  "schema": 1,
  "installs": {
    "trip-explorer": {
      "path": "/Users/you/Documents/Fused/community/trip-explorer",
      "commit": "<per-app sha from index.json at install time>",
      "installed_at": "2026-08-12T10:03:00Z"
    }
  }
}
```

6. Offer **Open** → `/apps/community/<slug>` — the app hub's normal builder
   route; the folder is already a first-class workspace app, discovered by the
   two-level walk with no registration.

`community` is an ordinary workspace tag (tags are just folders). Reserving it
is a docs-level convention, not code: the marketplace installs there, and
CONTRIBUTING tells users the folder is managed. Nothing breaks if a user
hand-creates apps in it — they simply have no `installs.json` entry and the
marketplace ignores them.

Uninstall = trash the folder (the explorer's existing trash semantics) +
remove the registry entry. User edits are in the folder's git history, which
goes to trash with it — the confirm copy says so.

---

## 6. Update

The listing joins `index.json` per-app `commit` against `installs.json`:
differing shas ⇒ "Update available" badge.

Update flow in `install.py`:

1. `git -C <install> status --porcelain` — **clean copy**: replace contents
   with the new cache version, commit "Update to <sha>" on top of the existing
   history, bump `installs.json`. One linear history: pristine → user edits
   (none) → update.
2. **Dirty copy / user commits beyond the first**: stop and warn. v1 offers
   two buttons — "Keep my version" (dismiss, badge stays) and "Overwrite"
   (commit the user's current state first as "Local edits before update", then
   apply upstream on top, so nothing is ever lost from history). No merge UI
   in v1 — that's what the git history and the user's own tools are for.

Yanked/removed apps (folder deleted upstream): slug disappears from
`index.json`; installed copies keep working (they're plain local apps and we
never reach into the workspace uninvited); the marketplace shows them under
Installed as "no longer in the catalog" and stops offering updates. That's the
whole removal story for v1 — takedown of already-installed copies is
explicitly out of scope.

---

## 7. Trust and moderation

fused-render's stance is already "your own machine, your own trusted code" —
no sandboxing, deliberate (SPEC §9). A community app is *someone else's* code,
so the marketplace must not blur that line:

- **PR review is the publishing gate.** Every app enters via a reviewed PR to
  the community repo; maintainers are the moderation layer. CI's lint checks
  (§2) catch accidents, not adversaries — the human review is the control.
- **Trust-on-confirm at install**, same doctrine as deep-link clone (D110):
  one explicit dialog stating that the app runs with the same trust as the
  user's own files, including Python execution when `requires_python`. No
  repeated nagging after that — installed means trusted, consistent with
  everything else in the workspace.
- **Preview-before-install is not a weaker trust level** — rendering from the
  cache already executes the app's JS same-origin. The detail view says so in
  small print. If review standards ever loosen (auto-merge, etc.), preview
  needs to be revisited before anything else.
- Reporting a bad app = GitHub issue on the community repo; removal = revert
  PR. No in-app report flow in v1.

---

## 8. Submission flow (v1)

Documented in the community repo's CONTRIBUTING.md:

1. Build the app locally under `~/Documents/Fused/<anything>/<slug>/`.
2. Add `readme.md`, `icon.svg`, `metadata.json` (checklist + JSON schema file
   in the repo; CI tells you what's missing).
3. Fork, copy the folder in flat at the repo root, open a PR.
4. CI validates; a maintainer reviews and merges; `index.json` regenerates;
   the app is live in everyone's marketplace on their next catalog refresh.

A future "Share to community" button in the app builder (pre-filling the PR
via `gh`) is the obvious v2, and the folder layout is already identical on
both sides by construction — but it's out of scope now.

---

## 9. Open questions (with recommendations)

- **Should installed community apps surface `icon.svg`/metadata in the app
  hub?** Requires growing `app_listing.app_dict` and the hub cards.
  *Recommendation: no for v1* — installed apps look like ordinary apps (title
  from `<title>`, hashed card color); the marketplace's own Installed view is
  where the rich presentation lives. Revisit if the hub grows icons for all
  apps.
- **Org ownership of the repo** — `fusedio/fused-render-community-apps`
  assumed; needs creating, branch protection, and 1–2 maintainers named.
- **Catalog refresh cadence** — proposed stale-while-revalidate on page open +
  manual refresh; no background polling from the shell.
