def main(dir: str = ""):
    """Resolve the app folder's entry page — the html the view renders full-bleed.

    The rule is shared with `claude/app.py` (../shared/app_entry.py) so the
    plain view and the split view can never open different pages for the same
    folder. Returns {"entry": <abs path> | None}."""
    import os
    import sys

    shared = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
    # Guarded insert: /api/run may exec this module repeatedly in one worker.
    if shared not in sys.path:
        sys.path.insert(0, shared)
    from app_entry import entry_html

    return {"entry": entry_html(dir)}
