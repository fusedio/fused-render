"""The entry contract shared by the sources `GET /api/apps` merges.

The Home/apps listing has two sources now (D205): the Fused workspace
(`server/routers/apps.py` — one folder per app, entry = its single direct-child
`.html`) and the Claude Science artifact store (`claude_science.py` — one
directory per artifact, entry = its newest version file). They report the same
shape, so the two pieces of it that would otherwise be written twice live here:
what counts as an HTML entry, and how a title is read out of one.

`.htm` is included alongside `.html` because the template registry binds both
to the render pipeline — an entry the shell will render as a page is an entry
this listing should treat as one.
"""
import html
import os
import re

#: Suffixes the shell renders as a page (registry.json binds both to `_render`).
HTML_SUFFIXES = (".html", ".htm")

_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def is_html(path: str) -> bool:
    """Whether `path` names a renderable HTML page (by extension alone — no I/O)."""
    return path.lower().endswith(HTML_SUFFIXES)


def entry_title(entry_html: str) -> str | None:
    """The <title> of an entry file, from its first 4 KiB — cheap enough to run
    per app on every listing. None when absent, empty, or unreadable."""
    try:
        with open(entry_html, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    match = _TITLE_RE.search(head)
    if not match:
        return None
    title = html.unescape(match.group(1).decode("utf-8", "replace"))
    return " ".join(title.split()) or None


def dir_updated_at(dir_path: str) -> float | None:
    """When a folder-shaped app was last touched, as an epoch float (st_mtime).

    Max of the dir's own mtime and its DIRECT children's — the dir mtime alone
    only moves on add/remove/rename, so editing index.html in place wouldn't
    register; a deep walk is unbounded work per listing for marginal gain
    (edits in an app land overwhelmingly in top-level files). One extra stat
    per child, no recursion. None when nothing stats (racing delete)."""
    latest = None
    try:
        latest = os.stat(dir_path).st_mtime
        with os.scandir(dir_path) as it:
            for child in it:
                try:
                    latest = max(latest, child.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return latest
