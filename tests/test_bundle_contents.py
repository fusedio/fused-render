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
import ast
import functools
import importlib.util
import os
import sys

import pytest

tomllib = pytest.importorskip("tomllib", reason="needs Python 3.11+")

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
    forced = set(module.OPTIONS["packages"]) | set(module.OPTIONS["includes"])
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
    forced = set(module.OPTIONS["packages"]) | set(module.OPTIONS["includes"])
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


@pytest.mark.parametrize("relpath", _template_files())
def test_a_header_only_macos_needs_is_scoped_to_macos(relpath):
    """A header that only macOS needs must be marker-scoped to macOS.

    The unit is the WHOLE header, not each dependency, because a header is
    all-or-nothing per platform: if it binds at all, the script gets an isolated
    venv that must contain everything it imports. So `pano` rightly declares
    numpy unmarked — py360convert is absent everywhere, so pano always gets a
    venv and that venv always needs numpy. But `slides` declares only things the
    AppImage and the Windows installer already ship, so its header must not bind
    there at all, or those platforms build a venv and re-download 24 MB of
    python-pptx they have on their own interpreter — the same waste D174 removed
    from the daemon launchers, reintroduced by the back door.

    PEP 723 carries PEP 508 markers, so the template states this itself rather
    than the engine guessing (see `engine._marker_applies`).
    """
    raw = _raw_header(relpath)
    if not raw:
        return
    # Would the OTHER platforms be missing anything this header declares?
    others_lack = {_norm(d) for d in raw} - _declared_dists()
    if others_lack:
        return  # the header is needed everywhere; unmarked deps are correct
    unmarked = sorted(_norm(d) for d in raw if ";" not in d)
    assert not unmarked, (
        f"every dependency {relpath} declares is already shipped by the Linux "
        f"AppImage and the Windows installer, so its header must bind on macOS "
        f"ONLY — but {unmarked} carry no environment marker. Add "
        "`; sys_platform == 'darwin'` to each, or those platforms will build a "
        "venv and re-download packages they already have."
    )
