"""A .py's project folder, and that folder's central venv.

A script's environment is a property of the FOLDER it belongs to, declared once
in that folder's `pyproject.toml` — not of anything written inside the file
(SPEC PY-16). Every `.py` under a project root runs in the same environment
however deep it sits, so one page calling five scripts installs one environment.

The boundary is resolved BEFORE the manifest is looked for, and in a fixed order:

  1. the app folder (`app_git.app_dir_for` — exactly `<fused_dir()>/<tag>/<name>`)
  2. an immediate child of a template root (the user override dir, the staged
     core copy, the dev override, and the in-package source tree)
  3. otherwise, the TOPMOST ancestor that holds a `pyproject.toml`

Topmost, not nearest, and structural containers first, because a manifest that
looks correct but is inert is the exact failure mode D177 was written about: a
stray `readers/pyproject.toml` inside an app must not quietly give `readers/`
its own environment while the rest of the app uses another. Inside a container
the container always wins; outside one, the outermost declaration wins.

Storage follows MD-7: the declaration (`pyproject.toml`, `uv.lock`) is source
and lives with the user's code; the venv is derived and lives in the home dir at
`<home_dir()>/venvs/<sha256(abs path)[:16]>`, never as a sidecar in the user's
tree. The uv cache sits beside it under the same home dir so both land on one
filesystem — the only way uv's hardlinks actually dedupe instead of silently
falling back to full copies.

The path is hashed AS GIVEN (abspath, not realpath). This is a deliberate
divergence from MD-7's canonicalisation: moving or renaming a folder yields a
fresh environment, which is a requested feature, and the orphaned venv is
reclaimed by `gc()`. The dangerous direction — two different folders colliding
on one key — remains impossible.

Staleness is a DIGEST comparison, never an mtime chain: `.fused-source.json`
inside the venv records the path and the sha256 of `uv.lock` (or of
`pyproject.toml` when unlocked) that it was built from. mtimes are wrong here
because `core_templates`' `copytree` uses `copy2`, so every release stamps a
template's `pyproject.toml` newer than its venv and an mtime rule would resync
byte-identical dependencies on every upgrade.

This module is consulted on every `/api/run`, so it imports nothing from
`fused.*` — pulling the engine in would cost a geopandas/pyproj import on the
request path.
"""
import hashlib
import json
import logging
import os
import re
import shutil

from fused_render.shell.storage import home_dir

logger = logging.getLogger(__name__)

# Sidecar written INSIDE the venv (never in the user's folder) naming the source
# path and the digest the venv was built from. Its absence or a digest mismatch
# is the staleness signal; see the module docstring for why not mtimes.
SIDECAR_NAME = ".fused-source.json"

# sha256 of the folder's absolute path, truncated. 16 hex chars = 64 bits, which
# is far past collision range for the number of project folders one user has,
# and keeps the directory name readable in a path the user may see in a log.
_KEY_LEN = 16

# PEP 723 reference regex (verbatim from the spec). Headers are no longer READ
# for dependencies — they are detected, so an orphan can be reported with the
# command that migrates it (SPEC PY-16) instead of being silently ignored and
# failing later on an import.
_PEP723_BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)


# --------------------------------------------------------------------------
# Where derived state lives
# --------------------------------------------------------------------------


def venvs_root() -> str:
    """`<home_dir()>/venvs` — every project venv, keyed by path hash.

    Resolved against `home_dir()` on each call so a `FUSED_RENDER_HOME` override
    (and the per-branch nesting it does) takes effect, matching
    `core_templates.core_templates_dir()`.
    """
    return os.path.join(home_dir(), "venvs")


def uv_cache_dir() -> str:
    """`<home_dir()>/uv-cache` — deliberately a sibling of `venvs_root()`.

    uv hardlinks wheels out of its cache into the venv, and can only do that
    when the two are on one filesystem; anywhere else it falls back to full
    copies and every project pays the whole size of numpy again.
    """
    return os.path.join(home_dir(), "uv-cache")


def venv_key_for(project_dir: str) -> str:
    """The directory name of *project_dir*'s venv: sha256 of its absolute path."""
    return hashlib.sha256(os.path.abspath(project_dir).encode("utf-8")).hexdigest()[:_KEY_LEN]


def venv_dir_for(project_dir: str) -> str:
    """Absolute path of *project_dir*'s venv, under the home dir."""
    return os.path.join(venvs_root(), venv_key_for(project_dir))


