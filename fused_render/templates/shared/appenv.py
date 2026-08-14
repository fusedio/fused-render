"""How a template learns about the app it is running inside — via the ENV.

SPEC PY-15 / DECISIONS D166.

This module is the supported contract between the server and a template: the
server exports a handful of `FUSED_RENDER_*` variables before it starts serving
(`server.export_app_env`, plus `shell.mounts.export_ro_mounts_env` for the
read-only list), every child process inherits them, and the helpers here are the
one place that knows how to read them.

Templates must NOT import `fused_render`. They used to reach into the app for
exactly these facts (`from fused_render.shell.mounts import mounts_dir, ...`,
behind a try/except), which works only when the template happens to run as a
child of a Python that can see the package. Under the fused local execution
backend it cannot: `PYTHONPATH` is stripped from child processes, the guarded
import silently takes its fallback branch, and a mount-backed path is quietly
treated as local. Environment variables survive that boundary, so the facts
travel as data instead of as an import.

Stdlib only, for the same reason — a template must stay runnable as a standalone
copy of its folder, with nothing but `../shared/` beside it.

Everything is resolved PER CALL from `os.environ`, never cached at import time:
some templates are long-lived daemons (`zarr_aoi/tile_server.py`) and the
read-only mount list changes underneath them as mounts attach and detach.

The store schema stays behind in `shell/mounts.py` — nothing here reads
`mounts.json`. A template gets the *derived answers* (which dirs, which
mountpoints are read-only), so the on-disk format can change freely without
breaking any template.
"""
import ntpath
import os

# The mounts dir's basename under the home dir, and the separator for the
# read-only mountpoint list. Kept as names so the env-var contract is greppable.
_MOUNTS_SUBDIR = "mounts"
_RO_MOUNTS_VAR = "FUSED_RENDER_RO_MOUNTS"


def home_dir() -> str:
    """The app's shell home dir (`~/.fused-render`, or its per-branch nesting).

    `FUSED_RENDER_HOME_DIR` is exported by the server ALREADY BRANCH-RESOLVED —
    it is the output of `shell.storage.home_dir()`, not its input. So this
    function deliberately re-derives nothing: no branch nesting, no ref
    sanitizing. Duplicating those rules here is how the two copies drift.

    The fallback is the un-branched baseline, for a template running as a
    standalone script with no server around: `FUSED_RENDER_HOME` if set (the
    same override the app honors), else `~/.fused-render`.
    """
    home = os.environ.get("FUSED_RENDER_HOME_DIR")
    if home:
        return home
    return os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")


def workspace_dir() -> str:
    """The user's Fused workspace (~/Documents/Fused), where app folders live
    two levels down (<workspace>/<tag>/<name>).

    `FUSED_RENDER_WORKSPACE_DIR` is exported by the server ALREADY RESOLVED —
    the output of `shell.seed.fused_dir()`. The fallback mirrors that function
    for a template running standalone: the same `FUSED_RENDER_DIR` override
    tests use, else the default location.
    """
    d = os.environ.get("FUSED_RENDER_WORKSPACE_DIR")
    if d:
        return d
    return os.path.abspath(
        os.path.expanduser(os.environ.get("FUSED_RENDER_DIR") or "~/Documents/Fused")
    )


def mounts_dir() -> str:
    """The dir holding one subdir per mounted remote.

    `normpath` on the fallback for the reason recorded at `shell/mounts.py`'s
    `mounts_dir()`: `expanduser("~/...")` on Windows keeps its forward slash,
    and a mixed-separator mountpoint never string-matches a normalized path —
    which would defeat every prefix check below.
    """
    d = os.environ.get("FUSED_RENDER_MOUNTS_DIR")
    if d:
        return d
    return os.path.normpath(os.path.join(home_dir(), _MOUNTS_SUBDIR))


def is_mount_backed(path: str) -> bool:
    """True when `path` sits under the mounts dir — i.e. its bytes come from a
    remote. Mirrors `shell/mounts.py:is_mount_backed`; keep the two in step.

    The abspath prefix check goes first because it settles the common case with
    no I/O at all. The `realpath` retry is not redundant: a symlink whose TARGET
    is inside the mounts dir slips past a pure string check and gets classified
    LOCAL, which is the one wrong answer that matters (a template would then
    hammer the mount with kernel stats instead of routing through the server).
    A genuine mount path matches on abspath and never reaches `realpath`, so the
    hot path pays no extra syscall — only local-looking paths pay one.
    """
    root = os.path.abspath(mounts_dir())
    ap = os.path.abspath(path)
    if ap == root or ap.startswith(root + os.sep):
        return True
    real_root = os.path.realpath(mounts_dir())
    rp = os.path.realpath(path)
    return rp == real_root or rp.startswith(real_root + os.sep)


