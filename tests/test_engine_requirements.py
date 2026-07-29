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
    # Declared by the template that needs them (see their `# /// script`
    # headers): slides/{engine,slides}.py, excel/reader.py, usd/reader.py,
    # {geotiff/tile_server,map/vector_tile_server}.py,
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

# Files that are not entry points: they run inside a venv that another file's
# header defined, because that file spawns them with `sys.executable` (which
# under this engine *is* the script venv's python — the child inherits the
# whole venv). Their deps therefore have to be declared over there.
INHERITS_VENV_FROM = {
    # usd/reader.py spawns convert_worker.py detached; its header carries
    # numpy + msgpack for both. (pxr is fetched on demand — D119.)
    os.path.join("usd", "convert_worker.py"): os.path.join("usd", "reader.py"),
}


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

    Covered by: DEFAULT_REQUIREMENTS (installed into every script venv), the
    file's own PEP 723 header, or — for a worker/daemon — the header of the
    file that spawns it. Anything else works only on the packaged app's
    interpreter and silently stops working under the fused engine.
    """
    path = os.path.join(_TEMPLATES, relpath)
    with open(path, encoding="utf-8") as f:
        text = f.read()

    covered = {_norm(d) for d in engine.DEFAULT_REQUIREMENTS} | _header_deps(text)
    parent = INHERITS_VENV_FROM.get(relpath)
    if parent:
        with open(os.path.join(_TEMPLATES, parent), encoding="utf-8") as f:
            covered |= _header_deps(f.read())

    missing = sorted(_imported_dists(text) - covered)
    assert not missing, (
        f"{relpath} imports {missing}, which its script venv would not contain. "
        "Declare them in its `# /// script` header (preferred, if the template "
        "is the only user), or add them to engine.DEFAULT_REQUIREMENTS."
    )
