# Lean wheel / Intel Mac compatibility — build spec

## Why

Intel Macs (x86_64) get no DMG release. The wheel is their delivery path, plus
Linux aarch64 and Python-native users. Today `pip install fused-render` on an
x86_64 Mac hits a compile cliff: two dependencies stopped publishing x86_64
macOS wheels, so pip falls back to building from source (~15 min, often fails).

This branch makes the wheel installable on those platforms and adds CI gates
that keep it that way.

## Scope — four items

### 1. Platform-conditional version ceilings

Two dists dropped x86_64 macOS wheels. Add PEP 508 markers so x86_64 macOS
resolves to the last version that still ships a binary wheel, while every other
platform stays unpinned.

- `zeroconf` (pyproject.toml:35, core dependency) — last x86_64 mac wheel is
  **0.148.0**.
- `cryptography` — last x86_64 mac wheel is **48.0.1**.

`cryptography` is the subtle one. It is NOT a direct dependency of fused-render
outside `[dev]` (pyproject.toml:148), but it reaches every real install
transitively: both `[bundled]` (pyproject.toml:288) and `[fused]`
(pyproject.toml:335) pin `mcp<2`, and mcp 1.30.0's metadata carries
`pyjwt[crypto]>=2.10.1` with **no marker and no extra** — verified against PyPI
on 2026-09-23. So the ceiling has to be applied where it actually constrains the
resolve, not just in `[dev]`. Decide the mechanism (a direct constrained entry
in the extras that need it is the obvious one) and write down the reasoning.

Note pyproject.toml:346 already has `'cryptography ; sys_platform == "win32"'`
in `[fused]` — that entry is a precedent for the shape, and it also needs the
ceiling on x86_64 mac.

Verify each ceiling against PyPI rather than trusting this document: fetch
`https://pypi.org/pypi/<name>/json` and check which releases carry a
`macosx_*_x86_64` wheel. If the real boundary differs from the version above,
use the real one and say so in the commit message.

### 2. Bump the fused pin — DEFERRED, see below

`fused==2.9.3b8` → `fused==2.9.3b9` at pyproject.toml:276 (`[bundled]`) and
pyproject.toml:323 (`[fused]`).

2.9.3b9 is published on PyPI (uploaded 2026-09-23 09:13 UTC) and carries
upstream PR fusedio/fused#369, which cut the core dependency set to 23 dists —
no pyarrow, geopandas, shapely, boto3 or cryptography. This is the release that
makes the `[fused]` path viable on an unserved platform.

**Builder finding (2026-09-23): NOT done.** The `requires_dist` claim above is
correct — `https://pypi.org/pypi/fused/2.9.3b9/json` confirms it — but the
version is not in PyPI's *simple index*, which is what pip/uv actually resolve
against. `pip download fused==2.9.3b9` and `uv pip compile --extra fused` both
fail with "no matching distribution" / "no solution found"; the simple index at
`https://pypi.org/simple/fused/` tops out at 2.9.3b8. Bumping the pin now would
make `pip install "fused-render[bundled]"`/`[fused]` unsatisfiable — the
opposite of this branch's goal. Left at 2.9.3b8. Full detail and re-check
instructions in DECISIONS.md under "Lean wheel / Intel Mac compatibility
(2026-09-23)". A later builder: re-check the simple index first; if 2.9.3b9 (or
newer) has propagated, the bump itself is a one-line, already-verified change.

### 3. Manifests for undeclared template imports — DEFERRED, see below

`fused_render/executor.py:155` `explain_missing_module` turns a bare
`ModuleNotFoundError` into a legible message — but it is strictly gated: the
missing module must map to a dist the folder **declares** in its own
`pyproject.toml`. Built-in templates that import a `[bundled]`-only package
without declaring it therefore fail with a bare traceback instead.

