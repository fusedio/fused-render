def main(dir: str = ""):
    """Resolve the project folder's app entry: the html file the left pane
    should render. Mirrors the shell's "Open as app" rule — index.html first,
    else the folder's single top-level .html — so the split view opens the
    same page that button would. Returns {"entry": <abs path> | None}."""
    import os

    dir = os.path.abspath(dir)
    if not os.path.isdir(dir):
        return {"entry": None}
    try:
        names = os.listdir(dir)
    except OSError:
        return {"entry": None}
    # Same filter as the apps API's `_app_entry` (server/routers/apps.py):
    # non-hidden direct children, `.html` only — a hidden html or a sibling
    # `.htm` must not change which folders count as apps.
    htmls = [n for n in sorted(names)
             if not n.startswith(".")
             and n.lower().endswith(".html")
             and os.path.isfile(os.path.join(dir, n))]
    for n in htmls:
        if n.lower() == "index.html":
            return {"entry": os.path.join(dir, n)}
    if len(htmls) == 1:
        return {"entry": os.path.join(dir, htmls[0])}
    return {"entry": None}
