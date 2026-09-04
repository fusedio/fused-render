# App Doctor — build log

Working notes for whoever resumes this build. Read this alongside the commit
history (one commit per task, exact messages from `app-doctor-plan.html`) —
the commits show what shipped, this file explains why, and records the two
places the plan needed a call it didn't spell out.

## Tasks 1–4: done

Committed, in order:

1. `App doctor: secrets and device-specific paths` — `fused_render/app_doctor.py`
   created with the file-enumeration plumbing (`git ls-files` first, bounded
   `os.walk` fallback, binary/size/ignore-name skipping) and the two
   HIGH-severity families. `tests/test_app_doctor.py`.
2. `App doctor: housekeeping — structure, API version, stray files` — the
   three LOW-severity families. `tests/test_app_doctor_housekeeping.py`.
3. `App doctor: flag unknown fused.* calls` — the fused API misuse check,
   plus the runtime.js parity test. `tests/test_app_doctor_api.py`.
4. `CLI: a doctor subcommand for reviewing an app` — `fused-render doctor`,
   human and `--json` output, `--check` exit code.
   `tests/test_app_doctor_cli.py`.

All stdlib-only in `app_doctor.py` itself, per the constraint: nothing in
that file imports from the rest of `fused_render`. Every rule it needs from
`app_listing.py`, `current_apps.py`, and `fused_api_version.py` is
duplicated, not imported, with a docstring pointing back at the original.

## Decisions and dead ends

**Duplication over import, every time app_doctor needed package logic.**
`_app_entry`/`_direct_child_pages`/`_has_fused_meta` mirror
`app_listing.py`; `_ICON_NAME` mirrors `current_apps.py`; the version
parsing mirrors `fused_api_version.py`. This is the whole point of the
stdlib-only constraint — a module that ships into a completely separate
CI context (the community-apps repo, Task 5) can't drag the rest of the
package's import graph with it. Where the original function is trivial
(a few lines), it's copied verbatim; the docstring says which function it
mirrors so a future change to the original is a prompt to check this file
too, not a silent drift.

**`CURRENT_API_VERSION = 1` is a hardcoded constant, not derived.**
`fused_api_version.py:current_version()` isn't reachable without importing
`skill_sources()`, which pulls in far more than stdlib. The doctor's copy
is a plain int with a comment that it must be bumped by hand in step with
`fused_api_version`'s own `docs/vN.md`. This is a real maintenance seam —
noted here so it isn't mistaken for an oversight.

**`_leaks()` test helper (Task 1 fixture fixed in Task 2).**
Once housekeeping checks (Task 2) landed, five Task-1 tests that asserted
`check(...) == []` on deliberately minimal fixtures started failing —
those fixtures were never complete apps, so they now also trip
`structure:*`/LOW findings that Task 1 was never testing for. Fixed by
adding a `_leaks(findings)` filter (secrets + device-path rules only) and
scoping those five assertions to it, rather than making the fixtures fully
valid apps (which would have buried the secrets/paths intent of those
tests under unrelated setup).

**`__pycache__` stays out of `_IGNORED_DIR_NAMES`.**
Originally listed alongside `.git`/`.venv`/`.fused`/`node_modules`, which
broke `test_a_stray_cache_dir_is_flagged` — `_walk_files` skips ignored
directories entirely, so the stray-file check never saw the directory to
flag it. Fix: `_IGNORED_DIR_NAMES` now holds only names that should never
be walked at all (true infra: `.git`, `.venv`, `.fused`, `node_modules`).
Generated-cache directories (`__pycache__`, `.pytest_cache`, etc.) are
walked — so the stray-file check can find them — but their contents are
still skipped by the ordinary binary/size sniff every other file goes
through, so nothing wastes time reading `.pyc` bytes as text.

**The runtime.js parity test (Task 3) needed its own bugfixes, in the test
file, before it could be trusted:**
- The object-literal-key parser initially cleared its in-progress token on
  every `{`, which threw away the `params` key name right before its
  nested-object value. Fixed by not resetting the token on `{`.
- It didn't track `(...)` depth at all, so a comma inside a call
  expression in a value position (`aiPost("/api/ai/cancel", capability ? {
  capability } : {})`, inside `ai`'s `cancel` entry) was misread as a
  top-level entry separator, producing a spurious `capability` key. Fixed
  by folding `{`/`(` into one depth counter and `}`/`)` into the same
  counter's decrement, with string-literal state tracked so quote
  characters never affect depth.
Both bugs were caught by running the test against the real
`fused_render/static/runtime.js` and inspecting the mismatch — exactly the
scenario Task 3 exists to guard against, just one turn earlier than a real
runtime change would have triggered it.

**CLI subprocess tests need `PYTHONPATH`, not just `cwd`.**
`tests/test_app_doctor_cli.py` spawns `python -m fused_render.cli` with
`cwd` set to a pytest `tmp_path`, far from any repo. The shared venv used
across worktrees has `fused_render` resolvable via a fixed `sys.path`
entry that points at the *main* checkout
(`/home/iamsdas/Work/fused-render`), not this worktree — so without
intervention the subprocess would run the main checkout's `cli.py` (which
has no `doctor` subcommand at all) instead of the code under test. Fixed
by having the test's `_run()` helper put this worktree's root first on the
child process's `PYTHONPATH`. This is specific to how the shared venv is
wired here, not a property of `fused_render` itself — worth knowing if a
similar subprocess test is added elsewhere in this worktree.

