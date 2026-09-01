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

## Post-review fixes (manual browser testing found four defects)

### Defect 2 — "Nothing listed" copy leak
`confirmInstall`'s all-PyPI branch said "A one-time download. Nothing
listed." — "Nothing listed" was the PLAN document's own table annotation
describing that no packages get named; it leaked into the shipped string.
Changed to "A one-time download." No test asserted the old string.

### Defect 3 — dead `showInstall`
`startInstall` calls `ensureInstallRow` / `mountInstallSoon` / `paintPreparing`
directly (it needs the row before it knows whether this call ends up asking a
question first), so nothing called `showInstall` any more — TypeScript
flagged it as declared but never read. Deleted, and the comments that still
described it as the live entry point were updated. `tests/test_server_env_install.py`
used `showInstall` as a convenient single call exercising that exact trio;
recreated it as a test-only helper (`_SHOW_INSTALL_TEST_HELPER`) rather than
resurrecting it in the shipped runtime.

### Defect 4 — monospace consent question
`detail`'s monospace face is right for its original job (uv's verbatim
resolver error) but wrong for the consent question, which is prose. `askRow`
now sets `row.detail.style.fontFamily` to a proportional stack for the
question's duration and restores the monospace default in `settle()`.

### Defect 1 — Install click did nothing (the blocker)

**This did NOT reproduce.** Root cause, as asked for: there isn't one in the
current code. Extensive investigation across multiple independent angles
found `askRow` / `confirmInstall` / `ensureInstallRow` / `startInstall` (the
functions named in the bug report) to be correct, and — more importantly —
building the REAL production server fresh (`fused_render.server.app.create_app`,
via uvicorn on a throwaway port, no `scripts/dev.sh`, not port 2422) and
driving it with a real headless-Chromium browser (Playwright) against the
exact repro folder (`consent-demo`, venv key `73e1f9faac6029dd`, deleted
before each attempt to match "not built") reproduced the OPPOSITE of the
report: Install worked every time, including with deliberate 3-second and
15-second waits between the prompt appearing and the click (standing in for
a human actually reading the question) — the install completed and the
page's own `#status` element showed "ok".

What was checked and ruled out:
- `askRow`'s listener wiring is symmetric between `row.install` and
  `row.cancel` (same call shape, no shadowing, nothing removes one listener
  early) — confirmed by reading and by an independent second-agent review
  that reached the same conclusion from a cold start.
- No CSS/attribute hides, disables, or covers the Install button.
- No timer/poll runs during the confirm stage that could reset the row
  (`paintInstall`/`installBarIndeterminate` only run once `runInstall`
  starts, i.e. after Install is clicked).
- D214's two-round (interpreter + packages) bootstrap does not apply here —
  this machine already has Python 3.12 resolved (other venvs on it already
  built successfully), confirmed by the real `/api/run` probe never carrying
  a `python` key.
- `nonstandard_dependencies_of` (the pre-flight classification) does static
  TOML/PEP 508 parsing only — no subprocess, no file writes — so it cannot be
  racing the live-reload file watcher into a spurious `window.location.reload()`
  before a click lands. (This was the leading hypothesis for a while — a
  reload mid-question would tear down the very listeners answering it, and
  would look exactly like "nothing happens" — but nothing in the pre-flight
  path can trigger one, and the real-server Playwright run above, with a real
  15-second wait for exactly this kind of race to show up, still worked.)
- The server's actual `/api/run` and `/api/env/install` response shapes were
  probed directly (in-process `TestClient` against the real
  `consent-demo` folder) and match what the client code assumes byte-for-byte
  (key `73e1f9faac6029dd`, no `python` field, `nonstandard: []`).

**Best guess for what the tester saw**: a stale tab. The six commits behind
this feature (confirm gate, retry, jobs-dock mirror) landed over the course
of development; a browser tab left open from before the confirm-gate commit
landed — or simply not hard-refreshed after a later fix — would have shown
an Install button wired to an earlier, genuinely broken version of this code.
Not provable after the fact, but it is the only theory consistent with
"current code is provably correct" plus "a human saw it fail every time in
one session."

**What shipped anyway**: the report named a real gap in the test harness,
independent of whether Defect 1 itself was ever a real code bug. The
`__autoInstall` stub in `tests/test_server_env_install.py` fired the Install
click via `queueMicrotask` from INSIDE `addEventListener` itself — the
instant the listener was registered, which is strictly EARLIER than any real
click could ever land, and therefore could never have caught a listener that
stops working by the time a real, later click reaches it (which is exactly
the shape of bug the report describes). Switched the stub to a macrotask
(`setTimeout`, matching how a real click actually arrives) and added
`test_a_real_delayed_click_on_install_still_posts`, which turns auto-install
off, waits several real macrotask ticks before the row even exists and
several more once it does, and only then clicks — asserting the POST still
fires. All 72 tests in the file still pass with the more realistic stub.

**If this recurs**: re-verify against a hard-refreshed tab (or a fresh
private window) against the live server first, before assuming the code
regressed — the investigation above is thorough but the live/real-server
Playwright repro is the strongest evidence and it could not reproduce the
bug at all.

### Also investigated — jobs-dock duplicate row for AI model loads (Task 6's flagged gap)

Confirmed real, and fixed. `ai/supervisor.py`'s bring-up loop mirrors an AI
model's environment build into its own jobs-dock row (`job_id_for(model)`,
titled with the model, richer per-model detail — "Preparing {short} —
installing…"), while `envinstall.start()` unconditionally opened a SECOND,
generic row (`sys:env-install:<key>`, titled with the project's
`display_name`) for the exact same `uv sync` call. These are not two
different pieces of work — they are the same install reported twice through
two different code paths — so a user watching a model load for the first
time would see two jobs-dock rows for one thing happening.

