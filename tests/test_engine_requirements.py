"""Which core templates may carry a PEP 723 header, and what it must contain
(SPEC PY-16/PY-17, D172).

Under the fused engine a script with **no** header runs on the app's own
interpreter, which has `[bundled]` + the core `dependencies` — so a header on
such a script buys nothing and costs a download. A script **with** a header gets
a venv containing exactly what the header declares and nothing else, so a
dependency it forgot to declare is simply absent.

That makes the header decision a two-part invariant, and both halves are
derived from the source here rather than written down, because a written-down
version is what failed before: the predecessor of this file pinned
`DEFAULT_REQUIREMENTS` against `[bundled]` with a hand-kept list of deltas, and
before that a *comment* claimed the two were in sync while being wrong in ten
places.

  1. **A header must be necessary** — it has to declare something the app's
     interpreter does not already have. Otherwise deleting it is free and
     keeping it makes a first run wait on PyPI for packages already installed.
  2. **A header must be complete** — every `[bundled]`/core distribution the
     file imports, at any nesting depth, is declared in the header of *each*
     entry point that can execute it. This is the half with teeth: it is what
     caught `pano/pano.py` importing numpy and pillow while declaring only
     `py360convert`, which worked solely because a baseline set used to be
     installed alongside every header. With that baseline gone, an incomplete
     header is a broken template — and a silent one (a guarded import degrades,
     an unguarded one 500s a tile request), never a startup error anyone sees.
"""
import ast
import functools
import os
import re

import pytest

from fused_render import engine

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

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_REPO, "fused_render", "templates")

# import name -> distribution name, for every distribution the app's own
# interpreter provides (`[bundled]` + core `dependencies`). Only these are
# checked: this is a guard on what the app ships versus what a script venv
# would contain, not a general dependency linter (on-demand binary fetches like
# pxr/pypandoc/typst and genuinely optional readers like rawpy have their own
# mechanisms and their own tests).
#
# `test_the_import_map_covers_everything_the_app_ships` keeps this honest — a
# distribution added to `[bundled]` with no entry here would be invisible to
# the completeness half and silently exempt from it.
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
    "zarr": "zarr",
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


def _header_deps(text: str) -> set[str]:
    """Distributions declared in a file's PEP 723 block ({} when it has none)."""
    return {_norm(d) for d in engine.script_requirements(text)}


def _imported_dists(text: str) -> set[str]:
    """`[bundled]`/core distributions this file imports, at any nesting depth.

    ast, not a regex: the imports that matter most here are the *function-level*
    ones (a tile handler's `import rasterio`), which is exactly what a naive
    "imports at the top of the file" check misses.
    """
    tree = ast.parse(text)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return {_IMPORT_TO_DIST[m] for m in mods if m in _IMPORT_TO_DIST}


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
        "header": {r: _header_deps(t) for r, t in texts.items()},
        "imports": {r: _imported_dists(t) for r, t in texts.items()},
        "invoked_by": invoked_by,
    }


def _venv_roots(relpath: str, graph: dict, _seen: frozenset = frozenset()) -> set[str]:
    """The files whose PEP 723 header can decide the venv `relpath` runs in.

    `engine.run_python` reads the header of the file it is *given* and of no
    other, so a helper module or a spawned daemon runs under whatever its
    caller declared. Walking `invoked_by` up to the callers that nothing else
    invokes gives the set of entry points that can end up executing this file;
    each of them has to cover it, since any of them may be the one that runs.

    A file with a header of its own also counts as a root: a header is how a
    file claims to be an entry point, so it is held to covering its own
    imports too (that is the direct-invocation path).
    """
    if relpath in _seen:  # cyclic sibling imports: stop, don't recurse forever
        return set()
    seen = _seen | {relpath}
    invokers = graph["invoked_by"][relpath]
    roots = {relpath} if graph["header"][relpath] or not invokers else set()
    for invoker in invokers:
        roots |= _venv_roots(invoker, graph, seen)
    return roots or {relpath}


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


@pytest.mark.parametrize("relpath", _template_files())
def test_a_core_template_header_declares_something_the_app_lacks(relpath):
    """Part 1: a header must be NECESSARY (PY-17).

    A header-less script runs on the app's own interpreter, which has all of
    `[bundled]` + core `dependencies`. So a header naming only things the app
    already ships changes nothing except forcing a first run to build a venv and
    download them from PyPI — slow, and it needs network the app otherwise
    doesn't. This is what makes "which templates keep a header" self-policing
    instead of a comment someone has to remember to update.
    """
    graph = _template_graph()
    header = graph["header"][relpath]
    if not header:
        return
    justification = sorted(header - _app_dists())
    assert justification, (
        f"{relpath}'s `# /// script` header declares {sorted(header)}, all of "
        "which the app's interpreter already provides — so the header only costs "
        "a venv build and a PyPI download on first run. Delete the whole block "
        "and the file runs on the app's own python (PY-17). Keep a header only "
        "for a dependency that is genuinely absent from `[bundled]` + the core "
        "`dependencies`."
    )


@pytest.mark.parametrize("relpath", _template_files())
def test_a_retained_header_is_complete(relpath):
    """Part 2: a header must be COMPLETE (PY-16).

    Checked against *every* entry point that can execute this file (see
    `_venv_roots`), not against the file's own header: the engine reads the
    header of the file it is handed and nothing else, so declaring a dependency
    on a helper module or a spawned daemon has no effect at all — the classic
    way this gap hides.

    Only roots that HAVE a header are checked. A root without one runs on the
    app's interpreter, where every distribution here is present by definition;
    there is nothing a venv could be missing.
    """
    graph = _template_graph()
    needed = graph["imports"][relpath]

    for root in sorted(_venv_roots(relpath, graph)):
        header = graph["header"][root]
        if not header:
            continue  # runs on the app's interpreter — it has all of these
        missing = sorted(needed - header)
        assert not missing, (
            f"{relpath} imports {missing}, which its script venv would not "
            f"contain when it runs via {root}: a header is the COMPLETE "
            f"dependency list now, with no baseline unioned in (D172). Declare "
            f"them in {root}'s `# /// script` header — a header on a "
            "non-entrypoint file is never read — or, if that template needs "
            "nothing outside `[bundled]`, delete the header entirely so it runs "
            "on the app's own interpreter."
        )
