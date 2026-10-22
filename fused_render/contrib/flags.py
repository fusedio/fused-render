"""The contribution loop's feature flags (docs/CONTRIB_LOOP.md §1).

ONE opt-in pref, `contrib_enabled` (`shell/prefs.py`, default OFF). It used to
be five phase switches (sources, changes, inbox, loop, remix) with a
prerequisite chain; on 2026-10-22 they were folded into one. The five phase
getters below are kept so every server branch keeps asking by phase — they
all answer the one switch now, and `snapshot()` keeps its five-key shape
because `/api/contrib/app` returns it as `flags`.

At most one `read_prefs()` per change of prefs.json (`_raw`'s stat memo): the
flag is asked on every sync, card and Doctor row, and a `stat` is all an
unchanged file costs. An unreadable prefs file reads as off — the default,
and the only answer that keeps today's behaviour byte for byte (§0.6).
"""
from __future__ import annotations

import os
import threading


_memo_lock = threading.Lock()
_memo: dict = {}  # {"key": (path, mtime_ns, size, ino), "raw": dict}


def _raw() -> dict:
    """The STORED switch as a bool (opt-in: only a literal `true` is on).
    Memoized on prefs.json's stat (path, mtime, size, inode): the flags are
    asked on every sync, card and Doctor row, and a `stat` is all an
    unchanged file costs. `storage.write_json` replaces the file atomically,
    so every write is a new inode and a fresh read."""
    try:
        from fused_render.shell import prefs

        p = prefs._path()
        try:
            st = os.stat(p)
            key = (p, st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            key = (p, None, None, None)
        with _memo_lock:
            if _memo.get("key") == key:
                return dict(_memo["raw"])
        stored = prefs.read_prefs()
    except Exception:  # noqa: BLE001 — an unreadable pref reads as the default
        return _switches({})
    raw = _switches(stored)
    with _memo_lock:
        _memo["key"], _memo["raw"] = key, raw
    return dict(raw)


def _switches(stored: dict) -> dict:
    return {"enabled": stored.get("contrib_enabled") is True}


def effective(raw: dict) -> dict:
    """`raw` (the stored switch) as the five phase values — every phase is
    the one switch; same rule `contrib-flags.ts` applies client-side."""
    on = raw.get("enabled") is True
    return {"sources": on, "changes": on, "inbox": on, "loop": on, "remix": on}


def snapshot() -> dict:
    """`{"sources", "changes", "inbox", "loop", "remix"}`, effective values."""
    return effective(_raw())


def raw() -> dict:
    """The stored switch as set (what Preferences shows): `{"enabled": bool}`."""
    return _raw()


def sources() -> bool:
    return snapshot()["sources"]


def changes() -> bool:
    return snapshot()["changes"]


def inbox() -> bool:
    return snapshot()["inbox"]


def loop() -> bool:
    return snapshot()["loop"]


def remix() -> bool:
    return snapshot()["remix"]


def any_on() -> bool:
    """Whether the loop is on — every phase is the one switch."""
    return sources()
