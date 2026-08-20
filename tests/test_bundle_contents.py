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

from fused_render import engine, projectenv  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_REPO, "fused_render", "templates")

# Turns "this distribution is not installed here, so skip it" into a failure.
#
# The reconciliation below can only speak about distributions it can find real
# metadata for, so in an environment WITHOUT `[bundled]` it quietly degrades to
# almost nothing while still reporting green — the same failure mode the
# `fused-engine` CI job was created for (a matrix that looked like coverage of
# the engine and provided none, because no job installed the extra). Set this
# wherever `[bundled]` really is installed — CI's `bundle-contents` job and
# `build_dmg.sh`'s build venv — so a run that was SUPPOSED to be the real check
# cannot silently be a no-op instead.
_REQUIRE_BUNDLED = os.environ.get("FUSED_RENDER_REQUIRE_BUNDLED") == "1"


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
def _declared_dists_installable_here() -> frozenset[str]:
    """`_declared_dists()` minus the ones this interpreter's markers exclude.

    Only `FUSED_RENDER_REQUIRE_BUNDLED` needs this. The reachability check below
    reasons about the bundle across platforms, so it wants every declared
    distribution; demanding one be *installed here* is a claim about this
    interpreter, and `tomli; python_version < "3.11"` is correctly absent on
    3.11+. Without the distinction the flag fails on a venv that has `[bundled]`
    installed exactly as declared — which is how this was found.
    """
    pp = _pyproject()
    raw = list(pp["project"]["optional-dependencies"]["bundled"]) + list(
        pp["project"]["dependencies"]
    )
    return frozenset(_norm(d) for d in raw if projectenv.marker_applies(d))


@functools.lru_cache(maxsize=1)
def _declared_dists_the_BUNDLE_can_have() -> frozenset[str]:
    """`_declared_dists()` minus the ones the DMG's OWN interpreter excludes.

    The reachability check below reasons about a bundle built on
    `envinstall.SCRIPT_PYTHON_VERSION` (3.12), not about the interpreter running
    this test — and a marker-gated requirement the build interpreter rejects can
    never be in that bundle, so demanding py2app force it is asking for the
    impossible.

    `tomli>=2.0; python_version < '3.11'` is the case that surfaced it. On 3.10
    it is declared, installed and forced; on 3.12/3.13 it is simply not
    installed and the loop skips it. On **3.11** it is neither — the marker
    excludes it, so the derivation does not force it, while some transitive
    dependency of the test tooling installs it anyway — and the check demanded a
    distribution the macOS bundle is right not to carry. That made a green suite
    depend on which packages the CI runner's resolver happened to drag in.

    Markers are evaluated with the build interpreter's version over THIS
    environment, so `sys_platform` stays the runner's. That costs nothing here:
    a darwin-only distribution is not installed on the Linux runner either, so
    the loop already skips it one line down.
    """
    from packaging.requirements import Requirement

    from fused_render.envinstall import SCRIPT_PYTHON_VERSION

    build_env = {"python_version": SCRIPT_PYTHON_VERSION,
                 "python_full_version": SCRIPT_PYTHON_VERSION + ".0"}
    pp = _pyproject()
    raw = list(pp["project"]["optional-dependencies"]["bundled"]) + list(
        pp["project"]["dependencies"]
    )
    keep = set()
    for spec in raw:
        marker = Requirement(spec).marker
        if marker is None or marker.evaluate(build_env):
            keep.add(_norm(spec))
    return frozenset(keep)


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


@pytest.mark.parametrize("home_scheme,other_scheme",
                         [("platlib", "purelib"), ("purelib", "platlib")])