Roughly 18 templates under `fused_render/templates/` are in this state (10 of
51 currently have a `pyproject.toml`). **Re-derive the list yourself** — do not
trust that count. Method: for each template dir, collect its third-party
imports, subtract the ones satisfied by fused-render's core dependencies, and
flag any remainder that the template does not declare.

Give each flagged template a `pyproject.toml` declaring what it actually
imports. Match the shape of the manifests already present in the 10 templates
that have one.

Two constraints:
- Templates are **mount-agnostic** — no mount-vs-local branching belongs in a
  template.
- `fused_render/executor.py:71` `INPROCESS_HELPERS` lists helpers that run
  in-process rather than in a spawned child; a manifest does not help those.
  `templates/structure/reader.py` is one. Note any template you skip for this
  reason and why.

Adding a template file changes the packaged-tree sha256, which is the gate in
`fused_render/core_templates.py` — expected, no action needed, but it means a
running dev server restages on next start.

**Builder finding (2026-09-23): NOT done — a real count, and a real conflict.**

Re-derived list, using `tests/test_engine_requirements.py`'s own AST helpers
(`_template_graph`, `_imported_dists`, `_app_dists`): **10 folders**, not
"roughly 18" — `autocad_viewer`, `claude`, `excel`, `las`, `log_studio`,
`netcdf`, `photos`, `slides`, `usd`, `xlsx`. Each imports a `[bundled]`-only
dist (pillow, openpyxl, fpdf2, python-pptx, drain3, msgpack, numpy — plus
`duckdb`/`pyarrow` for `excel`, which are core deps but still count: a folder
with *any* manifest must declare every app-dist import, core included —
`test_a_declared_environment_is_complete` gives no baseline credit, D172) with
no `pyproject.toml` of its own.

`xlsx/reader.py` is the one to skip as instructed: it is in
`executor.py`'s `INPROCESS_HELPERS`, so it never runs in a spawned child or a
project venv — a manifest cannot reach it, the same as `structure/reader.py`.

**The other 9 cannot be given a manifest without either breaking or relaxing
an existing, deliberate test.** Confirmed by writing one (`autocad_viewer`,
declaring only `pillow`) and running the suite:
`tests/test_bundle_contents.py::test_a_declaration_is_needed_for_what_the_MACOS_BUNDLE_lacks[autocad_viewer]`
fails — "declares ['pillow'], all of which the macOS bundle already ships...
Delete the file." That test (D176) requires a folder's declaration to name at
least one dist the **macOS bundle** does not already carry, on the theory that
anything else only costs a venv build and a download for no benefit. Checked
all 9: every flagged import (pillow, openpyxl, fpdf2, python-pptx, drain3,
msgpack, numpy, duckdb, pyarrow) is already in `[bundled]` today (D276 never
removed any of these), so **none of the 9 has a single dist that clears that
bar** — the necessity test would reject all 9, not just the one tried.

This is a genuine conflict between this item and D176, not a bug in either
side:
- D176's test is correct for the DMG/full-bundle reader: a locked manifest
  (the convention — all 10 existing declaring folders ship a `uv.lock`)
  disables the fused engine's `app_satisfies` fast path
  (`engine.py:547`/`projectenv.py:585`), so adding one of these 9 would force
  a real venv build + download for every DMG/`[bundled]` user, to declare
  something their interpreter already has. That is exactly the D276 defect
  D176 exists to prevent, applied to a different set of packages.
- The spec's premise is also correct: on a genuinely lean/wheel-only install
  (no `[bundled]`), none of these 9 folders can ever get
  `explain_missing_module`'s help or a working fused-engine auto-install,
  because nothing declares what they need. That gap is real and unaddressed.
- Two mechanisms were checked and ruled out as a way to have both: (a) the
  built-in executor (`executor.py`) never builds a venv at all — it always
  spawns `[sys.executable, _child.py]` on the app's own interpreter regardless
  of a manifest, so a manifest changes nothing there except unlocking
  `explain_missing_module`'s message; (b) `explain_missing_module` is
  deliberately gated on the folder *declaring* the module ("blaming the
  environment for a user's typo is worse than saying nothing" —
  `executor.py:186`), so extending it to fire for any known-bundled import
  with no manifest at all would reintroduce exactly the false-positive risk
  that gate exists to avoid.

