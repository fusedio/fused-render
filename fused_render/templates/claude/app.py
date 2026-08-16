def main(dir: str = ""):
    """Resolve the project folder's app entry: the html file the left pane
    should render. Mirrors the shell's "Open as app" rule — the first
    top-level .html carrying `<meta name="fused-app">` (D301) — so the split
    view opens the same page the `app` mode and that button open. The rule
    itself lives in
    ../shared/app_entry.py, shared with the `app` template so the two views can
    never disagree about which page an app folder is.
    Returns {"entry": <abs path> | None}."""
    import os
    import sys

    shared = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
    # Guarded insert: /api/run may exec this module repeatedly in one worker.
    if shared not in sys.path:
        sys.path.insert(0, shared)
    from app_entry import entry_html

    return {"entry": entry_html(dir)}