# --------------------------------------------------------------------------
# Resolving a path to its project root
# --------------------------------------------------------------------------


def _template_roots() -> list[str]:
    """Directories whose IMMEDIATE children are template projects.

    Deliberately resolved here rather than imported from `server.templates`:
    that module pulls FastAPI and the whole server package, and this one runs on
    the request path. The values are the same ones it computes —
    `home_dir()/templates` (D76) and the staged core copy — plus the dev
    override and the in-package source, because tests and
    `FUSED_RENDER_CORE_TEMPLATES` read templates straight out of the bundle.
    """
    from fused_render import core_templates

    roots = [
        os.path.join(home_dir(), "templates"),
        core_templates.core_templates_dir(),
        core_templates.PACKAGE_TEMPLATES_DIR,
    ]
    override = os.environ.get(core_templates._OVERRIDE_ENV)
    if override:
        roots.append(override)
    return roots


def _immediate_child(root: str, path: str) -> str | None:
    """The child of *root* that contains *path*, or None when *path* is outside
    *root* (or is *root* itself, or is a file sitting directly in it)."""
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        # Different drives on Windows — relpath raises rather than returning "..".
        return None
    if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    child = os.path.join(root, rel.split(os.sep)[0])
    return child if os.path.isdir(child) else None


def _ceiling() -> str:
    """The directory the ancestor walk must not reach.

    `home_dir()`'s parent — in production the user's home dir, where a stray
    `pyproject.toml` would otherwise make every file under `~` one enormous
    project. The ceiling itself is excluded; everything below it is fair game.
    """
    return os.path.abspath(os.path.dirname(home_dir()))


def project_root_for(path: str) -> str | None:
    """The project folder *path* belongs to, or None.

    Returns the BOUNDARY, which may or may not declare an environment — an app
    folder with no `pyproject.toml` is still that app's root. Use
    `project_env_for` when you want "the folder whose environment this script
    runs in".
    """
    from fused_render.app_git import app_dir_for

    ap = os.path.abspath(path)
    start = ap if os.path.isdir(ap) else os.path.dirname(ap)

    app = app_dir_for(ap)
    if app:
        return app

    for root in _template_roots():
        child = _immediate_child(root, ap)
        if child:
            return child

    # Topmost ancestor holding a manifest. Collect on the way up and take the
    # last hit, so an inner manifest cannot shadow the outer one it sits inside.
    ceiling = _ceiling()
    found = None
    d = start
    while True:
        if d == ceiling:
            break
        if os.path.isfile(os.path.join(d, "pyproject.toml")):
            found = d
        parent = os.path.dirname(d)
        if parent == d:  # filesystem root
            break
        d = parent
    return found


def project_env_for(path: str) -> str | None:
    """The project folder whose environment *path* runs in, or None.

    None means "run on the app's own interpreter" (SPEC PY-17) — either the file
    is in no project, or its project declares no environment.
    """
    root = project_root_for(path)
    if root and has_project_env(root):
        return root
    return None


def display_name(project_dir: str) -> str:
    """What to call the project in a progress row or an error message."""
    return os.path.basename(os.path.abspath(project_dir)) or project_dir


# --------------------------------------------------------------------------
# Reading the declaration
# --------------------------------------------------------------------------


def pyproject_path(project_dir: str) -> str:
    return os.path.join(project_dir, "pyproject.toml")


def lock_path(project_dir: str) -> str:
    return os.path.join(project_dir, "uv.lock")


def has_lock(project_dir: str) -> bool:
    """A lock is a request for exact resolution, and is always honoured with a
    real venv — the app-satisfies fast path is skipped for a locked project."""
    return os.path.isfile(lock_path(project_dir))


def _load_manifest(project_dir: str) -> dict | None:
    """Parse `<project_dir>/pyproject.toml`, or None when absent/unreadable.

    tomllib is 3.11+ stdlib and `requires-python` is >=3.10, so on 3.10 the
    `tomli` dependency supplies it. Unlike the PEP 723 reader this replaces, a
    missing parser is NOT an error the user can act on — every install of
    fused-render has one — so both names are tried and anything else reads as
    "no manifest".
    """
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            logger.warning(
                "neither tomllib (Python 3.11+) nor tomli is available; "
                "pyproject.toml files cannot be read"
            )
            return None
    try:
        with open(pyproject_path(project_dir), "rb") as f:
            return tomllib.load(f)
    except OSError:
        return None
    except tomllib.TOMLDecodeError as e:
        # Not raised: a broken manifest must not 500 the request. It reads as
        # "no environment", which lands the script on the app interpreter and
        # fails with a real ImportError naming the package it wanted.
        logger.warning("invalid TOML in %s: %s", pyproject_path(project_dir), e)
        return None


