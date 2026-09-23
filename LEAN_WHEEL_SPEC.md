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

### 3. Manifests for undeclared template imports

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
