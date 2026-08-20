"""Which core templates may declare an environment, and what it must contain
(SPEC PY-16/PY-17, D172).

Under the fused engine a script in a folder with **no** `pyproject.toml` runs on
the app's own interpreter, which has `[bundled]` + the core `dependencies` — so
declaring one there buys nothing and costs a download. A folder that DOES
declare one gives every `.py` beneath it a venv containing exactly what the
declaration names and nothing else, so a dependency any of them forgot is simply
absent.

That makes the declaration a three-part invariant, and every part is derived
from the source here rather than written down, because a written-down version is
what failed before: the predecessor of this file pinned `DEFAULT_REQUIREMENTS`
against `[bundled]` with a hand-kept list of deltas, and before that a *comment*
claimed the two were in sync while being wrong in ten places.

  1. **No file may carry a `# /// script` header.** Headers are no longer read
     at all (PY-16), so one left behind is inert *and looks correct*, which is
     why it survives review — the same class of defect as the never-read headers
     this file was originally written to catch (D170's on
     `map/vector_tile_server.py`, and `geotiff/_tiff_core.py`'s own, D174). The
     engine reports an orphan header at run time; this catches it at build time.
  2. **A declaration must be necessary** — it has to name something the app's
     interpreter does not already have. Enforced in
     tests/test_bundle_contents.py, because on macOS that means the BUNDLE's
     contents rather than `[bundled]`'s promises (D176).
  3. **A declaration must be complete** — every `[bundled]`/core distribution
     imported by any `.py` under the declaring folder, at any nesting depth, is
     named in it. This is the half with teeth: it is what caught `pano/pano.py`
     importing numpy and pillow while declaring only `py360convert`, which
     worked solely because a baseline set used to be installed alongside every
     header. With that baseline gone, an incomplete declaration is a broken
     template — and a silent one (a guarded import degrades, an unguarded one
     500s a tile request), never a startup error anyone sees.
  4. **A template that imports something the app does NOT ship must declare an
     environment at all** — the converse of 2, and the half that was missing.
     Parts 1-3 only ever constrain a folder that already has a
     `pyproject.toml`, so shrinking `[bundled]` (D276) could have quietly left
     `map`, `vector`, `slides` and friends importing distributions no packaged
     app carries, with every test still green and the failure landing on a user
     as `ModuleNotFoundError` — or, worse, as `pip install rasterio` advice a
     DMG cannot follow, which is exactly the D176 defect. Nothing about
     "necessary and complete" catches a template that declares *nothing*, so
     this rule does.

The scope of part 3 is what the folder rule changed: it used to be "each entry
point that can execute this file", walked from the source through `_venv_roots`,
because a header decided ONE file's venv. The venv is now the folder's, so the
set is structural — every file under it, no walk required. `_runpython_targets`
and `_module_refs` survive because they answer a question that is still derived
rather than tabulated: which files something can actually hand to `run_python`.

Under PY-18 a declaration also *triggers a download*, so each of these costs a
user-visible wait rather than only disk: an unnecessary one means a progress bar
for an environment nothing will ever import from.
"""
import ast
import functools
import importlib.util
import os
import re

import pytest


# tomllib is 3.11+, and so is the engine these tests describe: the `[fused]`
# extra's wheel is marked `python_version >= "3.11"`, so on 3.10 the package is
# never installed, `engine.available()` is False and no script venv is ever
# built. There is nothing here for 3.10 to constrain, so skipping the whole
# module is the honest answer rather than a workaround — the same reasoning as
# `test_engine.py`'s `requires_tomllib`, applied at import time because this
# module needs the parser to collect at all. The `fused-engine` CI job runs on
# 3.11, so the module still runs where it means something.
tomllib = pytest.importorskip(
    "tomllib", reason="tomllib (PEP 723 parsing) needs Python 3.11+"
)

_PEP723 = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_REPO, "fused_render", "templates")

