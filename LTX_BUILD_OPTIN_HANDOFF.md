# LTX video runner: per-runner build opt-in — handoff notes

Scope: this file is **not** the repo's shared `DECISIONS.md` (that file already
exists at the worktree root as the project-wide, D-numbered decision log
carried across every branch — appending an ad-hoc entry to it risks a D-number
collision with concurrent branches, per this repo's own known failure mode).
This file instead holds the task-specific notes the original brief asked for:
the cache-key decision, dead ends ruled out, and a resume point for a
follow-up builder. If a maintainer wants this folded into `DECISIONS.md` under
a real `D###`, that should happen at merge time, with the current tip of that
log in hand — not from inside an isolated worktree.

## The bug and the fix

Every packaged-app user's video generation was structurally broken:
`ltx_video/pyproject.toml` names `ltx-core-mlx` and `ltx-pipelines-mlx` as
git-sourced `[tool.uv.sources]` (no PyPI release exists upstream). Every
bundled runner installs through `ai/supervisor.py._ensure_venv` ->
`envinstall.start(runner.folder, report_job=False)`, which leaves `allow_build`
at its default `False`, so `_env_install_worker.py` appends `--no-build` to
`uv sync`. A git source can only ever be satisfied by building the checkout —
there is no wheel to fetch instead — so this runner's automatic install was
unconditionally uninstallable, with no consent-and-retry prompt in that code
path (that flow exists only behind the explicit `/api/env/install` retry a
user clicks after a genuine no-wheel failure).

Fix shape implemented, exactly as scoped:

1. `fused_render/projectenv.py`: new `runner_allows_build(project_dir) -> bool`
   reads `[tool.fused-render.runner] allow_build = true` off the runner's own
   manifest — same `[tool.fused-render.<table>]` convention
   `background_apps.py`'s `[tool.fused-render.app]` already established.
   Fails closed (absent manifest, non-dict table, missing/falsy/non-bool value
   all read False).
2. `fused_render/ai/supervisor.py._ensure_venv`: the one `envinstall.start`
   call site for every bundled runner now passes
   `allow_build=projectenv.runner_allows_build(runner.folder)` instead of
   leaving the parameter at its default. Every runner without the table keeps
   getting `allow_build=False`, byte-for-byte the old behaviour.
3. `fused_render/ai/runners/ltx_video/pyproject.toml`: declares
   `[tool.fused-render.runner]\nallow_build = true`, with the existing
   (already unusually thorough) header comment extended to explain why this
   folder specifically earns the exception.
4. Regression test in `tests/test_ai_runner_deps.py`: a new parametrized case
   over every runner folder asserting that any `[tool.uv.sources]` entry with
   a `git` or `url` key requires the declared opt-in table — the test that
   would have caught this the day the runner landed — plus two synthetic
   unit tests (positive/negative) against a `tmp_path` manifest, independent
   of the real `projectenv.runner_allows_build` reader (parses TOML itself on
   purpose, so a bug in one path can't hide a bug in the other).
5. Supervisor-side coverage in `tests/test_ai_runtime.py`: a new parametrized
   test (`test_ensure_venv_passes_through_the_runners_own_allow_build_declaration`)
   asserting `_ensure_venv` passes `allow_build=True` through to
   `envinstall.start` for a runner that declares the table, and `False` for
   one that doesn't (or declares it `false`) — plus 6 pre-existing
   `envinstall.start` fakes in that file updated to accept the new
   `allow_build=False` keyword so they don't `TypeError` once the supervisor
   started passing it.
6. `tests/test_projectenv.py`: two direct unit tests of the new
   `runner_allows_build` predicate itself.

## The cache-key question (task brief step 3) — decision: no change needed

The brief asked me to read `envinstall.py`'s own comment (originally cited as
"~line 2052") calling out that `allow_build` is deliberately EXCLUDED from
something, and decide whether a declarative per-runner opt-in changes that
reasoning.

Found the comment at `fused_render/envinstall.py` lines 2052-2054, inside
`start()`:

```python
# `allow_build` is excluded on purpose — an explicit "install anyway"
# click must always reach a real worker, never be answered out of a
# record from a run that never even tried building from source.
if not allow_build:
    poisoned = _permanent_failure(key, project_dir)
    ...
```

Two separate mechanisms were at risk of being conflated here, and I want to be
explicit that I checked both:

- **The venv cache key itself** (`envinstall.venv_key_for` /
  `projectenv.venv_key_for`, `fused_render/projectenv.py:264`) is a pure
  `sha256` of the project folder's stable identity (absolute path, or the
  bundled-relative path for anything under the package) — see
  `venv_key_for`'s own docstring at `projectenv.py:264-...`. It is **never**
  influenced by `allow_build`, by manifest content, or by anything else about
  *how* the venv gets built, only by *which folder* it is for. So there is no
  "cache key" in the sense of "two different builds of the same folder get
  different keys depending on `allow_build`" to worry about — one folder,
  one key, always.

- **The thing the comment actually guards** is `_permanent_failure`'s
  "poisoned" record bypass: a key whose last recorded attempt failed for a
  reason this machine can never fix by retrying (e.g. `platform_incompatible`)
  is normally treated as still-poisoned and refused outright on the next call,
  *unless* `allow_build=True`, in which case the poisoned check is skipped
  entirely so the retry worker actually gets to run. The comment's stated
  reasoning is: an explicit "install anyway" click must always reach a real
  worker, never be silently answered out of a stale record from a run that
  never even tried building from source.

