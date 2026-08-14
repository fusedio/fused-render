"""The mounts.json store: whole-file last-write-wins CRUD for the list of
configured mounts, same convention as shell/bookmarks.py."""

import json
import logging
import os
import re
import stat as stat_mod
import subprocess
import sys
import threading
import time
import uuid

from fused_render.shell import storage

logger = logging.getLogger(__name__)


def _path() -> str:
    p = os.path.join(storage.home_dir(), "mounts.json")
    # Prototype-era migration: rename a legacy connectors.json to mounts.json
    # the first time we read, so early users don't lose their entries.
    if not os.path.exists(p):
        legacy = os.path.join(storage.home_dir(), "connectors.json")
        if os.path.exists(legacy):
            try:
                os.rename(legacy, p)
            except OSError:
                pass
    return p


def mounts_dir() -> str:
    # normpath: expanduser("~/...") on Windows keeps its forward slash, and a
    # mixed-separator mountpoint never string-matches rcd's normalized paths.
    return os.path.normpath(os.path.join(storage.home_dir(), "mounts"))


def ensure_mounts_dir() -> str:
    """Create the mounts root and mark it so macOS Spotlight never indexes it,
    returning the path. A `.metadata_never_index` marker in a directory tells
    mds (the Spotlight daemon) to skip the whole subtree — the simplest,
    permission-safe, no-subprocess way to keep Spotlight from auto-walking the
    S3-backed mounts with readdir (a prefix-enumeration mount-wedge trigger,
    the browse-side companion to the "nobrowse" mount flag above). Dropped at
    the root, not per-mount, so it covers mountpoints created later too. A
    best-effort `mdutil -i off` would need privileges and often no-ops, so the
    marker is the primary mechanism; we don't shell out. Idempotent."""
    root = mounts_dir()
    os.makedirs(root, exist_ok=True)
    marker = os.path.join(root, ".metadata_never_index")
    if not os.path.exists(marker):
        try:
            with open(marker, "w"):
                pass
        except OSError:
            # Non-fatal: the mount still works, it just isn't Spotlight-excluded.
            pass
    return root


def list_mounts() -> list:
    data = storage.read_json(_path())
    return data if isinstance(data, list) else []


_mounts_generation = 0  # bumped on every _write; see _read_only_mountpoints


def _write(mounts: list) -> None:
    from fused_render.shell import mounts as _mounts_pkg
    from .access import export_ro_mounts_env
    storage.write_json(_path(), mounts)
    _mounts_pkg._mounts_generation += 1
    # Every store mutation funnels through here, so this is the one hook that
    # cannot miss a change to the read-only set (a create, a delete, or an
    # attach-time read_only re-detection via _update_mount).
    export_ro_mounts_env()


_store_lock = threading.Lock()


def mountpoint(m: dict) -> str:
    return os.path.join(mounts_dir(), m["name"])


_IO_REPARSE_TAG_MOUNT_POINT = getattr(stat_mod, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def _ismount(mp: str) -> bool:
    """os.path.ismount plus the WinFsp mounts it misses on win32: ntpath.ismount
    resolves their reparse point to a Volume{...} device and returns False, or
    raises WinError 123 when the backing device is gone (a disconnected mount) —
    still a reparse point, so the lstat check below detects it and lets reconnect
    heal it."""
    try:
        if os.path.ismount(mp):
            return True
    except OSError:
        pass
    if sys.platform != "win32":
        return False
    try:
        st = os.lstat(mp)
    except OSError:
        return False
    if getattr(st, "st_reparse_tag", 0) != _IO_REPARSE_TAG_MOUNT_POINT:
        return False
    try:
        return "Volume{" in os.readlink(mp)
    except OSError:
        return False


def add_mount(name: str, remote: str, read_only: bool | None = None) -> dict:
    """Validate and persist a new mount; raises ValueError on bad input.
    Does NOT mount — the endpoint decides whether create implies mount.
    Every mount is remounted at startup (see run_automount); there is no
    per-mount opt-in.

    `read_only` marks the remote as rejecting writes (stat.writable goes
    false for everything under the mountpoint — see server._writable). An
    explicit value is the user's call and is never overwritten by detection;
    leave it None to have attach_mount detect it from the remote's config."""
    from fused_render.shell.mounts import list_mounts
    name = (name or "").strip()
    remote = (remote or "").strip()
    if not name or any(ch in name for ch in "/\\:") or name.startswith("."):
        raise ValueError("name must be a plain folder-safe name")
    if ":" not in remote:
        raise ValueError(
            "remote must be an rclone spec like 'gdrive:' or 's3remote:bucket/prefix'"
        )
    # Strict bool, not truthiness: this comes straight off a JSON body, and
    # bool("false") is True — a client sending the string would lock a
    # writable mount read-only AND suppress detection forever.
    if read_only is not None and not isinstance(read_only, bool):
        raise ValueError("read_only must be a boolean")
    with _store_lock:
        mounts = list_mounts()
        if any(c["name"] == name for c in mounts):
            raise ValueError(f"a mount named '{name}' already exists")
        if any(c["remote"] == remote for c in mounts):
            raise ValueError(f"'{remote}' is already connected")
        m: dict = {"id": uuid.uuid4().hex[:12], "name": name, "remote": remote}
        if read_only is not None:
            m["read_only"] = read_only
            # Marks the flag as user-chosen so attach-time re-detection
            # leaves it alone (mount_view never exposes this field).
            m["read_only_user"] = True
        mounts.append(m)
        _write(mounts)
    return m


def _update_mount(m: dict) -> None:
    """Persist changed fields of an existing mount record (matched by id)."""
    from fused_render.shell.mounts import list_mounts
    with _store_lock:
        _write([m if c["id"] == m["id"] else c for c in list_mounts()])


def get_mount(cid: str) -> dict | None:
    from fused_render.shell.mounts import list_mounts
    return next((c for c in list_mounts() if c["id"] == cid), None)


def remove_mount(cid: str) -> None:
    from fused_render.shell.mounts import list_mounts
    with _store_lock:
        _write([c for c in list_mounts() if c["id"] != cid])