Left undone. A later builder (or a product call) has two real options, neither
of which this branch should make unilaterally:
1. Accept the DMG venv-build+download regression for these 9 folders and add
   the manifests, extending D176's necessity test with a documented exemption
   list (mirroring `_OPTIONAL_IMPORTS`'s shape: named entries, each with a
   reason) for folders whose declaration exists solely to serve the
   lean-wheel/no-`[bundled]` path.
2. Leave `[bundled]` folders undeclared and accept that these 9 templates stay
   broken-with-no-explanation on a lean/wheel-only install until `[bundled]`
   is available there too (e.g. once fused pin work — item 2 — or a `pip
   install fused-render[bundled]` on Linux — lands and is documented as the
   supported path for these templates).

No files changed for this item; `pyproject.toml`/`uv.lock` scaffolding for the
9 folders was not written. Full detail cross-referenced in DECISIONS.md under
"Lean wheel / Intel Mac compatibility (2026-09-23)".

### 4. CI gates

Port three gates from the sibling repo's proven implementation at
`/Users/iamsdas/Work/openfused/.claude/worktrees/lighter-deps/.github/workflows/ci.yml`.
**Read that file** — it is a separate repository, not a sibling worktree of this
one, so it is safe to read from. Adapt, do not copy verbatim; this repo's CI
layout differs.

- **Minimal-install job** (their `local-extra`, ci.yml:81) — install with NO
  extras, then assert the lean install actually works *before* running tests.
  Their comment explains why: without an explicit up-front `import` assertion
  the suite's `importorskip` tests silently skip and the job goes green having
  tested nothing.
- **Wheel distribution gate** (their `wheel` job, ci.yml:228) — build a wheel,
  assert the shipped paths are present in it by name (failing with the missing
  path), install into a throwaway venv, `cd /tmp` so no source tree is on the
  resolution path, then boot the real CLI and hit its health endpoint. The
  `cd /tmp` is load-bearing: without it the smoke test passes off the checkout
  and proves nothing. For this repo the shipped paths are the wheel `artifacts`
  globs at pyproject.toml:422-434 (`fused_render/static/shell-dist/**`,
  `fused_render/skills/**`), and the CLI is `fused-render serve`.
- **Import-weight guard** (their commit d68ddb45) — asserting a heavy package
  is absent from `sys.modules` "measures the venv, not the dependency graph"
  and false-positives. The working technique is a `sys.meta_path` blocker that
  makes the import genuinely *fail*, plus a narrow `except ModuleNotFoundError`
  with `exc.name in {...}` at each chokepoint. Never a bare `except Exception`.

The wheel build runs the hatchling hook in `scripts/hatch_build.py`, which
shells out to npm and needs Node 22 — the CI job must provide it.