# import name -> distribution name, for every distribution whose PRESENCE ON
# THE APP INTERPRETER the rules below reason about. Two kinds live here:
#
#   * everything the app's own interpreter provides (`[bundled]` + core
#     `dependencies`) — part 3 needs these, because a script venv gets only what
#     its folder declares, so an undeclared import of something the app happens
#     to have is simply absent there;
#   * the distributions `[bundled]` USED to promise and no longer does (polars,
#     matplotlib, scipy, the PDF stack, the geo stack — D276). Part 4 needs
#     these: dropping them from the map at the same time as from the extra would
#     have made every template that imports one silently exempt from BOTH halves,
#     which is the failure the extra was shrunk under, not a consequence of it.
#
# Nothing else is checked: this is a guard on what the app ships versus what a
# script venv would contain, not a general dependency linter (on-demand binary
# fetches like pxr/pypandoc/typst and genuinely optional readers like rawpy have
# their own mechanisms and their own tests).
#
# `test_the_import_map_covers_everything_the_app_ships` keeps the first half
# honest — a distribution added to `[bundled]` with no entry here would be
# invisible to the completeness rule and silently exempt from it.
_IMPORT_TO_DIST = {
    "numpy": "numpy",
    "pandas": "pandas",
    "pyarrow": "pyarrow",
    "requests": "requests",
    "duckdb": "duckdb",
    "polars": "polars",
    "matplotlib": "matplotlib",
    "mpl_toolkits": "matplotlib",
    "scipy": "scipy",
    "PIL": "pillow",
    "openpyxl": "openpyxl",
    "shapely": "shapely",
    "geopandas": "geopandas",
    "pptx": "python-pptx",
    "fpdf": "fpdf2",
    "msgpack": "msgpack",
    "rasterio": "rasterio",
    "rio_tiler": "rio-tiler",
    "zarr": "zarr",
    # Never named in `[bundled]`, but on the app interpreter until D276 all the
    # same — they arrived with geopandas and rasterio, which is precisely why
    # `map/vector_engine.py` could import both directly and nothing complained.
    # Mapped now so that borrowed edge is checked instead of assumed: a template
    # importing either must say so.
    "pyproj": "pyproj",
    "pyogrio": "pyogrio",
    "fitz": "pymupdf",
    "pymupdf": "pymupdf",
    "pikepdf": "pikepdf",
    "drain3": "drain3",
    "botocore": "botocore",
    # `google` is a namespace package shared with every other google library, so
    # the top-level name doesn't identify google-auth on its own — but it is the
    # only `google` distribution the app ships, so here the mapping is exact.
    "google": "google-auth",
    # Core `dependencies`. A template importing one of these is unusual but not
    # forbidden, and under a header it would be just as absent as a bundled one.
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "websockets": "websockets",
    "multipart": "python-multipart",
    "httpx": "httpx",
    # Only installed on <3.11 (it backports 3.11's stdlib tomllib), so on 3.11+
    # it maps a name nothing provides — harmless, and listing it is what keeps
    # `test_the_import_map_covers_everything_the_app_ships` honest either way.
    "tomli": "tomli",
    # PEP 508 parsing for `engine.app_satisfies`, which decides whether the app
    # interpreter already meets a script's header (and so whether a venv is needed
    # at all). Import name and distribution name coincide.
    "packaging": "packaging",
    # AppKit, for the macOS clipboard bridge (shell/pasteboard/_darwin.py).
    # Only installed on darwin, so on Linux and Windows these map names nothing
    # provides — the same harmless shape as `tomli` above, and listing them is
    # what keeps `test_the_import_map_covers_everything_the_app_ships` honest on
    # every platform. One distribution, three import names: pyobjc splits its
    # frameworks across packages, and Cocoa is the one that carries all three.
    "AppKit": "pyobjc-framework-cocoa",
    "Foundation": "pyobjc-framework-cocoa",
    "Cocoa": "pyobjc-framework-cocoa",
    # Native capture (SPEC §44). Same split, two more distributions: the
    # ScreenCaptureKit wheel carries only `ScreenCaptureKit`, and the
    # AVFoundation one brings `Quartz`, `CoreMedia` and `CoreAudio` with it.
    "ScreenCaptureKit": "pyobjc-framework-screencapturekit",
    "AVFoundation": "pyobjc-framework-avfoundation",
    "Quartz": "pyobjc-framework-avfoundation",
    "CoreMedia": "pyobjc-framework-avfoundation",
    "CoreAudio": "pyobjc-framework-avfoundation",
    # The engine itself (a `[bundled]` requirement so the macOS force-list
    # derives it — see pyproject and setup_py2app.py). Mapped, so
    # `test_the_import_map_covers_everything_the_app_ships` stays satisfied, but
    # exempt from the COMPLETENESS half below — see _COMPLETENESS_EXEMPT.
    "fused": "fused",
}