**Decision**: this reasoning generalizes cleanly to a declarative, per-runner
`allow_build` and needs no change. Concretely: before this fix, `ltx_video`
had `allow_build=False` on every automatic build, so every failed attempt got
recorded as a permanent (`--no-build`-shaped) failure and poisoned the key —
exactly the shipped bug. After this fix, the *first* automatic build attempt
for `ltx_video` now passes `allow_build=True` from the supervisor. If a stale
poisoned record from *before* this fix landed is still sitting in someone's
`~/.fused-render` store, the existing bypass (`if not allow_build: check
poisoned`) is precisely what lets the now-correct build attempt proceed
instead of being refused by a record that predates the fix. That is the
correct behaviour, and it already exists — I did not need to add it. A
declarative opt-in is functionally indistinguishable, at this call site, from
the "explicit install-anyway click" the comment was written for: both are
"this call means it, so don't let an old --no-build failure answer for it."

No production code change was needed for this decision beyond the two call
sites already changed (`supervisor.py`, `projectenv.py`). I did not touch
`envinstall.py`.

## Pure-Python verification (task brief step 5)

Verified rather than asserted from memory, per the brief's explicit
instruction to stop and report if false. Cloned
`https://github.com/xocialize/ltx-2-mlx` at the pinned commit
`8ebae0a7cb08312fbf884790b91b4d155e714cdc` into a scratch directory and
inspected both `packages/ltx-core-mlx` and `packages/ltx-pipelines-mlx`:

- No `.c`, `.pyx`, `.cpp`, or `.h` files anywhere in either package tree.
- No prebuilt `.so` files.
- Each package's own `pyproject.toml` declares `build-backend =
  "hatchling.build"` — a pure-Python wheel builder with no compiler
  invocation of its own.

Conclusion stands as documented in the runner's `pyproject.toml`: "building
from source" for this pair is uv copying `.py` files into a wheel, not
arbitrary native compilation. Safe to grant the opt-in.

## Dead ends / non-issues ruled out during this session

- **A ~30-minute detour chasing a phantom pytest collection bug** in
  `tests/test_projectenv.py`: `pytest -k "allow_build"` and `grep
  "allow_build"` both reported the two new tests as absent, even after
  clearing `__pycache__`/`.pytest_cache`, disabling the cache provider, and
  confirming (via `ast.parse`, `py_compile`, byte-search, and
  `--collect-only -v`) that the file parses fine, has no duplicate module,
  and resolves to the expected path. **Root cause: a naming mismatch, not a
  bug.** The actual test names say `allows_build`
  (`test_runner_allows_build_reads_the_declared_opt_in`,
  `test_runner_allows_build_is_false_with_no_manifest`) — "allow*s*_build",
  not "allow_build". `-k "allow_build"` and `grep "allow_build"` both
  correctly matched nothing, because the substring genuinely isn't there.
  `-k "runner_allows_build"` (or just `-k allows_build`) finds them
  immediately and both pass. **If you are the follow-up builder and see
  "no tests ran" for a `-k` filter, check the literal spelling in the
  function name before assuming a collection bug** — this cost real time.
- **`tests/test_env_install_worker_progress.py::test_a_read_loop_exception_both_kills_and_reaps_the_child`
  fails on this worktree** ("DID NOT RAISE RuntimeError") both with and
  without this diff applied — confirmed pre-existing and unrelated to this
  change. (Note per repo convention: a stash-compare only proves "not
  introduced this round," not "pre-existing on main" — the fully rigorous
  check would re-run against `origin/main` in a separate checkout, which I
  did not do given the worktree-only constraint on this task; flagging this
  explicitly rather than overclaiming. The failure mode — a monkeypatched
  parser exception not propagating out of a background reader thread — reads
  like an environment/threading timing issue unrelated to anything this diff
  touches, none of which touches that file or its call paths.)
- **No cache-key production code change was needed** — see decision above.
  Do not add `allow_build` to `venv_key_for`'s hash input; it was correctly
  never there and adding it now would be an unrelated, unrequested behaviour
  change (it would split one folder's install state across two keys for no
  reason tied to this bug).

## What a follow-up builder should know before touching this again

- The only production files touched: `fused_render/projectenv.py`,
  `fused_render/ai/supervisor.py`, `fused_render/ai/runners/ltx_video/pyproject.toml`.
  `fused_render/envinstall.py` and `fused_render/_env_install_worker.py` were
  read carefully but **not modified** — the existing `allow_build` plumbing
  in both already supported everything this fix needed.
- Test files touched: `tests/test_ai_runner_deps.py`, `tests/test_ai_runtime.py`,
  `tests/test_projectenv.py`.
- Narrow test commands run and their results (all via
  `.venv/bin/python -m pytest ...`, xdist enabled per repo default):
  - `tests/test_projectenv.py -q` → 93 passed
  - `tests/test_ai_runner_deps.py -q` → 54 passed
  - `tests/test_ai_runtime.py -q` → 566 passed, 10 warnings (pre-existing
    starlette `BlockingPortal` deprecation warnings, unrelated)
  - `tests/test_env_install.py tests/test_server_env_install.py -q` →
    261 passed (one background-thread `PytestUnhandledThreadExceptionWarning`
    from an intentional error-injection test in `test_server_env_install.py`,
    not a failure)
  - `tests/test_env_install_worker_progress.py -q` → 1 failed (pre-existing,
    see above), 50 passed
- Did not run the full suite, per the task's explicit instruction that this is
  the orchestrator's job.
- Did not push, did not open a PR, per the task's explicit instruction.
