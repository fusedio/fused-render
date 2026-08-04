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

#: Machine bookkeeping, never the user's work. Two consumers, same reason: a
#: name here is not an app (`two_level_apps`), and its mtime is not an edit
#: (`dir_updated_at`). `__pycache__` is why the set exists — the executor used
#: to write one on every run, so an app you merely OPENED reported as
#: touched-just-now and displaced one you had actually edited from the top of
#: Recent, and a folder of .pyc files listed as a card of its own. Nothing
#: writes it any more (`_child.py`, `engine.build_code`), but an app can still
#: acquire one from a terminal or an editor, and folders that already have one
#: must stop lying. `.git` is here for the same reason: a commit — including
#: the automatic one after a Claude turn — rewrites it, and the edit that
#: triggered that commit has already moved a real file's mtime.
IGNORED_CHILDREN = frozenset({"__pycache__", ".git"})


def is_html(path: str) -> bool:
    """Whether `path` names a renderable HTML page (by extension alone — no I/O)."""
    return path.lower().endswith(HTML_SUFFIXES)


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


def two_level_apps(root: str, source: str) -> list[dict]:
    """Every app in a Fused-shaped folder: `<root>/<tag>/<name>/`.

    A "tag" is any non-hidden top-level directory, an "app" any non-hidden
    directory directly inside one — no registry, so a new tag is just a new
    folder. Shared by the two sources that use this shape (D207): the workspace
    itself (`routers/apps.py`) and the extra workspaces discovered from Claude
    Code's project list (`claude_projects.py`), which differ only in `source`.

    Skips whatever it cannot read at every level and returns [] for a root that
    isn't listable — a listing degrades, it never fails."""
    apps: list[dict] = []
    try:
        tag_names = os.listdir(root)
    except OSError:
        return apps
    for tag in sorted(tag_names):
        if tag.startswith(".") or tag in IGNORED_CHILDREN:
            continue
        tag_path = os.path.join(root, tag)
        try:
            if not os.path.isdir(tag_path):
                continue
            names = os.listdir(tag_path)
        except OSError:
            continue  # unreadable/racing tag dir: skip, never fail the listing
        for name in sorted(names):
            # A machine-written directory is never an app, at either level.
            # Hidden names already go (that covers `.git`); `__pycache__` does
            # not start with a dot and did not, so a folder of .pyc files came
            # back as an entry-less card — seen for real on a user's tree, where
            # `render/soccer/__pycache__` listed as the app `soccer/__pycache__`.
            if name.startswith(".") or name in IGNORED_CHILDREN:
                continue
            path = os.path.join(tag_path, name)
            try:
                if not os.path.isdir(path):
                    continue
                entry_html = app_entry(path)
            except OSError:
                continue  # unreadable/racing entry: skip, never fail the listing
            apps.append({
                "name": name,
                "tag": tag,
                "path": os.path.abspath(path),
                # `entry` is the file a card opens and previews. For an app of
                # this shape that is exactly its entry HTML — the second key
                # exists for sources whose entry may be a figure or a table
                # (claude_science.py), and both are reported by every source so
                # the shell needs no per-source branch.
                "entry": entry_html,
                "entry_html": entry_html,
                "title": entry_title(entry_html) if entry_html else None,
                "updated_at": dir_updated_at(path),
                "source": source,
            })
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

    Max of the dir's own mtime and its DIRECT children's, skipping
    `IGNORED_CHILDREN` — the dir mtime alone only moves on add/remove/rename,
    so editing index.html in place wouldn't register; a deep walk is unbounded
    work per listing for marginal gain (edits in an app land overwhelmingly in
    top-level files). One extra stat per child, no recursion. None when nothing
    stats (racing delete).

    The dir's OWN mtime is still counted even though creating a `__pycache__`
    moves it: it is one add/remove-shaped event, not a per-run signal, and
    dropping it would lose the real add/remove/rename it exists to catch."""
    latest = None
    try:
        latest = os.stat(dir_path).st_mtime
        with os.scandir(dir_path) as it:
            for child in it:
                if child.name in IGNORED_CHILDREN:
                    continue
                try:
                    latest = max(latest, child.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return latest