def has_project_env(project_dir: str) -> bool:
    """Does this folder declare an environment?

    A `pyproject.toml` with a `[project]` table. A manifest that only carries
    `[tool.*]` configuration (black, ruff, mypy) is not a dependency
    declaration and must not build a venv.
    """
    meta = _load_manifest(project_dir)
    return isinstance(meta, dict) and isinstance(meta.get("project"), dict)


def dependencies_of(project_dir: str) -> list[str]:
    """`[project].dependencies`, or [] when there is no manifest or no table."""
    meta = _load_manifest(project_dir)
    if not isinstance(meta, dict):
        return []
    project = meta.get("project")
    if not isinstance(project, dict):
        return []
    deps = project.get("dependencies", [])
    if not isinstance(deps, list):
        return []
    return [d for d in deps if isinstance(d, str)]


# --------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------


def state_digest(project_dir: str) -> str:
    """sha256 of what the venv should have been built from, or "" when nothing
    is declared.

    The lock when there is one — it is the resolved truth, and a comment edit to
    `pyproject.toml` must not force a resync — else the manifest.
    """
    for path in (lock_path(project_dir), pyproject_path(project_dir)):
        try:
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except OSError:
            continue
    return ""


def read_sidecar(venv_dir: str) -> dict | None:
    """The `.fused-source.json` inside *venv_dir*, or None when absent/corrupt.

    None means "this venv cannot vouch for itself" and is treated exactly like a
    digest mismatch — rebuild.
    """
    try:
        with open(os.path.join(venv_dir, SIDECAR_NAME), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_sidecar(venv_dir: str, project_dir: str, digest: str) -> None:
    """Record what this venv was built from. Written by the install worker on
    success, BEFORE the ready marker, so a venv is never advertised as ready
    without the digest that lets the next request check it."""
    tmp = os.path.join(venv_dir, SIDECAR_NAME + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"path": os.path.abspath(project_dir), "digest": digest}, f)
    os.replace(tmp, os.path.join(venv_dir, SIDECAR_NAME))


def sidecar_matches(venv_dir: str, project_dir: str) -> bool:
    """Is *venv_dir* built from *project_dir*'s current declaration?"""
    got = read_sidecar(venv_dir)
    if not got:
        return False
    return got.get("digest") == state_digest(project_dir)


# --------------------------------------------------------------------------
# Orphan headers
# --------------------------------------------------------------------------


def has_script_header(text: str) -> bool:
    """Does this source still carry a `# /// script` block?

    Detection only. The block's dependencies are deliberately NOT read: a file
    that still declares them is reported with the migration command rather than
    being run in an environment that ignores half of what it asked for.
    """
    return any(m.group("type") == "script" for m in _PEP723_BLOCK.finditer(text))


# --------------------------------------------------------------------------
# Garbage collection
# --------------------------------------------------------------------------


def gc() -> int:
    """Delete venvs whose sidecar names a folder that no longer exists.

    Load-bearing, not housekeeping: keying on the path means moving or renaming
    a project orphans its venv by design, so without this the store grows by one
    full environment every rename. A venv with no readable sidecar is LEFT
    ALONE — it may be an install in flight, and deleting one out from under a
    running worker is worse than leaking it. Returns the number removed.

    Blocking I/O; call it off the event loop.
    """
    root = venvs_root()
    removed = 0
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    for name in entries:
        venv = os.path.join(root, name)
        if not os.path.isdir(venv):
            continue
        info = read_sidecar(venv)
        if not info:
            continue
        source = info.get("path")
        if not isinstance(source, str) or os.path.isdir(source):
            continue
        try:
            shutil.rmtree(venv)
        except OSError as e:
            logger.warning("could not reclaim orphaned venv %s: %s", venv, e)
            continue
        logger.info("reclaimed venv %s (source %s is gone)", venv, source)
        removed += 1
    return removed