# Distributions the app ships that a template may import WITHOUT declaring in its
# header. The completeness rule exists because a script venv contains only what
# the header names, so an undeclared import of something the app happens to have
# is simply absent there. For `fused` that reasoning does not apply: it is the
# engine that RUNS scripts, not payload a script imports to do its work, and a
# script venv is expected not to contain it — `engine.py`'s generated epilogue
# wraps its own `import fused` in `except ImportError` for exactly that reason.
# Declaring it in a header would instead install the pinned wheel and its whole
# dependency tree into that venv, which is the opposite of what a header is for.
#
# Every `import fused` in the core templates — the only files this module scans —
# is guarded by `except ImportError`, so the exemption costs nothing here. It is
# deliberately NOT a claim that no caller can depend on `import fused`
# succeeding: a USER script with a PEP 723 header legitimately can (e.g. reading
# a secret through `fused`), and for that case this exemption is silent. That gap
# is a known product question being decided separately; it is not something this
# exemption resolves.
#
# Kept as a named exemption rather than a missing mapping so it is visible and
# reasoned — the same shape as the on-demand binary fetches (pxr/pypandoc/typst)
# and optional readers (rawpy) noted above.
_COMPLETENESS_EXEMPT = {"fused"}

# (template file, distribution) -> why importing it does NOT oblige the folder to
# declare an environment (part 4 only).
#
# The rule part 4 enforces is "this template cannot work unless the app has it".
# An import that only ever runs when SOMEONE ELSE has already imported the module
# does not meet that bar: there is nothing for a venv of our own to supply.
# Kept as a stated delta rather than an AST heuristic for guarded imports —
# `try: import x / except ImportError` is ambiguous (it is just as often a
# degraded path we do not want silently accepted), while this is one line per
# case with a reason, checked for staleness by
# `test_every_optional_import_exemption_is_real_and_reasoned` (D177: if a hand
# list must exist, reduce it to the deltas and make each one justify itself).
#
# Paths are posix-separated, relative to fused_render/templates/.
_OPTIONAL_IMPORTS = {
    ("notebook/kernel_body.py", "matplotlib"): (
        "the kernel harvests figures the USER's own cell created, behind "
        '`if "matplotlib" not in sys.modules: return` — it never imports '
        "matplotlib itself, and a notebook whose interpreter lacks it simply "
        "produces no inline figures. Declaring it would be worse than useless: "
        "the notebook kernel runs arbitrary user code, so putting the folder in "
        "a venv would replace the app interpreter's whole stack with one package."
    ),
}

# NOTE: there is deliberately no hand-maintained "this file inherits that
# file's venv" table here. The first version of this test had one, listing a
# single pair — and it promptly greenlit a `rasterio` header placed on
# `map/vector_tile_server.py`, a file the engine never passes to run_python
# (map_render.py imports it and calls its main()), so the header was inert and
# the gap it was meant to close stayed open. The relationships are derived from
# the source instead — see _invoked_by / _venv_roots.