**CLI output shape**, since the plan intentionally left exact formatting
undecided: human mode groups findings by family (the part of `rule` before
`:`), each group labeled by its severity; a clean folder prints a
"no findings" line naming the path. `--json` emits
`{"ok": bool, "path": str, "findings": [...]}` — `ok` is `False` whenever
any HIGH-severity finding exists, independent of whether `--check` was
passed (so a script can read `ok` off a plain non-`--check` run too);
`--check` is the only thing that turns `not ok` into a non-zero exit.

## Task 5: repo mode, and CI for the community apps

`App doctor: repo mode, and a workflow for the community apps` — committed.
`fused_render/app_doctor.py`'s `check()` now branches into repo mode; the
membership rule (`_is_slug` + a top-level `metadata.json`) is a duplicate of
`community.py:_is_slug`/`_scan_catalog`, same reasoning as every other
duplication in this file. `skills/fused-render-app-doctor/ci/app-check.yml`
is the hand-written workflow for `fusedio/fused-render-community-apps`.
`tests/test_app_doctor_repo.py`.

**One correction to the plan's stated deviation surface, found while
implementing:** the plan describes repo mode as triggering "when the given
folder is not an app" — but `check()` is also called on tiny single-app
fixtures across the existing test suite, several of which put a stray file
under a subdirectory (`tests/test_app_doctor.py`'s
`test_a_private_key_block_is_flagged_and_fully_masked` writes
`creds/key.pem`) without that subdirectory being an app itself. A naive
"has any subdirectory → repo mode" branch silently swallowed that test:
`creds/` isn't a slug-and-metadata.json app folder, so `_check_repo` found
nothing under it and the single-app checks that would have caught the key
never ran at all. Fixed by requiring that repo mode see at least one
subdirectory that actually PASSES the app-folder test
(`_is_repo_app_dir`) before it takes over — a folder with no entry page and
no app-shaped subdirectories still falls through to single-app mode, which
already reports `structure:no-entry` for it. This is the one place the
plan's own membership rule (from `_scan_catalog`) had to be applied a beat
earlier than the plan's one-line description implied — worth flagging in
case a future review reads that description as final.

**`_run_doctor`'s human-readable output needed an update for repo mode.**
Before this task, `f["path"]` alone was enough to locate a finding.
Repo-mode findings carry an extra `app` key; verified against a real clone
of `fusedio/fused-render-community-apps` (see below) that the CLI's human
output was silently ambiguous — many different apps' findings printed with
identical-looking bare paths (`index.html`, `test_disk.py`) and no way to
tell which app owned which line. Fixed in `fused_render/cli.py`'s
`_run_doctor`: `where` is prefixed `app/` whenever the finding carries that
key. `--json` needed no fix — the raw finding dict (with `app` already in
it) was being serialized as-is.

**Manually verified against a real clone**, per the plan's own verification
step: cloned `fusedio/fused-render-community-apps` (network was available
in this environment) and ran `fused-render doctor --check .` against it
from outside the venv's own tree. It found real HIGH findings (a
`fused.mail`/`fused.io`/`fused.learn` call the runtime does not expose in
`open-mail`/`learn-fused-render`, hardcoded `/Users`/`/home` paths in
several apps' own test files, a credential-shaped assignment in a vendored
`pannellum.js`) and LOW housekeeping findings, exited non-zero under
`--check`, and named the owning app on every line. This also incidentally
confirmed the CI workflow's own test-discovery convention (`test_*.py` /
`*_test.py` / a `tests/` folder) against a real app in that repo
(`disk-usage/test_disk.py`). The clone was deleted afterward; nothing from
it was copied into this worktree.

## Task 6: the skill

`Skill: reviewing an app with the doctor` — committed.
`skills/fused-render-app-doctor/SKILL.md` points at the one command and
says what to do per family (move a secret out and rotate it, swap a
device path for a relative one, fix or drop an unknown call, bump the
declared API version, fix the named housekeeping gap, delete or
`.gitignore` a stray generated file) without restating the engine's own
rules or teaching its detection patterns. No test file changes were
needed — `pytest tests/test_skill_plugin.py -q` (64 tests, including both
`test_every_skill_in_the_repo_is_discovered` and
`test_the_build_hook_discovers_the_same_skills_as_the_runtime`) passed
against the new skill directory unmodified, confirming the plan's claim
that both discovery paths are plain directory scans.

## Post-build correction: DECISIONS.md

The Task 5 commit accidentally overwrote this repo's actual project
decision log (`DECISIONS.md` at the worktree root — the whole D1-onward
design history for fused-render, unrelated to this feature) with an
earlier draft of this file, because both files share the same name. It
was restored to its prior content in the very next commit
(`Fix: restore the project DECISIONS.md, move app-doctor build notes
out`), and this build's own notes moved here, to
`APP_DOCTOR_DECISIONS.md`, to make sure that collision can't recur. If a
later builder is looking for the project's actual design log, it is
`DECISIONS.md`; this file is scoped to the app-doctor build only.

## All six tasks: done

Every task in `app-doctor-plan.html` is committed, one commit each, exact
messages from the plan, in order:

1. `App doctor: secrets and device-specific paths`
2. `App doctor: housekeeping — structure, API version, stray files`
3. `App doctor: flag unknown fused.* calls`
4. `CLI: a doctor subcommand for reviewing an app`
5. `App doctor: repo mode, and a workflow for the community apps`
6. `Skill: reviewing an app with the doctor`

Plus one unplanned correction commit (see above) for the DECISIONS.md
mistake. Full scoped test run at the end of the build:
`pytest tests/test_app_doctor.py tests/test_app_doctor_housekeeping.py
tests/test_app_doctor_api.py tests/test_app_doctor_cli.py
tests/test_app_doctor_repo.py tests/test_skill_plugin.py -q` — all green.
