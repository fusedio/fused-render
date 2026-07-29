"""What the fused engine's script venvs must contain (SPEC DM-2 / PY-12/PY-16).

Under the fused engine a script's interpreter is a venv built from
`engine.DEFAULT_REQUIREMENTS` plus that script's own PEP 723 header — nothing
else. The `[bundled]` extra (what the packaged app's interpreter ships) is a
*different* set, and the two were connected only by a comment saying "keep the
two lists in sync", which was false in ten places and could not fail.

These tests replace that comment.

1. `test_default_requirements_relationship_to_pyproject` pins the intended
   relationship between DEFAULT_REQUIREMENTS, `[bundled]` and the core
   `dependencies`, with every deliberate delta listed and reasoned.
2. `test_bundled_imports_are_in_default_requirements` is the one that catches
   real breakage: a core template importing something the app ships but a
   script venv would not contain — a silent loss of function under this engine
   (a guarded import degrades, an unguarded one 500s a tile request), never a
   startup error anyone would notice. Note *in* DEFAULT_REQUIREMENTS, not
   "declared somewhere": D168 says core templates carry no headers for anything
   in `[bundled]`, because venvs are keyed on the requirement set and every
   distinct header builds another multi-minute venv.
3. `test_header_declarations_reach_an_interpreter_that_runs_them` guards the
   handful of headers that remain (dependencies in neither `[bundled]` nor
   core): a header is only ever read on the file `run_python` is *handed*, so
   declaring something on a helper module or a spawned daemon is inert. That
   mistake shipped once already — `rasterio` on `map/vector_tile_server.py`,
   which `map_render.py` imports.

Collecting these also parses every template's PEP 723 block, so a malformed one
(a prose line inside the TOML body, say) fails here rather than at runtime as
"invalid TOML in '# /// script' block" on every call.
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
# Everything else in `[bundled]` is a default: a script venv is keyed on its
# sorted requirement set, so one shared set means ONE venv for every core
# template, where per-template headers would mean up to fifteen — each a
# multi-minute `uv` install re-resolving the same base (D168). The app's bundled
# interpreter already ships all of `[bundled]`, so matching it here is parity,
# not bloat.
BUNDLED_NOT_DEFAULT = {
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


def _files_with_headers() -> list[str]:
    """Template files that declare a PEP 723 `dependencies` list."""
    graph = _template_graph()
    return [r for r in graph["files"] if graph["header"][r]]


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
def test_bundled_imports_are_in_default_requirements(relpath):
    """Every `[bundled]`/core distribution a core template imports is a default.

    A header would also put it in the venv, and that is deliberately not
    accepted here (D168): headers fragment the venv cache, and — the reason
    this assertion is a single line instead of a search over declarations —
    they let a dependency be declared on a file the engine never hands to
    run_python (a helper module, a spawned daemon) and still look covered.
    Whatever is missing here works on the packaged app's interpreter, which
    ships all of `[bundled]`, and silently stops working under this engine.
    """
    graph = _template_graph()
    defaults = {_norm(d) for d in engine.DEFAULT_REQUIREMENTS}

    missing = sorted(graph["imports"][relpath] - defaults)
    assert not missing, (
        f"{relpath} imports {missing}, which a script venv would not contain. "
        "Add them to engine.DEFAULT_REQUIREMENTS — core templates do not carry "
        "PEP 723 headers for anything the app already bundles (D168)."
    )


@pytest.mark.parametrize("relpath", _files_with_headers())
def test_header_declarations_reach_an_interpreter_that_runs_them(relpath):
    """A PEP 723 header is only read on the file `run_python` is handed.

    So a header on a helper module or a spawned daemon declares nothing: the
    interpreter that actually imports it was built from the *entrypoint's*
    header. This is the shape of the bug that shipped once (`rasterio` declared
    on map/vector_tile_server.py, imported by map_render.py) and it is the only
    thing the remaining headers — dependencies in neither `[bundled]` nor core,
    so out of scope for the test above — still need guarding against.
    """
    graph = _template_graph()
    defaults = {_norm(d) for d in engine.DEFAULT_REQUIREMENTS}
    declared = graph["header"][relpath] - defaults

    for root in sorted(_venv_roots(relpath, graph)):
        if root == relpath:
            continue  # run directly: its own header is the one that gets read
        inert = sorted(declared - graph["header"][root])
        assert not inert, (
            f"{relpath} declares {inert} in its `# /// script` header, but it "
            f"runs inside the venv {root} defines (it imports or spawns this "
            f"file), and {root} does not declare them. Move the declaration to "
            f"{root} — a header on a file the engine never runs is inert."
        )
