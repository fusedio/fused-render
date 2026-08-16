"""Which html file IS an app folder — the one rule, in one place.

An app is a folder with a single entry page. Two templates have to resolve that
page from the folder alone: `claude` (the app beside a Claude chat) and
`app` (the app plainly, full-bleed). They must never disagree — switching modes
would silently swap which page you are looking at — so the rule lives here and
both `app.py` backends delegate to it.

The rule: the FIRST non-hidden top-level `.html` (name order) carrying
`<meta name="fused-app">` — the declarative marker, and THE ONLY SIGNAL
(D301). A folder with no tagged page resolves to None, and then the UI opens
the plain folder listing. Filenames declare nothing: `index.html` has no
special status, not even as a tiebreaker — the name rules this replaced (D269:
index.html, else first html) were a guess about intent read off a filename,
and a name rule kept "just in case" is the guess sneaking back in. First in
NAME order among tagged pages, so the answer is deterministic and every
consumer lands on the same page.

The server keeps its OWN copy in `fused_render/app_listing.py` (`app_entry`),
deliberately: a template must not import `fused_render` (SPEC PY-15 / D166).
The two are the same rule to the letter, held together by tests.

Imported by a template with the guarded `sys.path.insert` pattern the shared
`appenv` uses:

    shared = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "shared")
    if shared not in sys.path:
        sys.path.insert(0, shared)
    from app_entry import entry_html
"""
import os
import re

# COPY of `fused_render/app_listing.py`'s marker rule (a template cannot import
# `fused_render` — SPEC PY-15 / D166); parity is held by the tests both modules
# share. Head bytes only, same budget as the listing's title read.
_FUSED_META_RE = re.compile(
    rb"<meta\s[^>]*name\s*=\s*[\"']?fused-app[\"']?", re.IGNORECASE)


def _has_fused_meta(html_path: str) -> bool:
    try:
        with open(html_path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return _FUSED_META_RE.search(head) is not None


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
    # `.htm` must not change which page an app folder opens on. `sorted` is what
    # makes "the first" a fact rather than whatever order the filesystem handed
    # back — two consumers reading the same folder must land on the same page,
    # and readdir order is not a promise anywhere.
    for n in sorted(names):
        if n.startswith(".") or not n.lower().endswith(".html"):
            continue
        p = os.path.join(dir, n)
        if os.path.isfile(p) and _has_fused_meta(p):
            return p
    return None
