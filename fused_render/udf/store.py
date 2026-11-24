"""Saved UDF results on disk (SPEC §54.3).

One entry per run key: `<key>.body` (the bytes) and `<key>.json` (content
type, run output, when saved). Where an entry lives depends on what ran:

  - a canvas node file's result: in its canvas folder,
    `<clone>/.fused/cache/udf/` (the `.fused/cache` of app_fused_dir.py:
    bytes that can be rebuilt, safe to delete). Deleting that folder drops
    the saved results of that one canvas and nothing else.
  - anything else (a UDF named by canvas and name, a .py outside a canvas):
    `<FUSED_RENDER_HOME or ~/.fused-render>/cache/udf/results/`.

A canvas folder publishes itself, so it holds results only while its
`.fusedignore` lists `.fused/` (`clone_folder`; canvases.py seeds the line).
A clone without the line (an external `fused` binary, a reference copy, a
folder not seeded yet) keeps its results in the home folder instead. A hard
pull (`canvas pull --force`) deletes every file the canvas does not have, the
saved results among them.

A result that must not be kept (no key: the file changed while it ran) still
needs a URL for the page that asked for it: it lands in the home folder's
`tmp/<token>.body` with its `.json` beside it, and is swept after
TMP_MAX_AGE_S. Every run's body is first written there.

Rules:
  - only successful runs are put here (the service's rule, not checked here)
  - no size limits: any size is kept and nothing is evicted for space
  - an entry older than MAX_AGE_S (7 days) is dropped on read
  - a run of the same key replaces its entry; Preferences → Debugging →
    "Saved UDF cache" (`clear`) empties the home folder and every canvas's

All writes go through a temp file and `os.replace`, so a reader never sees
half an entry.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time

MAX_AGE_S = 7 * 24 * 3600.0
TMP_MAX_AGE_S = 3600.0

_TOKEN_RE = re.compile(r"^(?:[0-9a-f]{64}|t[0-9a-f]{32})$")
_LOCK = threading.Lock()

#: The line a canvas folder's `.fusedignore` must hold before results are
#: kept inside it: `canvas push` uploads every file no rule ignores.
IGNORE_LINE = ".fused/"

# Which canvas folder holds a key's entry, as last seen by this process: a
# result is fetched by its token alone (`open_result`). Only a shortcut; a
# miss scans the canvas folders.
_WHERE: dict[str, str] = {}
_WHERE_MAX = 4096


def root() -> str:
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(base, "cache", "udf")


def _results_dir() -> str:
    return os.path.join(root(), "results")


def _tmp_dir() -> str:
    return os.path.join(root(), "tmp")


def _clone_path(clone_dir: str) -> str:
    from fused_render import app_fused_dir

    return os.path.join(app_fused_dir.cache_dir(clone_dir), "udf")


def clone_folder(clone_dir: str | None) -> str | None:
    """`<clone_dir>/.fused/cache/udf` when that canvas folder may hold saved
    results, else None (they go to the home folder): its `.fusedignore` must
    list `.fused/`. A folder that is gone has no `.fusedignore`, so a run
    that outlives its canvas never brings the folder back."""
    if not clone_dir:
        return None
    try:
        with open(os.path.join(clone_dir, ".fusedignore"), encoding="utf-8",
                  errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    return _clone_path(clone_dir) if IGNORE_LINE in lines else None


def _clone_folders() -> list[str]:
    """Every canvas folder's results folder that exists on disk."""
    from fused_render import canvases

    out: list[str] = []
    try:
        envs = os.listdir(canvases.canvases_base())
    except OSError:
        return out
    for env in envs:
        if env.startswith("."):
            continue
        clones = canvases.clones_root(env)
        try:
            names = os.listdir(clones)
        except OSError:
            continue
        for name in names:
            folder = _clone_path(os.path.join(clones, name))
            if os.path.isdir(folder):
                out.append(folder)
    return out


def _remember(key: str, folder: str) -> None:
    if len(_WHERE) >= _WHERE_MAX:
        _WHERE.clear()
    _WHERE[key] = folder


def _remove_entry(folder: str, key: str) -> None:
    for ext in (".body", ".json"):
        try:
            os.remove(os.path.join(folder, key + ext))
        except OSError:
            pass


def _move(src: str, dst: str) -> None:
    """`src` onto `dst`, whole or not at all: a rename, or across volumes a
    copy beside `dst` renamed into place. `src` is left where it was when
    this raises."""
    try:
        os.replace(src, dst)
        return
    except OSError:
        if not os.path.exists(src):
            raise
    part = f"{dst}.{secrets.token_hex(4)}.part"
    try:
        shutil.copyfile(src, part)
        os.replace(part, dst)
    except OSError:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    drop_body(src)


def new_body_path() -> tuple[str, str]:
    """(token, path) for a fresh run's body to be written to."""
    os.makedirs(_tmp_dir(), exist_ok=True)
    token = "t" + secrets.token_hex(16)
    return token, os.path.join(_tmp_dir(), token + ".body")


def _write_json(path: str, meta: dict) -> None:
    tmp = f"{path}.{secrets.token_hex(4)}.part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    os.replace(tmp, path)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _get_in(folder: str, key: str) -> dict | None:
    meta = _read_json(os.path.join(folder, key + ".json"))
    if meta is None or not os.path.exists(os.path.join(folder, key + ".body")):
        return None
    saved_at = meta.get("saved_at")
    if not isinstance(saved_at, (int, float)) or time.time() - saved_at > MAX_AGE_S:
        _remove_entry(folder, key)
        return None
    return dict(meta, token=key)


