---
name: fused-render-app-doctor
description: Use when reviewing or sharing an app — checking for secrets, hardcoded paths, stray generated files, missing structure, or a stale API version.
---

# App Doctor

`fused-render` is a packaged desktop app for end users: the Python package is not installed and not on PATH, so nothing here shells out to it — there is no `fused-render` subcommand to reach for, and an attempt to run one buys a failed round trip and nothing else.

This skill has two shapes now, and which one you are in matters:

**A fix session on ONE row.** The App Doctor panel (`fused_render/app_doctor.py`) already computed the whole checklist — section, severity, pass/fail/skip, and every finding — and the task that invoked you names exactly one check id and carries that row's findings inline. Jump straight to the section below whose heading matches that id. Do not re-derive the checklist, do not re-run the other checks, and do not read past the one section you were pointed at unless it tells you to route elsewhere. This is the ordinary way this skill runs now.

**Invoked directly, no panel in front of you.** Someone asks you to review or share an app with no App Doctor report to hand. Walk every section below yourself, in order, the way `## Reviewing a whole app with no panel` at the bottom describes.

## What this review is not

The sections below are the whole of it. This is a share-readiness pass, not a code audit: it does not read an app's logic looking for bugs, and it forms no opinion on cache eviction order, timezone and date math, deduplication, DOM injection, error handling, or performance. Skip that work even when the request says "review for correctness" — say plainly that this skill covers share-readiness and offer the audit as separate work rather than quietly doing both.

## `secrets` — leaked credentials

**Kind: candidate.** The row's findings come from a regex over arbitrary text (`ci/app_check.py`'s `_PREFIXED_SECRET_PATTERNS`, `_PRIVATE_KEY_RE`, `_ASSIGNMENT_SECRET_RE`) — it locates a SHAPE, it does not know whether the shape is a real credential. Triage every finding before touching anything.

**Triage first.** For each finding, read the surrounding file and decide:

