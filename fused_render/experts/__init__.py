"""Experts (docs/experts.md): bots that keep one memory file about one subject.
The harness (Claude Code) does the thinking; this package only delivers messages."""


def enabled() -> bool:
    """The Experts experimental flag (`experts_enabled`, shell/prefs.py; default OFF, docs/experts.md §17).

    Off, experts are HIDDEN, never removed: their folders, memory.md, sessions and settings stay on disk as they
    are, and turning the flag back on brings them back with the same memory. Never raises: a failed read is off."""
    try:
        from fused_render.shell import prefs
        return prefs.experts_enabled()
    except Exception:  # noqa: BLE001 — an unreadable pref is the default, off
        return False
