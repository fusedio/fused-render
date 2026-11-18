"""The counts a specialist keeps for the user (PRODUCT §9, PLAN B.6), and the small file helpers the learning loop
shares (`read_json`, `write_json`, `file_lock`).

    <home>/.specialist/counts.json
    {"breakouts_opened", "repeat_breakouts", "pulls", "pulls_confirmed", "corrections_received",
     "notes_by_source": {"task", "breakout", "main", "reviewer", "user", "nightly"}}

Nothing acts on them: they are for the user to see whether the specialist is working. `bump` never raises (a
count that could not be written is logged, never a failed tool call), and it is safe across threads and server
processes (a flock beside the file).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading

logger = logging.getLogger(__name__)

KEYS = ("breakouts_opened", "repeat_breakouts", "pulls", "pulls_confirmed", "corrections_received")
SOURCES = ("task", "breakout", "main", "reviewer", "user", "nightly")
FILE = "counts.json"

_LOCK = threading.RLock()


# ------------------------------------------------------------------ file helpers ---
def read_json(path: str, default=None):
    """The JSON in `path`, or `default` when it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: str, data) -> None:
    """Write `data` to `path` atomically (temp file in the same folder, then rename)."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def file_lock(path: str, blocking: bool = True):
    """An exclusive flock on `path` (created). Yields True when held; with `blocking=False` yields False at once
    when another holder has it. Where flock does not exist (Windows) it only takes the in-process lock."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover — Windows
        fcntl = None
    fh = open(path, "a+")
    try:
        held = True
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except OSError:
                held = False
        try:
            yield held
        finally:
            if held and fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# ------------------------------------------------------------------------ counts ---
def _path(spec_id: str) -> str:
    from fused_render.specialists import paths
    return os.path.join(paths.app_dir(spec_id), FILE)


def _blank() -> dict:
    return {**{k: 0 for k in KEYS}, "notes_by_source": {s: 0 for s in SOURCES}}


def _shape(raw) -> dict:
    out = _blank()
    if not isinstance(raw, dict):
        return out
    for k in KEYS:
        v = raw.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            out[k] = max(0, v)
    by = raw.get("notes_by_source")
    if isinstance(by, dict):
        for s in SOURCES:
            v = by.get(s)
            if isinstance(v, int) and not isinstance(v, bool):
                out["notes_by_source"][s] = max(0, v)
    return out


def get(spec_id: str) -> dict:
    """The B.6 counts of `spec_id` (all zero for one that has none yet)."""
    return _shape(read_json(_path(spec_id), {}))


def bump(spec_id: str, key: str, n: int = 1, source: str | None = None) -> None:
    """Add `n` (may be negative; a count never goes below 0) to `key`. `key` "notes_by_source" (or "notes") counts
    under `source`. An unknown key, an unknown source, the general inbox or a missing home is ignored."""
    try:
        if not spec_id or spec_id == "general":
            return
        from fused_render.specialists import paths
        if not os.path.isdir(paths.home(spec_id)):
            return
        path = _path(spec_id)
        with _LOCK, file_lock(path + ".lock"):
            c = get(spec_id)
            if key in ("notes_by_source", "notes"):
                if source not in SOURCES:
                    return
                c["notes_by_source"][source] = max(0, c["notes_by_source"][source] + int(n))
            elif key in KEYS:
                c[key] = max(0, c[key] + int(n))
            else:
                return
            write_json(path, c)
    except Exception:  # noqa: BLE001
        logger.warning("specialist %s: could not bump count %s", spec_id, key, exc_info=True)
