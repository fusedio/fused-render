"""The Tasks listing's derived facts, kept across restarts.

WHAT THIS IS (2026-10-09, Tasks latency design D1). The listing is derived from
disk — every Claude transcript on the machine, read once from byte zero and then
incrementally (`routers/tasks._scan`), plus a head parse per transcript
(`tasks_store.head`). Those two caches live in memory, so every launch started
with both empty and the first build read close to a gigabyte before the Tasks
page could show a row: 4 to 15 seconds on the owner's machine, in every launch
logged. This file is those two caches on disk, keyed by what makes them valid.

WHAT THIS IS NOT. Not a second source of truth: the transcripts stay the truth,
the watcher still stat-polls them, and a file whose size or mtime moved is
re-read exactly as before. A row served from the index is the row a fresh build
would produce, because the derivation is a pure function of the file's bytes
and `_scan`'s own validity test (size) is what the key reproduces; the mtime is
one more guard on top. The file is a cache: delete it and the next launch is a
cold one, nothing more.

THE FORMAT IS THE RELEASE. The facts stored here are whatever `_absorb`,
`_condense_reply` and `_parse_head` produce, and a code change to either is a
format change nobody should have to remember to version. So the table is
stamped with the package version and dropped whole when it differs: one cold
build per release, never a stale fact across one.

SQLite, one table, upserts of the handful of transcripts a build actually
touched. A JSON blob would be rewritten whole once a second while a session
is live; a database writes the two rows that changed.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

from fused_render import __version__, tasks_store

logger = logging.getLogger(__name__)

FILE_NAME = "tasks-index.sqlite"

_LOCK = threading.Lock()
# path -> size last written, so a build's save touches only what moved.
_saved: dict[str, int] = {}
_enabled = False


def path() -> str:
    return os.path.join(tasks_store.STATE_DIR, FILE_NAME)


def _connect() -> sqlite3.Connection:
    os.makedirs(tasks_store.STATE_DIR, exist_ok=True)
    con = sqlite3.connect(path(), timeout=5.0, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS transcripts ("
        " path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,"
        " head TEXT NOT NULL, scan TEXT NOT NULL, updated REAL NOT NULL)")
    row = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    if row is None or row[0] != __version__:
        con.execute("DELETE FROM transcripts")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)", (__version__,))
    return con


def _stat(p: str) -> tuple[int, int] | None:
    try:
        st = os.stat(p)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def seed(scan_cache: dict[str, dict]) -> tuple[int, int]:
    """Fill the router's scan cache and the store's head cache from the index,
    for every transcript whose size and mtime still match. Returns (seeded,
    held) — how many were taken, how many the index held. Called ONCE, by the
    warm-up, before the first build; never by a build itself."""
    global _enabled
    started = time.monotonic()
    seeded = held = 0
    try:
        with _LOCK:
            con = _connect()
            try:
                rows = con.execute(
                    "SELECT path, size, mtime_ns, head, scan FROM transcripts").fetchall()
            finally:
                con.close()
        held = len(rows)
        for p, size, mtime_ns, head_text, scan_text in rows:
            live = _stat(p)
            if live is None or live != (size, mtime_ns):
                continue
            try:
                head = json.loads(head_text)
                scan = json.loads(scan_text)
            except ValueError:
                continue
            if not isinstance(scan, dict) or scan.get("size") != size:
                continue
            if not isinstance(head, list) or len(head) != 6:
                continue
            cwd, first_ts, prompt, pane, entrypoint, settled = head
            tasks_store._HEAD_CACHE[p] = (size, cwd, first_ts, prompt, pane,
                                          entrypoint, bool(settled))
            scan_cache[p] = scan
            _saved[p] = size
            seeded += 1
        _enabled = True
    except Exception:  # noqa: BLE001 — a cache that cannot be read is a cold start
        logger.warning("tasks index: unreadable, starting cold", exc_info=True)
        _reset_file()
        _enabled = True
    logger.info("tasks index: seeded %d of %d transcripts in %.2fs",
                seeded, held, time.monotonic() - started)
    return seeded, held


def save(scan_cache: dict[str, dict]) -> int:
    """Write every transcript whose scan moved since the last save, with the
    head the store holds for it. Returns how many rows were written. A no-op
    until `seed` has run, so tests and lean servers never touch the disk."""
    if not _enabled:
        return 0
    pending: list[tuple] = []
    now = time.time()
    for p, scan in list(scan_cache.items()):
        size = scan.get("size")
        if not isinstance(size, int) or size < 0:
            continue
        if _saved.get(p) == size:
            continue
        head = tasks_store._HEAD_CACHE.get(p)
        if head is None or head[0] != size:
            # The head is re-read lazily, on the next `_place`; without it the
            # row is not reproducible from the index, so it waits a build.
            continue
        live = _stat(p)
        if live is None or live[0] != size:
            continue
        try:
            pending.append((p, size, live[1],
                            json.dumps(list(head[1:]), default=str),
                            json.dumps(scan, default=str), now))
        except (TypeError, ValueError):
            continue
    if not pending:
        return 0
    try:
        with _LOCK:
            con = _connect()
            try:
                con.execute("BEGIN")
                con.executemany(
                    "INSERT OR REPLACE INTO transcripts"
                    " (path, size, mtime_ns, head, scan, updated) VALUES (?,?,?,?,?,?)",
                    pending)
                con.execute("COMMIT")
            finally:
                con.close()
    except Exception:  # noqa: BLE001 — a save that fails costs the next launch its warmth
        logger.debug("tasks index: save failed", exc_info=True)
        return 0
    for row in pending:
        _saved[row[0]] = row[1]
    return len(pending)


def forget() -> None:
    """Drop what this process remembers having saved (tests, `reset_cache`)."""
    _saved.clear()


def _reset_file() -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path() + suffix)
        except OSError:
            pass