def test_both_site_schemes_are_probed_for_the_packages_includes_split(
    tmp_path, home_scheme, other_scheme
):
    """The packages/includes split must look in BOTH site directories.

    `bundled_force_lists` decides `packages` vs `includes` by whether the import
    name is a directory on disk. Pure-Python distributions land in `purelib` and
    extension ones in `platlib`; the two coincide inside a venv, so probing only
    one scheme happens to find everything here and silently misfiles the other
    scheme's packages on any interpreter where they differ — sending them to
    `includes`, which is exactly the failure the `_duckdb` comment documents:
    py2app copies a bare `<name>.py` that shadows the real package.

    Proven WITHOUT betting on where any real distribution landed on this machine
    (a `pip install -e` into a non-venv prefix moves them around, and CI's 3.10
    job does exactly that). Instead both schemes are redirected: one at a
    synthetic package directory holding a real bundled import name, the other at
    an empty directory. Run both ways round, so each scheme is shown to be
    consulted on its own.
    """
    import sysconfig

    module = _packaging_module()
    # A name the derivation genuinely reaches from `[bundled]`, so the synthetic
    # directory below is classified rather than skipped. Taken from the real
    # output instead of hardcoded — the list is derived, so the test asks for it.
    real_packages, real_includes = module.bundled_force_lists()
    forced = sorted(set(real_packages) | set(real_includes))
    assert forced, "bundled_force_lists() forced nothing; nothing to redirect"
    name = forced[0]

    home = tmp_path / home_scheme
    (home / name).mkdir(parents=True)
    (home / name / "__init__.py").write_text("")
    empty = tmp_path / other_scheme
    empty.mkdir()

    fake = dict(sysconfig.get_paths(),
                **{home_scheme: str(home), other_scheme: str(empty)})
    orig = sysconfig.get_paths
    try:
        sysconfig.get_paths = lambda *a, **kw: fake
        packages, includes = module.bundled_force_lists()
    finally:
        sysconfig.get_paths = orig

    assert name in packages, (
        f"{name!r} is a real package directory under {home_scheme} but was not "
        f"classified into `packages` — {home_scheme} is not being probed, so "
        f"py2app would copy a bare {name}.py shadowing the real package "
        f"(it landed in `includes`: {name in includes})"
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


def test_the_bundled_and_fused_extras_pin_the_same_wheel():
    """`fused` is declared twice on purpose, so the two copies must not drift.

    `[bundled]` is what the packaging derivation reads (setup_py2app.py's
    `bundled_force_lists`, and therefore the reconciliation below) plus what the
    Linux/Windows installers install; `[fused]` is the documented light install
    path — `pip install "fused-render[fused]"` (README, docs/usage.md) and CI's
    `fused-engine` job (`.[dev,fused]`) — which must be able to get the engine
    without the ~650 MB scientific stack. Keeping both is the deliberate call;
    the cost is a duplicated requirement string, and this is the guard that makes
    the duplication safe instead of hopeful. Byte-identical, not merely
    same-version: the version pin and the `python_version` marker are both
    load-bearing, and a mismatch would mean the bundle and the pip path ship
    different engines.
    """
    pp = _pyproject()
    extras = pp["project"]["optional-dependencies"]
    in_bundled = [r for r in extras["bundled"] if _norm(r) == "fused"]
    in_extra = [r for r in extras["fused"] if _norm(r) == "fused"]
    assert len(in_bundled) == 1, (
        "`[bundled]` must declare the `fused` requirement exactly once (the "
        f"packaging force-list derives the engine from it); got {in_bundled}"
    )
    assert len(in_extra) == 1, (
        f"`[fused]` must declare the `fused` requirement exactly once; got {in_extra}"
    )
    assert in_bundled[0] == in_extra[0], (
        "the `fused` requirement in `[bundled]` and in `[fused]` have drifted:\n"
        f"  [bundled] {in_bundled[0]!r}\n  [fused]   {in_extra[0]!r}\n"
        "They must be updated TOGETHER and stay byte-identical. `[bundled]` is "
        "what the packaging derivation reads (so it decides what the DMG ships); "
        "`[fused]` is the documented light install path that gets the engine "
        "without the scientific stack."
    )
    # The `mcp` constraint travels WITH the engine pin, in both extras and
    # byte-identical for the same reason: it exists only because `fused` pulls
    # `mcp` in, and the engine's own floor (`mcp[cli]>=1.0.0`) admits mcp 2.x
    # where `mcp.server.fastmcp` is gone — which breaks `fused app serve`, the
    # command the MCP panel registers globally (SPEC MC-5). One extra carrying
    # the constraint and the other not would mean the DMG and the pip path serve
    # MCP from different libraries.
    mcp_bundled = [r for r in extras["bundled"] if _norm(r) == "mcp"]
    mcp_extra = [r for r in extras["fused"] if _norm(r) == "mcp"]
    assert len(mcp_bundled) == 1 and len(mcp_extra) == 1, (
        "both `[bundled]` and `[fused]` must constrain `mcp` exactly once; got "
        f"{mcp_bundled} and {mcp_extra}"
    )
    assert mcp_bundled[0] == mcp_extra[0], (
        "the `mcp` constraint in `[bundled]` and in `[fused]` have drifted:\n"
        f"  [bundled] {mcp_bundled[0]!r}\n  [fused]   {mcp_extra[0]!r}"
    )


def test_the_fused_pin_reads_the_app_serve_python_seam():
    """The pinned engine must be one whose `app serve` READS
    OPENFUSED_APP_SERVE_PYTHON (openfused #364, released in `2.9.3b7`).

    `fusedcli._wrapper_text` exports that variable in the `fused` wrapper every
    Claude session gets, so the `fused app serve` the MCP panel registers
    (SPEC MC-5) computes on the SAME interpreter page runs use: one venv cache
    key instead of two, and a tool with no declared dependencies running on the
    engine's interpreter instead of a bare stdlib venv. An older engine ignores
    the export in SILENCE — the tools still answer, just out of venvs nothing
    else shares — so the export and the pin are one change, and this is the
    guard that keeps them one. A floor rather than an equality: the next bump
    must not have to come back here.
    """
    from packaging.version import Version

    pinned = [r for r in _pyproject()["project"]["optional-dependencies"]["fused"]
              if _norm(r) == "fused"]
    assert len(pinned) == 1, pinned
    version = pinned[0].split("==", 1)[1].split(";")[0].split(",")[0].strip()
    assert Version(version) >= Version("2.9.3b7"), (
        f"the `fused` pin is {version}, which does not read "
        "OPENFUSED_APP_SERVE_PYTHON; `fusedcli._wrapper_text` exports it, so the "
        "pin must be >= 2.9.3b7 or the export is a silent no-op"
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
    absent = []
    for dist in sorted(_macos_dists()):
        names = by_dist.get(dist)
        if not names:
            # Not installed here, so there is no metadata to map it through and
            # nothing this test can say — it degrades to a no-op per
            # distribution. That silent degradation is the whole reason
            # FUSED_RENDER_REQUIRE_BUNDLED exists: under `pip install -e
            # ".[dev]"` almost every iteration of this loop takes this branch,
            # and the test passes while asserting nearly nothing. An environment
            # that claims to have `[bundled]` must not reach here.
            if _REQUIRE_BUNDLED and dist in _declared_dists_installable_here():
                absent.append(dist)
            continue
        if dist not in _declared_dists_the_BUNDLE_can_have():
            # Declared, installed here, and marker-excluded from the interpreter
            # the DMG is built on — so it cannot be in the bundle and must not be
            # demanded of it. See that helper: this is `tomli` on 3.11.
            continue
        # A dotted entry (google.auth) covers its namespace parent (google).
        if not any(n in forced or any(f.startswith(n + ".") for f in forced)
                   for n in names):
            unreachable.append((dist, sorted(names)))
    assert not absent, (
        "FUSED_RENDER_REQUIRE_BUNDLED is set, so this environment claims to have "
        f"the `[bundled]` extra installed — but these are missing: {absent}. "
        "Without them the reconciliation below skips them one by one and proves "
        "nothing. Install `.[bundled]` here, or unset the variable and accept "
        "that this run is not a real check."
    )
    assert not unreachable, (
        "these distributions are promised but the macOS bundle would not carry "
        f"them: {unreachable}. Either the derivation in setup_py2app.py missed "
        "them, or they belong in BUNDLED_EXCLUDED with their measured size."
    )


@pytest.mark.parametrize("require", [False, True])
def test_require_bundled_turns_the_per_distribution_skip_into_a_failure(
    monkeypatch, require
):
    """The flag is the mechanism, so it needs its own proof.

    A promised distribution that is simply not installed is the case the
    reconciliation cannot speak about, and its silence is indistinguishable from
    success. Both directions are pinned: without the flag an absent distribution
    is tolerated (that is what makes an ordinary `[dev]` run possible at all),
    with it set the same absence fails. Otherwise the flag could stop working —
    a rename, a moved read — and every job that relies on it would keep passing
    while checking nothing, which is the exact failure it was added to prevent.
    """
    fake = "a-distribution-nobody-has-installed"
    this = sys.modules[__name__]
    monkeypatch.setattr(this, "_REQUIRE_BUNDLED", require)
    monkeypatch.setattr(this, "_macos_dists", lambda: frozenset({fake}))
    monkeypatch.setattr(
        this, "_declared_dists_installable_here", lambda: frozenset({fake})
    )
    if require:
        with pytest.raises(AssertionError, match="claims to have"):
            test_the_bundle_ships_everything_it_does_not_explicitly_exclude()
    else:
        test_the_bundle_ships_everything_it_does_not_explicitly_exclude()


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


def test_the_learn_page_only_promises_what_the_app_ships():
    """The Learn page's library table is a promise; keep it true (D177).

    `core_apps/learn/check_libs.py` is a hand-written mirror of `[bundled]` —
    the shape that always diverges — and it is read by USERS deciding what they
    may import. Divergence here does not break a build; it tells someone
    `polars` is available and then fails their page in the packaged app, where
    `pip install` is not a thing they can do (D176).

    Only one direction is asserted. Every name promised must be shipped; the
    reverse is a curation choice, since the app's own plumbing (fastapi,
    packaging, tomli, pyobjc, the engine itself) is not something a page should
    be told to import.
    """
    path = os.path.join(_REPO, "core_apps", "learn", "check_libs.py")
    spec = importlib.util.spec_from_file_location("_learn_check_libs", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    promised = {_norm(n) for _group, names in module.SUPPORTED for n in names}
    lying = sorted(promised - _macos_dists())
    assert not lying, (
        f"core_apps/learn/check_libs.py tells users they can import {lying}, "
        "which the app does not ship. They render as '—' in the live table and "
        "as ModuleNotFoundError in their page. Drop them from SUPPORTED (and "
        "from the static table in core_apps/learn/index.html), or put them back "
        "in `[bundled]`."
    )


def _learn_groups() -> list[tuple[str, list[str]]]:
    """`core_apps/learn/check_libs.py`'s SUPPORTED, imported."""
    path = os.path.join(_REPO, "core_apps", "learn", "check_libs.py")
    spec = importlib.util.spec_from_file_location("_learn_check_libs", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.SUPPORTED)


# (file, section-start marker, section-end marker, row pattern). The markers are
# what keep this honest: an unanchored row pattern matches ANY `<tr class="lg-h">`
# or ANY `- **Label:** …` bullet in the file, so an unrelated note added to the
# skill would fail a test whose message claims the library list drifted — a
# false accusation is worse than no check, because the next reader "fixes" the
# wrong thing. Both markers must exist, which is itself asserted: a renamed
# anchor must break loudly rather than silently narrow the section to nothing.
_DOC_LIBRARY_LISTS = [
    # The Learn page's static table — what a user sees before the live versions
    # arrive, and all they ever see outside the app.
    (
        os.path.join("core_apps", "learn", "index.html"),
        '<table class="apitable" id="libTable">',
        "</table>",
        r'<tr><td class="lg-h">([^<]+)</td><td>(.*?)</td></tr>',
    ),
    # The authoring skill's list — what an agent writing a page reads.
    (
        os.path.join("skills", "fused-render-authoring", "SKILL.md"),
        "### Available Python libraries",
        "Anything outside this set",
        r"(?m)^- \*\*([^:*]+):\*\* (.*)$",
    ),
]


@pytest.mark.parametrize(
    "relpath,start,end,pattern", _DOC_LIBRARY_LISTS,
    ids=[row[0] for row in _DOC_LIBRARY_LISTS],
)
def test_the_documented_library_list_matches_check_libs(relpath, start, end, pattern):
    """Three copies of one promise; pin them to each other (D177).

    `check_libs.py`'s SUPPORTED is the source of truth — it is the only one that
    RUNS, and the previous test pins it to what the bundle really ships. The
    Learn page's static table and the authoring skill's bullet list restate it
    for a human and for an agent respectively, which is exactly the shape that
    rotted here: `polars`, `scipy`, `matplotlib`, `geopandas` and the rest were
    advertised in all three long after anyone would have wanted to check.

    Deriving them at build time was considered and rejected: the packaged app
    ships no `pyproject.toml`, the skill is read as plain Markdown outside any
    build, and `importlib.metadata` cannot tell a promised library from a
    transitive one. So this is D177's third rung — the copies stay, and their
    divergence is a test failure.
    """
    import html as _html
    import re

    with open(os.path.join(_REPO, relpath), encoding="utf-8") as f:
        text = f.read()
    begin = text.find(start)
    assert begin >= 0, f"{relpath} no longer contains {start!r}; re-anchor this test"
    finish = text.find(end, begin + len(start))
    assert finish >= 0, f"{relpath} no longer contains {end!r}; re-anchor this test"
    section = text[begin:finish]
    found = [
        (_html.unescape(group).strip(),
         [_norm(n) for n in re.findall(r"<code>([^<]+)</code>|`([^`]+)`", body)
          for n in [n[0] or n[1]]])
        for group, body in re.findall(pattern, section)
    ]
    expected = [(g, [_norm(n) for n in names]) for g, names in _learn_groups()]
    assert found == expected, (
        f"{relpath}'s library list has drifted from core_apps/learn/check_libs.py's "
        f"SUPPORTED, which is the one that actually runs.\n  doc:       {found}\n"
        f"  check_libs: {expected}\n"
        "Update the doc (or SUPPORTED, if the app's contents changed). Users and "
        "agents read these lists to decide what they may import."
    )


def test_no_doc_claims_a_shipped_package_was_removed():
    """The other half of the promise: what a doc says is GONE must be gone.

    `test_the_documented_library_list_matches_check_libs` pins the positive
    list — what the app has. Nothing pinned the negative one, and that is
    exactly where this rotted: two docs went on saying `fpdf2` had left
    `[bundled]` after the removal was reversed, including
    `fused-render-authoring`, which is what an agent reads to decide what it may
    import. The consequence is not a broken build but a worse one — an agent
    steered away from a package that is right there, or into declaring a folder
    manifest it does not need, which is the precise outcome the reversal existed
    to prevent.

    Scanned as a set of names rather than by parsing prose: any distribution
    `[bundled]` or the core dependencies actually ship must not appear in a
    sentence claiming things were removed. Cheap, and it would have caught this.
    """
    import re

    shipped = _declared_dists()
    offenders = {}
    for relpath, marker in [
        (os.path.join("skills", "fused-render-authoring", "SKILL.md"),
         "no longer does:"),
        (os.path.join("core_apps", "learn", "check_libs.py"),
         "What is deliberately NOT here:"),
    ]:
        with open(os.path.join(_REPO, relpath), encoding="utf-8") as f:
            text = f.read()
        start = text.find(marker)
        assert start >= 0, f"{relpath} no longer contains {marker!r}; re-anchor"
        # The claim runs to the end of its sentence.
        sentence = text[start:start + 400].split(".")[0]
        named = {_norm(n) for n in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", sentence)}
        wrong = sorted(named & shipped)
        if wrong:
            offenders[relpath] = wrong
    assert not offenders, (
        f"{offenders} are named as REMOVED but `[bundled]`/the core dependencies "
        "still ship them, so the doc tells a reader (or an authoring agent) a "
        "package is unavailable when it is importable with no declaration and no "
        "install. Fix the doc, or remove the package for real."
    )


# ------------------------------------------------- templates vs the real bundle


def _template_folders():
    """Template folders that declare an environment (SPEC PY-16)."""
    return sorted(
        name for name in os.listdir(_TEMPLATES)
        if os.path.isfile(os.path.join(_TEMPLATES, name, "pyproject.toml"))
    )


def _raw_declaration(folder):
    """The folder's dependencies verbatim, markers included — all platforms.

    Markers are deliberately not evaluated: necessity is a property of the SOURCE
    and has to hold on every platform, not on the machine running pytest.

    Uses the module-level `tomllib` from `_import_toml()` — a bare local
    `import tomllib` here shadowed it and raised ModuleNotFoundError on 3.10,
    where the parser comes from the `tomli` dependency instead.
    """
    with open(os.path.join(_TEMPLATES, folder, "pyproject.toml"), "rb") as f:
        meta = tomllib.load(f)
    return list(meta.get("project", {}).get("dependencies", []))


@pytest.mark.parametrize("folder", _template_folders())
def test_a_declaration_is_needed_for_what_the_MACOS_BUNDLE_lacks(folder):
    """Necessity is judged against the bundle, not against `[bundled]` (D176).

    macOS ships the narrowest set, so it is the binding constraint: a dependency
    absent there needs a declaration, whatever the other platforms have. Judging
    this against `[bundled]` is what deleted slides' `python-pptx` header and
    shipped a DMG that told the user to `pip install` on a read-only app.

    Scope moved from the file to the FOLDER with the environment itself: it is
    the folder that now costs a venv build and a download.
    """
    declared = {_norm(d) for d in _raw_declaration(folder)}
    if not declared:
        return
    justified = sorted(declared - _macos_dists())
    assert justified, (
        f"fused_render/templates/{folder}/pyproject.toml declares "
        f"{sorted(declared)}, all of which the macOS bundle already ships — so it "
        "only costs a venv build and a download. Delete the file (and its lock)."
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


@pytest.mark.parametrize("folder", _template_folders())
def test_a_declaration_names_the_sibling_that_actually_works(folder):
    """Catch "importable but non-functional" for the pairs where it can happen.

    `latex/engine.py` declared `pypandoc` while calling
    `pypandoc.convert_file` — which needs the pandoc binary the plain
    distribution does not ship. `docs/docs.py` had it right, and the two sat side
    by side for a while, because every other invariant here is satisfied by a
    module that imports.
    """
    for raw in _raw_declaration(folder):
        dist = _norm(raw)
        better = _MUST_USE_HEAVIER_SIBLING.get(dist)
        assert better is None, (
            f"fused_render/templates/{folder}/pyproject.toml declares {dist!r}, "
            "which installs the import name but not the payload behind it — use "
            f"{better!r} instead. The venv would build cleanly and fail at runtime."
        )


# -- the standard library ------------------------------------------------------
#
# py2app ships the stdlib it TRACED from app_entry.py. The bundled interpreter
# is the base of every environment the app builds (PY-18), so that subset is
# inherited by every project venv, every runner venv, and every third-party
# package inside them — which is how a DMG shipped without `filecmp` and an MLX
# load died in transformers with a message about the model.


def test_the_bundle_ships_the_whole_importable_stdlib():
    """Every stdlib module this host can resolve is forced in, or excluded WITH
    A REASON. The same rule the distributions already live under: never just
    absent."""
    module = _packaging_module()
    forced = set(module.OPTIONS["packages"]) | set(module.OPTIONS["includes"])

    absent = []
    for name in sorted(sys.stdlib_module_names):
        if name in module.STDLIB_EXCLUDED or name.startswith("__"):
            continue
        if name in sys.builtin_module_names:
            continue  # compiled into the interpreter; no file to carry
        if importlib.util.find_spec(name) is None:
            continue  # another platform's module (msvcrt, winreg) on this host
        if name not in forced:
            absent.append(name)

    assert absent == [], (
        "these stdlib modules would ship only if the app itself happened to "
        "import them: " + ", ".join(absent)
    )
    assert "filecmp" in forced, "the module whose absence started this"


#: Excluded names CPython has since REMOVED, and the version that removed them.
#: The DMG is built on 3.12 (`envinstall.SCRIPT_PYTHON_VERSION`), while this
#: suite runs on 3.10–3.13, so an entry can be perfectly valid for the build
#: interpreter and absent from the one asserting about it — `lib2to3` is gone in
#: 3.13. Listed rather than waved through so a typo is still caught: an
#: exclusion has to be a real module on SOME version we know about.
_STDLIB_REMOVED_UPSTREAM = {"lib2to3": (3, 13)}


def test_every_stdlib_exclusion_is_real_and_reasoned():
    """An exclusion is a claim about a module that EXISTS, with a reason someone
    can disagree with. A typo'd name would otherwise silently widen the list."""
    module = _packaging_module()
    for name, reason in module.STDLIB_EXCLUDED.items():
        if name in _STDLIB_REMOVED_UPSTREAM:
            gone_in = _STDLIB_REMOVED_UPSTREAM[name]
            assert sys.version_info[:2] >= gone_in or name in sys.stdlib_module_names, (
                f"{name} is listed as removed in {gone_in} but is missing here too")
            continue
        assert name in sys.stdlib_module_names, f"{name} is not a stdlib module"
        assert isinstance(reason, str) and len(reason) > 15, (
            f"{name} is excluded without a usable reason: {reason!r}")
    assert "tkinter" in module.STDLIB_EXCLUDED, (
        "build_dmg.sh prunes Tcl/Tk, so tkinter must stay excluded or the "
        "stdlib check would fail every build")


def test_the_stdlib_split_puts_packages_and_modules_in_the_right_list():
    """A stdlib PACKAGE forced through `includes` would ship only the submodules
    modulegraph traced — the same partial-copy problem one level down."""
    module = _packaging_module()
    for name in module.STDLIB_PACKAGES:
        spec = importlib.util.find_spec(name)
        assert spec.submodule_search_locations is not None, f"{name} is not a package"
    for name in module.STDLIB_INCLUDES:
        spec = importlib.util.find_spec(name)
        assert spec.submodule_search_locations is None, (
            f"{name} is a package and must be forced whole, not traced")


def test_no_windows_only_stdlib_module_reaches_a_macos_build():
    """py2app fails on an `includes` entry it cannot resolve, so the derivation
    has to filter by what this host can actually find."""
    module = _packaging_module()
    named = set(module.STDLIB_PACKAGES) | set(module.STDLIB_INCLUDES)
    for name in ("msvcrt", "winreg", "winsound", "_winapi", "nt"):
        assert name not in named


def test_the_build_verifies_the_stdlib_in_a_venv_not_just_in_the_app():
    """The regression guard has to run where the bug appeared.

    A check against `Contents/MacOS/python` alone would have passed on a bundle
    whose venvs were broken, which is exactly the shape of 4b-bis's earlier bug —
    so build_dmg.sh builds a venv on the bundled interpreter and re-runs it.
    """
    script = os.path.join(_REPO, "scripts", "build_dmg.sh")
    with open(script, encoding="utf-8") as f:
        text = f.read()
    assert "STDLIB_EXPECTED" in text, "the stdlib completeness check is gone"
    assert "-m venv --without-pip" in text, (
        "the stdlib check must also run through a venv built on the bundled "
        "interpreter — that is the path PY-18 uses and the one that failed")
    assert 'for STDLIB_WHO in bundled venv' in text
