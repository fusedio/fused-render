---
name: writing-tests-that-dont-flake
description: Use when writing, changing or reviewing any test under tests/ — especially one that waits on a thread, subprocess, file, server or timer, measures elapsed time, signals a pid, or calls a subprocess's entry point (main/serve) in-process — and when a test passes locally but fails intermittently in CI or an xdist worker crashes ("node down: Not properly terminated"). Lists the race patterns that have actually flaked main, the fix for each, and how to prove a fix under load.
---

# Writing Tests That Don't Flake

## Overview

CI runs the suite with 4 xdist workers plus coverage on a 4-vCPU runner, so
every test runs on a heavily loaded machine. Any step whose correctness
depends on *how fast* something happens will eventually lose. In Nov 2026
`main` was red in ~10 of 12 runs, each time on a *different* test. Every one
of those failures was a test asserting on something that was not yet true, or
a test that changed the pytest process itself. **None was bad luck**: each
had a specific race, and each fix below removes the race rather than widening
a margin.

**The rule underneath all of them:** wait for the *exact* state you are about
to assert on, signalled by the code that produces it. Never use a proxy for it
(an earlier event, a wall-clock bound, a sleep).

## Rules

### 1. Wait on the thing you assert, not on an earlier thing
When a product writes several things in sequence (reply, then file; init row,
then transcript row; an RPC answer, then a `.res.json`), seeing the first one
says nothing about the rest.
- Poll for the exact file/row/key you are about to read, through a helper
  that treats *missing* and *half-written* (empty, invalid JSON) as "not yet".
- *Flaked:* `test_gemini_host` read `perm/<id>.res.json` right after seeing
  the answer it is written after, and read `tx()[0]` right after the init row.

### 2. A test hook must signal the state the code under test checks
If production checks "thread finished and result stored", the test must wait
for *that* (join the thread). An event set inside the callee's `finally`
fires *before* the return, and a preemption in that gap flips the outcome.
- *Flaked:* `test_ai_mlx_whisper_worker` waited on a `done` event set in the
  fake's `finally`; the product's `_finished()` checks `not thread.is_alive()`.

### 3. Prove concurrency with a rendezvous, not a stopwatch
"Faster than serial" cannot tell parallel from slow machine. Make each
participant wait on a `threading.Barrier(N)`: only a concurrent run can pass,
however slow the box; a serial run breaks the barrier and fails.
- *Flaked:* `test_conditions_run_concurrently` (`elapsed < 0.9`, got 1.63 s).

### 4. Assert how a wait is bounded, not how long a request took
"Never waits long on the lock" is a claim about the code: spy the lock and
assert the `timeout` it was acquired with. A stopwatch around a TestClient
request also measures thread hops, middleware and CPU contention.
- *Flaked:* `test_a_window_shot_never_waits_long_on_the_native_lock` (< 1.0 s, got 1.13 s).

### 5. Don't let a production give-up timeout sit between the test and its observation
Fail-open/fail-silent deadlines (a hook that prints nothing after 2 s) turn
"slow" into "different answer" under load. Keep the same code path, but lift
that deadline in the test (or assert the fallback on purpose).
- *Flaked:* `test_the_refusal_reaches_claude_as_a_pretooluse_deny`: `canvas_hook`'s
  2 s `_TIMEOUT_S` against a cold in-process app.

### 6. Never use a fixed sleep as a handshake with a fake process
`mirror_delay: 1.5` ("the other side will have finished by then") is a race.
Hold the fake on an explicit gate (a file, pipe or event) that the test opens
once the precondition is actually true.
- *Flaked:* `test_a_mirror_run_in_another_process_loses_to_the_marker`.

### 7. Publish a job's terminal status last
If a test polls for `failed`/`done` and then checks side effects, the product
must finish those side effects (cleanup, files) *before* the status becomes
visible. A test that flakes here usually means **the product publishes too
early**, and real pollers (UI, API) see the same half-finished state. Fix the
order in the product; don't add a wait to the test.
- *Flaked:* `test_markers_left_every_round_fails_and_leaves_app_alone`
  (`merge_job._finish("failed")` before removing the worktree).

### 8. Filter shared loopback traffic by a per-test id
Detached processes from *other* tests can post to a port the OS has handed
out again. Give each test a unique run id and only count messages carrying it.
- *Flaked:* `test_the_permission_server_posts_its_card_to_the_relaunched_server`.

### 9. Never change the pytest process itself
The test runs inside an xdist worker; whatever process state it changes,
every later test on that worker inherits, and a worker that dies takes
whatever test it is running with it (`[gwN] node down: Not properly
terminated`, no traceback, a different "crashed" test every time).
- **Calling a subprocess's real entry point in-process** (`main()`, `serve()`)
  runs its `os.setsid()`, `os.nice()`, `os.closerange(3, …)`, `dup2` over
  stdio, signal handlers, `os._exit`. Under `-n 0` `setsid()` fails with EPERM
  and is swallowed, so it looks harmless locally; in an xdist worker it
  succeeds and the worker becomes its own session/group leader — after which
  `killpg(<worker pid>)`, a harmless ESRCH before, kills it. Monkeypatch those
  calls, or run the entry point in a real subprocess.
- **Never write `os.getpid()` as the pid of a fake process** that code may
  later *signal* (run `pid` files, `.meta`, registries). Use a child you
  spawned (and reap it), or monkeypatch the kill. `os.getpid()` is fine only
  for code that merely *probes* liveness (`kill(pid, 0)`).
- `tests/conftest.py` enforces this: `os.setsid()` is a no-op in the test
  process, a signal aimed at the test process or its group raises and fails
  the test by name, and `os._exit` / a fatal SIGTERM or SIGHUP print a stack.
  If that guard fires, fix the test; don't loosen the guard.

### Never "fix" a flake by
- `skip`/`xfail`/quarantine, or rerun-until-green.
- Raising a timeout or threshold without naming the race it hid. A liveness
  bound for the *failing* case (e.g. a barrier's `timeout=10`) is fine;
  a margin on the *passing* case is not.
- Adding a `time.sleep` before the assert.

## Proving a fix

A flake fix is not done until you have shown the race and its absence:

1. **Reproduce first.** Run the single test 20-30× under CPU load:
   ```bash
   for i in 1 2 3 4; do python -c 'while 1: pass' & done   # burners
   for i in $(seq 30); do .venv/bin/python -m pytest -n 0 -o addopts="" "tests/test_x.py::test_y" -q -p no:cacheprovider || echo FAIL; done
   kill %1 %2 %3 %4
   ```
   If the window is too narrow to hit, widen it on purpose: a temporary
   `time.sleep()` at the suspected gap in the product, reverted afterwards.
2. **Show the fix holds** under the same load (0 of 30).
3. **Show the test still guards its behaviour:** temporarily break the
   behaviour it is about (go serial, block on the lock, skip the cleanup)
   and confirm it fails, then revert.
4. For a worker crash, run the files concerned under xdist (`-n 1` or more,
   never `-n 0`, which hides it) in collection order, and check the worker's
   state afterwards (`os.getsid(0) != os.getpid()`).
