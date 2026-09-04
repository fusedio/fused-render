---
name: fused-render-app-doctor
description: Use when reviewing or sharing an app — checking for secrets, hardcoded paths, or a stale API version.
---

# App Doctor

One command, `fused-render doctor [path] [--json] [--check]`, reviewing exactly one app folder — no path reviews the current directory. `--check` exits non-zero when a HIGH finding fired; LOW findings are always reported and never fail a run. `--json` gives the same findings as records instead of a printed digest.

Run it before sharing an app, or whenever asked to review one. A finding's `excerpt` never contains a whole secret — safe to paste into a reply.

## Per family

- `secrets` (HIGH) — move the value out of the file (env var, local config the app reads at runtime, `.gitignore`'d file) and rotate the credential if it was ever committed. Never just edit the excerpt back into place; the finding tells you WHERE, not what to replace it with.
- `device-path` (HIGH) — swap the absolute path for one relative to the app folder, or for one the runtime hands the app at call time. An app another person opens does not have your home folder.
- `api-version` (LOW) — the page's declared `fused-api-version` is behind current. For the remediation, load `fused-render-api-migration` rather than guessing what changed between versions.
- `structure` (LOW) — housekeeping: a missing entry page, README, or thumbnail, or a `pyproject.toml` that doesn't parse. Fix what's named. For `structure:bad-icon` specifically (an `icon.svg` that exists but fails to parse), load `fused-render-app-icon` rather than hand-fixing the SVG. (An icon that's simply absent is not flagged — only one that exists and fails to parse.)
- `generated` (LOW) — a cache or log file loose in the tree; delete it or add it to `.gitignore`.

## Judging the app's actual `fused.*` calls

The command checks the five mechanical families above, forming no opinion on whether a real `fused.*` call is being used correctly. Real judgment on each API surface belongs to the skill that already owns that surface, kept current independently of this one. Read what the app actually calls and route to the skill for each surface it touches instead of re-deriving that judgment here:

| App touches | Load |
|---|---|
| `fused.ai` (text/image/video/transcribe/embed), model or provider choice | `fused-render-ai` |
| `fused.runPython`, `fused.params`, or general `.html`/`.py` view authoring | `fused-render-authoring` |
| `fused.trackJob` / `fused.watchJob`, or a `runPython` call that risks the 60s timeout | `fused-render-jobs` |
| `fused.fileIndex` | `fused-render-index` |
| `fused.capture` | `fused-render-capture` |
| `fused.daemon`, or `[tool.fused-render.app]` (Python that must stay alive after the page closes) | `fused-render-background-apps` |
| `api-version:behind` finding | `fused-render-api-migration` |
| `structure:bad-icon` finding | `fused-render-app-icon` |

This mapping is read from each skill's own `description:` line, not guessed from its name — re-check there if a skill's scope changes. This pass needs a reader who understands the code well enough to know which surfaces are in play, which CI doesn't have, so it is manual/agent-driven only and is never wired into `app-check.yml`. Don't restate any routed skill's guidance here — hand off to it.

## Setting up checks for a repo of apps

`skills/fused-render-app-doctor/ci/app-check.yml` is the workflow already written for `fusedio/fused-render-community-apps` — copy it into that repo's `.github/workflows/`, nothing to fill in. The doctor itself only knows how to review one app folder, so the workflow loops the repo's top-level folders itself: for each one that carries a `metadata.json`, it runs `fused-render doctor --check` against that folder, installs the app's own declared dependencies (from its `pyproject.toml`, if it has one) before running its tests, and runs those tests when it has any (`test_*.py`, `*_test.py`, or a `tests/` folder with collectible test files) — one pass per app, a nonzero exit at the end if any app failed either step. Do not invent a different workflow shape or a `--init-ci` flag — there isn't one; this file is the setup.