def _pyproject() -> dict:
    with open(os.path.join(_REPO, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)


def _norm(requirement: str) -> str:
    """`"fpdf2>=2.8.7"` / `"fused @ https://… ; python_version >= '3.11'"` -> name."""
    return re.split(r"[<>=!~;\[ ]", requirement.strip())[0].lower().replace("_", "-")


def _has_script_header(text: str) -> bool:
    """Does this file still carry a `# /// script` block? Never read any more."""
    return bool(_PEP723.search(text))


def _project_deps(folder: str) -> set[str]:
    """Distributions declared by `<_TEMPLATES>/<folder>/pyproject.toml`, or {}.

    Markers are deliberately NOT evaluated: these invariants are properties of
    the SOURCE and must hold on every platform, not of the machine running
    pytest. This already bit once under the old per-file rule — a
    `sys_platform == 'darwin'` header read as EMPTY everywhere but macOS, so on
    the Linux `fused-engine` job the file looked declaration-less and both
    invariants skipped it silently. Same forward guard here: the next
    marker-scoped dependency must not be able to be incomplete behind a green
    suite.
    """
    path = os.path.join(_TEMPLATES, folder, "pyproject.toml") if folder else None
    if not path or not os.path.isfile(path):
        return set()
    with open(path, "rb") as f:
        meta = tomllib.load(f)
    return {_norm(d) for d in meta.get("project", {}).get("dependencies", [])}


def _declaring_folder(relpath: str) -> str:
    """The template folder whose declaration governs `relpath`.

    The top-level template directory — `projectenv` treats an immediate child of
    a template root as the project root, so nesting below it changes nothing.
    """
    return relpath.split(os.sep)[0] if os.sep in relpath else ""


def _self_managed(text: str) -> bool:
    """Does this file build and run under a venv of its OWN (D174)?

    Derived from the source — a module-level `DAEMON_VENV` assignment — rather
    than listed, for the same reason nothing else here is listed. The geotiff and
    zarr_aoi tile daemons re-exec their heavy half under that venv, so their
    imports are not the project environment's problem and requiring the folder
    to declare them would download the same packages twice.
    """
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DAEMON_VENV" for t in node.targets
        ):
            return True
    return False


def _imported_dists(text: str) -> set[str]:
    """`[bundled]`/core distributions this file imports, at any nesting depth.

    ast, not a regex: the imports that matter most here are the *function-level*
    ones (a tile handler's `import rasterio`), which is exactly what a naive
    "imports at the top of the file" check misses.

    `_COMPLETENESS_EXEMPT` distributions are dropped: they are mapped (so the
    coverage test above stays honest) but deliberately not required in a header.
    """
    tree = ast.parse(text)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return {
        _IMPORT_TO_DIST[m]
        for m in mods
        if m in _IMPORT_TO_DIST and _IMPORT_TO_DIST[m] not in _COMPLETENESS_EXEMPT
    }


def _template_files() -> list[str]:
    out = []
    for dirpath, _dirnames, filenames in os.walk(_TEMPLATES):
        if "__pycache__" in dirpath or os.sep + "vendor" in dirpath:
            continue
        out += [
            os.path.relpath(os.path.join(dirpath, f), _TEMPLATES)
            for f in filenames
            if f.endswith(".py")
        ]
    return sorted(out)


def _module_refs(text: str) -> tuple[set[str], list[str]]:
    """(top-level names this file imports, string literals it contains).

    The second half is how a *spawn* is spotted: usd/reader.py doesn't import
    convert_worker, it builds a path to "convert_worker.py" and hands it to
    subprocess. Docstrings are excluded — a module docstring that merely
    mentions a sibling ("served by a warm daemon (vector_tile_server.py)") is
    prose, not an invocation, and counting it would invent invokers.
    """
    tree = ast.parse(text)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))

    mods, literals = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            literals.append(node.value)
    return mods, literals