def read_only_mountpoints() -> list:
    """The absolute mountpoints of mounts whose remote rejects writes.

    Read from `FUSED_RENDER_RO_MOUNTS`, an `os.pathsep`-joined list the shell
    re-exports whenever the mount store changes; absent or empty means "none
    known". Empty segments are dropped so a trailing separator (or an
    accidental `":"`) can never produce a `""` entry that prefix-matches
    everything.
    """
    raw = os.environ.get(_RO_MOUNTS_VAR) or ""
    return [p for p in raw.split(os.pathsep) if p]


def mount_read_only(path: str) -> bool:
    """True when `path` sits under a read-only mount. Mirrors
    `shell/mounts.py:mount_read_only`; keep the two in step.

    Local paths are never read-only *for this reason*, hence the
    `is_mount_backed` gate first (it is also the cheap check). Beyond that this
    is a plain abspath prefix match — exact mountpoint or anything below it.
    Like the app's version it ignores whether the mount is currently attached:
    bytes written into a detached read-only mountpoint would be shadowed by the
    next attach, so refusing is right either way.
    """
    if not is_mount_backed(path):
        return False
    p = os.path.abspath(path)
    return any(p == mp or p.startswith(mp + os.sep)
               for mp in read_only_mountpoints())


def origin() -> str | None:
    """The origin (`http://host:port`) the server is ACTUALLY serving on, or None
    when nothing published it. The server sets `FUSED_RENDER_ORIGIN`
    unconditionally before it starts serving, so None means "no server around" —
    the caller decides what to do (a daemon that must fetch bytes back through
    `/api/fs/raw` has nowhere to go and should say so, rather than guessing a
    default port that is wrong under any `--port` override).
    """
    return os.environ.get("FUSED_RENDER_ORIGIN") or None


def skill_plugin_dir() -> str | None:
    """The Claude Code plugin root to hand a session we spawn (`--plugin-dir`),
    or None when there is none to hand it.

    fused-render assembles the canonical skills into a plugin under its home dir
    and exports the path here (`skill_plugin.export_skill_plugin_env`, D216), so
    a chat this app launches knows the `fused` bridge contract regardless of the
    state of the user's `~/.claude`.

    The var is absent in exactly the cases where the flag must not be passed:
    no server around to have synced anything, or a sync that failed. Deciding
    that is the server's job — the answer arrives here already made, like every
    other value in this module.
    """
    return os.environ.get("FUSED_RENDER_SKILL_PLUGIN_DIR") or None


def _sidecar_subpath(abs_path: str) -> str:
    """Mirrors shell/storage.py:_sidecar_subpath; keep the two in step.

    Pure classification of an absolute path (Windows or POSIX-shaped) into a
    forward-slash-joined relative location under the sidecar subtree, built
    on ntpath.splitdrive so it stays correct for Windows-shaped input on any
    host. Case is preserved exactly — never fold it, that would collide two
    distinct paths on a case-sensitive filesystem.
    """
    drive, tail = ntpath.splitdrive(abs_path)
    if not drive:
        # A bare POSIX path: backslash is a legal filename character here (no
        # drive/UNC prefix means ntpath found no separator to interpret), so
        # it must round-trip untouched — replacing it would collide a real
        # "weird\file.txt" with the entirely different "weird/file.txt".
        return tail.lstrip("/")
    tail = tail.replace("\\", "/").lstrip("/")
    if drive.endswith(":"):
        return "/".join(filter(None, [drive[0].upper(), tail]))
    return "/".join(filter(None, ["unc", *drive.strip("\\").replace("\\", "/").split("/"), tail]))


def nearest_existing_dir(path: str) -> str:
    """Walk up from `path` to the nearest existing ancestor directory — the
    dir an `os.makedirs(path)` would actually need write access to. A fresh
    sidecar's subtree under home_dir()/sidecar/ usually doesn't exist yet, so
    a writability check must not mkdir it just to answer a status query; it
    answers against whatever ancestor is already there instead (worst case,
    home_dir() itself). Never returns something that doesn't exist; stops at
    the filesystem root if nothing along the way does."""
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            return path
        path = parent
    return path


def sidecar_path(file: str) -> str:
    """Mirrors shell/storage.py:sidecar_path; keep the two in step.

    The `<file>.json` sidecar's home: home_dir()/sidecar/<mapped path>. Uses
    abspath (not realpath) so a symlink's own apparent location decides where
    its sidecar lives, matching the prior co-located behavior exactly.
    """
    parts = [p for p in _sidecar_subpath(os.path.abspath(file)).split("/") if p]
    # An empty mapping (abs_path == "/", the filesystem root) must still land
    # INSIDE the sidecar subtree, not as a "sidecar.json" sibling of it — the
    # unpacked *parts below drops straight to "sidecar" with nothing to
    # descend into unless something is there to join.
    return os.path.join(home_dir(), "sidecar", *(parts or [""])) + ".json"
