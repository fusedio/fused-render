# Decisions / notes while building install-consent-plan.html

## Dev env
- Worktree had no `.venv` and no `frontend/shell-dist` (both gitignored). Set
  up per `setting-up-dev-env` skill: `uv venv --python 3.12 .venv`, then
  `uv pip install --python .venv/bin/python -e ".[dev,bundled,fused]"`.
  Frontend built with `bun install && bun run build` (user's global CLAUDE.md
  prefers bun over npm; `bun run build` runs the same `package.json` script
  npm would).

## Task 1 — projectenv.nonstandard_dependencies_of()
- Reason strings settled on: "from a git repository", "from a URL", "from a
  local path", "from a custom index" (per [tool.uv.sources] key), and "a
  custom package index for everything" (per project-wide index, reported once
  under the host via urllib.parse.urlparse(url).netloc).
- PEP 508 direct references detected by splitting on "@" after stripping the
  marker (`;...`) — matches the plan's example table exactly (`foolib @
  https://...` -> "from a URL", `git+https://...` -> "from a git repository").
- `explicit = true` on a `[[tool.uv.index]]` table excludes it from the
  project-wide report — it's confined to whatever `[tool.uv.sources]` routes
  to it, so it doesn't carry the "redirects everything" risk this classifier
  exists to flag.
- Dedup/order not treated as a contract; only ever feeds a one-line-per-entry
  prompt.

## Task 2 — engine.py / env.py
- `engine.py`'s `needs_install` dict spreads `nonstandard` conditionally
  (absent when empty), matching the existing `python` key's pattern exactly.
- `/api/env/install` in `env.py` always includes `nonstandard` (even `[]`)
  rather than conditionally — plan didn't call for conditional spreading
  there, and the route's response isn't reused elsewhere the way the
  needs_install dict is, so there was no existing convention to match.

## Task 3 — runtime.js consent gate
- Split `showInstall` into `ensureInstallRow` (get-or-create the {row, count}
  entry) + `paintPreparing` (the "Preparing X…" paint) so the confirm step and
  the ordinary progress path can share the exact same row without
  double-incrementing `entry.count`.
- Added a `confirmInstall(need, row, ui)` function reusing the SAME row
  `installRow()` builds (added a `track` field to its return value, plus a new
  hidden `install` button next to `cancel`, wrapped both in a `buttons` div).
  During confirm: track hidden, install button shown; on settle, reversed.
- The REAL cancel handler (`onCancel`, POSTs `/api/env/cancel`) is now
  attached only inside `runInstall()`, i.e. only after the confirm resolves —
  previously it was attached unconditionally at the top of `startInstall`.
  This matters: attaching it during the confirm stage would fire both it and
  the confirm's own cancel handler off one click.
- `approvedInstalls` Set keyed by `need.project || need.key` (fallback exists
  because some test scenarios' stubbed `needs_install` omit `project`; real
  server responses always include it per engine.py).
- Test harness (`tests/test_server_env_install.py`): `_JS_PRELUDE`'s stub
  `addEventListener` now auto-clicks a button whose `textContent === "Install"`
  via `queueMicrotask`, controlled by `globalThis.__autoInstall` (default
  true). This is what keeps every pre-existing install test working unchanged
  — they all effectively answer the confirm "Install" without knowing a
  confirm exists. `globalThis.__installPrompts` counts how many times that
  fired, used to assert "prompts once" for the five-concurrent-scripts case.
- One existing test (`test_one_cancel_click_fires_one_cancel_request`) reached
  `installing.get(key).row.cancel._h.click` SYNCHRONOUSLY right after calling
  `installEnv`, before the auto-install's microtask had a chance to resolve
  the confirm and attach the real `onCancel` listener — so it was hitting the
  confirm-stage `onCancelConfirm` handler instead. Fixed by wrapping that
  access in a `setTimeout(fn, 0)` (a macrotask runs after all currently
  queued microtasks drain).
