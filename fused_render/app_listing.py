"""The entry contract behind `GET /api/apps`, extracted from its router.

An app is a folder with a single entry page: `<workspace>/<tag>/<name>/`, whose
entry is its one non-hidden direct-child `.html`. The walk that finds them, and
the two facts reported about each one — a title read out of the entry, and when
the folder was last touched — live here rather than inside the route handler, so
they can be tested and reused without a `TestClient`.

Nothing in this module raises for a directory it cannot read: a listing degrades
to what it could see. The one deliberate exception is `app_entry`, which lets
its `OSError` out so the CALLER can tell "unreadable, skip it" from "no entry,
list it as a folder" — see `app_dict`.
"""
import html
import os
import re

_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def app_entry(dir_path: str) -> str | None:
    """An app folder's entry: the single non-hidden direct-child `.html`, or None
    when the folder has zero or several (ambiguous — the UI opens the folder).
    Raises OSError when the dir can't be listed; every caller skips those."""
    children = os.listdir(dir_path)
    htmls = [
        c for c in sorted(children)
        if not c.startswith(".")
        and c.lower().endswith(".html")
        and os.path.isfile(os.path.join(dir_path, c))
    ]
    if len(htmls) != 1:
        return None
    return os.path.abspath(os.path.join(dir_path, htmls[0]))


def app_dict(path: str, name: str, tag: str, entry_html: str | None) -> dict:
    """One app's listing entry — the single place the shape is built.

    `entry_html` is resolved by the CALLER, deliberately. Resolving it in here
    would mean swallowing `app_entry`'s `OSError`, which quietly turns "this
    directory cannot be read, skip it" into "this directory has no entry, list
    it as a folder" — an unreadable app comes back as a card. The caller is the
    only one that knows which of those it wants, so it decides.
    """
    return {
        "name": name,
        "tag": tag,
        "path": os.path.abspath(path),
        # `entry` is the file a card opens and previews. For an app of this
        # shape that is exactly its entry HTML; it is reported under its own key
        # because "the file to open" and "this entry is a renderable page" are
        # different claims, and only the second one may be handed to the
        # HTML-only /render iframe. The shell reads `entry` (see the frontend's
        # entryOf) and falls back to `entry_html` against an older server.
        "entry": entry_html,
        "entry_html": entry_html,
        "title": entry_title(entry_html) if entry_html else None,
        "updated_at": dir_updated_at(path),
    }


def two_level_apps(root: str) -> list[dict]:
    """Every app in a Fused-shaped folder: `<root>/<tag>/<name>/`.

    A "tag" is any non-hidden top-level directory, an "app" any non-hidden
    directory directly inside one — no registry, so a new tag is just a new
    folder.

    Skips whatever it cannot read at every level and returns [] for a root that
    isn't listable (no workspace yet, on a first run) — a listing degrades, it
    never fails. Sorted at both levels so a partial result is still stable.
    """
    apps: list[dict] = []
    try:
        tag_names = os.listdir(root)
    except OSError:
        return apps
    for tag in sorted(tag_names):
        if tag.startswith("."):
            continue
        tag_path = os.path.join(root, tag)
        try:
            if not os.path.isdir(tag_path):
                continue
            names = os.listdir(tag_path)
        except OSError:
            continue  # unreadable/racing tag dir: skip, never fail the listing
        for name in sorted(names):
            if name.startswith("."):
                continue
            path = os.path.join(tag_path, name)
            try:
                if not os.path.isdir(path):
                    continue
                entry_html = app_entry(path)
            except OSError:
                # Unreadable or racing: SKIPPED, not listed as an entry-less
                # card. Resolving the entry inside this guard is what makes
                # that distinction — see `app_dict`.
                continue
            apps.append(app_dict(path, name, tag, entry_html))
    return apps


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
    register; a deep walk is unbounded work per listing for marginal gain (edits
    in an app land overwhelmingly in top-level files). One extra stat per child,
    no recursion. None when nothing stats (racing delete)."""
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