- Is it inside a committed **generated log** (a `runs/*.json`, a captured stdout dump)? A real credential pasted into a live run's output is still a real leak — but the SAME absolute path or token repeated 26 times across log files is one root cause, not 26 findings; say so once and point at the root cause, not at every line it echoes on.
- Is it inside a **vendored third-party file** (a stdlib module, a bundled dependency's docstring)? Text like `/tmp/xxx` in someone else's documentation comment is not this app's secret and not this app's path either.
- Is it inside a **markdown code span** or a documented example? A backtick-fenced `sk-ant-...` in a README showing "here's what your key looks like" is documentation, not a leak.
- Is it a **deliberately fake value** — a test fixture's password, an obviously placeholder-shaped string the floor script's own pattern-matching didn't catch? `xxxxxxxx`, `<your-key-here>`, `sk-ant-REDACTED`, `changeme`, `fake`, `sample`, `example`, `test`, an empty string, are placeholders, not findings, even when the exact shape slips past the floor script's own placeholder detector.

**What's real.** A live-looking key shape, a long random-looking string, an actual PEM block, sitting in a file that ships with the app (not a log, not a vendored file, not prose).

**The fix.** Never quote a whole secret back in your reply — mask the middle, or say only where it lives. For anything real, say which file and line, and advise rotating the credential if it was ever committed. The fix is never to just retype the same value somewhere else and call it fixed.

## `entry` — has an app entry page

**Kind: fact.** The runtime already resolved this (`app_listing.app_entry`): a page carries `<meta name="fused-app">` or none does. There is nothing to judge — if it failed, no page in the folder carries the marker.

**The fix.** Add the tag to the page meant to be opened: `<meta name="fused-app">` in its `<head>`. If there are multiple candidate pages, the first non-hidden one in name order carrying the tag wins — pick accordingly. This is authoring work; if the app also needs `fused.*` calls fixed up, route there (see the table below).

## `api-version` — declares the current fused API version

**Kind: fact.** The row already names both numbers — the entry's declared version and the runtime's current one. There is no judgment here about whether the gap matters; that question, and the whole migration, belongs to **`fused-render-api-migration`**. Invoke it and stop — don't open the intervening `docs/v{N}.md` files yourself, and don't hand-edit the version tag: the migration skill stamps it once the actual migration lands, and stamping it here would claim a migration that didn't happen.

## `pyproject` — pyproject.toml parses

**Kind: fact.** Skip means the file doesn't exist, which is fine — it's optional, and its only job is declaring dependencies beyond the bundled Python environment. Fail means it exists and doesn't parse as TOML.

**The fix.** Read the parse error (the row's `detail` carries it) and fix the syntax. This file being broken costs the app its extra packages, not its ability to open at all — that's why it's a warning, not critical, in the checklist.

## `readme` — has a README

**Kind: fact.** Plain existence: a file named `README` or `README.*` at the app's root.

**The fix.** Write one. A sentence or two on what the app does is enough — this is for whoever you share the folder with, not documentation in the deep sense.

## `icon` — icon.svg parses

**Kind: fact.** Skip means there's no `icon.svg` at all, which is fine — missing is not a finding. Fail means one exists and doesn't parse as SVG.

**The fix.** Route to **`fused-render-app-icon`** rather than hand-editing the file yourself — it owns how an icon gets authored or fixed.

## `device-paths` — no paths tied to one machine

**Kind: candidate.** Like `secrets`, this row's findings come from a regex over text (`ci/app_check.py`'s `_POSIX_DEVICE_RE`/`_WIN_DEVICE_RE`), and the same discipline applies: read each hit before flagging it as real.

**Triage first.** For each finding:

- Is it inside a **URL** (`https://example.com/opt/...`)? A URL's path component sharing a root's spelling says nothing about a local filesystem — not a finding.
- Is it in **prose** — a comment, a doc, an explanation of what NOT to do? The floor script now skips `.md`/`.rst`/`.txt` files for this family entirely, and no longer treats a backtick as an opening quote, but a docstring or comment inside a `.py`/`.js`/etc. file can still slip through; read it in context.
- Is it a **deliberate system-path constant**, like a `SKIP_DIRS = (..., "/private/var/vm", ...)` naming an OS-internal path on purpose? That's the app being correct about the filesystem, not a hardcoded dependency on one.
- Does it continue past a bare system directory (`/var`, `/tmp`, `/private/var`, `/media`) into nothing user-specific — a single generic filename, another OS-jargon segment? That reads as talking about the machine in general, not about a particular person's data.

**What's real.** A hardcoded `/Users/alex/data.csv` (or `/home/…`, `/Volumes/…`) an app actually opens at runtime, in source the app ships and executes.

**The fix.** A path relative to the app folder, or one the runtime hands the app at call time — never another hardcoded absolute path.

## `git` — everything committed

**Kind: fact.** `git status --porcelain` already answered this, scoped to the app's own folder. Skip means the folder isn't in a git repo this server can read, or git isn't available — not a finding, just unanswerable.

**The fix.** Commit the listed paths (or add them to `.gitignore` if they shouldn't be tracked at all) — "commit them so what you share is what you tested."

## `pushed` — everything pushed

**Kind: fact.** `git rev-list --count @{upstream}..HEAD` already answered this — no network call is made, so this can be as stale as the last fetch. Skip means there's no upstream configured or no remote at all; that is not a failing app, it just has nothing to compare against.

**The fix.** Push the branch. The row's findings are the unpushed commits' subject lines — if any of them look like work-in-progress that shouldn't ship yet, say so rather than pushing blindly; the row only knows commits exist locally, not whether they're ready.

## `generated` — no generated files outside .fused/

**Kind: fact.** A bounded walk already found these: `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/` (reported as the directory itself), and stray `.pyc`/`.pyo`/`.log`/`.db`/`.sqlite`/`.sqlite3` files sitting outside `.fused/`.

**The fix.** Delete them, move them under `.fused/` (an app's own machine-local state belongs there), or add them to `.gitignore` if they'll just regenerate.

## `preview` — has a preview.png thumbnail

**Kind: fact.** Plain existence and non-emptiness of `preview.png` at the app's root — this is what a card shows in a grid of other apps.

**The fix.** Author one, or point at whatever capture flow produces it for this app. Nothing to parse or judge — either the file is there and non-empty, or it isn't.

## Judging the app's actual `fused.*` calls

None of the rows above form an opinion on whether a real `fused.*` call is being used correctly. That judgment belongs to the skill that owns each API surface, kept current independently of this one. Read what the app actually calls and route to the skill for each surface it touches instead of re-deriving that judgment here:

| App touches | Load |
|---|---|
| `fused.ai` (text/image/video/transcribe/embed), model or provider choice | `fused-render-ai` |
| `fused.runPython`, `fused.params`, or general `.html`/`.py` view authoring | `fused-render-authoring` |
| `fused.trackJob` / `fused.watchJob`, or a `runPython` call that risks the 60s timeout | `fused-render-jobs` |
| `fused.fileIndex` | `fused-render-index` |
| `fused.capture` | `fused-render-capture` |
| `fused.daemon`, or `[tool.fused-render.app]` (Python that must stay alive after the page closes) | `fused-render-background-apps` |
| stale or missing `fused-api-version` | `fused-render-api-migration` |
| an `icon.svg` that exists and fails to parse | `fused-render-app-icon` |

This mapping is read from each skill's own `description:` line, not guessed from its name, re-check there if a skill's scope changes. Don't restate any routed skill's guidance here, hand off to it.

## Setting up checks for a repo

When asked to set up CI checks for an app, don't tell the user to copy files by hand, do it yourself.

1. From the app folder, run `git rev-parse --show-toplevel`. If it fails, the app isn't inside a git repo at all, say so plainly and stop, a workflow file has nowhere to run.
2. Read `ci/app-check.yml`, the file that ships alongside this SKILL.md, and write it verbatim to `<repo root>/.github/workflows/app-check.yml`, creating `.github/workflows/` if needed.
3. Read `ci/app_check.py`, the file that ships beside it, and write it verbatim to `<repo root>/.github/app_check.py`.
4. Tell the user the paths you wrote, that the workflow runs on push to `main` and on every pull request, and that both files still need to be committed.

That workflow is a floor, not a substitute for this review: it runs `app_check.py` — a plain, stdlib-only script, nothing to install — against each app folder in the repo. It fails the run on a **fact** finding (a missing `index.html`/README/`preview.png`) but only PRINTS a **candidate** finding (a secrets or device-path hit) without failing the build — a real run of this exact engine against 8 apps in a live workspace found every one of 40 content findings from those two families was a false positive, so a candidate alone is not allowed to block a push. It is deliberately a subset of what this skill checks by hand — no `entry`/`api-version`/`git`/`pushed`/`generated` rows, since those need either the runtime's own knowledge or a live git repo a plain checkout may not have committed yet.

## Reviewing a whole app with no panel

Invoked directly, with no App Doctor report already computed: walk every section above yourself, in the app's actual folder, using your ordinary tools (grep, read, bash). Judge each hit, don't just report matches — the triage discipline under `secrets` and `device-paths` applies exactly the same whether a panel handed you one candidate row or you're finding the candidates yourself.

Report one line per finding, in the same shape the CI check prints, sorted by path then line:

```
path:line: rule: excerpt
```

Mask the secret itself — first two characters, last two, stars between — so the report is safe to paste into a log or a chat. Use `.` as the path for a finding about the folder rather than a file. Close with a count and nothing else. When the app is clean, say so in one line.
