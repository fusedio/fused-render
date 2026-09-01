# Plan: a project's venv lives at `<project>/.venv`

## The change in one sentence

`projectenv.venv_dir_for()` stops always answering `<home_dir()>/venvs/<key>` and
answers `<project>/.venv` for every project folder EXCEPT one that ships inside
the installed `fused_render` package — those keep the home store, mirror and all,
because the packaged app's own tree is read-only (D376).

## Why (context you need, not repeated elsewhere)

Today every project venv lives centrally at `<home_dir()>/venvs/<sha256 of the
folder's abs path>[:16]` (SPEC PY-16, MD-7). That was chosen so derived state never
lands in the user's tree, and so a core template's venv survives the release-time
re-stage (`core_templates.core_templates_dir()` does `rmtree` + `copytree` + atomic
swap on every version/digest change, `fused_render/core_templates.py:156-165`).

The owner has decided:

* Standard layout wins for user folders — `.venv` in the project is what every
  other Python tool expects (`uv run`, VS Code, and our own notebook kernel picker
  at `fused_render/templates/notebook/kernel.py:565-579`, which already looks for
  `<dir>/.venv` and will now find the project env for free).
* Core templates are ALREADY copied to a writable place by the staging step, so
  they go in-tree with everything else. **A re-stage wiping a template's `.venv` is
  an accepted cost**: the uv cache lives outside the staged tree either way
  (`<home_dir()>/uv-cache` when `FUSED_RENDER_HOME` is set — the packaged
  Linux/Windows app always sets it — and uv's own platform default otherwise), and
  it is on the same filesystem as the staged tree, so the rebuild is a hardlink
  relink, not a re-download.
* The AI runner folders (`fused_render/ai/runners/*`) are NOT staged and stay
  read-only on the AppImage squashfs mount and under a Windows `Program Files`
  install. They keep the home store and the `<key>.src` manifest mirror exactly as
  they are. Staging the runner tree the way templates are staged was considered and
  deferred — D376 already records why (`registry.RUNNERS_DIR` is a module-level
  constant baked into frozen `Runner` rows at import time).

## Placement rule

One predicate, in `projectenv.py`, in this order:

1. `FUSED_RENDER_VENV_IN_TREE` is `"0"` → home store. (Escape hatch. The hazard
   this change creates is real: a cloud-synced or network-mounted project folder
   would sync a multi-gigabyte venv.)
2. The folder is inside the installed `fused_render` package → home store.
   **Reuse the existing test** — `_venv_identity(project_dir)` returns a string
   starting with `_PACKAGE_IDENTITY` for exactly this case. Do not write a second
   derivation of "is this in the package"; that is the same mistake
   `venv_key_for`'s docstring warns about.
3. The folder is not writable → home store. Probed, never `os.access` — see
   `_env_install_worker._writable_dir` (`fused_render/_env_install_worker.py:1229`)
   for why, and copy that probe's technique. **Memoise the answer per project dir**
   for the life of the process, with a lock and a `reset_*` test seam, in the shape
   `projectenv._digest_cache` / `reset_state_digest_cache()` already uses: this
   runs on the `/api/run` pre-flight path via `envinstall.is_installed`, and an
   unmemoised create-exclusive probe per request is filesystem churn on the hot
   path.
4. Otherwise → `os.path.join(os.path.abspath(project_dir), ".venv")`.

The worker needs its own copy of the probe (it has one) because `_env_install_worker`
must not import `fused_render` (D152). Two copies of the technique is correct here;
do not try to share it.

## What deliberately does NOT change

Read these before touching anything, and preserve them:

* **`venv_key_for` is untouched.** The key is not the storage path. It names the
  progress directory (`envinstall.py:991`, `<home>/cache/_env_install/<key>`), it is
  the `/api/env/progress?key=` and `/api/env/cancel` API parameter, and it is the
  install-dedup lock key (`envinstall.py:1697`). Every one of those still works and
  still wants a stable per-folder identifier.
* **Staleness.** `.fused-source.json` + the `pyproject.toml` digest
  (`projectenv.state_digest`, `sidecar_matches`) is unchanged, and the sidecar keeps
  recording `_venv_identity(project_dir)` as `path`.
* **`_env_install_worker._build` needs NO logic change.** It already sets
  `UV_PROJECT_ENVIRONMENT=venv_dir` from the `venv_dir` it is handed in argv
  (`_env_install_worker.py:1474`), and the parent computes that with
  `envinstall.venv_dir_for` (asserted by `tests/test_env_install.py:1996`). For an
  in-tree project it now happens to equal uv's own default, which is harmless and
  still worth setting explicitly. Only that function's DOCSTRING is wrong now.
* **`_sync_root` and the `<key>.src` mirror.** Unchanged. Its scope narrows to
  in-package folders, which is what it was written for.
* **The `.venv` ecosystem is already in place** — do not re-add any of it:
  `app_git._GITIGNORE` already carries `.venv/` (`fused_render/app_git.py:106-117`),
  `index/ignore.py:81` already prunes it, `appfile.py:85,236` already drops it from
  app export, `git_upstream.py:277` already tolerates it, and the repo's own
  `.gitignore:3` has `.venv/`.

## Behaviour that changes, and is intended

* **Renaming or moving a project no longer rebuilds its environment.** Today the key
  is the abs path, so a rename orphans the venv by design (SPEC PY-16 calls that a
  feature). In-tree, the venv travels with the folder. Note in the DECISIONS entry
  that uv writes absolute shebangs into `.venv/bin/*` console scripts, so a user's
  own `uv run` after a move may need a resync — we are unaffected because we only
  ever spawn `<venv>/bin/python` directly (`envinstall._venv_python`).
* **A user's hand-made `<project>/.venv` gets adopted.** `envinstall.is_installed`
  treats a venv without the `.openfused-ready` marker as not installed, and
  `_build` removes an unmarked venv directory before syncing (the D212 repair). So a
  `.venv` the user made with `uv sync` is rebuilt once, from cache, and marked. That
  is now a directory in the USER's folder that we delete and recreate — call it out
  in DECISIONS as accepted rather than leaving it to be discovered. It is the right
  outcome: it is the same environment, and after this change it is genuinely the
  project's one venv rather than a rival to ours.
* **Hardlinking can be lost more often.** A user project on a different mount from
  uv's default cache pays full copies instead of hardlinks. `uv_cache_dir()` already
  documents and accepts this trade; in-tree just makes it reachable more often
  (external drives, network mounts). One sentence in the DECISIONS entry, no code.

## The stranded-venv sweep (required — do not skip)

Every existing `<venvs_root>/<key>` whose project folder still exists will otherwise
leak forever: `projectenv._source_is_deleted` correctly answers "not deleted", so
`gc()` keeps it, and nothing will ever read it again. Some of those are multi-gigabyte.

Do this as a new arm inside `gc()` rather than a versioned one-shot migration — it
is self-maintaining and needs no stamp:

> for a venv directory with a readable sidecar whose source folder EXISTS, if
> `venv_dir_for(source)` is not this directory, the venv has been relocated by
> policy — reclaim it (and its `<key>.src` sibling) the same way an orphan is
> reclaimed, and count it in the return value.

Check the interaction with the two things `gc()` deliberately leaves alone, and keep
both true: a venv with no readable sidecar is still skipped, and a source that is
merely UNREACHABLE (unplugged drive) must still be KEPT. The second falls out
correctly if you order it right — an unreachable folder fails the writability probe,
so `venv_dir_for` answers the home store, so it is not "relocated". Write a test that
pins exactly that, because it is the one way this arm could delete something
expensive and unrecoverable.

Do NOT migrate directories by moving them. Per the project's standing preference,
a clean break plus a rebuild-from-cache beats a move with its own failure modes.

## Defense in depth

`core_templates` stages with `shutil.copytree(PACKAGE_TEMPLATES_DIR, staging)`
(`fused_render/core_templates.py:161`). Our rule never puts a `.venv` inside the
package, but a developer running `uv sync` by hand in
`fused_render/templates/<name>/` would. Add
`ignore=shutil.ignore_patterns(".venv")` to that call so a dev checkout can never
copy a multi-gigabyte tree into the staging dir on every release-digest change.

## Docs that must move in the same change

These state the current rule as a guarantee, in detail, and a stale one here is worse
than no doc. Amend, don't delete the reasoning — the "why not in the user's tree"
history stays as the reason the IN-PACKAGE case still works that way.

* `SPEC.md` PY-16 (line ~405) and PY-18 (line ~407) — the storage sentence, and
  PY-18's "the environment can live under the app's home dir".
* `DECISIONS.md` — a NEW row (next free D-number, appended in order) recording this
  decision: the predicate, the accepted re-stage rebuild for staged templates and why
  the uv cache makes it cheap, the runners staying on the home store with the reason
  deferred to D376, the adopted hand-made `.venv`, the rename semantics, the
  hardlink trade, and the `gc()` relocation arm. Amend D376's row with a pointer
  rather than rewriting it.
* `fused_render/projectenv.py` module docstring — the "Storage follows MD-7" paragraph.
* `fused_render/envinstall.py` module docstring ("**The key is the project folder's,
  and it is OURS.**" paragraph) and the `BACKEND_ATTRS` comment at line ~196.
* `projectenv.state_digest`'s paragraph about "doing that would create an in-folder
  `.venv` and diverge from the home-dir store" — that argument is now backwards; the
  requirement it supports (a manifest edit must be picked up without a hand `uv sync`)
  is unchanged, so restate the reason, keep the rule.
* `_env_install_worker._build`'s `UV_PROJECT_ENVIRONMENT` block (line ~1403) and that
  module's docstring line 58.
* `fused_render/app_git.py:106` — the `.venv/` comment says "the app NEVER creates
  one". It does now, and the ignore rule matters MORE, not less.
* `envinstall.venv_dir_for` / `venv_python_for` docstrings ("under OUR home dir").

Follow the house style in these files: state the rule, then why the alternative was
rejected. Per project instruction, describe the code as it is — no "previously", no
"this change", no PR references, in code comments. (DECISIONS.md is the exception:
that file is a history by construction.)

## Tests

`tests/test_env_install.py` has ~30 `venv_dir_for`/`venv_key_for` call sites and
`tests/conftest.py:203` builds fake venvs at the home store. Most fixture projects are
writable tmp dirs, so they will simply resolve in-tree — expect real breakage where a
test asserts the path SHAPE, and fix those to assert through `venv_dir_for` rather
than reconstructing the path.

New tests to add (in the file that already covers the area):

1. A writable user project resolves to `<project>/.venv`.
2. A folder inside the `fused_render` package resolves to the home store — assert it
   for a real runner folder, and that `_sync_root` still mirrors for it.
3. `FUSED_RENDER_VENV_IN_TREE=0` forces the home store.
4. A non-writable project folder falls back to the home store (chmod the dir; skip on
   Windows and when running as root, where the probe legitimately succeeds).
5. The writability answer is memoised — one probe per project per process — and the
   reset seam clears it.
6. `gc()` reclaims a home-store venv whose project now resolves in-tree.
7. `gc()` KEEPS a home-store venv whose source folder is unreachable (parent missing),
   which is the dangerous case.
8. Renaming a project keeps its in-tree venv (no rebuild): `is_installed` still true
   after the move.

## Verification

Read `.claude/skills/setting-up-dev-env/SKILL.md` FIRST and follow it — a fresh
worktree needs setup before pytest works at all.

Keep the inner loop scoped: `pytest tests/test_env_install.py -x -q`,
`pytest tests/test_projectenv.py -q`, and the specific `-k` selection you are working
on. Do NOT run the full suite — this machine's `/tmp` quota kills it partway through
and the result is noise, not signal. The orchestrator runs the broader set and CI
covers the rest. Before reporting done, also run: `pytest tests/test_projectenv.py
tests/test_env_install.py tests/test_engine.py tests/test_ai_runtime.py
tests/test_bundle_contents.py tests/test_template_locks.py -q`.

## Commits

One per logical unit, in this order, each with its scoped tests green:

1. the placement predicate + memoised writability probe in `projectenv.py`
2. the `gc()` relocation arm
3. the `core_templates` copytree ignore
4. the test updates and new tests
5. the docs (SPEC, DECISIONS, module docstrings/comments)

Append anything you learn that this plan got wrong to the bottom of this file before
you report — the next builder reads from disk, not from your context.

## Learnings from the build (append-only, written after the fact)

* **The `tests/test_env_install.py` breakage was real but small, not the "~30 call
  sites, expect real breakage" scale the plan warned about.** Only ONE test in that
  file asserted the path SHAPE directly (`test_the_venv_lives_in_our_home_dir_never_in_the_project`,
  which checked `venv.startswith(str(tmp_path / "home"))`). Every other call site —
  all ~29 of them — already went through `envinstall.venv_dir_for(proj)`/`venv_key_for(proj)`
  dynamically rather than reconstructing a path, so they kept passing unchanged once
  the underlying function's answer changed. `tests/conftest.py`'s serialization
  fixture (the thing the plan flagged at line 203) is the same story: it calls
  `envinstall.venv_key_for`/`is_installed`/`venv_dir_for` dynamically and never
  reconstructs a path either, so it needed no changes at all.
* **One breakage the plan did NOT anticipate**: `tests/test_engine.py::test_a_declared_project_runs_in_its_own_venv`
  asserted `"venvs" in out["result"]["prefix"]` — a path-shape substring check on the
  real interpreter prefix returned by an end-to-end venv build, in a file the plan's
  test section never mentions. It only surfaces when running the broader verification
  set (`test_engine.py` is not in the narrow inner loop), which is exactly why the
  plan says to run that broader set before reporting done — it caught something the
  scoped `test_env_install.py`/`test_projectenv.py` runs could not have. Fixed by
  asserting `out["result"]["prefix"] == envinstall.venv_dir_for(warm_fused_backend_venv)`
  instead of the substring. Worth widening the plan's "Tests" section next time to say
  "grep the whole tests/ tree for hardcoded venv-path shapes", not just the one file.
* **The `gc()` test list in the plan (items 6–7) omitted one edge worth calling out
  explicitly for the next person**: the "keeps an unreachable-source venv" test needs
  the source's *parent* directory to be missing too (not just the leaf), matching
  `_source_is_deleted`'s own contract — a leaf-only-missing folder with an existing
  parent reads as "deleted", not "unreachable", and would (correctly) get reclaimed by
  the ALREADY-EXISTING orphan arm, not exercise the new relocation-vs-unreachable
  ordering the plan is actually worried about. Worth stating in the plan's own test
  item 7 wording next time, since it is easy to write a test that looks like it pins
  the dangerous case but actually falls through a different, already-correct arm.
* **Test item 2's "and that `_sync_root` still mirrors for it" is not exercisable
  with a REAL bundled runner folder in a dev checkout** — those folders are writable
  on disk in a checkout (only read-only in an actual AppImage/Program-Files install),
  and `_sync_root`'s own mirroring decision is independent of package identity (it
  asks `_writable_dir`, nothing about `_PACKAGE_DIR`). The test that satisfies this
  item therefore builds a SYNTHETIC package folder the same way the existing
  `test_a_bundled_venvs_sidecar_records_its_place_in_the_PACKAGE` test already does
  (`monkeypatch.setattr(worker, "_PACKAGE_DIR", ...)` / `monkeypatch.setattr(projectenv, "_PACKAGE_DIR", ...)`)
  and then `chmod`s that SYNTHETIC folder read-only to force the mirror — never the
  real `fused_render/ai/runners/*` tree, which would be reckless to chmod during a
  test run. Worth stating this pattern directly in the plan's test section rather
  than leaving it to be rediscovered.
* **The `core_templates` test for the copytree ignore was placed in commit 3, not
  commit 4** — bundling a change's own regression test with its code commit (rather
  than deferring ALL test writing to a single later commit) kept every commit
  independently green, and nothing in the plan's commit-sequence description actually
  requires otherwise; it just lists "3. the copytree ignore" and "4. the test updates
  and new tests" as separate items. Read literally, commit 4 could be interpreted as
  "every test change in the whole series", but that would leave commit 3 unverified
  on its own. The five-commit sequence given is fine as an ORDERING constraint, but a
  strict "commit 3 has zero test content" reading is not required and produces a
  worse commit history (an implementation commit nothing tests should have caught
  regressions in).