**Builder status (2026-09-23): done — three jobs added to `.github/workflows/test.yml`
(`minimal-install`, `wheel`), plus `tests/test_import_weight.py`, all wired into
`test-status`.** `minimal-install` installs `.[dev]` only, asserts
`fused_render.cli`/`fused_render.server` import and `create_app()` constructs
cleanly, runs the new import-weight test (blocks every `[bundled]`-only
distribution via a `sys.meta_path` finder, subprocess-isolated, per
d68ddb45's technique), then boots the real CLI (`python -m fused_render.cli
serve`) and curls `/api/config`. `wheel` builds a real wheel (`uv build
--wheel`, which shells out to npm the same way `pip`/`build` would — verified
locally), asserts the `artifacts` glob paths
(`fused_render/static/shell-dist/index.html`, `fused_render/skills/`) are
actually inside it via `unzip -l`, installs into a throwaway venv, `cd /tmp`,
boots `fused-render serve`, and curls `/api/config`. Both boot+curl sequences
and the wheel build/artifact-check were run for real, locally, before
committing (not just YAML-reviewed) — see DECISIONS.md for the exact commands
and their output.

**Critical finding from that local run, outside the four items but directly
relevant to this spec's whole goal:** the wheel's boot-smoke step FAILED the
first time it was run for real, off a throwaway venv with `dist/*.whl`
installed and no extras — `ModuleNotFoundError: No module named
'cryptography'` inside FastAPI's `lifespan`, from
`fused_render/update/common.py:28` (`from cryptography.exceptions import
InvalidSignature`), reached via `update/mac.py` (imported unconditionally by
`update/__init__.py:start()` on `sys.platform == "darwin"`) from
`app.py:_startup_update_dev_manager`. **A bare `pip install fused-render`
(no extras, any macOS, not just x86_64) crashes at server startup today** —
`cryptography` is not a core dependency (item 1 only ceilinged it for
`[dev]`/`[bundled]`/`[fused]`, per the mcp-transitive finding at the top of
this file), and `update/common.py`'s signature-verification import has no
guard.

This is the actual blocker for "the wheel is installable on Intel Macs" — item
1 fixes *resolution* (pip can find compatible wheels), but the app still will
not *run* once installed, on any Mac. It was found here, not in items 1-3,
because those changes never exercise a real boot; the CI gates in this item
are what caught it, on the first real run.

**Also worth flagging:** both new CI jobs run on `ubuntu-latest` (matching the
sibling repo's convention and this repo's other non-desktop jobs), so on CI
this darwin-only crash does NOT reproduce — the jobs will show green on every
PR despite the bug being real. Catching it in CI would need a `macos-latest`
leg for at least the boot-smoke step. Not added here: outside item 4's stated
scope (port the sibling repo's three gates, adapted, not add new platform
coverage) and a real cost/scope decision (macOS runner minutes, whether to
also gate on `macos_packaging`/`app`) that a builder should not make
unilaterally, same posture as items 2 and 3.

**Left unfixed, by design — this is a fifth, unscoped finding, not one of the
four items.** The safe fix is almost certainly a guard in `update/mac.py`
mirroring the module's own documented "nothing to swap" no-op convention
(catch `ModuleNotFoundError`/`ImportError` around the `common` import in
`start()`, log once, leave `manager()` returning `None` — the same shape
`/api/config` and `/api/update` already handle for "no update manager on this
platform"). That touches a security-critical, signature-verification code
path, so it deserves its own reviewed change, not a rushed patch appended to
a CI-gates commit. Flagged for the orchestrator/next builder as a real,
verified, high-priority bug — full repro in DECISIONS.md.

## Explicitly out of scope

- **pyarrow → duckdb in the file index.** Deferred by the user pending a
  measurement of the write path. Do not touch `fused_render/index/store.py` or
  `fused_render/index/scan.py`. `pyarrow>=14` stays at pyproject.toml:39.
- **Publishing to PyPI.** The `license` field at pyproject.toml:10-12 is
  deliberately unset and blocks publish. Leave it unset; distribution stays via
  wheel URLs.
- Changing the `uv` resolution order in `envinstall.py:1524`.

## Verification

Scoped tests only during the build — the full suite is the orchestrator's job
at the end. Use the repo's existing test selection; do not run all ~8400 tests
per commit.

For the dependency markers specifically, the meaningful check is that the
metadata resolves as intended. A dry-run resolve for an x86_64 macOS target is
worth more than any unit test here.

## Conventions

- Commit per logical unit, clear messages. Do not squash the four items into one
  commit.
- `DECISIONS.md` at the repo root is a tracked project log — **append only**,
  never rewrite existing entries.
- Record anything you discover that this spec got wrong, directly in this file
  or in DECISIONS.md, so a later builder resumes from disk.