@functools.lru_cache(maxsize=1)
def _template_graph() -> dict:
    """Per-file header deps, imported distributions, and in-template invokers.

    Built once for the whole module (the parametrized test would otherwise
    re-parse every template file once per case).
    """
    files = _template_files()
    texts = {}
    for relpath in files:
        with open(os.path.join(_TEMPLATES, relpath), encoding="utf-8") as f:
            texts[relpath] = f.read()

    refs = {r: _module_refs(t) for r, t in texts.items()}
    invoked_by = {r: set() for r in files}
    for relpath in files:
        directory = os.path.dirname(relpath)
        basename = os.path.basename(relpath)
        modname = basename[: -len(".py")]
        for other in files:
            if other == relpath or os.path.dirname(other) != directory:
                continue
            mods, literals = refs[other]
            # Two ways one template file puts another one on an interpreter:
            # importing it as a sibling module, or naming its file to spawn it.
            if modname in mods or any(basename in lit for lit in literals):
                invoked_by[relpath].add(other)

    return {
        "files": files,
        "header": {r: _has_script_header(t) for r, t in texts.items()},
        "self_managed": {r: _self_managed(t) for r, t in texts.items()},
        "imports": {r: _imported_dists(t) for r, t in texts.items()},
        "invoked_by": invoked_by,
    }


@functools.lru_cache(maxsize=1)
def _runpython_targets() -> frozenset[str]:
    """Template .py files that something can actually hand to `run_python`.

    Derived, not listed: a `.py` is an entry point when a NON-.py file in its own
    template folder names it — which is what a `fused.runPython('./x.py')` call
    site in the template's .html is, and equally a registry/manifest reference.
    Sibling .py files are excluded on purpose: one module importing another is
    exactly the relationship that does NOT make the importee an entry point.
    """
    targets = set()
    for dirpath, _dirnames, filenames in os.walk(_TEMPLATES):
        if "__pycache__" in dirpath or os.sep + "vendor" in dirpath:
            continue
        prose = ""
        for name in filenames:
            if name.endswith(".py"):
                continue
            try:
                with open(os.path.join(dirpath, name), encoding="utf-8", errors="replace") as f:
                    prose += f.read()
            except OSError:
                continue
        for name in filenames:
            if name.endswith(".py") and name in prose:
                targets.add(os.path.relpath(os.path.join(dirpath, name), _TEMPLATES))
    return frozenset(targets)


@functools.lru_cache(maxsize=1)
def _app_dists() -> frozenset[str]:
    """What the app's own interpreter provides: `[bundled]` + core `dependencies`.

    Read from pyproject.toml, never restated: a second copy of the list is the
    exact failure the predecessor of this file existed to prevent.
    """
    pp = _pyproject()
    return frozenset(
        {_norm(d) for d in pp["project"]["optional-dependencies"]["bundled"]}
        | {_norm(d) for d in pp["project"]["dependencies"]}
    )


