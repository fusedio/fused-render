"""Saved UDF results on disk (SPEC §54.3).

One entry per run key: `results/<key>.body` (the bytes) and
`results/<key>.json` (content type, run output, when saved). A result that
must not be kept (no key: the file changed while it ran) still needs a URL
for the page that asked for it: it lands in `tmp/<token>.body` with its
`.json` beside it, and is swept after TMP_MAX_AGE_S.

Rules:
  - only successful runs are put here (the service's rule, not checked here)
  - no limits: any size is kept, nothing is evicted and nothing expires.
    A run of the same key replaces its entry; Preferences → Debugging →
    "Saved UDF cache" (`clear`) is how the folder is emptied.

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
    """The saved entry's meta for `key` (with `token` = key), or None."""
    if not _TOKEN_RE.match(key or "") or key.startswith("t"):
        return None
    meta = _read_json(os.path.join(_results_dir(), key + ".json"))
    if meta is None or not os.path.exists(os.path.join(_results_dir(), key + ".body")):
        return None
    return dict(meta, token=key)


def keep(body_path: str, meta: dict, key: str | None) -> dict:
    """File a fresh run's body. With a `key` it becomes the saved entry for
    that key (replacing any earlier one) and its token is the key; without
    one it stays a temporary result under its own token. Returns the meta
    with `token` and `saved_at`."""
    meta = dict(meta, saved_at=time.time())
    token = os.path.basename(body_path)[:-len(".body")]
    if key and _TOKEN_RE.match(key):
        os.makedirs(_results_dir(), exist_ok=True)
        with _LOCK:
            os.replace(body_path, os.path.join(_results_dir(), key + ".body"))
            _write_json(os.path.join(_results_dir(), key + ".json"), meta)
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


def stats() -> dict:
    """`{entries, bytes}` of the saved results (`results/`)."""
    entries = 0
    total = 0
    try:
        names = os.listdir(_results_dir())
    except OSError:
        return {"entries": 0, "bytes": 0}
    for name in names:
        if not name.endswith((".body", ".json")):
            continue
        try:
            total += os.path.getsize(os.path.join(_results_dir(), name))
        except OSError:
            continue
        if name.endswith(".body"):
            entries += 1
    return {"entries": entries, "bytes": total}


def clear() -> dict:
    """Delete every saved result (Preferences → Debugging). Returns what
    was cleared, `{entries, bytes}`. Temporary results older than a minute
    go too; a younger one may be a run's body about to be filed or a page's
    result on screen, so it is left to the hourly sweep."""
    with _LOCK:
        cleared = stats()
        folder = _results_dir()
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
        for name in names:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
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
