"""Saved UDF results on disk (SPEC §54.3).

One entry per run key: `results/<key>.body` (the bytes) and
`results/<key>.json` (content type, run output, when saved). A result too
big to keep, or one that must not be kept, still needs a URL for the page
that asked for it: it lands in `tmp/<token>.body` with its `.json` beside
it, and is swept after TMP_MAX_AGE_S.

Rules:
  - only successful runs are put here (the service's rule, not checked here)
  - a body over MAX_ENTRY_BYTES is never kept
  - MAX_TOTAL_BYTES across `results/`, least recently read evicted first
    (a read touches the entry)
  - an entry older than MAX_AGE_S is dropped on read

Lives under `<FUSED_RENDER_HOME or ~/.fused-render>/cache/udf/`, never inside
a canvas folder (a clone publishes itself). All writes go through a temp file
and `os.replace`, so a reader never sees half an entry.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time

MAX_ENTRY_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_AGE_S = 7 * 24 * 3600.0
TMP_MAX_AGE_S = 3600.0

_TOKEN_RE = re.compile(r"^(?:[0-9a-f]{64}|t[0-9a-f]{32})$")
_LOCK = threading.Lock()


def root() -> str:
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(base, "cache", "udf")


def _results_dir() -> str:
    return os.path.join(root(), "results")


def _tmp_dir() -> str:
    return os.path.join(root(), "tmp")


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


def get(key: str) -> dict | None:
    """The saved entry's meta for `key` (with `token` = key), or None. An
    entry past MAX_AGE_S is removed and is a miss. A hit is touched so the
    eviction order is least recently READ."""
    if not _TOKEN_RE.match(key or "") or key.startswith("t"):
        return None
    meta_path = os.path.join(_results_dir(), key + ".json")
    body_path = os.path.join(_results_dir(), key + ".body")
    meta = _read_json(meta_path)
    if meta is None or not os.path.exists(body_path):
        return None
    saved_at = meta.get("saved_at")
    if not isinstance(saved_at, (int, float)) or time.time() - saved_at > MAX_AGE_S:
        _remove(key)
        return None
    now = time.time()
    for p in (meta_path, body_path):
        try:
            os.utime(p, (now, now))
        except OSError:
            pass
    return dict(meta, token=key)


def keep(body_path: str, meta: dict, key: str | None) -> dict:
    """File a fresh run's body. With a `key` and a body within
    MAX_ENTRY_BYTES it becomes the saved entry for that key (replacing any
    earlier one) and its token is the key; otherwise it stays a temporary
    result under its own token. Returns the meta with `token` and
    `saved_at`."""
    meta = dict(meta, saved_at=time.time())
    size = os.path.getsize(body_path)
    token = os.path.basename(body_path)[:-len(".body")]
    if key and _TOKEN_RE.match(key) and size <= MAX_ENTRY_BYTES:
        os.makedirs(_results_dir(), exist_ok=True)
        with _LOCK:
            os.replace(body_path, os.path.join(_results_dir(), key + ".body"))
            _write_json(os.path.join(_results_dir(), key + ".json"), meta)
            _evict()
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


def open_result(token: str) -> tuple[str, dict] | None:
    """(body path, meta) for a result token, or None."""
    if not _TOKEN_RE.match(token or ""):
        return None
    folder = _tmp_dir() if token.startswith("t") else _results_dir()
    body = os.path.join(folder, token + ".body")
    meta = _read_json(os.path.join(folder, token + ".json"))
    if meta is None or not os.path.exists(body):
        return None
    return body, meta


def _remove(key: str) -> None:
    for ext in (".body", ".json"):
        try:
            os.remove(os.path.join(_results_dir(), key + ext))
        except OSError:
            pass


def _evict() -> None:
    """Drop the least recently read entries until `results/` fits
    MAX_TOTAL_BYTES. Called under _LOCK."""
    folder = _results_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        return
    entries = []
    total = 0
    for name in names:
        if not name.endswith(".body"):
            continue
        try:
            st = os.stat(os.path.join(folder, name))
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, name[:-len(".body")]))
        total += st.st_size
    entries.sort()
    for _mtime, size, key in entries:
        if total <= MAX_TOTAL_BYTES:
            break
        _remove(key)
        total -= size


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
