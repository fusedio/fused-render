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
inside the venv records the path and the sha256 of the `pyproject.toml` it was
built from. The MANIFEST, and only the manifest — `uv.lock` is an OUTPUT of
`uv sync`, so folding it in would make the environment's own side effect a reason
to rebuild the environment. mtimes are wrong here for a different reason:
`core_templates`' `copytree` uses `copy2`, so every release stamps a template's
`pyproject.toml` newer than its venv and an mtime rule would resync
byte-identical dependencies on every upgrade. See `state_digest`.

This module is consulted on every `/api/run`, so it imports nothing from
`fused.*` — pulling the engine in would cost its whole import tree (pandas and
friends, historically geopandas/pyproj too) on the request path.
"""
import hashlib
import json
import logging
import os
import shutil
import threading

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

    The parent of the UN-nested shell home — in production the user's home dir,
    where a stray `pyproject.toml` would otherwise make every file under `~` one
    enormous project. The ceiling itself is excluded; everything below it is fair
    game.

    Deliberately NOT `os.path.dirname(home_dir())`. `home_dir()` nests to
    `<base>/branches/<ref>` when `FUSED_RENDER_BRANCH` is set (see
    `_branch.branch_dir`), so that spelling made the ceiling `<base>/branches` —
    a directory that is not an ancestor of anything a user works on. The walk for
    a file under `~` then never met the ceiling at all and ran to the filesystem
    root, which is precisely the failure this function exists to prevent, silently
    switched on by a branch ref.
    """
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.abspath(os.path.dirname(os.path.abspath(base)))


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
    """Does this folder declare an environment WORTH BUILDING?

    Three things have to hold: a `pyproject.toml`, a `[project]` table in it, and
    at least one dependency that applies on this platform.

    The last one is not a nicety. An empty declaration — a bare `uv init`
    scaffold, or a manifest added only for `[tool.*]` config that happens to
    carry `[project]` — would otherwise take the script OFF the app interpreter
    and onto an empty venv: no numpy, no pandas, no duckdb, no pillow, so a
    script that worked yesterday fails on its first import. The pre-flight would
    also render the empty list as "…are not installed yet: . They need a one-time
    download." Nothing to install means PY-17: run on the app's own interpreter,
    which already has everything.

    Markers are applied for the same reason, one step further out: a folder whose
    only dependency is `; sys_platform == 'darwin'` has nothing to install on
    Linux, and building an empty venv there is the identical trap reached by a
    different route.
    """
    meta = _load_manifest(project_dir)
    if not (isinstance(meta, dict) and isinstance(meta.get("project"), dict)):
        return False
    return bool(applicable_dependencies_of(project_dir))


def dependencies_of(project_dir: str) -> list[str]:
    """`[project].dependencies` verbatim, markers included.

    For tooling that must reason about ALL platforms (the packaging invariants in
    tests/). Anything deciding what THIS machine will install wants
    `applicable_dependencies_of`.
    """
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


def marker_applies(requirement: str) -> bool:
    """Does this PEP 508 requirement's environment marker hold here?

    A requirement with no marker always applies. Markers exist so a template can
    declare a dependency **only where the app doesn't already ship it**:

        dependencies = ["python-pptx; sys_platform == 'darwin'"]

    No template needs that today — all three platform builds now ship the whole
    `[bundled]` extra (D176, as amended), so a `[bundled]` distribution is
    present everywhere and a template that only needed one would declare nothing
    at all. Support stays because the situation is one packaging decision away:
    the moment a build holds something back (`BUNDLED_EXCLUDED`), a declaration
    that ignored the marker would make the other platforms build a venv and
    re-download a package already on their interpreter.

    An unparseable or unevaluatable marker is treated as APPLYING: the dependency
    then gets installed where it might not have been needed, which is wasteful.
    Guessing the other way would drop a dependency the script really needs and
    fail at import — the worse of the two.

    Lives here rather than in `engine.py` (where it used to) so that the one
    filter serves every caller: the run's routing decision, the pre-flight's
    message, and `has_project_env`. Two of those disagreed before — the loader
    row named packages `uv sync` would never install. `packaging` is imported
    lazily and only for a requirement that actually carries a marker, so the
    common case stays free on the request path.
    """
    if ";" not in requirement:
        return True
    marker = requirement.split(";", 1)[1].strip()
    if not marker:
        return True
    try:
        from packaging.markers import InvalidMarker, Marker
    except ImportError:
        return True
    try:
        return bool(Marker(marker).evaluate())
    except (InvalidMarker, KeyError, ValueError):
        logger.warning(
            "could not evaluate the environment marker %r in a pyproject.toml "
            "dependency; treating it as applying", marker,
        )
        return True


def applicable_dependencies_of(project_dir: str) -> list[str]:
    """The declared dependencies that apply on THIS platform, markers included.

    The single answer to "what will `uv sync` put in this environment here", used
    by the routing decision, by `has_project_env`, and by the pre-flight's
    message — so the loader can never name a package the install will skip.
    Markers are kept on the strings: `app_satisfies` parses them itself, and
    stripping them would lose information for no gain.
    """
    return [d for d in dependencies_of(project_dir) if marker_applies(d)]


# --------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------


# project dir -> (stat fingerprint, digest). A process-local memo, described in
# `state_digest`; `_digest_lock` guards it because `is_installed` reaches this
# through `asyncio.to_thread` and several runs can be in flight at once.
_digest_cache: dict[str, tuple[tuple | None, str]] = {}
_digest_lock = threading.Lock()