## Corrections from the code-review-fix pass (appended after the fact)

* **The rmtree-on-sight guard in `_env_install_worker._build` was the highest-severity
  finding a code review turned up on this branch, and the plan/spec never anticipated
  it**: once a project's venv defaults to `<project>/.venv`, an unmarked directory is
  no longer proof of "our own half-built venv" — it is just as often a venv a
  developer built by hand (`uv sync`, `uv venv`, `python -m venv`) with dev-group
  packages and editable installs that deserve to survive. The fix restates
  `envinstall._venv_runs`'s three-valued probe inside the worker (D152 forbids
  importing `fused_render` there) and only destroys a directory whose own
  interpreter cannot start; a runnable one is left for `uv sync` to reconcile.
  Worth stating explicitly in any future in-tree-venv-shaped plan: **a rmtree
  guarded only by "is there a ready marker" is safe only for a directory nothing
  else could plausibly have created — moving the venv into a shared, developer-
  writable location invalidates that assumption even though the guard's own code
  did not change.**

* **Tests that monkeypatch `worker.subprocess.Popen` to fake `uv sync`'s streaming
  invocation cannot also exercise a real `subprocess.run` probe** — `subprocess.run`
  calls `Popen` internally, so a `_build`-level test wanting to control
  `_venv_runs`'s verdict has to monkeypatch `worker._venv_runs` directly rather than
  route a real probe through the same Popen fake (the fake's `_FakeProc` has no
  `communicate()`, which only `subprocess.run` needs). The genuine probe behaviour
  (a real symlinked interpreter, a missing one, a timeout) is exercised by calling
  `worker._venv_runs` directly instead, unmediated by any `_build`-level fake.

* **`index.ignore.SHARED_IGNORE_DIRS` already existed as exactly the "what should
  never be walked into" list** (`.venv`, `node_modules`, `__pycache__`,
  `site-packages`) and was the right thing to reuse for the template-export walk
  (`templates_api.py`) rather than inventing a second hardcoded set — worth
  checking for a shared floor like this before adding a bespoke exclusion anywhere
  a tree gets walked for export/serialization purposes. `export.py`'s page-glob
  walk, by contrast, only needed `.venv` pruned specifically (a page folder has no
  reason to also exclude `node_modules`/`site-packages` the way a template does),
  so the two call sites ended up with different scopes on purpose rather than by
  oversight.

* **DECISIONS.md rows get amended in place when a later fix changes the mechanism
  a row describes, not just appended-to** — D630's sentence describing hand-built
  `.venv` adoption said the old (buggy) destroy-then-rebuild-from-cache behaviour,
  which the `_venv_runs`-probe fix in this same pass made false; it was edited in
  place to describe the real mechanism, following the "amended by DXXX" precedent
  D376 already used elsewhere in the same file, rather than left stale or given a
  new decision row of its own for what is really the same decision working
  correctly.