Fixed by adding `envinstall.start(..., report_job=True)`: `ai/supervisor.py`
now passes `report_job=False`, since it already reports this exact install
into its own row. Every other caller (the loader's own `/api/env/install`)
keeps the generic mirror unchanged, since it has no row of its own for this
key. Covered by `test_report_job_false_opts_out_of_the_generic_mirror` in
`tests/test_env_install.py`.

## Second code review round — 8 findings, all fixed

One commit per finding (`git log --oneline` on this branch shows them in
order). Test commands run per finding are the three named in the task:
`pytest tests/test_server_env_install.py`, `pytest tests/test_env_install.py`,
`pytest tests/test_projectenv.py` — plus all three together at the end (294
passed). Never ran a bare `pytest` sweep.

### 1 (CRITICAL) — `--no-build` bricked any project declaring `[build-system]`

Reproduced and fixed exactly as directed. Verified against the real `uv
0.12.5` on this machine, in a throwaway `/tmp` directory (`uv init
--no-workspace initproj`, zero dependencies):

- `uv sync --no-default-groups --no-build` alone: fails —
  `` error: Distribution `initproj==0.1.0 @ editable+.` can't be installed
  because it is marked as `--no-build` but has no binary distribution ``.
  Confirms the report's premise: this text does not contain a `hint:` line,
  so `NO_BUILD_HINT` in runtime.js never matched it and no retry was ever
  offered — the folder was permanently stuck.
- Same command with `--no-install-project` added: succeeds, venv created,
  nothing to install.
- Adding `dependencies = ["uwsgi"]` (a real sdist-only package) and re-running
  with `--no-install-project` still in place: still refused, and the refusal
  still carries `` hint: Wheels are required for `uwsgi` because building
  from source is disabled for all packages (i.e., with `--no-build`) `` —
  confirming `--no-install-project` does not mask the retry path Task 5
  depends on.

Fix: `_env_install_worker.py`'s `_build` now appends `--no-install-project`
in the same `if not allow_build:` branch as `--no-build`. Trade-off
documented in `_build`'s own docstring and above: a src-layout project's own
package is no longer installed editable into the venv, so a script relying on
that editable install to `import mypkg` would lose it. Flat-layout script
folders (the norm here, per PY-16) are unaffected — Python already puts a
script's own directory on `sys.path` with no editable install involved. A
src-layout project that genuinely needs its own package importable would have
to declare it as an ordinary dependency (so uv builds *a* wheel for it) or run
`uv sync` by hand outside this app. Not expected to come up in practice: the
folders this feature targets are scripts, not distributable packages.

### 2 (HIGH) — a locked project never got the retry prompt

Reproduced with the exact repro from the task (`uv lock && uv sync
--no-default-groups --no-build --no-install-project`, same `initproj` +
`uwsgi` folder): the refusal shape at install time, once a lock already
resolves cleanly, has NO `hint:` line at all —
`` error: Distribution `uwsgi==2.0.31 @ registry+https://pypi.org/simple`
can't be installed because it is marked as `--no-build` but has no binary
distribution ``. `NO_BUILD_HINT` genuinely could not match this; `_sync_root`
deliberately keeps `uv.lock` across builds and a folder gains one just by
being run once, so this was highly reachable, exactly as flagged.

Fix: added `NO_BUILD_DISTRIBUTION` as a second alternative in
`noBuildPackage` (runtime.js), anchored on `` Distribution `NAME==VERSION
@ SOURCE` can't be installed because it is marked as `--no-build` `` and
capturing the name up to the first `=` (never the version). Regression test
mirrors the existing no-wheel-retry test but with this exact error shape.

### 3 (HIGH) — a manifest edit could install new nonstandard deps with no prompt

Confirmed the mechanism: `approvedInstalls` was a `Set` of `need.project ||
need.key` strings, so once a project's key was in the set, EVERY later
`startInstall` call for that project skipped `confirmInstall` outright,
regardless of what `need.nonstandard` said this time. Editing
`pyproject.toml` and letting live-reload re-run is the app's own documented
core workflow (see `startInstall`'s `activeKey` comment), so this was not a
contrived edge case.

Fix: added `nonstandardFingerprint(need)` — a stable, order-independent
serialization of `need.nonstandard` — and folded it into the approval key
(`(need.project || need.key) + " " + nonstandardFingerprint(need)`). A
project whose disclosure changes gets a fresh key and re-asks; a project
whose disclosure is untouched (the overwhelming majority of edits — version
bumps, added ordinary PyPI deps, code changes) reuses the earlier approval
exactly as before. Two regression tests: one proving a changed disclosure
re-asks (posts == 2, prompts == 2), one proving an unrelated version bump
does not (posts == 2 — the install still happens — but prompts == 1).

### 4 (MEDIUM) — `allow_build` is client-asserted

Read the plan's own accepted risk ("the prompt is not unforgeable... a
deliberately hostile page can reach `window.top`... Deferred") and agree with
the finding that `allow_build` is a real escalation past that baseline: the
baseline lets a hostile page get ITS OWN declared dependencies installed
un-prompted; `allow_build` lets it get an arbitrary build backend EXECUTED
un-prompted, which is a materially bigger blast radius (arbitrary code at
build time, not just "whatever `pip install` would have done anyway").

Considered a narrowing: only honour `allow_build=True` when the server's own
last-recorded `progress()` for this project's key already shows a
`--no-build` refusal, so a single blind POST could never trigger a build.
Rejected it as false comfort rather than shipping it: the same attacker who
can POST to this endpoint (no real auth, just `X-Fused`) also AUTHORED the
folder's `pyproject.toml`, so they can trivially manufacture that "prior
failure" themselves — declare a source-only dependency, POST once without
`allow_build`, watch it fail exactly as designed, then POST again with
`allow_build: true`. It costs the attacker one extra fetch() call in the same
hostile page and gives a real defender nothing to rely on. Documented the
exposure honestly at the read site instead (`server/routers/env.py`, beside
`allow_build = bool(body.get("allow_build"))`), and flagging it here as a
genuine follow-up: the real fix is the unforgeable-prompt work the plan
already defers (a native dialog in the supervisor process, which has no
response channel yet — `supervisor/protocol.py`).

**Follow-up for whoever picks up the unforgeable-prompt work**: when that
lands, `allow_build` should very likely require a SEPARATE, more explicit
confirmation than the ordinary install prompt — "run this package's setup
code" is a different consent question than "install these packages", and
today both go through the same client-asserted channel with no distinction
server-side.

### 5 (MEDIUM) — dead `nonstandard` response field and a false comment

Confirmed the field was genuinely dead: `confirmInstall` runs BEFORE the
`/api/env/install` POST is ever made (it has to — the whole point is asking
before any network activity), so it is built from the PRE-FLIGHT's
`need.nonstandard` (`/api/run`'s `needs_install`, via `engine.py`), never
from this endpoint's response. Grepped runtime.js for `data.nonstandard` —
zero hits.

Dropped the field (`env.py`'s `JSONResponse` no longer includes it) and the
`nonstandard_dependencies_of(project)` call that fed it, rather than wiring
the client to re-check it before spawning. Reasoning: the real-world race
this field could have guarded — a manifest edited between the pre-flight
request and this POST — is dominated by the same
edit-and-let-live-reload-rerun workflow Finding 3's fix already re-asks on,
for the NEXT run. The remaining window (between a user's click and this POST
landing, typically milliseconds) was never actually checked by anything
before this fix either, so removing the field changes no real behaviour —
only removes a false comment claiming the dialog was "built from THIS
answer, not from the pre-flight's." Deleted the one test that existed purely
to assert the now-removed field
(`test_install_reports_a_nonstandard_source_beside_the_requirements`);
`nonstandard_dependencies_of` itself stays fully covered by
`tests/test_projectenv.py`, which is the layer that actually owns its
correctness.

### 6 (MEDIUM) — list-form `[tool.uv.sources]` and `workspace = true` skipped

Confirmed both gaps by reading `nonstandard_dependencies_of`'s Shape 2 loop:
`isinstance(entry, dict)` unconditionally `continue`d past a list value, and
`_UV_SOURCE_REASONS` had no `"workspace"` key at all despite `workspace =
true` being a real, undocumented-here uv source form (a name that resolves to
another member of the same workspace — a local package, the same "not from
PyPI" risk shape as `path`).

Fix: normalised a source value (dict OR list) to a list of tables and
iterated all of them, taking the first key from `_UV_SOURCE_REASONS` found in
the first table that has one — same "checked in this order, one reason per
name" rule the single-table case already used, just extended across every
platform-conditional entry. Added `("workspace", "from a workspace member")`
to `_UV_SOURCE_REASONS`. Three new tests: a `workspace = true` case, a
list-form case that matches (`git`, behind a `marker`), and a list-form case
that matches nothing (only a bare `marker`, no source key) to prove silence
is still silence when nothing non-standard is actually there.

### 7 (MEDIUM) — `uv.toml`'s project-wide index shapes went undisclosed

Confirmed the asymmetry: `_env_install_worker.py`'s `_MIRRORED_NAMES`
includes `uv.toml` specifically BECAUSE `uv sync` reads it as real
configuration for the folder (its own comment there says so), but
`nonstandard_dependencies_of` only ever loaded `pyproject.toml`. `uv.toml`
uses the identical key names to `pyproject.toml`'s `[tool.uv]` table, just at
the file's TOP LEVEL rather than nested under `[tool.uv]` (it is itself a
dedicated uv config file, so there is no `[tool.uv]` to nest under).

Fix: extracted the index-URL-collection logic shared between the two shapes
into `_tool_uv_index_urls(uv: dict) -> list[str]`, added `_load_uv_toml`
(sharing the TOML-parsing guts with `_load_manifest` via a new `_load_toml`
helper), and Shape 3 now unions the index URLs from BOTH `pyproject.toml`'s
`[tool.uv]` and `uv.toml`'s top level — reporting both if a folder somehow
declares an index in each. Three new tests: `uv.toml` alone, both files at
once (both reported), and no `uv.toml` at all (answer unchanged from before
this fix — the common case).

Scope note: the finding named "the project-wide index shapes" specifically
(`index-url`/`default-index`/`extra-index-url`/`[[index]]`), and that is what
got read from `uv.toml`. `uv.toml` can also carry a top-level `[sources]`
table equivalent to `[tool.uv.sources]` (Shape 2) — not covered here, since
it was not what the finding asked for and Shape 2's per-dependency reasoning
(git/url/path/workspace/index) does not obviously need the same doubling
Shape 3 needed (a project-wide index is a NEW class of risk uv.toml could
introduce that pyproject.toml's absence of `[tool.uv]` would otherwise hide
completely; a `uv.toml`-only `[sources]` entry would very likely ALSO route
through `uv sync` unnoticed by the prompt, but proving that and wiring it up
was out of scope for this pass — flagging it here as a real follow-up gap,
not a deliberate exclusion on the merits).

### 8 (LOW) — the D214 two-round bootstrap's identical jobs-dock titles

Confirmed: round 1 (`PYTHON_BOOTSTRAP_KEY`) and round 2 (the venv key) each
get their own `_mirror_into_jobs` call with their own job id (the key
differs), so this was never the exact duplicate-row bug `report_job=False`
fixed for the AI path (two rows for ONE piece of work) — it is two rows for
two REAL, sequential pieces of work, both titled identically
(`f"Preparing {display_name}"`), which reads as a glitch (a row finishing and
instantly restarting) rather than as "first Python, then packages." Round
1's title was also inaccurate on its own terms: nothing about downloading a
prebuilt CPython interpreter is "preparing" the user's project.

Fix: `_mirror_into_jobs` takes a new `downloading_python: bool = False`
parameter (`start()` passes its own `acquire_python` truthiness one call
earlier) and titles round 1 `f"Downloading Python for {name}"`, leaving
round 2's `f"Preparing {name}"` unchanged. Regression test drives an actual
two-round `start()` sequence (mocking `script_python_ready` False then True,
same pattern `test_start_REPORTS_the_key_it_used_rather_than_leaving_it_to_be_recomputed`
already uses) and asserts both rows' titles.

## Stale test doubles after `envinstall.start()` gained `report_job`

CI's `fused-engine` job failed: 10 tests in `tests/test_ai_runtime.py` raised
`TypeError: <lambda>() got an unexpected keyword argument 'report_job'` at
`ai/supervisor.py:1008`'s `envinstall.start(runner.folder, report_job=False)`.
The `report_job` keyword (added earlier on this branch so the AI venv-bringup
path doesn't open a second, duplicate jobs-dock row for the same install)
was added to production's `envinstall.start()` but the test doubles standing
in for it in `test_ai_runtime.py` — six of them, all predating the keyword —
still had the old signature and blew up the moment the supervisor called
through them with the new argument.

Fixed all six: the `fake_start` in
`test_the_venv_wait_polls_the_key_the_installer_reports` and the `start`
closure in the `shared_install` fixture now take `report_job=True` and (for
`fake_start`) record every value they were called with; the four bare
lambdas now accept `report_job=True` and ignore it, since those tests aren't
about what gets passed. Strengthened
`test_the_venv_wait_polls_the_key_the_installer_reports` with
`assert report_jobs == [False, False]` — the one assertion pinning that
`_ensure_venv` actually passes `report_job=False` on both rounds, which
nothing previously checked.

Checked for the same staleness against the branch's other signature change
(`_env_install_worker._build` gaining `--no-build`/`--no-install-project`
and an `allow_build` slot): no other stale doubles found — every
`envinstall.start` monkeypatch and every `worker._build(...)` call in the
suite already matches the current signatures.

Did not touch `envinstall.start` or `ai/supervisor.py` — production
behaviour there is correct; only the doubles were behind.

## Dogfooding fix — the --no-build refusal reaching users as a sticky error
- Real-world dogfooding surfaced a defect Tasks 4/5 didn't anticipate: uv's
  `--no-build` refusal (the resolver question "Install anyway" exists to
  answer) also reached the jobs dock as a sticky red `error` row carrying
  uv's raw stderr verbatim — a wall of jargon a non-expert user cannot act
  on, that stays put until dismissed (`jobs.py`'s `_sweep` keeps `error`
  rows on purpose).
- Root cause: `_mirror_into_jobs` (envinstall.py) had no way to tell a
  `--no-build` refusal apart from a genuine resolver failure — both show up
  as `progress()`'s `done: true` with a non-empty `error`.
- Fixed by moving classification to where the fact actually originates: the
  worker (`_env_install_worker.py`), the one process that knows whether
  `--no-build` was passed. `install()`'s except block now calls
  `_no_build_package(str(e))` (gated on `not allow_build`) and writes a
  `needs_build` field — the bare package name — alongside `error`, which
  stays uv's verbatim text per SPEC PY-18 (`_write` was given the new
  parameter; it's additive, like `activity`/`bytes_done`/`bytes_total`
  before it).
- `_no_build_package` carries over the same two regexes runtime.js used to
  own (`_NO_BUILD_HINT`, the resolution-time `hint:` line; `_NO_BUILD_DISTRIBUTION`,
  the install-time wording once a `uv.lock` exists), with the same reasoning
  for each anchor point transcribed into the Python comments. Both were
  re-verified against the wordings already confirmed real in Task 4/5's
  notes — no new empirical uv run needed, since the text itself is unchanged,
  only where it's parsed.
- `_mirror_into_jobs`: a finished record carrying `needs_build` now maps to
  `state="cancelled"` with `message=f"waiting for your approval to compile
  {pkg}"`, checked BEFORE the plain `error` branch. `jobs.py`'s
  `TERMINAL_STATES` has exactly three members (`done`/`error`/`cancelled`)
  and none of them actually means "stopped, awaiting you" — `cancelled` is
  the least-wrong of the three (terminal, ages out normally instead of
  sticking, reads as "stopped" rather than "broken"). This is a real
  limitation: a jobs-dock user who never opens the page to see the actual
  "Install anyway" prompt sees a row that reads as a cancelled install, not
  as a question waiting on them. Accepted since the alternative (a fourth
  job state, or reusing `error`) is worse on every axis that matters more.
  The existing `_CANCELLED_ERROR` branch (a real user Cancel) is checked
  first and is completely unaffected.
- Confirmed by test (`test_a_needs_build_row_flips_back_to_running_on_the_retry`,
  using two real `envinstall.start()` calls rather than hand-written
  progress records) that the retry genuinely restarts the row: a finished
  claim is stale (`_claim_is_stale` reads `not _in_flight`), so `start(...,
  allow_build=True)` on the same project takes it over, re-`_spawn`s under
  the SAME key, and `_mirror_into_jobs` opens a fresh thread under the
  identical `sys:env-install:<key>` id — the "cancelled" row is a mid-flight
  word, not oneshot's last one.
- `runtime.js`: `NO_BUILD_HINT`, `NO_BUILD_DISTRIBUTION`, and `noBuildPackage`
  deleted outright — `/api/env/progress` returns `prog` verbatim (confirmed
  by reading `server/routers/env.py`), so `needs_build` reaches the client
  with no new plumbing. `poll()`'s terminal-error branch now attaches
  `e.needsBuild = prog.needs_build || null` to the thrown `Error`, and
  `tryInstall`'s catch reads `err.needsBuild` instead of re-deriving it from
  `err.message`. One detector now, not two that can disagree (the thing the
  brief called out as the actual bug class here).
- Fix 2 (prompt copy): `confirmBuildRetry` now takes `appName` and renders
  "`<pkg>` has to be compiled on this computer" / "`<AppName>` needs it, but
  there's no prebuilt version for this system. Compiling runs code from
  `<pkg>` and can take several minutes or fail. Only continue if you trust
  it." `startInstall`'s call site passes `need.name || "the environment"` —
  the identical fallback `confirmInstall` already uses, so both dialogs stay
  consistent when a project has no display name. Button label ("Install
  anyway") and Cancel behaviour (declining lets the original resolver error
  stand as `EnvInstallError`) are untouched.
- Tests added: `tests/test_env_install.py` — `test_needs_build_is_set_for_the_resolution_time_hint`,
  `test_needs_build_is_set_for_the_install_time_distribution_wording`,
  `test_allow_build_true_never_sets_needs_build`, a `needs_build is None`
  assertion added to the existing unrelated-RuntimeError test, plus
  `test_a_needs_build_refusal_mirrors_as_cancelled_not_error` and
  `test_a_needs_build_row_flips_back_to_running_on_the_retry` for the
  jobs-mirror side. `tests/test_server_env_install.py` — the two existing
  no-wheel/locked-project tests now carry `needs_build` on their mocked
  progress records (client no longer regexes `error` at all); added
  `test_the_retry_prompt_is_driven_by_the_field_not_by_regexing_error`
  (an `error` string that would never have matched either old regex still
  triggers the retry, proving detection isn't merely duplicated
  client-side), `test_an_unrelated_resolver_failure_never_offers_a_retry`,
  and `test_the_retry_prompt_names_the_app_not_jargon`.
- Ran only the narrowed files (`test_env_install.py`, `test_server_env_install.py`,
  `test_projectenv.py`) — 224 + 78 passed. Did not run the full suite (left
  to the orchestrator; `/tmp` quota kills a full run on this machine anyway).

## Round 3: four more review findings (credential leak, find-links, a stale
## cancel, and two mirror threads on one job id)

- Finding A (`projectenv.py`, index-host reporting): `urlparse(url).netloc`
  includes userinfo, so a `https://user:token@host/simple` index rendered
  the token into both the consent prompt and `/api/run`'s `needs_install`
  payload. Confirmed by reading the line before touching it. Replaced with
  a new `_index_host(url)` that reads `.hostname` (plus `.port` when one is
  present — informative, no secret) and, for the no-netloc fallback
  (`urlparse` finds nothing when the value has no scheme, which `or url`
  used to echo raw), strips a leading `userinfo@` from the text with a
  regex rather than trusting it verbatim — `user:token@host/path` with no
  `//` still parses as scheme=`user`, empty netloc, so the fallback needed
  the identical protection.
- Finding B (`projectenv.py`, `_tool_uv_index_urls`): `find-links` was never
  enumerated alongside `index-url`/`default-index`/`extra-index-url`/
  `[[index]]`, in either `[tool.uv]` or `uv.toml`'s top level (shared
  function, confirmed by reading the call site). uv prefers wheels from a
  `find-links` host exactly like a custom index, so a folder naming one
  disclosed nothing — `nonstandard_dependencies_of` returned empty and the
  prompt rendered the no-op "a one-time download" detail while uv routed
  every wheel-less package through that host. Handled identically to
  `extra-index-url` (string or list).
- Finding C (`envinstall.py`/`jobs.py`, a stale `cancel_requested` killing a
  retry) and Finding D (two mirror threads on one job id after a fast
  retry) are the same machinery — confirmed by reading `_mirror_into_jobs`,
  `jobs.upsert`, and `jobs.request_cancel` together rather than fixed as
  two separate patches, per the brief's instruction. The ownership story
  that resolves both:

  - **One thread per CLAIM, not per job id.** The job id
    (`sys:env-install:<key>`) is deterministic per venv key and gets reused
    across attempts, but the on-disk claim (`_claim_path`) changes identity
    on every retry: `_claim`'s takeover of a stale claim unlinks it and
    creates a new one, so its raw content (`f"{pid} {time}\n"`) is a cheap,
    already-existing fingerprint for "which attempt is this". A new
    `_claim_token(key)` helper reads it back.
  - `_mirror_into_jobs` captures its own claim's token the moment it is
    called (right after `start()`'s own `_claim()`), and the loop
    reconfirms the current on-disk token still matches at the TOP of every
    tick, before calling `progress(key)` at all. A retry's takeover changes
    the token, so the superseded thread notices immediately and returns —
    it never gets to read the fresh claim's synthetic `{"stage": "spawn",
    "done": False}` and mistake it for "still running", which is exactly
    what used to keep it looping forever alongside the new thread (Finding
    D: the comment at old line ~1913 claiming "there is never a second
    thread mirroring the same key" was true only because this case had
    never been exercised — corrected to describe the actual overlap and
    where it's resolved).
  - `current_token is None` is deliberately NOT treated as "superseded" —
    `_claim`'s own unlink-then-recreate takeover briefly passes through
    no-claim-at-all, and reading that instant as supersession would race
    the *other* new terminal case (below) into misdiagnosing a live retry
    as a vanished install.
  - The opening `jobs.upsert` is followed by a new `jobs.clear_cancel_requested(job_id)`
    (added to `jobs.py`, since `upsert`'s body has no key for this — a
    reporter's tick has nothing to say about a request it did not make).
    This disowns a `cancel_requested` a PREVIOUS attempt's dead mirror left
    set: `jobs.upsert` only clears the flag on a transition INTO a terminal
    state, and a mirror whose own `jobs.upsert` calls are best-effort can
    die without ever writing that transition, leaving the row `running`
    with the flag still set for the next attempt's identical job id to
    inherit silently (the state doesn't change, so `upsert`'s own clearing
    branch never runs). Called once, right after the opening upsert, so a
    ✕ pressed from then on still cancels normally through the unmodified
    `jobs.request_cancel` path (Finding C).
  - Fixed alongside: `prog = progress(key) or {}` made `finished =
    bool({}.get("done"))` permanently False once the record AND the claim
    were both gone (`progress_dir(key)` lives under
    `home_dir()/cache/_env_install`, reachable by any cache-clearing path)
    — a thread that would then spin forever, periodically resurrecting a
    phantom "Preparing X" row. `prog = progress(key)` (no fallback) now
    exits the loop with a `state="error"` record naming what happened the
    moment `prog is None`.
  - Corrected the `needs_build` branch's stale comment (old line ~1798):
    it never itself assigns `"running"` — its own loop returns the same
    tick it sets `"cancelled"` (`finished` is `True`). It is the RETRY's
    *new* `_mirror_into_jobs` thread, with its own opening upsert, that
    flips the row back on the next attempt.
- Tests added: `tests/test_projectenv.py` —
  `test_a_credentialed_index_url_discloses_host_not_the_token`,
  `test_a_credentialed_uv_toml_index_url_discloses_host_not_the_token`,
  `test_an_unparseable_index_value_falls_back_without_leaking_credentials`,
  `test_find_links_string_form_is_disclosed`,
  `test_find_links_list_form_is_disclosed`,
  `test_find_links_in_uv_toml_is_disclosed`. `tests/test_jobs_api.py` —
  `test_clear_cancel_requested_disowns_a_stale_flag_but_not_state`,
  `test_clear_cancel_requested_on_a_gone_row_says_so`.
  `tests/test_env_install.py` —
  `test_a_stale_cancel_requested_does_not_kill_a_fresh_attempt` (a
  hand-simulated dead mirror's leftover flag, then a real `start()`,
  proving the fresh attempt survives and a fresh ✕ still works),
  `test_a_retry_inside_the_poll_window_leaves_one_live_mirror_thread`
  (a real race: `_JOB_MIRROR_POLL_S` widened to 0.2s, a `needs_build`
  write immediately followed by a real retry `start()` call, then
  `threading.enumerate()` filtered by thread name to assert only one
  `env-install-jobs-mirror` thread stays alive), and
  `test_a_vanished_progress_record_and_claim_ends_the_mirror_thread`
  (`shutil.rmtree(progress_dir(key))` after a real `start()`, asserting the
  row reaches `state="error"` with a message naming what disappeared
  rather than staying stuck `running` forever).
- Ran narrowed: `test_projectenv.py` (84 passed), `test_jobs_api.py` +
  `test_server_env_install.py` (124 passed), `test_env_install.py` (150
  passed, the three new race/threading tests re-run three times each with
  no flakes observed). Did not run the full suite — left to the
  orchestrator, per the same `/tmp`-quota note as Round 2.

## Round 4: delete the disclosure-free confirm, run an all-PyPI install silently

- The screenshot that triggered this round: "Install dependencies for
  OpenWhisper? A one-time download. [Cancel] [Install]" — a confirm with no
  decision content in it. A screen like that does not inform a choice, it
  trains a reflexive click on whichever button reads affirmative, which
  makes the NEXT screen — the one that actually carries risk — more likely
  to get the same reflexive click. The fix is not to word the empty screen
  better; it is to not show it. The two screens that remain are exactly the
  ones with something to disclose: `confirmInstall` when `nonstandard` is
  non-empty (a git/URL/local-path/named-index source, a workspace member, a
  project-wide custom index, or a `find-links` host), and
  `confirmBuildRetry` for a `--no-build` refusal, which gates an actual
  source build (`setup.py` can run arbitrary code; a wheel install runs
  none).
- `confirmInstall` (runtime.js) had a fallback branch rendering the detail
  "A one-time download." when `need.nonstandard` was empty — the exact copy
  in the screenshot. Deleted rather than left inert: with the call site
  below never reaching it on an empty `nonstandard`, the branch had no
  caller left, and stranded code gets deleted, not commented out.
- `startInstall`'s consent gate now short-circuits before the
  `approvedInstalls` check: `if ((need.nonstandard || []).length === 0)
  return runInstall();`. Placed above the existing `approvedInstalls`/
  `nonstandardFingerprint` check and the `confirmInstall`/cancel branches,
  which are otherwise untouched — a non-empty `nonstandard` still goes
  through exactly the approval-dedup and cancel-rejection path Round 1 built.
  The empty case never touches `approvedInstalls` at all: there was no
  question to have approved, so no entry is added, which is what keeps
  invariant 3 (below) true for free rather than by a separate check.
- While reading the two lines this change touches (`nonstandardFingerprint`'s
  join and the gate's `approvalKey` concatenation), found both had a literal
  NUL byte (`\x00`) standing in for the ` ` separator, and the `.join()`
  call in `nonstandardFingerprint` had a literal `\x01` where `""` belongs —
  three stray control bytes, already committed on this branch, invisible in
  every editor view because they render as whitespace. `grep` (silently,
  with no "binary file" notice under `-c`) and `Read` both hid them; only
  `rg -a` and a raw byte scan (`od -c` / a small Python script counting
  bytes `< 0x20`) surfaced them. Harmless to `Set`/string-equality semantics
  — a NUL or SOH is still a valid, deterministic character in a JS string —
  so the approval-key and fingerprint logic never actually misbehaved, but
  shipping literal control bytes in a security-relevant comparison is not
  something to leave in place once seen. Restored to the evident intent (`"
  "` and `""`) with a byte-level rewrite (`old_string`-based Edit would not
  match the corrupted bytes) and verified with `node --check` plus a full
  scan confirming no control bytes below 0x20 remain in the file.
- Invariants verified:
  1. **A non-standard dependency still prompts.** The gate only fires the
     silent path when `nonstandard` is empty; every source Round 1–3 taught
     `nonstandard_dependencies_of` to name (git/URL/local-path/named-index,
     workspace members, `uv.toml`'s project-wide index, `find-links`) still
     produces a non-empty array, and the untouched `confirmInstall` branch
     below the new check still runs for all of them — confirmed by reading
     the code path (this task didn't touch `projectenv.py`) and by
     `test_projectenv.py` staying green (84 passed) with `runtime.js`'s gate
     reading whatever that module emits.
  2. **`confirmBuildRetry` is untouched.** The diff touches only
     `nonstandardFingerprint`'s separator, `confirmInstall`, and the gate in
     `startInstall`; `confirmBuildRetry` and the `needs_build` branch that
     calls it are unmodified. Added
     `test_the_no_build_retry_prompt_still_fires_even_though_the_initial_install_was_silent`:
     an `installEnv` call with no `nonstandard` posts silently
     (`allow_build: false`, zero prompts), then the worker reports
     `needs_build` and the "Install anyway" question still comes up — the
     retry path does not care how `runInstall` was reached.
  3. **The re-prompt semantics of `nonstandardFingerprint` survive.** Since
     the silent path never adds an `approvalKey` entry, a later manifest
     edit that adds a `nonstandard` entry finds nothing "already approved"
     and prompts for real. Renamed and rewrote the existing
     `test_a_changed_disclosure_re_asks_even_for_an_already_approved_project`
     to `test_a_manifest_gaining_a_nonstandard_dependency_prompts_where_the_silent_install_did_not`
     — an all-PyPI `installEnv` call (0 prompts) followed by the same
     project gaining a git dependency (1 prompt) — this is exactly what the
     brief calls invariant 3. Its old assertion (`prompts == 2`, from a
     version where the first, all-PyPI call still prompted once) no longer
     holds now that the first call is silent; the new assertion is
     `prompts == 1`. The original "already-approved NONSTANDARD project
     re-asks on a changed but still-nonstandard disclosure" case that test
     used to also stand in for is now its own test,
     `test_a_changed_nonstandard_disclosure_re_asks_even_for_an_already_approved_project`
     (git-reason → local-path-reason on the same project, `prompts == 2`),
     so that coverage isn't lost. Also added
     `test_an_all_pypi_install_never_prompts` as the direct, isolated
     invariant check.
  4. **The jobs-dock row still appears, with a working Cancel, while a
     silent install runs.** `runInstall` (the function the new short-circuit
     calls) is unchanged: it still calls `mountInstallSoon(ui)` and still
     registers the real `onCancel` handler before `tryInstall` starts. The
     row was never mounted synchronously for the trivial all-PyPI case
     either, before or after this change — `askRow`'s immediate mount only
     ever applied to the confirm dialog itself, which no longer exists for
     this case; the debounced jobs-dock mount an install actually runs under
     is untouched. Verified by reading `runInstall` (unedited by this diff)
     and by the existing `test_a_fast_install_never_mounts_the_overlay`,
     `test_two_distinct_installs_get_their_own_rows`, and the mid-install
     cancel tests (`test_cancelling_cancels_the_install_that_is_actually_running`
     et al.) staying green with no changes needed.
- Test updates in `tests/test_server_env_install.py`, and why each
  expectation moved:
  - `_CONCURRENT_RUNS` (the shared fixture for the five-concurrent-scripts,
    cancel-the-confirm, delayed-real-click, and detail-font tests) gained a
    `nonstandard` entry. Left as an all-PyPI manifest, every one of those
    tests would now build no confirm row at all, and several of them exist
    specifically to click that row's Install or Cancel button — they'd have
    nothing to click. Giving the fixture something to disclose keeps them
    exercising exactly the flow (invariant 1) they were written to cover;
    none of their own assertions changed.
  - `test_the_confirm_questions_detail_line_is_proportional_not_monospace`'s
    docstring dropped its "A one-time download." quote (the deleted copy)
    for a description of the real disclosure text now rendered under
    `_CONCURRENT_RUNS`'s nonstandard entry; the test's own assertions (font
    family only, never the text) were already independent of which copy
    renders.
  - `test_the_projects_manifest_is_watched_so_a_fix_triggers_a_reload`'s
    `.replace(...)` target string was the literal old `requirements:
    ["cowsay"] }` substring of `_CONCURRENT_RUNS`; updated to match the
    fixture's new text so the `pyproject` field still gets spliced in.
  - `test_an_unrelated_version_bump_does_not_re_ask` used two all-PyPI
    manifests (`nonstandard: []` both times) — under the new gate neither
    call ever reaches `approvedInstalls` or `confirmInstall`, so it was
    asserting `prompts == 1` for a mechanism (the fingerprint dedup) it no
    longer exercised at all. Rewritten to carry the same non-empty
    `nonstandard` entry on both manifests (a version bump alongside an
    unrelated dependency that stays nonstandard) so the dedup path is the
    one actually under test; the assertion (`prompts == 1`) is unchanged
    because that is still the correct answer for an unchanged disclosure.
  - Added `test_an_all_pypi_install_never_prompts` and
    `test_the_no_build_retry_prompt_still_fires_even_though_the_initial_install_was_silent`
    (invariants 1's negative case and invariant 2, respectively — see above).
- Ran narrowed: `tests/test_server_env_install.py` (80 passed),
  `tests/test_server_env_install.py tests/test_env_install.py` together (230
  passed). One `test_env_install.py` thread-count race
  (`test_a_retry_inside_the_poll_window_leaves_one_live_mirror_thread`,
  added in Round 3) failed once under `pytest-xdist` parallelism and passed
  twice in isolation immediately after, and was called an xdist flake here.
  **That diagnosis was wrong — see Round 5.** It fails deterministically
  with `-n 0` (no xdist at all) when the whole file runs, because
  `threading.enumerate()` is process-global: other tests in the file leave
  their own same-named mirror threads alive, and this test counted those
  too. Did not run the full suite — left to the orchestrator, per the same
  `/tmp`-quota note as prior rounds.

## Round 5: three more review findings (stale caption, a broken test misdiagnosed as flaky, two stale comments)

- **A successful retry's dock row kept the "waiting for your approval"
  caption.** `_mirror_into_jobs`'s `needs_build` branch sets `message` to
  the approval caption on the row it leaves `cancelled`. The "Install
  anyway" retry's mirror thread reuses the SAME job id, and its opening
  upsert set `title`/`kind`/`state`/`cancellable` but not `message` —
  `jobs.upsert`'s `"message" in body` guard leaves an absent key untouched,
  so the caption survived straight through to a `done` row. Fixed by adding
  `"message": ""` to the opening upsert. Checked whether any other field the
  opening upsert leaves out is stale for the same reason: no — `detail`,
  `done`, `total`, and `unit` are all rewritten unconditionally from the
  live `progress(key)` record on every tick of the loop below, regardless
  of which branch is taken, so a fresh attempt's first tick overwrites them
  within one poll interval. `message` is the only field this loop ever sets
  conditionally (only inside the `needs_build`/`error` sub-branches of
  `finished`) and never clears elsewhere, which is what let it go stale.
  Added `test_a_successful_retry_clears_the_needs_build_row_caption`;
  confirmed it fails without the fix (row still carries the old `message`
  at `done`) and passes with it.

- **`test_a_retry_inside_the_poll_window_leaves_one_live_mirror_thread` was
  broken, not flaky — the Round 4 diagnosis above was wrong.** Its
  `alive_mirrors()` filtered `threading.enumerate()` by thread name
  (`env-install-jobs-mirror`), which is process-global: other tests in the
  same file start their own mirror threads that are still alive (they exit
  only once their own job reaches a terminal state on their own schedule)
  when this test runs, so it was counting unrelated threads, not measuring
  what its own name claims. Confirmed this deterministically —
  `.venv/bin/python -m pytest tests/test_env_install.py -n 0 -q` (no xdist
  at all) failed every time the whole file ran, never in isolation. Fixed
  by snapshotting `threading.enumerate()`'s thread idents before the test
  starts its own install, and filtering `alive_mirrors()` on that
  difference instead of name alone. Regression check: inverted the
  `_claim_token` equality check in `_mirror_into_jobs` (`==` instead of
  `!=`) and reran the test alone — it failed immediately (`0 == 1`, the
  first thread retiring itself on its very first tick), confirming the
  fixed test still catches the supersession logic breaking. Reverted the
  inversion before committing.

- **Two comments misdescribed the security consequence of an undisclosed
  index**, left over from Round 4's deletion of the disclosure-free
  install-confirm prompt: `projectenv.py`'s shape-3 comment and a
  `test_projectenv.py` docstring both said an undisclosed index would leave
  "the prompt" saying "a one-time download" — but that prompt path no
  longer exists, so the actual consequence is silent install with no
  consent prompt at all, which is worse than a vague one. Rewrote both to
  describe the code as it stands (no history references, per repo
  convention). Left `engine.py:1132`/`:1138` alone — those are the
  `needs_install` notice payload for the *is this dependency installed at
  all* question, unrelated to shape 3's classifier, and still accurate.

- Ran scoped: `tests/test_env_install.py tests/test_projectenv.py
  tests/test_jobs_api.py tests/test_server_env_install.py` (362 passed) and
  `tests/test_env_install.py -n 0 -q` (150 passed, 1 skipped). Did not run
  the full suite.

## Round 6: a futile compile prompt on an unfixable platform, and a job row that lied about being cancelled

Two changes, both scoped by the task brief rather than found via review.

### Change 1 — platform-incompatible `--no-build` refusals must not offer a futile compile

**Problem.** `_no_build_package()` (`_env_install_worker.py`) parses uv's
`--no-build` refusal out of two distinct stderr wordings
(`_NO_BUILD_HINT` — resolution time, no version; `_NO_BUILD_DISTRIBUTION`
— install time, `uv.lock` already pins one), but uv's text does not say
*why* it refused. A macOS-only dependency (e.g.
`pyobjc-framework-applicationservices`, no `sys_platform` marker) on Linux
gets exactly the same refusal text as a package that genuinely has no
wheels anywhere and would need a real compile — so both reached
`confirmBuildRetry`'s "Install anyway" prompt, and the platform-incompatible
one could never succeed no matter how many times the user clicked it.

**Fix.** `_incompatible_platform_name(name, version)`: queries PyPI's JSON
API (`https://pypi.org/pypi/{name}/json`, or `.../{name}/{version}/json`
when uv named a specific pinned version — `_no_build_pinned_version()`
extracts that from the install-time wording only, since the resolution-time
`hint:` wording never names one), reads the `urls` list's `bdist_wheel`
entries, and derives each wheel's platform family from its filename's own
platform tag (`_wheel_platform_tags`/`_platform_tag_family` — PEP 427
`{name}-{version}(-{build})?-{python}-{abi}-{platform}.whl`, manylinux/
musllinux/linux normalized to one "linux" family, `macosx*` to "macosx",
`win*` to "win"). If every family found excludes the current platform (and
`any`/unrecognized tags never count as exclusive), returns a human platform
name (or a joined list — "macOS and Windows", "A, B, and C") built from
whichever families actually turned up — never a hardcoded package or
platform list, so nothing here special-cases pyobjc. If PyPI has no wheels
at all, or the entry is source-only, or the lookup cannot complete for any
reason, returns `None` and the caller's `needs_build` stays exactly as it
was: the compile prompt is still offered, since compiling might genuinely
work.

**Fail-open modeled on `ai/hub_metadata.py`'s `_fetch_raw`, not
`update/common.py`'s manifest fetch.** `update/common.py` is fail-CLOSED on
purpose — a corrupted or unreachable update manifest must not be treated as
"no update available" (a security property: silently trusting a fetch
failure there could hide an update meant to ship a fix). This lookup is the
opposite: it exists only to UPGRADE a message's wording, and getting it
wrong in the conservative direction (leave `needs_build` set, offer the
prompt exactly as today) has no security consequence at all — worst case is
an unfixable prompt the user could always ignore, same as today. So
`_fetch_pypi_json` catches `urllib.error.URLError`, `OSError`,
`TimeoutError`, and `ValueError` (malformed JSON) and returns `None` on
every one, exactly like `_fetch_raw`'s blanket `except Exception`. Timeout
is a plain 3-second `urllib.request.urlopen(..., timeout=_PYPI_TIMEOUT_S)`
— short enough not to stall a user-visible install, generous enough for a
JSON API call under normal network conditions. A response body capped at 2
MiB guards against a pathological reply hanging JSON parsing.

No new third-party dependency: parsing is hand-rolled stdlib
(`urllib.request`/`urllib.error`/`json`), consistent with
`_env_install_worker.py`'s existing zero-third-party-import convention
(D152: the detached worker must never import `fused_render`, and by
established practice imports nothing else either) — `packaging` was
available but a manual wheel-tag split was chosen instead to keep that
convention rather than break it for one call site.

**`install()`'s except block**: computes `platform_incompatible` only when
`needs_build` was already set (never invents either from nothing), and
clears `needs_build` when the platform check succeeds — the two fields are
mutually exclusive by construction, so every downstream consumer can branch
on `needs_build` alone with no additional guard. `progress.json` gains
`platform_incompatible` as a purely additive field; `error` keeps carrying
uv's raw stderr verbatim throughout (SPEC PY-18) — the friendly sentence
lives only in the new field, never overwrites `error`.

**Signal path**: worker's `install()` except block → `write()`/`_write()`
→ `progress.json`'s `platform_incompatible` key → `GET /api/env/progress`
(returns `prog` verbatim, no server plumbing needed) → client's `poll()`
in `runtime.js` (`e.platformIncompatible = prog.platform_incompatible ||
null`, alongside the existing `e.needsBuild`) → `tryInstall`'s rejection
handler, which checks `err.platformIncompatible` *before* the
`!allowBuild && err.needsBuild` check and, when set, composes the
plain-language sentence, assigns it to `err.message`, and falls straight
into the ordinary `err.type = "EnvInstallError"` terminal-error path —
`confirmBuildRetry` is never reached for a platform-incompatible refusal.

**Tests** (`tests/test_env_install.py`, all offline — `_worker_module`'s
helper stubs `_fetch_pypi_json` to return `None` by default so the whole
suite stays network-free without every test remembering to patch it):
covers a wheel that covers the current platform (no incompatibility), a
real pyobjc filename set (names "macOS"), two-and-three-platform joins, a
source-only project, a pure-Python wheel, five distinct lookup-failure
shapes (`None`, missing `urls`, empty `urls`, non-dict payload, non-list
`urls`), `_fetch_pypi_json` itself failing open on `TimeoutError`/`OSError`/
`URLError`/malformed JSON, `needs_build` staying set when the platform
check cannot complete, and a full end-to-end `install()` run asserting the
exact `platform_incompatible` dict, that `error` still carries uv's literal
text, and that the exact refused version was threaded through to the PyPI
lookup.

### Change 2 — a job row must not say "Cancelled" while it is waiting on the user

**Problem.** `jobs.py`'s `TERMINAL_STATES` has exactly three members
(`done`/`error`/`cancelled`) and none of them means "stopped, waiting on
you" — so `envinstall._mirror_into_jobs`'s `needs_build` branch borrowed
`cancelled` (documented in the "Dogfooding fix" section above as "the least
wrong of the three"), producing a status-bar Activity row reading
"Preparing OpenWhisper / Cancelled" while the compile-consent prompt was
still on screen, open and unanswered.

**Fix.** `jobs.py` gains `WAITING = "waiting"`, a second non-terminal state
alongside `RUNNING`. Not `RUNNING`, because nothing is actually in flight —
a bar or spinner would lie. Not `TERMINAL`, because `upsert`'s
finished-transition bookkeeping (`finished_at`, clearing
`cancel_requested`) does not apply to a row that has not actually finished,
and `_sweep`'s age-out clock must not start either. `_sweep`'s retention
loop now `continue`s on `job.state in ("error", WAITING)` — kept until
dismissed, same reasoning `error` already had: a `WAITING` row's worker has
already exited (nothing is polling any more to keep it "fresh" the way a
`RUNNING` row's own ticks do), so aging it out on any clock would make an
still-open question vanish from the dock. `request_cancel`'s guard stays
`if job.state == RUNNING` unchanged — a `WAITING` row's worker has already
exited, so there is nothing left to signal a cancel TO; its docstring now
explains that the dock's ✕ on a `WAITING` row goes through `dismiss`
instead, exactly like it already does for `done`/`cancelled`. `dismiss`,
`is_stalled`, `_public`, and the `_sweep` cap/eviction ordering needed no
code changes: each already keys off `RUNNING` (or `error`) specifically,
so `WAITING` falls through them correctly by construction — verified by
the new tests below, not just by inspection.

**`envinstall._mirror_into_jobs`** (job-id `sys:env-install:<key>`): the
`needs_build` branch now sets `fields["state"] = jobs.WAITING` (was
`"cancelled"`) with the same "waiting for your approval to compile X"
caption; its long comment, which used to justify the `cancelled` borrow, is
rewritten to explain `WAITING` instead. A new `elif platform_incompatible:`
branch (checked ahead of the plain `elif error:` fallback, same as
`needs_build` is) sets a genuine terminal `"error"` state with a
plain-language message built from the project's display name and the
worker's `platform_incompatible` dict (`package`/`platform`/
`current_platform`) — `error` (used only when the terminal record's own
`error` field is read directly via `envinstall.progress()`) keeps carrying
uv's raw stderr, per SPEC PY-18. The outer loop variable that used to be
`name` was renamed to `app_name` — it was already being shadowed every tick
by an inner `for name in ("done", "total")`, and the new
`platform_incompatible` branch needed the project's display name in scope
to compose its message.

**Client side.** `frontend/src/platform/lib/jobs.ts`: `JobState` gains
`"waiting"`; `jobStatusLine` renders `job.message` for it (falling back to
"Waiting for you") ahead of the `stalled`/`cancel_requested` checks, since
a `WAITING` row's reporter exited on purpose rather than having gone
quiet. `frontend/src/platform/ui/DownloadManager.tsx` needed **no code
changes**: `inFlightJobs` excludes only `isFailure` (`state === "error"`),
so a `WAITING` row counts as in-flight and fills the Activity dot's
`runningCount` — deliberately: a row sitting in front of an open question
is legitimately "work happening right now" that needs the user's
attention, the same way a running download is. `Bar`'s `indeterminate`
check is gated on `isRunning` (`state === "running"` only), so a `WAITING`
row draws no progress bar at all — correct, since nothing is actually
progressing. `canCancel` is gated on the same `isRunning`, so the Cancel
button never appears for a `WAITING` row; `canDismiss` is `!running ||
stalled`, so its ✕ already routes to `dismiss`.

**`runtime.js`**: `poll()`'s error branch now also carries
`e.platformIncompatible = prog.platform_incompatible || null` alongside
`e.needsBuild`. `tryInstall`'s rejection handler checks
`err.platformIncompatible` before the existing `!allowBuild &&
err.needsBuild` check; when set, it composes the same plain-language
sentence as the mirror's own message, assigns it to `err.message`, and
falls through to the ordinary `err.type = "EnvInstallError"` path —
`confirmBuildRetry` is skipped entirely, since no retry could ever satisfy
it. Left `installRow`/`mountInstallSoon`/`paintPreparing` (runtime.js's
separate overlay row) untouched, as scoped — that duplication is a
follow-up PR.

**Repo-wide search for other job-state call sites**, beyond the ones
listed in the task brief: none found. `fused_render/server/routers/jobs.py`
has no hardcoded state enum to extend — `upsert` accepts whatever `state`
string a reporter sends, unvalidated against a closed set, so `WAITING`
needed no route change. `fused_render/server/routers/env.py`'s
`/api/env/progress` returns `prog` verbatim (confirmed again this round).
The job-state-literal hits inside `fused_render/static/runtime.js` outside
the two edited spots (`JOB_STATE_RUNNING`, `record.state === "cancelled"`
around lines 3280–5000) all belong to a separate AI-job polling system
(`ai_error`/`cancelled` job records for scheduled runs), unrelated to
env-install's job-dock mirror — confirmed by reading each site; none
reference `needs_build` or the env-install job-id prefix.

**Tests.** `tests/test_env_install.py`: renamed
`test_a_needs_build_refusal_mirrors_as_cancelled_not_error` to
`..._as_waiting_not_error` and updated its assertions to `jobs.WAITING`;
updated the two other `needs_build`-mirror tests
(`..._flips_back_to_running_on_the_retry`,
`..._clears_the_needs_build_row_caption`) to wait for `jobs.WAITING`
instead of `"cancelled"`; added
`test_a_platform_incompatible_refusal_mirrors_as_a_plain_language_error`
covering the new terminal-`error` branch end-to-end, asserting the exact
composed message, that uv's raw stderr is absent from it, and that
`envinstall.progress(key)["error"]` still carries that raw text verbatim.
`tests/test_jobs_api.py`: added four tests — a `WAITING` row surviving both
the unread backstop and the ordinary `FINISHED_TTL_S` exactly like `error`
does, `request_cancel` leaving a `WAITING` row's `cancel_requested` false
(nothing to signal), and a `WAITING` row being dismissible through the
ordinary `/dismiss` route. `frontend/src/platform/lib/jobs.test.ts`: added
two `jobStatusLine` tests for the `"waiting"` state (with and without a
`message`).

**Verification.**
`.venv/bin/python -m pytest tests/test_env_install.py
tests/test_server_env_install.py tests/test_jobs_api.py
tests/test_projectenv.py -q` → 376 passed.
`.venv/bin/python -m pytest tests/test_env_install.py -n 0 -q` → 160
passed, 1 skipped. `bunx tsc --noEmit` (frontend) → clean. `bun test
src/platform/lib/jobs.test.ts src/platform/ui/DownloadManager.test.tsx` →
75 passed. Did not run the full suite (pre-existing unrelated failures on
this machine, per the task brief, are not in scope).

**Commits**: `5a2895f5` (worker-side platform-incompatible detection),
`ff2bccb8` (`WAITING` state, `_mirror_into_jobs` rewrite, client wiring,
tests for both).

## Round 7: CI's precomputed-key test fragility (D214's bootstrap key vs the venv key)

CI failed on all Python jobs (3.11, 3.12, 3.13, Windows), one test, passing
on this local machine:

```
FAILED tests/test_env_install.py::test_a_stale_cancel_requested_does_not_kill_a_fresh_attempt
AssertionError: assert '400beb22b400bedd' == '030d988afe3257c2'
```

**The mechanism, confirmed rather than assumed.** `'400beb22b400bedd'` is
exactly `PYTHON_BOOTSTRAP_KEY` — `hashlib.sha256(b"fused-render:script-
python-bootstrap").hexdigest()[:16]`, a FIXED constant, identical on every
machine, unrelated to the project. `venv_key_for` (`projectenv.py`) does
NOT fold the interpreter in at all — it hashes only the venv identity (an
absolute or package-relative path), so the earlier hypothesis that
`venv_key_for` itself computes differently under different interpreter
states is wrong. The real mechanism is `start()`'s own branch
(`envinstall.py`):

```python
acquire_python = None if script_python_ready() else SCRIPT_PYTHON_VERSION
key = PYTHON_BOOTSTRAP_KEY if acquire_python else venv_key_for(project_dir)
```

On this local machine `script_python_ready()` is True (a pinned 3.12
already exists from `setting-up-dev-env`), so `start()`'s key IS
`venv_key_for(proj)` and a test that precomputes that value before calling
`start()` happens to agree. On a fresh CI runner with no script Python
pinned yet, `script_python_ready()` is False, so `start()` claims
`PYTHON_BOOTSTRAP_KEY` for its FIRST round instead — a different, equally
legitimate key, per D214's design (see the "identical jobs-dock titles"
entry above). The failing test precomputed `venv_key_for(proj)` and
asserted `start()`'s return used it, which is only true on a machine that
already has a pinned interpreter. Nothing about the venv key itself is
unstable; the bug is comparing the WRONG round's key.

**Reproduced locally**: temporarily added
`monkeypatch.setattr(envinstall, "script_python_ready", lambda: False)` to
the top of the failing test (nothing else changed) and reproduced the exact
CI assertion shape — `'400beb22b400bedd' == '<some venv key>'` — then
reverted that line before writing the real fix.

**The fix**: this test is about a stale `cancel_requested` surviving into a
fresh attempt on the same job id, not about D214 at all — so the correct
fix pins `script_python_ready` to `True` before precomputing the key
(the row has to be seeded under a known id *before* `start()` runs, so
"read `start()`'s own return" isn't available here). Regression-checked by
temporarily replacing the `jobs.clear_cancel_requested(job_id)` call inside
`_mirror_into_jobs` with `pass` — the test went red with a plausible
failure (`prog["error"] == "the install was cancelled"`) — then reverted.

**Audit of the same fragility across `tests/test_env_install.py`.** Searched
every `venv_key_for(` call for one made before a `start()` call it's then
compared against. Two were already immune (one only compares
`PYTHON_BOOTSTRAP_KEY != venv_key_for(proj)`, which holds regardless of
environment since the two hash sources never coincide; one forces
`script_python_ready` True immediately before its own `venv_key_for` call).
`test_a_retry_inside_the_poll_window_leaves_one_live_mirror_thread` — named
in the task brief as a "prime suspect" — turned out already safe: it reads
`key = rec["key"]` off `start()`'s own return rather than precomputing.
Six more had the identical latent bug and are fixed here, each verified
red-then-green under a global `script_python_ready` forced to `False`
(the CI condition), and were shown to fail without the fix under that same
condition:

- `test_the_worker_builds_the_venv_and_reports_done` — precomputed key,
  polled with it. Fixed: `key = envinstall.start(proj)["key"]`.
- `test_a_resolver_failure_reaches_the_user_verbatim` — same shape, same fix.
- `test_two_scripts_in_one_project_still_spawn_exactly_one_worker` — asserted
  `spawned == [venv_key_for(proj)]` computed AFTER two concurrent `start()`
  calls (timing wasn't the issue; the venv key just isn't necessarily what
  either call used). Fixed: both threads now record their `start()` return
  into a shared list, and the assertion compares `spawned` against
  `results[0]["key"]`.
- `test_a_stale_claim_from_a_dead_installer_is_taken_over` — precomputed key
  used for `progress_dir`/`progress` after the first `start()`. Fixed: reads
  `key = envinstall.start(proj)["key"]`.
- `test_joining_an_install_mid_spawn_yields_a_pollable_record` — needs the
  key BEFORE `start()` (to pre-claim it and force the join branch), so
  `start()`'s own return isn't available yet. Fixed by pinning
  `script_python_ready` to `True`, same as the target test.
- `test_the_spawn_record_never_overwrites_a_record_the_worker_already_wrote`
  — precomputed key polled after `start()`; the mocked `_spawn` itself
  already used whichever key `start()` passed it, so only the final
  `progress()` call was wrong. Fixed: polls `progress(record["key"])`.
- `test_a_retry_does_not_inherit_the_previous_attempt_s_record` — same shape
  as the dead-claim test above; same fix.

One more outside this exact pattern, same underlying mechanism:
`test_a_declared_project_with_no_venv_asks_for_an_install` (does not call
`start()` at all — asserts on `engine._needs_install_dict`'s `key`, which
carries the identical `script_python_ready()` branch). Fixed by pinning
`script_python_ready` to `True`, since the test is about the ordinary
packages case, not the bootstrap round.

Two categories of `venv_key_for(proj)` call were confirmed NOT to need a
fix: calls that pass `key` straight into `envinstall._spawn(key, proj)`
directly (never go through `start()`'s branch at all — `_spawn` takes
whatever key it's handed, correctly, as an argument-shape test), and calls
inside tests that already read the actual key off `jobs`/`start()` returns.

**Verification.**
`.venv/bin/python -m pytest tests/test_env_install.py
tests/test_server_env_install.py tests/test_jobs_api.py -q` → 292 passed.
`.venv/bin/python -m pytest tests/test_env_install.py -n 0 -q` → 160
passed, 1 skipped (pre-existing, unrelated to this branch). Additionally
ran all seven fixed tests under a session-wide `script_python_ready`
forced to `False` (a `conftest`-style autouse fixture, simulating the CI
condition directly rather than hoping): all seven passed with the fix in
place and all seven failed (with the exact `PYTHON_BOOTSTRAP_KEY`-vs-venv-
key mismatch shape) with the fix reverted.

**Commit**: test-only fix, `tests/test_env_install.py`.

## Round 8: merged `worktree-in-tree-venv` (PR #948) — project venvs move to `<project>/.venv`

Merged `origin/worktree-in-tree-venv` (14 commits) into `install-consent`.
`git merge-tree --write-tree` reported a clean tree — zero textual
conflicts. HEAD is now `28b08d42`.

Five files overlapped between the branches: `fused_render/envinstall.py`,
`fused_render/_env_install_worker.py`, `fused_render/projectenv.py`,
`tests/test_env_install.py`, `tests/test_projectenv.py`. The overlap is
safe because #948 only moves where a venv is STORED — `venv_dir_for` now
answers `<project_dir>/.venv` for the common writable case, falling back
to `<home_dir()>/venvs/<key>` — while leaving `venv_key_for` as the venv's
NAME, unchanged. That name is exactly the seam this branch is built on: it
keys the install-progress directory, `/api/env/progress?key=`, the
`sys:env-install:<key>` dock rows, and D214's `PYTHON_BOOTSTRAP_KEY`
round, so none of that needed to change.

The worker argv contract was the one place both branches actually touched
the same interface. Confirmed coherent post-merge: 8 slots — `<key>
<progress_dir> <project_dir> <venv_dir> <uv_cache_dir> <python_executable>
<acquire_python> <allow_build>` — with `envinstall.py`'s producer and
`_env_install_worker.py`'s `main()` consumer agreeing, #948's recomputed
`venv_dir_for` at slot 3 and this branch's `allow_build` at slot 7
coexisting without collision.

**Verification.** `tests/test_env_install.py tests/test_projectenv.py
tests/test_server_env_install.py tests/test_jobs_api.py -n 0
-p no:randomly` → 391 passed, 1 skipped.

PR #935's base was retargeted from `main` to `worktree-in-tree-venv`
ahead of this merge, so its diff shows only its own ~4772-line delta
across 16 files; GitHub auto-retargets it to `main` once #948 merges.

## Round 9: a preview card must never install, and a permanent failure must never loop

Dogfooding surfaced two independent bugs. A home-page preview card boots the
real app's `entry_html` live in a sandboxed iframe (`AppPreviewCard.tsx`'s
3-step fallback, hover-live included) purely to paint it, and that iframe's
own JS calls `fused.runPython()` at boot the same as a real open — so a
folder with dependencies triggered a real install just from being displayed
or hovered. For OpenWhisper specifically (`pyobjc-framework-applicationservices`,
macOS-only wheels on Linux) the install can never succeed, and the toast
("Preparing OpenWhisper…") reappeared every few seconds as `preview-start.ts`
cycled its 2 live iframes across many cards, tearing the page down and
remounting it — restarting the install from zero each time.

**Bug A — the preview gate.** `runtime.js` already computes `IS_THUMBNAIL`
(climbing same-origin ancestor frames for the `_preview` query flag) and
already uses it to refuse every `fused.daemon.*` call with a message calling
out exactly this principle — a picture of a page must not act like the page.
The install path never consulted it. Fixed by gating `handle()`'s
`shouldInstall` branch on `!IS_THUMBNAIL`: a preview's `needs_install` now
falls straight through to the existing `!data.ok` branch and rejects with the
server's own verbatim message — the SAME rejection shape every other run
failure already produces, so no caller anywhere needs a new catch path, and
nothing throws unhandled. `installEnv`, `/api/env/install`, the install row,
and `/api/env/progress` polling are never reached. A real open (no `_preview`
flag inherited) is untouched, since `IS_THUMBNAIL` is false there exactly as
before.

Tested in `tests/test_server_env_install.py` by lifting `handle()` itself
(the existing `_loader_and_runpython_js()` slice) into node and adding
`IS_THUMBNAIL` to the slice's own stub prelude (a new dependency the slice
closes over but does not declare — the file's own documented pattern for
this). `test_a_previewed_page_never_triggers_an_install` sets it `true` and
asserts zero `/api/env/install` POSTs and zero install-consent prompts.

**Bug B — the permanent-failure poison key.** Not fixed by Bug A: ANYTHING
that remounts the previewed page (or, for a real open, retries `/api/run`)
restarts the cycle, because each remount is a fresh JS context with no
memory of the last attempt — a client-side "already tried this" `Set` dies
with the page by construction, so the fix has to live server-side, in
`envinstall.start()`'s own progress record.

The worker already computes exactly the right classification:
`platform_incompatible` (`_env_install_worker.install`'s except block) is set
only when a `--no-build` refusal names a package published solely for a
platform this machine is not — provably permanent, not a guess. Everything
else that can terminate an install (an ordinary resolver failure, an
ordinary `needs_build` refusal, a cancelled run, a network timeout, a
crash-diagnosed record) is left alone: none of those are provably permanent,
and poisoning one would strand an offline user, or one whose first attempt
was simply slow, on a hard "no" forever. This is the conservative reading of
the brief's "only poison what you can justify" — one field, one branch,
rather than "any terminal error, twice in a row."

The worker now tags a `platform_incompatible` record with the
`pyproject.toml` digest (`_state_digest`, byte-identical to
`projectenv._compute_state_digest`) it was reached against —
`manifest_digest`, `None` on every other stage and every other error shape.
`envinstall.start()`'s new `_permanent_failure(key, project_dir)` reads that
back: a key whose last recorded attempt is `done`, carries
`platform_incompatible`, and whose `manifest_digest` still equals
`projectenv.state_digest(project_dir)` right now is reported AS-IS —
`_reported(key, poisoned)` — instead of spawning attempt N+1. The check sits
in `start()` before `_claim`/the progress-file unlink that a real spawn does,
so the poisoned record survives to be read on every repeat call instead of
being clobbered the moment a new attempt begins. Editing `pyproject.toml`
changes the digest and the very next `start()` call runs for real — this is
the invalidation the brief calls out by name, and it costs nothing extra: it
falls out of comparing against `state_digest` at call time rather than
caching the verdict.

`allow_build=True` (the explicit "install anyway" retry off `/api/env/install`)
skips the gate outright, checked before `_permanent_failure` is even called —
a user who chose to compile from source must always reach a real worker,
never be answered out of a record from a run that never attempted that in
the first place.

Tested end to end at the `envinstall.start()` layer (`tests/test_env_install.py`):
a `platform_incompatible` record written with the CURRENT `state_digest`
must not call `_spawn` (`test_a_permanently_incompatible_install_is_not_retried_on_the_same_manifest`);
a record whose digest disagrees with the current manifest must let the next
call spawn (`test_editing_the_manifest_lets_the_next_attempt_run`); and
`allow_build=True` must spawn regardless of a matching-digest poison record
(`test_allow_build_bypasses_the_poison_record`). Also tested at the worker
layer: `test_platform_incompatible_record_carries_the_manifest_digest` (the
digest is written, and is non-empty) and an added assertion on the
neighbouring `needs_build`-only test that an ordinary `--no-build` refusal
carries no digest at all — the control for "only the provably-permanent
branch is tagged."

**A third item — `fused.engine` warm workers on preview hover (Bug C) — was
raised, then explicitly cancelled by the user before any code was written for
it.** `fused.engine` was confirmed NOT an install trigger (`POST
/api/engine` refuses to build a missing venv and 409s), so Bug A's gate does
not need to cover it; nothing here touches `runtime.js`'s `engine`/`engineCall`/
`engineForget`.

**A dead-code check, requested alongside the cancellation.** `noFocusRequested`
(`runtime.js:528`) is declared but never called anywhere in the file — a
genuine editor warning. Confirmed pre-existing: `git show HEAD~1` (the commit
immediately before this round's Bug A commit) already has it unused, and
Bug A's own edit is nowhere near this region (`handle()`, ~2540 lines further
down). Left alone rather than deleted, since the file's stranded-code
convention applies to code a CHANGE here made dead, not to an unrelated
pre-existing warning outside this round's scope.

**Verification.** `.venv/bin/python -m pytest tests/test_env_install.py
tests/test_server_env_install.py tests/test_projectenv.py
tests/test_jobs_api.py -n 0 -p no:randomly -q` → 396 passed, 1 skipped (the
documented pre-existing `test_a_project_whose_venv_exists_just_runs` flake,
unrelated to this round). `tests/test_server_env_install.py` alone → 81
passed (80 + the new Bug A test).

**Commits.** Two, matching the brief's "at minimum two separate commits":
Bug A touches only `fused_render/static/runtime.js` and
`tests/test_server_env_install.py`; Bug B touches
`fused_render/envinstall.py`, `fused_render/_env_install_worker.py`, and
`tests/test_env_install.py`.
