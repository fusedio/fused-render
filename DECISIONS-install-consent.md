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
