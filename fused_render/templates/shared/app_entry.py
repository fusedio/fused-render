"""Which html file IS an app folder — the one rule, in one place.

An app is a folder with a single entry page. Two templates have to resolve that
page from the folder alone: `claude` (the app beside a Claude chat) and
`app` (the app plainly, full-bleed). They must never disagree — switching modes
would silently swap which page you are looking at — so the rule lives here and
both `app.py` backends delegate to it.

The rule: `index.html` if the folder has one, else the FIRST non-hidden
top-level `.html` in name order. Only a folder with no top-level html at all
resolves to None, and then the UI opens the plain folder listing.

Several pages and no `index.html` used to resolve to None as "ambiguous". It is
ambiguous, but None is not the better answer to it: every consumer of this rule
dead-ended on such a folder — the `app` mode and the chat's pane drew "no entry
page" over a folder plainly full of pages, and a `versions` snapshot of one
showed the same notice instead of the app at that commit. Picking the first page
in name order is deterministic (`sorted`, the order the listing shows), it is
the same page every consumer picks, and it is one click from any of the others
once the folder is open. Owner call, on the user's own wording: "for multiple
html files, just pick the first one".

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
    # `sorted` above is what makes "the first" a fact rather than whatever order
    # the filesystem handed back — two consumers reading the same folder must
    # land on the same page, and readdir order is not a promise anywhere.
    return os.path.join(dir, htmls[0]) if htmls else None