def get(key: str, clone_dir: str | None = None) -> dict | None:
    """The saved entry's meta for `key` (with `token` = key), or None. An
    entry older than MAX_AGE_S is removed and is a miss. `clone_dir` is the
    canvas folder of the file that runs: its results folder is read first,
    then the home folder."""
    if not _TOKEN_RE.match(key or "") or key.startswith("t"):
        return None
    folder = clone_folder(clone_dir)
    if folder is not None:
        found = _get_in(folder, key)
        if found is not None:
            _remember(key, folder)
            return found
    return _get_in(_results_dir(), key)


def keep(body_path: str, meta: dict, key: str | None, clone_dir: str | None = None) -> dict:
    """File a fresh run's body. With a `key` it becomes the saved entry for
    that key (replacing any earlier one) and its token is the key; without
    one it stays a temporary result under its own token. The entry goes into
    `clone_dir`'s results folder when that canvas folder may hold one
    (`clone_folder`), else into the home folder; a copy of the key in the
    other place is removed. Returns the meta with `token` and `saved_at`."""
    meta = dict(meta, saved_at=time.time())
    token = os.path.basename(body_path)[:-len(".body")]
    if key and _TOKEN_RE.match(key):
        folder = clone_folder(clone_dir)
        with _LOCK:
            if folder is not None:
                try:
                    # From `.fused` down only: `clone_folder` just read a
                    # file in the canvas folder, so that one exists.
                    os.makedirs(folder, exist_ok=True)
                    _move(body_path, os.path.join(folder, key + ".body"))
                    _write_json(os.path.join(folder, key + ".json"), meta)
                    _remember(key, folder)
                    _remove_entry(_results_dir(), key)
                except OSError:
                    # The canvas folder went away or cannot be written.
                    _remove_entry(folder, key)
                    folder = None
            if folder is None:
                os.makedirs(_results_dir(), exist_ok=True)
                os.replace(body_path, os.path.join(_results_dir(), key + ".body"))
                _write_json(os.path.join(_results_dir(), key + ".json"), meta)
                _WHERE.pop(key, None)
                if clone_dir:
                    _remove_entry(_clone_path(clone_dir), key)
        token = key
    else:
        _write_json(os.path.join(_tmp_dir(), token + ".json"), meta)
    _sweep_tmp()
    return dict(meta, token=token)


def drop_body(body_path: str) -> None:
    try:
        os.remove(body_path)
    except OSError:
        pass


def _open_in(folder: str, token: str) -> tuple[str, dict] | None:
    body = os.path.join(folder, token + ".body")
    meta = _read_json(os.path.join(folder, token + ".json"))
    if meta is None or not os.path.exists(body):
        return None
    return body, meta


def open_result(token: str) -> tuple[str, dict] | None:
    """(body path, meta) for a result token, or None. A saved result is
    looked for where this process last saw its key, then in the home folder,
    then in every canvas folder."""
    if not _TOKEN_RE.match(token or ""):
        return None
    if token.startswith("t"):
        return _open_in(_tmp_dir(), token)
    seen = _WHERE.get(token)
    if seen is not None:
        found = _open_in(seen, token)
        if found is not None:
            return found
        _WHERE.pop(token, None)
    found = _open_in(_results_dir(), token)
    if found is not None:
        return found
    for folder in _clone_folders():
        found = _open_in(folder, token)
        if found is not None:
            _remember(token, folder)
            return found
    return None


def _folder_stats(folder: str) -> tuple[int, int]:
    entries = 0
    total = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0, 0
    for name in names:
        if not name.endswith((".body", ".json")):
            continue
        try:
            total += os.path.getsize(os.path.join(folder, name))
        except OSError:
            continue
        if name.endswith(".body"):
            entries += 1
    return entries, total


def _all_folders() -> list[str]:
    return [_results_dir(), *_clone_folders()]


def stats() -> dict:
    """`{entries, bytes}` of the saved results: the home folder's and every
    canvas folder's."""
    entries = 0
    total = 0
    for folder in _all_folders():
        n, size = _folder_stats(folder)
        entries += n
        total += size
    return {"entries": entries, "bytes": total}


def clear() -> dict:
    """Delete every saved result, in the home folder and in every canvas
    folder (Preferences → Debugging). Returns what was cleared,
    `{entries, bytes}`. Temporary results older than a minute go too; a
    younger one may be a run's body about to be filed or a page's result on
    screen, so it is left to the hourly sweep."""
    with _LOCK:
        cleared = stats()
        for folder in _all_folders():
            try:
                names = os.listdir(folder)
            except OSError:
                names = []
            for name in names:
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass
        _WHERE.clear()
    cutoff = time.time() - 60.0
    try:
        tmp_names = os.listdir(_tmp_dir())
    except OSError:
        tmp_names = []
    for name in tmp_names:
        path = os.path.join(_tmp_dir(), name)
        try:
            if os.stat(path).st_mtime < cutoff:
                os.remove(path)
        except OSError:
            pass
    return cleared


def _sweep_tmp() -> None:
    folder = _tmp_dir()
    cutoff = time.time() - TMP_MAX_AGE_S
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        path = os.path.join(folder, name)
        try:
            if os.stat(path).st_mtime < cutoff:
                os.remove(path)
        except OSError:
            pass
