"""Which html file IS an app folder — the one rule, in one place.

An app is a folder with a single entry page. Two templates have to resolve that
page from the folder alone: `claude_split` (the app beside a Claude chat) and
`app` (the app plainly, full-bleed). They must never disagree — switching modes
would silently swap which page you are looking at — so the rule lives here and
both `app.py` backends delegate to it.

The rule: `index.html` if the folder has one, else the folder's single
non-hidden top-level `.html`. Zero or several (without an index) is ambiguous
and resolves to None — the UI opens the plain folder listing instead of picking
a page for the user.

The server keeps its OWN copy in `fused_render/app_listing.py` (`app_entry`),
deliberately: a template must not import `fused_render` (SPEC PY-15 / D166), and
that copy answers a narrower question — which folders count as apps in the
`GET /api/apps` listing, where an `index.html` beside other pages is not enough
to call the folder a single-entry app.

Imported by a template with the guarded `sys.path.insert` pattern the shared
`appenv` uses:

    shared = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "shared")
    if shared not in sys.path:
        sys.path.insert(0, shared)
    from app_entry import entry_html
"""
import os


def entry_html(dir: str) -> str | None:
    """The app entry page inside `dir`, as an absolute path, or None.

    Never raises: an unreadable or missing directory is "no entry", which is
    what every caller wants to render as a notice.
    """
    dir = os.path.abspath(dir)
    if not os.path.isdir(dir):
        return None
    try:
        names = os.listdir(dir)
    except OSError:
        return None
    # Non-hidden direct children, `.html` only — a hidden html or a sibling
    # `.htm` must not change which page an app folder opens on.
    htmls = [n for n in sorted(names)
             if not n.startswith(".")
             and n.lower().endswith(".html")
             and os.path.isfile(os.path.join(dir, n))]
    for n in htmls:
        if n.lower() == "index.html":
            return os.path.join(dir, n)
    if len(htmls) == 1:
        return os.path.join(dir, htmls[0])
    return None
