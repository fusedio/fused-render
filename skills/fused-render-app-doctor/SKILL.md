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

## Judgment pass

The command's `api-misuse` check only knows whether a called name exists on `window.fused`. It has no idea whether a real method is being used correctly, so once it returns, read the app's own source for the things a regex can't reach. This pass needs a reader who understands the code, which is exactly what CI doesn't have, so it stays a manual or agent-driven step and is never wired into `app-check.yml`.

Look for at least these three:

- A promise-returning call used without `await` or `.then`. `runPython`, `writeFile`, `uploadFile`, `mkdir`, `stat`, `readFile`, `trackJob`, `watchJob`, and every `ai.*` verb (`ai.text`, `ai.image`, `ai.video`, `ai.transcribe`, `ai.embed`, `ai.cancel`, `ai.models.load`, `ai.models.download`, `ai.models.unload`) return a promise. A correct call awaits it (or chains `.then`/`.catch`) and does something with failure. A call left to run unawaited fires and moves on, so an error the page never sees, a write that hasn't landed when the next line assumes it has, or two calls racing each other on the same file all look like nothing happened until a user hits it.
- Generated data or cache written somewhere other than `.fused/`. An app's own state belongs under its `.fused/` folder (see `_IGNORED_DIR_NAMES` and the generated-file check above); a `writeFile` or `mkdir` call building a cache, log, or scratch file at a path outside it leaves junk loose in the app folder that git then has to be told to ignore by hand, or worse, that gets committed.
- An expensive call fired per keystroke or per render instead of on demand. `runPython` and the `ai.*` verbs are real network or subprocess round-trips, not free reads. A call wired straight into an input handler or a render path fires on every keystroke or every frame; a correct version debounces, waits for an explicit action (submit, click), or otherwise fires only when the user actually asked for the result.

Ground anything else you flag here the same way: open `fused_render/static/runtime.js` and check the method actually behaves the way you're about to describe. Don't name a member or a behavior you haven't confirmed there.

## Setting up checks for a repo of apps

`skills/fused-render-app-doctor/ci/app-check.yml` is the workflow already written for `fusedio/fused-render-community-apps` — copy it into that repo's `.github/workflows/`, nothing to fill in. It runs the doctor with `--check` against the repo root, plus each app's own tests when it has any (`test_*.py`, `*_test.py`, or a `tests/` folder). Do not invent a different workflow shape or a `--init-ci` flag — there isn't one; this file is the setup.