def _digest_fingerprint(root: str) -> tuple | None:
    """`(st_ino, st_size, st_mtime_ns)` of the manifest, or None when absent.

    ONLY a cache-invalidation hint — never the staleness signal itself. The
    digest below is what any decision is made on, so the `copy2` problem in the
    module docstring (a re-staged template's manifest is newer than its venv but
    byte-identical) is untouched: a moved mtime costs one re-hash and then agrees
    with the digest already recorded.
    """
    try:
        st = os.stat(os.path.join(root, "pyproject.toml"))
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _compute_state_digest(root: str) -> str:
    """The uncached digest. Kept byte-identical to
    `_env_install_worker._state_digest`, which WRITES what this reads — a
    divergence there means every request reads its own fresh venv as stale."""
    try:
        with open(os.path.join(root, "pyproject.toml"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def state_digest(project_dir: str) -> str:
    """sha256 of `pyproject.toml`, or "" when there is none.

    The MANIFEST only. `uv.lock` is deliberately not part of this: it is an
    OUTPUT of `uv sync`, not an input to it, so folding it in would make the
    environment's own side effect a reason to rebuild the environment. The
    declaration is the manifest, and the manifest is what decides staleness.

    That the manifest is hashed at all — rather than the lock, on the reasoning
    that the lock is the resolved truth — is the requirement this exists for: a
    user adding a dependency must have it picked up without ever running
    `uv sync` by hand, because doing that would create an in-folder `.venv` and
    diverge from the home-dir store. Hashing the lock instead meant such an edit
    changed nothing, `sidecar_matches` said fresh, no install was offered, and
    the run failed later on an ImportError with no loader and no explanation. The
    cost in the other direction is a resync for a comment edit, which is a fast
    no-op through uv's cache — a silently ignored dependency edit is a broken app.

    The intended consequence: a hand-edit to `uv.lock` ALONE does not trigger a
    resync. The lock is generated; the manifest is the declaration. (The sync it
    would have triggered runs bare rather than `--frozen`, so uv reconciles the
    lock itself whenever the manifest moves — see `_env_install_worker._build`.)

    Still a digest and never an mtime chain, for the reason in the module
    docstring: core templates are re-staged with `copy2` on every release, so an
    mtime rule would resync byte-identical dependencies at every upgrade.

    Memoised per process on a `(st_ino, st_size, st_mtime_ns)` fingerprint,
    because `is_installed` calls this on every `/api/run`: the steady state is
    one `stat`. The memo is process-local and dies with the app process, which is
    what keeps it safe across upgrades — there is no persisted verdict to go
    stale. Its one blind spot is an edit that preserves BOTH size and nanosecond
    mtime, which needs two writes inside a single filesystem timestamp tick; the
    fingerprint is deliberately not strengthened past that, since the alternative
    is re-reading the file on every request to close a window nothing can
    realistically hit.
    """
    root = os.path.abspath(project_dir)
    fingerprint = _digest_fingerprint(root)
    with _digest_lock:
        cached = _digest_cache.get(root)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
    # Computed outside the lock: reading the file must not serialise every
    # concurrent pre-flight in the process. A duplicate computation in a race is
    # harmless — the inputs are the same, so both threads produce the same digest
    # and the later store simply overwrites an identical value.
    digest = _compute_state_digest(root)
    with _digest_lock:
        _digest_cache[root] = (fingerprint, digest)
    return digest


def reset_state_digest_cache() -> None:
    """Forget every memoised digest. A test seam, mirroring
    `envinstall.reset_venv_validation_cache`."""
    with _digest_lock:
        _digest_cache.clear()


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
# Garbage collection
# --------------------------------------------------------------------------


def _source_is_deleted(source: str) -> bool:
    """Is `source` genuinely gone, as opposed to merely unreachable right now?

    The distinction `gc()` cannot do without. `os.path.isdir(source) == False`
    covers both "the user deleted this project" and "the external drive it lives
    on is unplugged", and those want opposite answers: reclaiming on the second
    means one boot with a disk detached wipes every venv for that workspace, and
    the user pays a full re-download for each when they plug it back in.

    A deletion leaves the CONTAINER behind — you cannot delete `~/work/app`
    without `~/work` still being there. An absent volume takes the whole chain
    with it. So: gone means the folder is missing while its parent still exists.
    A parent that is itself missing is not evidence of anything, and the
    conservative answer is to keep the venv — it costs disk, which `gc` can
    reclaim on any later boot, whereas the other mistake is unrecoverable.
    """
    if os.path.isdir(source):
        return False
    parent = os.path.dirname(os.path.abspath(source))
    return parent != source and os.path.isdir(parent)


def gc() -> int:
    """Delete venvs whose sidecar names a folder that no longer exists.

    Load-bearing, not housekeeping: keying on the path means moving or renaming
    a project orphans its venv by design, so without this the store grows by one
    full environment every rename. Returns the number removed.

    Two things are deliberately LEFT ALONE, both because this runs unattended at
    every server startup and a wrong deletion costs the user a full re-download:

      * a venv with no readable sidecar — it may be an install in flight, and
        deleting one out from under a running worker is worse than leaking it;
      * a venv whose source is merely UNREACHABLE rather than deleted, e.g. on an
        unplugged external drive. See `_source_is_deleted`.

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
        if not isinstance(source, str) or not _source_is_deleted(source):
            continue
        try:
            shutil.rmtree(venv)
        except OSError as e:
            logger.warning("could not reclaim orphaned venv %s: %s", venv, e)
            continue
        logger.info("reclaimed venv %s (source %s is gone)", venv, source)
        removed += 1
    return removed
