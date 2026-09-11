---
name: fused-render-app-doctor
description: Use when reviewing or sharing an app — checking for secrets, hardcoded paths, stray generated files, missing structure, or a stale API version.
---

# App Doctor

Share-readiness pass over one app folder. `fused-render` ships as a packaged desktop app — the Python package is not on PATH, so never shell out to a `fused-render` subcommand.

**Two modes.**

- **Fix session on one row** (the usual). The task names one check id and carries that row's findings inline; `fused_render/app_doctor.py` already computed section, severity, state. Go to the matching `##` heading, do only that. Never re-derive the checklist or re-run other checks.
- **No panel.** Someone asks for a review with no report to hand — see [No panel](#no-panel).

**Not a code audit.** No opinion on logic bugs, cache eviction, date math, dedup, DOM injection, error handling, perf. Even when the request says "review for correctness": say this skill covers share-readiness, offer the audit as separate work.

**Two kinds of row.** `fact` — a file read or git call settled it, nothing to judge. `candidate` — a regex over arbitrary text, so it locates a SHAPE, not a verdict; read the surrounding file before calling any hit real. This engine over 8 apps in a live workspace: all 40 candidate findings were false positives.

## `secrets` — leaked credentials

**candidate** — `ci/app_check.py`'s `_PREFIXED_SECRET_PATTERNS`, `_PRIVATE_KEY_RE`, `_ASSIGNMENT_SECRET_RE`.

Not a finding:

- **Committed generated log** (`runs/*.json`, a captured stdout dump). A real key pasted into live output is still a leak — but one value echoed 26 times is one root cause. Say it once, point at the cause.
- **Vendored third-party file** — stdlib module, bundled dep's docstring. Someone else's `/tmp/xxx` is not this app's secret.
- **Markdown code span or documented example** — a fenced `sk-ant-...` showing "your key looks like this".
- **Deliberate fake** — `xxxxxxxx`, `<your-key-here>`, `sk-ant-REDACTED`, `changeme`, `fake`, `sample`, `example`, `test`, empty string. Placeholders, even when the shape slips past the floor script's own detector.

Real: live-looking key shape, long random string, or a PEM block, in a file the app ships and executes.

**Fix.** Never quote a whole secret back — mask the middle, or name only where it lives. Give file and line; advise rotating anything ever committed. Retyping the same value elsewhere is not a fix.

## `entry` — has an app entry page

**fact** — `app_listing.app_entry` found no page carrying `<meta name="fused-app">`.

**Fix.** Add that tag to the `<head>` of the page meant to open. Multiple candidates: first non-hidden one in name order wins, so pick accordingly.

## `api-version` — declares the current fused API version

**fact** — the row names both numbers.

**Fix.** Invoke **`fused-render-api-migration`** and stop. Don't read the intervening `docs/v{N}.md` yourself. Don't hand-edit the tag — that skill stamps it when the migration actually lands, and stamping it here claims a migration that didn't happen.

## `pyproject` — pyproject.toml is valid TOML

**fact** — skip means no file, which is fine (optional; it only declares deps beyond the bundled Python). Fail means it exists and won't parse.

**Fix.** Read the parse error from the row's `detail`, fix the syntax. Broken costs the app its extra packages, not its ability to open.

## `readme` — has a README explaining the app

**fact** — a `README` or `README.*` at the app root.

**Fix.** Write one. A sentence or two on what the app does, for whoever receives the folder.

## `icon` — icon.svg is valid SVG

**fact** — skip means no `icon.svg`, which is fine. Fail means one exists and won't parse.

**Fix.** Route to **`fused-render-app-icon`**; it owns icon authoring.

## `device-paths` — no paths tied to one machine

**candidate** — `ci/app_check.py`'s `_POSIX_DEVICE_RE`/`_WIN_DEVICE_RE`.

Not a finding:

- **Inside a URL** — `https://example.com/opt/...` says nothing about a local filesystem.
- **Prose** — a comment or doc, including one explaining what NOT to do. The floor script skips `.md`/`.rst`/`.txt` entirely, but a docstring inside `.py`/`.js` still reaches you.
- **Deliberate system-path constant** — `SKIP_DIRS = (..., "/private/var/vm", ...)`. That is the app being correct about the OS.
- **Bare system directory going nowhere user-specific** — `/var`, `/tmp`, `/private/var`, `/media` followed by a generic filename or more OS jargon. Talking about the machine, not about a person's data.

Real: a hardcoded `/Users/alex/data.csv` (or `/home/…`, `/Volumes/…`) the app opens at runtime, in shipped source.

**Fix.** A path relative to the app folder, or one the runtime hands the app at call time. Never another absolute path.

## `git` — every change is committed

**fact** — `git status --porcelain`, scoped to the app folder. Skip means no readable git repo or no git; unanswerable, not failing.

**Fix.** Commit the listed paths, or `.gitignore` them if they shouldn't be tracked — and commit that `.gitignore` edit too, or it is itself an uncommitted change and this row fails again — so what you share is what you tested.

## `pushed` — every commit is pushed

**fact** — `git rev-list --count @{upstream}..HEAD`. No network call, so it is as stale as the last fetch. Skip means no upstream or no remote; nothing to compare against.

**Fix.** Push the branch. The findings are the unpushed subject lines — if any read as work-in-progress, say so instead of pushing blindly. The row knows the commits exist, not that they're ready.

## `generated` — no generated files outside .fused/

**fact** — a bounded walk found `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/` (reported as the directory), or stray `.pyc`/`.pyo`/`.log`/`.db`/`.sqlite`/`.sqlite3` outside `.fused/`.

**Fix.** Delete them, move them under `.fused/` (where an app's machine-local state belongs), or `.gitignore` what will just regenerate.

## `preview` — has a preview.png thumbnail

**fact** — `preview.png` exists at the app root and is non-empty. It is what a card shows in a grid.

**Fix.** Author one, or point at the capture flow that produces it.

## Judging the app's `fused.*` calls

No row above judges whether a real `fused.*` call is correct. Read what the app calls, route to the owning skill, hand off — never restate its guidance here.

| App touches | Load |
|---|---|
| `fused.ai` (text/image/video/transcribe/embed), model or provider choice | `fused-render-ai` |
| `fused.runPython`, `fused.params`, general `.html`/`.py` view authoring | `fused-render-authoring` |
| `fused.trackJob`/`fused.watchJob`, or a `runPython` risking the 60s timeout | `fused-render-jobs` |
| `fused.fileIndex` | `fused-render-index` |
| `fused.capture` | `fused-render-capture` |
| `fused.daemon`, `[tool.fused-render.app]` (Python alive after the page closes) | `fused-render-background-apps` |
| stale or missing `fused-api-version` | `fused-render-api-migration` |
| an `icon.svg` that exists and fails to parse | `fused-render-app-icon` |

Each row comes from that skill's own `description:` line — re-check there rather than guessing from a name.

## Setting up checks for a repo

Asked to set up CI: write the files yourself, don't tell the user to copy them.

1. Run `git rev-parse --show-toplevel` from the app folder. Fails → not in a git repo, say so and stop; a workflow has nowhere to run.
2. Copy `ci/app-check.yml` (ships beside this file) verbatim to `<repo root>/.github/workflows/app-check.yml`, creating dirs as needed.
3. Copy `ci/app_check.py` verbatim to `<repo root>/.github/app_check.py`.
4. Report both paths, that it runs on push to `main` and on every PR, and that both files still need committing.

That workflow is a floor, not a substitute for this review. It runs `app_check.py` (stdlib-only, nothing to install) per app folder and exits 1 only on a **fact** finding of severity **critical** or **warning**. It prints without failing: every **candidate** (all 40 in the measurement above were false positives, so one must never block a push) and a **suggested** fact — a missing README or `preview.png`, real but cosmetic. `suggested` exists only for this exit-code decision; the checklist itself has no such tier. Gating on `kind == "fact"` alone would fail a build over a thumbnail while a leaked-credential candidate exited 0.

The floor is deliberately a subset — no `entry`/`api-version`/`git`/`pushed`/`generated`, which need the runtime's own knowledge or a live repo a fresh checkout may not have.

## No panel

Walk every section above in the app's real folder with ordinary tools (grep, read, bash). Judge each hit; don't just report matches. The `secrets` and `device-paths` triage applies exactly as it does to a handed-over row.

One line per finding, sorted by path then line:

```
path:line: rule: excerpt
```

Mask the secret itself — first two characters, last two, stars between — so the report is safe to paste anywhere. Use `.` as the path for a finding about the folder. Close with a count and nothing else. Clean app: say so in one line.
