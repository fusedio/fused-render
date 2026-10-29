"""The contribution loop's feature flags (docs/CONTRIB_LOOP.md §1).

The loop (sources, changes, inbox, loop) is ALWAYS ON since 2026-10-29 — it
was one opt-in pref (`contrib_enabled`), and until 2026-10-22 four separate
phase switches with a prerequisite chain. One opt-in pref remains
(`shell/prefs.py`, default OFF): `contrib_remix_enabled` is Remix. The five
phase getters below are kept so every server branch keeps asking by phase,
and `snapshot()` keeps its five-key shape because `/api/contrib/app` returns
it as `flags`.

At most one `read_prefs()` per change of prefs.json (`_raw`'s stat memo): the
flag is asked on every sync, card and Doctor row, and a `stat` is all an
unchanged file costs. An unreadable prefs file reads as off — the default,
and the only answer that keeps today's behaviour byte for byte (§16.0 P1).
"""
from __future__ import annotations

import os
import threading


_memo_lock = threading.Lock()
_memo: dict = {}  # {"key": (path, mtime_ns, size, ino), "raw": dict}


def _raw() -> dict:
    """The STORED switches as bools (opt-in: only a literal `true` is on).
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
    # `enabled` (the loop) and `marketplace` (the Community) are always on
    # (2026-10-29); the keys stay so `effective` and `raw` keep their shape.
    return {"enabled": True,
            "remix": stored.get("contrib_remix_enabled") is True,
            "marketplace": True}


def effective(raw: dict) -> dict:
    """`raw` (the stored switches) as the five phase values — the loop's
    four phases are always on, remix is its own switch; same rule
    `contrib-flags.ts` applies client-side."""
    on = raw.get("enabled") is True
    return {"sources": on, "changes": on, "inbox": on, "loop": on,
            "remix": on and raw.get("remix") is True}


def snapshot() -> dict:
    """`{"sources", "changes", "inbox", "loop", "remix"}`, effective values."""
    return effective(_raw())


def raw() -> dict:
    """The stored switches as set (what Preferences shows):
    `{"enabled": bool, "remix": bool}`, no prerequisite applied."""
    got = _raw()
    return {"enabled": got.get("enabled") is True, "remix": got.get("remix") is True}


def sources() -> bool:
    return snapshot()["sources"]


def marketplace_ui() -> bool:
    """Whether a MARKETPLACE copy's UI is shown (docs/CONTRIB_LOOP.md §18.8,
    owner 2026-10-26): its Source panel, its buttons (Check for a new
    release, Bring in, the switches, the block fixes) and its card
    label. Always on since 2026-10-29 (it followed the Marketplace switch OR
    the contribution loop); kept as the one rule every site asks."""
    return _raw().get("marketplace") is True or sources()


def changes() -> bool:
    return snapshot()["changes"]


def inbox() -> bool:
    return snapshot()["inbox"]


def loop() -> bool:
    return snapshot()["loop"]


def remix() -> bool:
    return snapshot()["remix"]
