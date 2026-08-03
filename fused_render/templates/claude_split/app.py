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
    htmls = [n for n in sorted(names)
             if n.lower().endswith((".html", ".htm"))
             and os.path.isfile(os.path.join(dir, n))]
    for n in htmls:
        if n.lower() in ("index.html", "index.htm"):
            return {"entry": os.path.join(dir, n)}
    if len(htmls) == 1:
        return {"entry": os.path.join(dir, htmls[0])}
    return {"entry": None}
