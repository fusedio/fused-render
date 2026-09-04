---
name: fused-render-app-doctor
description: Use when reviewing or sharing an app — checking for secrets, hardcoded paths, a stale API version, or setting up checks for a repo of apps.
---

# App Doctor

One command, `fused-render doctor [path] [--json] [--check]`. No path reviews the current directory. A path that is itself an app reviews just it; a path that is a folder of apps (a slug-named directory with its own `metadata.json`, per top-level entry) reviews every one inside and names which app each finding belongs to — this is how it runs unattended against a whole repo, no extra flag. `--check` exits non-zero when a HIGH finding fired; LOW findings are always reported and never fail a run. `--json` gives the same findings as records instead of a printed digest.

Run it before sharing an app, or whenever asked to review one. A finding's `excerpt` never contains a whole secret — safe to paste into a reply.

## Per family

- `secrets` (HIGH) — move the value out of the file (env var, local config the app reads at runtime, `.gitignore`'d file) and rotate the credential if it was ever committed. Never just edit the excerpt back into place; the finding tells you WHERE, not what to replace it with.
- `device-path` (HIGH) — swap the absolute path for one relative to the app folder, or for one the runtime hands the app at call time. An app another person opens does not have your home folder.
- `api-misuse` (HIGH) — the call doesn't exist. Fix the name or drop the call; don't guess a fix from the excerpt alone.
- `api-version` (LOW) — bump the page's declared `fused-api-version` after confirming the app doesn't rely on removed behavior.
- `structure` (LOW) — housekeeping: a missing entry page, README, icon, or thumbnail, or one that doesn't parse. Fix what's named.
- `generated` (LOW) — a cache or log file loose in the tree; delete it or add it to `.gitignore`.

## Setting up checks for a repo of apps

`skills/fused-render-app-doctor/ci/app-check.yml` is the workflow already written for `fusedio/fused-render-community-apps` — copy it into that repo's `.github/workflows/`, nothing to fill in. It runs the doctor with `--check` against the repo root, plus each app's own tests when it has any (`test_*.py`, `*_test.py`, or a `tests/` folder). Do not invent a different workflow shape or a `--init-ci` flag — there isn't one; this file is the setup.