@functools.lru_cache(maxsize=1)
def _bundle_dists() -> frozenset[str]:
    """What a PACKAGED app's interpreter really has: `_app_dists()` minus what
    the build holds back (`BUNDLED_EXCLUDED`, DM-2).

    Read from `scripts/setup_py2app.py` rather than restated, for the same
    reason `_app_dists()` reads pyproject.toml: the exclusion mechanism is empty
    today, and a second copy of an empty list is exactly the thing that is wrong
    the day it stops being empty.
    """
    path = os.path.join(_REPO, "scripts", "setup_py2app.py")
    spec = importlib.util.spec_from_file_location("_setup_py2app_for_reqs", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return _app_dists() - {_norm(n) for n in module.BUNDLED_EXCLUDED}


def test_the_import_map_covers_everything_the_app_ships():
    """Every app distribution must be reachable through `_IMPORT_TO_DIST`.

    Without this, adding a distribution to `[bundled]` quietly exempts it from
    the completeness half below — the check would keep passing while no longer
    checking that dependency at all.
    """
    unmapped = sorted(_app_dists() - set(_IMPORT_TO_DIST.values()))
    assert not unmapped, (
        f"{unmapped} are in `[bundled]`/core `dependencies` but have no entry in "
        "_IMPORT_TO_DIST, so a template importing one would not be checked. Add "
        "the import name -> distribution mapping."
    )


# Part 2 — a header must be NECESSARY — lives in tests/test_bundle_contents.py,
# not here. Necessity has to be judged against what the macOS BUNDLE ships
# (setup_py2app.py), and judging it against `[bundled]` is the bug that shipped
# a DMG telling the user to `pip install python-pptx` (D176). One home for it.


@pytest.mark.parametrize("relpath", _template_files())
def test_no_template_file_carries_a_script_header(relpath):
    """Part 1: headers are never read (PY-16), so one left behind is inert.

    Inert while looking entirely correct, which is why this class of defect
    survives review — D170 shipped one on `map/vector_tile_server.py` and
    `geotiff/_tiff_core.py` carried a full, accurate, never-read list of its own
    (D174). Every one of those is now the DEFAULT state, so the rule is simply
    that no header may exist: dependencies belong in the folder's
    `pyproject.toml`.

    The engine reports an orphan header at run time rather than ignoring it; this
    catches it before it ships.
    """
    graph = _template_graph()
    assert not graph["header"][relpath], (
        f"{relpath} carries a `# /// script` header, which is never read: "
        "dependencies are declared once per folder in `pyproject.toml` "
        "(SPEC PY-16). Move what it names into "
        f"`fused_render/templates/{_declaring_folder(relpath)}/pyproject.toml` "
        "(and re-lock), or delete the block if the app's own interpreter already "
        "has it."
    )


@pytest.mark.parametrize("relpath", _template_files())
def test_a_declared_environment_is_complete(relpath):
    """Part 3: a declaration must be COMPLETE (PY-16).

    Every `.py` under the declaring folder shares its venv, so the declaration
    has to cover all of them — not just the entry point. That scope is
    STRUCTURAL now: the environment belongs to the folder, so there is no
    per-entrypoint walk to get wrong (which is what the old `_venv_roots` did,
    and what let a dependency declared on a helper module read as covered).

    A folder with no declaration runs on the app's interpreter, where every
    distribution here is present by definition; there is nothing a venv could be
    missing. A file that manages its OWN venv (D174) is exempt for the opposite
    reason — its imports never reach the project environment.
    """
    graph = _template_graph()
    folder = _declaring_folder(relpath)
    declared = _project_deps(folder)
    if not declared:
        return  # runs on the app's interpreter — it has all of these
    if graph["self_managed"][relpath]:
        return  # re-execs under DAEMON_VENV; see `_self_managed`

    missing = sorted(graph["imports"][relpath] - declared)
    assert not missing, (
        f"{relpath} imports {missing}, which the venv built from "
        f"`fused_render/templates/{folder}/pyproject.toml` would not contain: a "
        "declaration is the COMPLETE dependency list, with no baseline unioned "
        "in (D172). Add them there and re-lock — or, if that template needs "
        "nothing outside `[bundled]`, delete the pyproject.toml entirely so the "
        "folder runs on the app's own interpreter."
    )


def test_the_runtime_module_alias_table_covers_every_mismatch_here():
    """Two maps of the same fact; make divergence a failure (D177).

    `_IMPORT_TO_DIST` above is the test-side map. `projectenv._MODULE_TO_DIST` is
    the RUNTIME one, read by `executor.explain_missing_module` to decide whether
    a failed import is the thing the folder's manifest asked for. They exist for
    different reasons and cover different sets — the runtime one also carries
    `pypandoc` -> `pypandoc-binary`, which is a template's declaration and never
    the app's — so they are not merged. What must not happen is the runtime one
    lacking a mismatch this one knows about: the enrichment would then silently
    fail to fire for that distribution, and every test of it would still pass on
    its "no match" branch.

    Only the genuine mismatches are checked. Everything else is resolved by
    normalisation on both sides, which needs no table and cannot drift.
    """
    from fused_render import projectenv

    missing = {
        name: dist
        for name, dist in _IMPORT_TO_DIST.items()
        if projectenv.distribution_for_module(name) != _norm(dist)
    }
    assert not missing, (
        f"{missing} are import-name -> distribution mismatches this file knows "
        "about that projectenv._MODULE_TO_DIST cannot resolve, so "
        "executor.explain_missing_module would not connect a failed import of "
        "them to the manifest entry that declared them. Add them there."
    )


def test_every_optional_import_exemption_is_real_and_reasoned():
    """A stale exemption is as misleading as an undocumented one (D176's rule
    for `BUNDLED_EXCLUDED`, applied to the one hand list in this file).

    Both halves are checked: the file must exist and must still import the
    distribution the entry excuses. An exemption that has outlived its import is
    a standing hole in part 4 that nothing else would ever notice.
    """
    graph = _template_graph()
    for (relpath, dist), reason in _OPTIONAL_IMPORTS.items():
        native = relpath.replace("/", os.sep)
        assert native in graph["imports"], (
            f"_OPTIONAL_IMPORTS names {relpath}, which is not a template .py file"
        )
        assert dist in graph["imports"][native], (
            f"_OPTIONAL_IMPORTS excuses {relpath} from declaring {dist!r}, but it "
            "no longer imports it — delete the entry"
        )
        assert reason and len(reason) > 40, (
            f"the exemption of {dist!r} in {relpath} must say why the import does "
            f"not oblige the folder to declare an environment; got {reason!r}"
        )


@pytest.mark.parametrize("relpath", _template_files())
def test_a_template_declares_whatever_the_app_does_not_ship(relpath):
    """Part 4: importing what the app lacks obliges the FOLDER to declare it.

    Parts 1-3 all start from "this folder has a pyproject.toml". None of them
    can see the template that has none and needs one — and that is the state
    every template importing `rasterio`/`geopandas`/`matplotlib`/… was left in
    the moment those left `[bundled]` (D276). The user-visible failure is a
    `ModuleNotFoundError` inside a tile request, or an install hint a packaged
    app cannot act on (D176). So the obligation runs in both directions now:
    declare nothing you already have (test_bundle_contents.py), and declare
    everything you do not.

    Judged against the BUNDLE (`_bundle_dists()`), not against `[bundled]`, for
    the D176 reason: macOS ships the narrowest set and is the binding platform.

    A file that manages its own venv (D174) is exempt — geotiff's and netcdf's
    daemons install their heavy half themselves, and duplicating that in the
    folder manifest would download the same packages twice.
    """
    graph = _template_graph()
    if graph["self_managed"][relpath]:
        return
    optional = {
        dist for (path, dist) in _OPTIONAL_IMPORTS
        if path.replace("/", os.sep) == relpath
    }
    needed = graph["imports"][relpath] - _bundle_dists() - optional
    folder = _declaring_folder(relpath)
    missing = sorted(needed - _project_deps(folder))
    assert not missing, (
        f"{relpath} imports {missing}, which no packaged app ships — so on a DMG "
        "or AppImage this template fails at import with nothing the user can do "
        "about it. Declare them in "
        f"`fused_render/templates/{folder}/pyproject.toml` (SPEC PY-16) and run "
        f"`uv lock` there, or move the import behind a venv the file manages "
        "itself (D174)."
    )


def _declaring_folders() -> list[str]:
    """Template folders that ship a `pyproject.toml`."""
    return sorted(
        name for name in os.listdir(_TEMPLATES)
        if os.path.isfile(os.path.join(_TEMPLATES, name, "pyproject.toml"))
    )


@pytest.mark.parametrize("folder", _declaring_folders())
def test_a_declared_environment_has_something_to_run(folder):
    """A declaration nothing can reach is inert, and inert-but-correct is the
    failure this file has always been about.

    Entry points stay DERIVED from the source rather than tabulated — a `.py` is
    one when a non-.py file in its folder names it, which is what a
    `fused.runPython('./x.py')` call site is (`_runpython_targets`). That is the
    durable half of the old invariant (D177): it is what stops a "this file
    inherits that file's venv" table from being written down and going stale.

    A file that manages its own venv (D174) does not count — its heavy half never
    runs in the project environment, so it cannot be the reason the folder
    declares one.
    """
    graph = _template_graph()
    reachable = [
        r for r in _runpython_targets()
        if _declaring_folder(r) == folder and not graph["self_managed"][r]
    ]
    assert reachable, (
        f"fused_render/templates/{folder}/pyproject.toml declares an environment, "
        "but nothing in that folder is handed to run_python (no non-.py file "
        "names a .py there), so the venv would be built and never used. Either "
        "the entry point's call site should reference it, or the declaration "
        "should be deleted."
    )
