"""What the fused engine's script venvs must contain (SPEC DM-2 / PY-12).

Under the fused engine a script's interpreter is a venv built from
`engine.DEFAULT_REQUIREMENTS` plus that script's own PEP 723 header — nothing
else. The `[bundled]` extra (what the packaged app's interpreter ships) is a
*different* set, and the two were connected only by a comment saying "keep the
two lists in sync", which was false in ten places and could not fail.

These tests replace that comment. The first pins the intended relationship
between DEFAULT_REQUIREMENTS, `[bundled]` and the core `dependencies`, with
every deliberate delta listed and reasoned. The second is the one that catches
real breakage: a template importing a `[bundled]` distribution that its venv
would not contain — which is a silent loss of function under this engine (a
guarded import degrades, an unguarded one 500s a tile request), never a
startup error anyone would notice.
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

# Distributions in `[bundled]` that DEFAULT_REQUIREMENTS deliberately omits.
# The rule: a dependency used by exactly one template belongs in that
# template's PEP 723 header, not in the set installed into *every* script's
# venv — the header mechanism exists for this, and it keeps the shared venv
# (built on first run, per requirement set) small.
BUNDLED_NOT_DEFAULT = {
    # Declared by the *entrypoint* of the template that needs them (see their
    # `# /// script` headers): slides/{engine,slides}.py, excel/reader.py,
    # usd/reader.py, geotiff/tile_server.py, map/map_render.py,
    # {netcdf/grid_tile_server,zarr_aoi/tile_server}.py, pdf_studio/pdf.py,
    # log_studio/reader.py.
    "python-pptx",
    "fpdf2",
    "msgpack",
    "rasterio",
    "zarr",
    "pymupdf",
    "pikepdf",
    "drain3",
    # Server-side only: the s3sign/gcssign credential chains run in the
    # fused-render process, never in a user script's venv. Installing botocore
    # (~80 MB) into every script venv would buy nothing.
    "botocore",
    "google-auth",
}

# In DEFAULT_REQUIREMENTS but not in `[bundled]`: both are *core* dependencies
# (`[project] dependencies`), so the packaged interpreter has them via the
# install rather than via the extra — but a script venv is built from scratch
# and would not, and the tabular readers (structure/, duckdb/) are unusable
# without them.
DEFAULT_NOT_BUNDLED = {"pyarrow", "duckdb"}

# import name -> distribution name, for every distribution in `[bundled]` plus
# the two core ones above. Only these are checked: this is a guard on the
# bundled/default relationship, not a general dependency linter (on-demand
# fetches like pxr/pypandoc and genuinely optional readers like rawpy have
# their own mechanisms and are covered by their own tests).
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


def test_default_requirements_relationship_to_pyproject():
    """DEFAULT_REQUIREMENTS vs `[bundled]` vs core `dependencies` — exactly.

    Read from pyproject.toml rather than restated here: a second hardcoded copy
    of the list is the failure this test exists to prevent.
    """
    pp = _pyproject()
    bundled = {_norm(d) for d in pp["project"]["optional-dependencies"]["bundled"]}
    core = {_norm(d) for d in pp["project"]["dependencies"]}
    defaults = {_norm(d) for d in engine.DEFAULT_REQUIREMENTS}

    # Nothing invented: every default is something the app itself ships.
    assert defaults <= bundled | core

    assert bundled - defaults == BUNDLED_NOT_DEFAULT, (
        "the `[bundled]` extra changed: either add the distribution to "
        "engine.DEFAULT_REQUIREMENTS, or list it in BUNDLED_NOT_DEFAULT with "
        "the template header that declares it instead"
    )
    assert defaults - bundled == DEFAULT_NOT_BUNDLED


@pytest.mark.parametrize("relpath", _template_files())
def test_bundled_imports_are_reachable_under_the_fused_engine(relpath):
    """Every `[bundled]` distribution a template imports must be in the venv.

    Checked against *every* entry point that can execute this file (see
    _venv_roots), not against the file's own header: the engine reads the
    header of the file it is handed and nothing else, so declaring a dependency
    on a helper module or a spawned daemon has no effect at all — the classic
    way this gap hides. What isn't covered works on the packaged app's
    interpreter (which has all of `[bundled]`) and silently stops working here.
    """
    graph = _template_graph()
    needed = graph["imports"][relpath]
    defaults = {_norm(d) for d in engine.DEFAULT_REQUIREMENTS}

    for root in sorted(_venv_roots(relpath, graph)):
        missing = sorted(needed - (defaults | graph["header"][root]))
        assert not missing, (
            f"{relpath} imports {missing}, which its script venv would not "
            f"contain when it runs via {root}. Declare them in {root}'s "
            "`# /// script` header (preferred, if that template is the only "
            "user) — a header on a non-entrypoint file is never read — or add "
            "them to engine.DEFAULT_REQUIREMENTS."
        )
