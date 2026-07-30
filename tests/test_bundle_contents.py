"""What the macOS bundle actually ships, versus what templates assume (D176).

`pyproject.toml`'s `[bundled]` is **not** the bundle. It is the dev-install and
Linux/Windows shipping list:

  * Linux   — `uv pip install "<wheel>[bundled,...]"` (build_linux_appimage.sh)
  * Windows — `uv pip install "<wheel>[bundled,...]"` (build_windows_installer.ps1)
  * macOS   — installs the extra into a build venv, then **py2app copies only
              what setup_py2app.py names**

So macOS is the outlier, and "does the app have python-pptx?" is a per-platform
question. This file is where that asymmetry is written down and enforced, because
the last time it was only implicit a DMG shipped where opening a .pptx said
`pip install python-pptx` — advice a DMG user cannot follow.

The invariants below are deliberately sourced from the PACKAGING SCRIPT, not from
`[bundled]`. Deriving a template's needs from `[bundled]` is precisely the bug:
it claims things the bundle does not contain, so it cannot fail.
"""
import functools
import importlib.util
import os
import sys

import pytest

def _import_toml():
    """tomllib (3.11+) or the tomli dependency that covers 3.10."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            pytest.skip("needs tomllib (3.11+) or the tomli package")
    return tomllib


tomllib = _import_toml()

from fused_render import engine  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_REPO, "fused_render", "templates")


def _norm(requirement: str) -> str:
    name = requirement.split("[")[0]
    for sep in ("<", ">", "=", "!", "~", " ", "(", ";"):
        name = name.split(sep)[0]
    return name.strip().lower().replace("_", "-")


@functools.lru_cache(maxsize=1)
def _packaging_module():
    """setup_py2app.py, imported. It derives its own list, so it must be asked.

    Importable on purpose (its build-only work is behind a `py2app in sys.argv`
    guard) — a test that had to re-implement the derivation would be a second
    copy of the thing being checked.
    """
    path = os.path.join(_REPO, "scripts", "setup_py2app.py")
    spec = importlib.util.spec_from_file_location("_setup_py2app_under_test", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=1)
def _pyproject():
    with open(os.path.join(_REPO, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)


@functools.lru_cache(maxsize=1)
def _declared_dists() -> frozenset[str]:
    """`[bundled]` + core `dependencies` — what Linux and Windows ship."""
    pp = _pyproject()
    return frozenset(
        {_norm(d) for d in pp["project"]["optional-dependencies"]["bundled"]}
        | {_norm(d) for d in pp["project"]["dependencies"]}
    )


@functools.lru_cache(maxsize=1)
def _excluded_dists() -> frozenset[str]:
    return frozenset(_norm(n) for n in _packaging_module().BUNDLED_EXCLUDED)


@functools.lru_cache(maxsize=1)
def _macos_dists() -> frozenset[str]:
    """What the macOS bundle ships: everything declared, minus the exclusions."""
    return _declared_dists() - _excluded_dists()


# ------------------------------------------------ what py2app will actually take
#
# These three exist because a previous version of this file passed while
# `scripts/build_dmg.sh` failed at py2app — a unit test green against a broken
# build is the precise failure D177 is about. The contents of the force-list were
# checked; whether py2app would accept them was not.


def test_no_dotted_entries_in_packages():
    """`packages` must contain plain top-level names only.

    A dotted entry looks reasonable — py2app really does have a dotted-aware path
    (`included_subpkg = [pkg for pkg in self.packages if "." in pkg]`) — but it is
    not the FIRST consumer. `collect_packagedirs()` (build_app.py:1210) maps
    `get_bootstrap()` over every entry, which calls
    `modulegraph.util.imp_find_module`; that splits the name and calls
    `imp.find_module("google")`, raising ImportError for a namespace parent before
    the dotted-aware code runs. `google.auth` in `packages` therefore breaks the
    build outright. Such packages go in `STAGED_PACKAGES` and are copied in by
    build_dmg.sh instead.
    """
    dotted = sorted(p for p in _packaging_module().OPTIONS["packages"] if "." in p)
    assert not dotted, (
        f"{dotted} are dotted entries in py2app's `packages`, which fails the build "
        "in collect_packagedirs() -> imp_find_module(). Add the distribution to "
        "STAGED_PACKAGES so build_dmg.sh copies it in, as it does for `google`."
    )


def test_nothing_forced_as_a_package_is_a_namespace_package():
    """A PEP 420 namespace package (no `__init__.py`) cannot be forced.

    py2app's package bootstrap uses pre-namespace `imp.find_module` semantics, so
    forcing one fails — already documented for mpl_toolkits and PyObjCTools, and
    the reason `google` is staged. Checked across the WHOLE derived set rather
    than the one name we knew about, since the derivation reads whatever is
    installed and a future dependency could introduce another.

    Asked via `find_spec` rather than by looking for `__init__.py` under
    site-packages: a namespace package is exactly one whose spec has submodule
    search locations but no `origin`, and that holds wherever the code lives. The
    site-packages form of this check flagged `fused_render` (an editable install)
    as a namespace package — the sort of false positive that gets a real test
    deleted rather than believed.
    """
    offenders = []
    for name in _packaging_module().OPTIONS["packages"]:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue  # not importable here; nothing this test can say
        if spec is None:
            continue
        if spec.submodule_search_locations is not None and spec.origin is None:
            offenders.append(name)
    assert not offenders, (
        f"{offenders} have no __init__.py, so py2app cannot force them via "
        "`packages`. Add them to STAGED_PACKAGES (copied in by build_dmg.sh) or to "
        "NEVER_FORCE_AS_PACKAGE if nothing needs them."
    )


def test_native_packages_are_found_when_platlib_is_not_purelib():
    """The packages/includes split must look in BOTH site directories.

    `bundled_force_lists` decides `packages` vs `includes` by whether the import
    name is a directory on disk. In this venv `purelib == platlib`, so reading
    only `purelib` happens to find everything — but extension packages (numpy,
    pandas, scipy, pyarrow, shapely) install into `platlib`, and on any
    interpreter whose schemes differ every one of them would miss the directory
    test and be forced via `includes`. That is exactly the failure the `_duckdb`
    comment documents: py2app copies a bare `<name>.py` that shadows the real
    package.

    Simulated by pointing `purelib` at an empty directory and leaving the real
    site dir as `platlib` — the divergence, without needing such an interpreter.
    """
    import sysconfig

    module = _packaging_module()
    real = sysconfig.get_paths()
    empty = os.path.join(_REPO, "build", "_no_such_purelib")
    fake = dict(real, purelib=empty, platlib=real["platlib"])
    orig = sysconfig.get_paths
    try:
        sysconfig.get_paths = lambda *a, **kw: fake
        packages, includes = module.bundled_force_lists()
    finally:
        sysconfig.get_paths = orig

    native = {"numpy", "pandas", "scipy", "pyarrow", "shapely"}
    installed = {n for n in native if importlib.util.find_spec(n) is not None}
    misforced = sorted(installed & set(includes))
    assert not misforced, (
        f"{misforced} were forced via `includes` because only `purelib` was "
        "probed; py2app would copy a bare .py shadowing the real package"
    )
    assert installed <= set(packages), (
        f"{sorted(installed - set(packages))} vanished from the force lists "
        "entirely when purelib and platlib differed"
    )


def test_staged_packages_exist_and_are_actually_staged():
    """The staging list must name real directories, and the build must read it.

    build_dmg.sh gets the list from `scripts/_staged_packages.py`, which imports
    the same constant — so this asserts the two agree rather than trusting them to.
    """
    import subprocess
    import sysconfig

    module = _packaging_module()
    site = sysconfig.get_paths()["purelib"]
    for name in module.STAGED_PACKAGES:
        path = os.path.join(site, name)
        if not os.path.isdir(path):
            continue  # not installed here; the build's own FATAL check covers it
        # `cp -R` of an empty directory succeeds silently, so assert there is
        # something to copy. Not asserting the ABSENCE of __init__.py: staging is
        # for what py2app cannot carry, and a regular package can qualify too.
        assert os.listdir(path), f"{path} is empty; staging it would copy nothing"

    # Runs even when nothing is installed locally: this half compares two
    # declarations, and needs no package on disk.
    helper = os.path.join(_REPO, "scripts", "_staged_packages.py")
    out = subprocess.run([sys.executable, helper], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == list(module.STAGED_PACKAGES), (
        "scripts/_staged_packages.py (which build_dmg.sh calls) disagrees with "
        "setup_py2app.STAGED_PACKAGES"
    )


# --------------------------------------------------------------- reconciliation


def test_every_exclusion_is_declared_and_reasoned():
    """An exclusion must name something real and say what it costs.

    The whole defect this file exists to prevent is an omission indistinguishable
    from a bug. A stale exclusion (a distribution no longer in `[bundled]`) is
    just as misleading as an undocumented one.
    """
    excluded = _packaging_module().BUNDLED_EXCLUDED
    declared = _declared_dists()
    for name, reason in excluded.items():
        assert _norm(name) in declared, (
            f"{name!r} is excluded from the bundle but is not in `[bundled]` or the "
            "core dependencies — a stale exclusion is as confusing as a silent one"
        )
        assert reason and any(ch.isdigit() for ch in reason), (
            f"the exclusion of {name!r} must state its measured cost (e.g. '60 MB'); "
            f"got {reason!r}. An omission without a number reads as an oversight."
        )


def test_the_bundle_ships_everything_it_does_not_explicitly_exclude():
    """No third state: shipped, or excluded-with-a-reason. Never just absent.

    `setup_py2app.py` derives its force-list from the installed distributions, so
    this holds by construction — which is the point. It used to be a hand list,
    and eight distributions were quietly missing from it.
    """
    module = _packaging_module()
    # STAGED_PACKAGES reach the bundle too — copied in by build_dmg.sh rather
    # than forced through py2app, but shipped all the same.
    forced = (set(module.OPTIONS["packages"]) | set(module.OPTIONS["includes"])
              | set(module.STAGED_PACKAGES))
    # Distributions -> the top-level names they install, from real metadata.
    import importlib.metadata as md

    by_dist: dict[str, set[str]] = {}
    for import_name, dist_names in md.packages_distributions().items():
        for dist_name in dist_names:
            by_dist.setdefault(_norm(dist_name), set()).add(import_name)

    unreachable = []
    for dist in sorted(_macos_dists()):
        names = by_dist.get(dist)
        if not names:
            continue  # not installed in this env; nothing this test can say
        # A dotted entry (google.auth) covers its namespace parent (google).
        if not any(n in forced or any(f.startswith(n + ".") for f in forced)
                   for n in names):
            unreachable.append((dist, sorted(names)))
    assert not unreachable, (
        "these distributions are promised but the macOS bundle would not carry "
        f"them: {unreachable}. Either the derivation in setup_py2app.py missed "
        "them, or they belong in BUNDLED_EXCLUDED with their measured size."
    )


def test_excluded_distributions_are_not_forced_into_the_bundle():
    """The exclusion has to actually take effect."""
    module = _packaging_module()
    # STAGED_PACKAGES reach the bundle too — copied in by build_dmg.sh rather
    # than forced through py2app, but shipped all the same.
    forced = (set(module.OPTIONS["packages"]) | set(module.OPTIONS["includes"])
              | set(module.STAGED_PACKAGES))
    import importlib.metadata as md

    by_dist: dict[str, set[str]] = {}
    for import_name, dist_names in md.packages_distributions().items():
        for dist_name in dist_names:
            by_dist.setdefault(_norm(dist_name), set()).add(import_name)
    for dist in sorted(_excluded_dists()):
        for name in by_dist.get(dist, ()):
            assert name not in forced, (
                f"{dist} is in BUNDLED_EXCLUDED but its module {name!r} is still "
                "forced into the bundle"
            )


# ------------------------------------------------- templates vs the real bundle


def _template_files():
    out = []
    for dirpath, _dirs, files in os.walk(_TEMPLATES):
        if "__pycache__" in dirpath or os.sep + "vendor" in dirpath:
            continue
        out += [os.path.relpath(os.path.join(dirpath, f), _TEMPLATES)
                for f in files if f.endswith(".py")]
    return sorted(out)


def _raw_header(relpath):
    """The header verbatim, markers included — all platforms, not just this one."""
    with open(os.path.join(_TEMPLATES, relpath), encoding="utf-8") as f:
        return engine.script_requirements(f.read(), apply_markers=False)


@pytest.mark.parametrize("relpath", _template_files())
def test_a_header_is_needed_for_what_the_MACOS_BUNDLE_lacks(relpath):
    """Necessity is judged against the bundle, not against `[bundled]` (D176).

    macOS ships the narrowest set, so it is the binding constraint: a dependency
    absent there needs a header, whatever the other platforms have. Judging this
    against `[bundled]` is what deleted slides' `python-pptx` header and shipped
    a DMG that told the user to `pip install` on a read-only app.
    """
    header = {_norm(d) for d in _raw_header(relpath)}
    if not header:
        return
    justified = sorted(header - _macos_dists())
    assert justified, (
        f"{relpath}'s header declares {sorted(header)}, all of which the macOS "
        "bundle already ships — so it only costs a venv build and a download. "
        "Delete the block."
    )


# A distribution whose IMPORT NAME is satisfied by a lighter sibling that omits
# the thing the template actually needs. `pypandoc` and `pypandoc-binary` are the
# same version and the same `import pypandoc`, but the plain wheel is 0.0 MB with
# no pandoc executable and the binary one is ~41 MB with one — so declaring the
# wrong sibling builds a venv that imports fine and fails at runtime.
#
# This is the general gap: "is it importable" cannot decide whether a dependency
# is the RIGHT one. There is no metadata that says "this wheel omits the binary",
# so the check is a named pairing rather than a derivation — an honest small
# whitelist beats a general rule that cannot exist.
_MUST_USE_HEAVIER_SIBLING = {"pypandoc": "pypandoc-binary"}


@pytest.mark.parametrize("relpath", _template_files())
def test_a_header_declares_the_sibling_that_actually_works(relpath):
    """Catch "importable but non-functional" for the pairs where it can happen.

    `latex/engine.py` declared `pypandoc` while calling
    `pypandoc.convert_file` — which needs the pandoc binary the plain
    distribution does not ship. `docs/docs.py` had it right, and the two sat side
    by side for a while, because every other invariant here is satisfied by a
    module that imports.
    """
    for raw in _raw_header(relpath):
        dist = _norm(raw)
        better = _MUST_USE_HEAVIER_SIBLING.get(dist)
        assert better is None, (
            f"{relpath} declares {dist!r}, which installs the import name but not "
            f"the payload behind it — use {better!r} instead. The venv would build "
            "cleanly and fail at runtime."
        )