- New test `test_cancelling_the_confirm_makes_no_install_post_and_rejects`:
  sets `__autoInstall = false`, calls `runPython`, and polls
  (`setTimeout(fn, 0)` loop) for the row to exist before clicking Cancel —
  needed because `runPython`'s path to the row goes through a REAL Promise
  tick (`/api/run`'s mocked fetch), unlike `installEnv` called directly,
  so the row does not exist synchronously right after the call the way it
  does in the `_run_loader`-based tests.

## Task 4 — --no-build default
- Threaded `allow_build` (bool) through `_build` -> `install` -> the worker's
  argv (added an 8th slot, `"1"`/`""`) -> `main()` -> `envinstall._spawn` ->
  `envinstall.start(project_dir, allow_build=False)` -> `/api/env/install`
  reads `body.get("allow_build")`.
- Had to bump `main()`'s `len(args) < 7` check to `< 8` and update 6 existing
  `worker.main([...])` test calls in tests/test_env_install.py to pass a
  trailing `""` for the new slot.
- One existing test asserted `_build`'s exact cmd list
  (`test_the_worker_syncs_the_project_into_the_named_venv`) — updated to
  include `"--no-build"`, since that's now the default.
- Verified uv's actual `--no-build` failure wording empirically (uv 0.12.5,
  real `uv sync --no-build` against a package with no wheel — `uwsgi`):
  ```
  × No solution found when resolving dependencies:
    ╰─▶ Because all versions of uwsgi have no usable wheels and your project
        depends on uwsgi, we can conclude that your project's requirements
        are unsatisfiable.

  hint: Wheels are required for `uwsgi` because building from source is
  disabled for all packages (i.e., with `--no-build`)
  ```
  The "Because ... has no usable wheels" line's wording changes shape
  depending on whether the requirement is pinned (`X==1.2.3` vs "all versions
  of X"), so Task 5's client-side regex anchors on the `hint:` line instead,
  which is stable and names the bare package name in backticks.
- Note: `envinstall.start()`'s new `allow_build=False` default also applies
  to every existing caller, including the bundled AI runners
  (`ai/supervisor.py`'s `envinstall.start(runner.folder)`, unchanged call
  site — no `allow_build` passed). This was a deliberate read of the plan
  (not explicitly spelled out there): AI-2a's "wheels-only" rule
  (test_ai_runner_deps.py) already holds those manifests to wheel-only
  installs via explicit `[[tool.uv.index]]` tables, so `--no-build` should be
  a no-op for correctly-declared runners and actually hardens that existing
  rule (a violation now fails loudly instead of silently building from
  source). Not verified end-to-end against a real AI runner install (heavy
  network/GPU deps, not exercisable in this sandbox) — worth a real install
  smoke-test before shipping if that hasn't already happened elsewhere.

## Task 5 — install-anyway retry
- `startInstall`'s POST+poll cycle split into `tryInstall(allowBuild)` so the
  retry reuses the same row/activeKey/cancel-handler machinery.
- Extracted `askRow(row, ui, title, detail, installLabel)` out of
  `confirmInstall` so the retry's differently-labelled question
  ("Install anyway") shares the same resolve/reject/mount mechanics.
- Regex: `/hint: Wheels are required for `([^`]+)` because building from
  source is disabled/` — anchored on uv's own hint line (see Task 4 notes for
  why, and the verified real-world wording).
- Declining the retry does NOT reject with `EnvInstallCancelled` — it lets the
  original resolver error stand as `EnvInstallError`, since refusing a source
  build isn't the same speech act as cancelling the install.
- Test-writing gotcha: the harness's default auto-click (`__autoInstall`,
  keys off `textContent === "Install"`) still fires for the FIRST ordinary
  confirm even in a scenario built for testing the retry — so a naive
  "poll until row.install is visible, then click" in the test scenario
  clicks the WRONG (first) confirm's button, since by the time the test's
  own polling IIFE gets its first synchronous check, `confirmInstall` has
  already made the install button visible (synchronously, inside
  `startInstall`). Fix: poll for `row.install.textContent === "Install
  anyway"` specifically, not just visibility.

## Task 6 — jobs dock row
- Thread spawned in `start()` right after `_spawn(...)`, gated on reaching
  that line at all (only the claiming call gets there — a joiner returns
  earlier, from the `if not _claim(key)` branch).
- Job id: `sys:env-install:<key>`. Title: `Preparing <display_name>`.
  `kind="task"`, `cancellable=True`, `owner` follows from the `sys:` prefix
  automatically (jobs.py).
- Cancel wiring: the thread reads `cancel_requested` off the dict its OWN
  `jobs.upsert()` call returns (not a separate read), and calls the module-
  level `cancel(key)` — same function `/api/env/cancel` calls.
- State mapping from `progress()`: not done -> stays `running`; done with
  `error == "the install was cancelled"` (envinstall.cancel's own literal
  string) -> `cancelled`; done with any other error -> `error` (+ message);
  done with no error -> `done`.
- Mirrors `activity`/`bytes_done`/`bytes_total` -> jobs' `done`/`total`/`unit`
  fields, same pattern `ai/supervisor.py`'s own progress-mirroring loop
  already uses for the identical `_UvProgress` fields.
- Poll interval: `_JOB_MIRROR_POLL_S = 0.5`, matching `ai/supervisor.py`'s
  own bring-up loop's `time.sleep(0.5)` against the same file.
- **Known overlap, not fixed here**: `ai/supervisor.py`'s `_bring_up` already
  runs its OWN polling loop over `envinstall.progress(key)` and mirrors it
  into a job under `job_id_for(worker.model)` (a DIFFERENT id from
  `sys:env-install:<key>`). Since Task 6's thread is unconditional inside
  `start()`, an AI model load now produces TWO job-dock rows for the one
  install: the supervisor's own (byte-level progress, richer detail) and
  this new generic one. The plan's spec places the thread inside `start()`
  with no exception carved out for this caller, so it was implemented
  exactly as specified; flagging the duplicate-row UX as something the next
  person may want to either accept, suppress (e.g. skip the mirror when the
  caller is known to report its own row), or fold into one.
- Test hygiene: `jobs._jobs`/`_dismissed` are process-global. Added an
  autouse `_clean_jobs_registry` fixture (mirrors test_jobs_api.py's own
  `clean_registry`) and made every test write a terminal progress record
  (or trigger one via cancel) so its mirror thread actually exits — a
  daemon thread left polling forever after `jobs.reset()` would silently
  resurrect the row for whichever later test happened to still be running
  half a second later.
- Test gotcha: do NOT fake a pid `_pid_alive` reports as dead to simulate a
  "cancel a fake install" scenario — `_recorded_progress`'s own crash
  diagnosis treats a not-done record with a dead pid as a crash and writes
  a terminal error on the very first poll, racing (and beating) the test's
  own assertion. Used the test process's own (genuinely alive) pid instead,
  with `_kill` stubbed to a no-op so nothing real gets signalled.

## Summary
All six tasks (1-6) are implemented and committed, one commit per task (plus
one small follow-up commit fixing test stubs after Task 4's route change).
Test commands run, per the plan's per-task verify lines:
- `pytest tests/test_projectenv.py` — 72 passed
- `pytest tests/test_server_env_install.py` — 70 passed
- `pytest tests/test_server_env_install.py tests/test_env_install.py` — 70 + 139 passed
- `pytest tests/test_env_install.py` — 139 passed (both Task 4 and Task 6 checks)
Additionally spot-checked (not required by the plan, but touched code):
`tests/test_ai_runtime.py` (512 passed, 1 pre-existing skip).
Never ran a bare `pytest` sweep, per the environment note.
