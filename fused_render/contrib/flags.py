"""The contribution loop's EFFECTIVE feature flags (docs/CONTRIB_LOOP.md §1).

Five opt-in prefs (`shell/prefs.py`, default OFF), one per phase, each with a
prerequisite chain: changes and inbox need sources; loop needs changes AND
inbox; remix needs changes. Every server branch on a phase asks THIS module,
never the raw pref, so a phase switched on without its prerequisite behaves
as off everywhere at once — a half-enabled loop (Propose without the record
that says what a copy follows) is a state no code path has to reason about.

One `read_prefs()` per call: `snapshot()` answers all five off a single read
of prefs.json rather than five, and each getter is a snapshot lookup. An
unreadable prefs file reads as every flag off — the default, and the only
answer that keeps today's behaviour byte for byte (§0.6).
"""
from __future__ import annotations


def _raw() -> dict:
    """The five STORED switches as bools (opt-in: only a literal `true` is on)."""
    try:
        from fused_render.shell import prefs

        stored = prefs.read_prefs()
    except Exception:  # noqa: BLE001 — an unreadable pref reads as the default
        stored = {}
    return {
        "sources": stored.get("contrib_sources_enabled") is True,
        "changes": stored.get("contrib_changes_enabled") is True,
        "inbox": stored.get("contrib_inbox_enabled") is True,
        "loop": stored.get("contrib_loop_enabled") is True,
        "remix": stored.get("contrib_remix_enabled") is True,
    }


def effective(raw: dict) -> dict:
    """`raw` (the stored switches) with the prerequisites applied — the same
    rule `contrib-flags.ts` applies client-side."""
    sources = raw.get("sources") is True
    changes = sources and raw.get("changes") is True
    inbox = sources and raw.get("inbox") is True
    return {
        "sources": sources,
        "changes": changes,
        "inbox": inbox,
        "loop": changes and inbox and raw.get("loop") is True,
        "remix": changes and raw.get("remix") is True,
    }


def snapshot() -> dict:
    """`{"sources", "changes", "inbox", "loop", "remix"}`, effective values."""
    return effective(_raw())


def raw() -> dict:
    """The stored switches as set (what Preferences shows), no prerequisites."""
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
    """Whether any phase is effectively on — `sources` is every phase's root
    prerequisite, so this is just that."""
    return sources()
