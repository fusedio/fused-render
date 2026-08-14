"""The entry contract behind `GET /api/apps`, extracted from its router.

An app is a folder in the workspace, one to three levels down, whose entry is
its `index.html`, else its first non-hidden direct-child `.html` in name
order (`app_entry`, the shared rule — D269). The walk that finds them
(`workspace_apps`, which is where the per-level rules are written down), and
the facts reported about each one — a title read out of the entry, an authored
`preview.png` thumbnail if there is one, and when the folder was last touched —
live here rather than inside the route handler, so they can be tested and reused
without a `TestClient`.

Nothing in this module raises for a directory it cannot read: a listing degrades
to what it could see. The one deliberate exception is `app_entry`, which lets
its `OSError` out so the CALLER can tell "unreadable, skip it" from "no entry,
list it as a folder" — see `app_dict`.
"""
import html
import json
import os
import re
import stat

from fused_render.index.ignore import SHARED_IGNORE_DIRS, MountGuard

_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def app_entry(dir_path: str) -> str | None:
    """An app folder's entry page: `index.html` if the folder has one, else the
    FIRST non-hidden direct-child `.html` in name order. None only when the
    folder has no top-level `.html` at all.

    Raises OSError when the dir can't be listed; every caller skips those.

    THE SAME RULE AS `templates/shared/app_entry.py::entry_html`, deliberately
    and to the letter — that module's docstring carries the reasoning, and
    `tests/test_shared_app_entry.py` and `tests/test_app_listing.py` ask both the
    same questions. A folder resolving to one page for the card that opens it and
    another for the template that renders it is the failure this parity prevents.

    It USED to be narrower on purpose: the single non-hidden `.html`, with zero
    or several meaning None ("ambiguous — the UI opens the folder"). D269 removed
    that divergence rather than preserving it, on the owner's rule that a folder
    with a top-level html IS that page at every surface. The narrow rule made a
    two-page folder a card that opened a file listing — the one outcome the rule
    forbids — and "ambiguous" was never a better answer than the deterministic
    first page, which is the same page the chat, the history and the preview pane
    have all been picking since the shared rule widened.

    The ONE divergence that remains is the OSError: this raises where the shared
    copy swallows, so `workspace_apps` can tell "unreadable, skip this folder"
    from "no entry, list it as a folder" (see `app_dict`). A template has no such
    distinction to draw — it renders a notice either way.

    Still a COPY rather than an import: a template must not import `fused_render`
    (SPEC PY-15 / D166), and `templates/` is packaged data, not an importable
    package, so neither side can reach the other. The parity is held by tests.
    """
    children = os.listdir(dir_path)
    htmls = [
        c for c in sorted(children)
        if not c.startswith(".")
        and c.lower().endswith(".html")
        and os.path.isfile(os.path.join(dir_path, c))
    ]
    if not htmls:
        return None
    for c in htmls:
        if c.lower() == "index.html":
            return os.path.abspath(os.path.join(dir_path, c))
    # `sorted` above is what makes "the first" a fact rather than whatever order
    # the filesystem handed back — the shell and the templates read the same
    # folder and must land on the same page.
    return os.path.abspath(os.path.join(dir_path, htmls[0]))


# The one authored thumbnail name. A card's picture of an app is otherwise the
# entry page rendered live in a scaled iframe, which is honest but is also a
# whole page load per card and shows whatever the app looks like with no data in
# it; dropping a `preview.png` in the folder is how an author overrides that.
#
# Exactly one name, not a search over `preview.*` or `screenshot.*`: a user
# adding a thumbnail should never have to work out which of several candidates
# wins. Matched EXACTLY, case included — see `app_preview_image` for why that
# costs a listdir rather than a stat.
PREVIEW_IMAGE_NAME = "preview.png"


def app_preview_image(dir_path: str) -> str | None:
    """The app folder's authored thumbnail (`preview.png` at its root), or None.

    Resolved by LISTING the directory rather than probing the path, and the
    reason is case: `os.path.isfile` inherits the filesystem's own case-folding,
    so a `Preview.png` would be a thumbnail on macOS/Windows and not on ext4 —
    the same folder answering differently per machine, and disagreeing with the
    explorer's peek rule (apps/explorer/lib/folder-peek.ts), which compares the
    name exactly. An exact membership test is the only rule both sides can hold.

    Zero-length is treated as absent. A truncated or interrupted write leaves a
    file `isfile` is perfectly happy with, and a non-null answer here is
    load-bearing in a way the entry rule's is not: the card's fallbacks (the
    live render, the monogram) are only reachable while this is None, so a
    wrongly-confident path is a permanently broken image rather than a
    degraded one. It is not a validity check and does not pretend to be — a
    corrupt non-empty PNG still gets through, which is why both card surfaces
    also carry an onError fallback.

    Never raises: an unreadable or vanished directory is "no picture", which is
    exactly what the caller renders.
    """
    try:
        if PREVIEW_IMAGE_NAME not in os.listdir(dir_path):
            return None
        p = os.path.join(dir_path, PREVIEW_IMAGE_NAME)
        st = os.stat(p)
    except OSError:
        return None
    return os.path.abspath(p) if stat.S_ISREG(st.st_mode) and st.st_size > 0 else None


def app_category(dir_path: str) -> str | None:
    """The app's authored category: the `category` field of a `metadata.json`
    at the folder's root — the same per-app metadata shape the showcase repo
    ships. None when the file is absent, unreadable, malformed, or the field
    isn't a non-empty string; an app without a category only ever appears
    under the UI's "All" filter. Never raises — a listing degrades."""
    try:
        with open(os.path.join(dir_path, "metadata.json"), "rb") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    cat = meta.get("category") if isinstance(meta, dict) else None
    if not isinstance(cat, str):
        return None
    return cat.strip() or None


def app_dict(path: str, name: str, tag: str, entry_html: str | None) -> dict:
    """One app's listing entry — the single place the shape is built.

    `entry_html` is resolved by the CALLER, deliberately. Resolving it in here
    would mean swallowing `app_entry`'s `OSError`, which quietly turns "this
    directory cannot be read, skip it" into "this directory has no entry, list
    it as a folder" — an unreadable app comes back as a card. The caller is the
    only one that knows which of those it wants, so it decides.

    `preview_image` is resolved HERE, and the asymmetry is the point: it has no
    such ambiguity to hand back (see `app_preview_image`), so resolving it once
    in the shared shape is what keeps the workspace walk and the linked-app
    registry from having to remember it separately.
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
        # The card's thumbnail when the author supplied one: absolute path to
        # `preview.png` at the folder's root, else None and the card falls back
        # to rendering `entry_html` live.
        "preview_image": app_preview_image(path),
        # The authored category from `metadata.json`, or None. Drives the apps
        # page's category filter; None means "All only".
        "category": app_category(path),
        "title": entry_title(entry_html) if entry_html else None,
        "updated_at": dir_updated_at(path),
    }


# Deepest level below the workspace root the walk looks at. Level 3 is where the
# `index.html` requirement applies and where descent stops outright.
MAX_APP_DEPTH = 3

# Vendor/build directory names that are never an app and never walked into.
# Taken from the index's shared floor (`index/ignore.SHARED_IGNORE_DIRS`, which
# `server/walk.WALK_IGNORE_DIRS` and `junk_path` are also built from) rather than
# spelled out again here: "which directories are machine-managed noise" already
# has one answer in this codebase and a second copy would drift from it.
#
# LOWERCASED and matched case-folded, unlike the index's own set lookup: this
# module's other name rule (the package suffixes below) is case-folded, and one
# gate that skips `node_modules` while walking `Node_Modules` is a difference
# nobody can see until a listing behaves differently on two machines.
PRUNE_DIR_NAMES = frozenset(n.lower() for n in SHARED_IGNORE_DIRS)

# Directory suffixes that are genuinely opaque containers — machine-managed
# innards, never an app, never walked into.
#
# Deliberately NOT `index/ignore.is_leaf_dir`, though it is right next door and
# covers more. Its extra entry is `.app`, and its purpose is INVISIBILITY: the
# index must never record a macOS package's ten thousand internal files. Here
# invisibility has the opposite cost — a workspace folder a user named `todo.app`
# would vanish from their apps page with nothing to explain it. So `.app` is
# handled separately (a page makes it an app, no page makes it a bundle, and
# either way it is never descended) and only these three, which nobody names a
# project after, are dropped outright. `.git` needs no entry here: it is hidden,
# and the dot rule takes it before any suffix is consulted.
OPAQUE_DIR_SUFFIXES = (".framework", ".bundle", ".photoslibrary")

# `.app`: a real macOS bundle when it holds no page, a folder a user happened to
# name that way when it does. Never descended either way — the inside of a bundle
# is `Contents/`, `MacOS/`, `Resources/`, and the permissive depth-2 rule would
# turn each of those into a card.
PACKAGE_DIR_SUFFIXES = (".app",)


def workspace_apps(root: str) -> list[dict]:
    """Every app in the workspace: a BOUNDED recursive walk, depths 1-3.

    It used to be exactly two levels (`<root>/<tag>/<name>/`, and the name said
    so). Two levels is not where people put their work: a folder dropped
    straight into the workspace (`~/Documents/Fused/sine/sine.html`) was invisible
    to the page, and so was an app one level below a tag dir. The walk is now
    recursive, with the depth bound and the per-level rules below.

    A PAGE IS WHAT MAKES A FOLDER AN APP, at every level — an entry-less folder
    is a SHELF the apps sit on, never a card. So, relative to `root`, an app is a
    non-hidden directory that:

      * DEPTH 1 or 2 — has an entry, i.e. any `*.html` that `app_entry` resolves.
        A folder saved straight into the workspace
        (`~/Documents/Fused/sine/sine.html`) is an app; `local/`, `showcase/` and
        a `sandbox/<person>/` are not.
      * DEPTH 3 — has an `index.html`, directly. The permissive "any page" rule
        cannot be carried this deep: a checked-out code repo in the workspace
        turns every third-level folder into a card (measured: 55 candidates on one
        repo, 47 of them a single `templates/` tree). An explicit `index.html` is
        the author saying "this folder is a page", the only signal worth trusting
        that far down.

    The two-level walk this replaced emitted entry-less folders as cards that
    opened a directory, and depth 2 kept that rule at first. A real workspace
    showed why it had to go: `sandbox/` held ten PEOPLE's folders with the 14 real
    apps one level inside them, so the page drew a blank, title-less card for
    every person beside the apps it now also found. The shelf and its contents
    both appearing is the tell. `app_dict` still accepts `entry=None` — the
    linked-app registry (`linked_apps.linked_apps`) passes one for a registered
    folder that has no page — so this changed what the WALK emits, never the dict
    contract.

    DESCENT IS A SEPARATE DECISION and did not change with that one: an
    entry-less folder is not a card but it IS walked, which is exactly how the
    apps inside those ten people's folders are found. Descent stops (depth 2
    onward) only at a folder whose entry is literally
    `index.html`. That name is the author declaring "this folder IS the page, and
    what is below it is my assets" — without the rule, an app with an
    `index.html` and a `sub/index.html` lists twice and a multi-page app scatters
    its own pages across the grid as separate cards. ANY OTHER `.html` declares
    nothing of the sort and does not block the walk: a repo cloned to
    `<ws>/local/<repo>/` routinely carries a `coverage.html` or a `docs.html` at
    its root, and treating that as "this repo is one page, its subfolders are
    assets" silently deleted every app in the repo from the grid.

    Depth 1 is descended unconditionally, even on an `index.html`: the top level
    is the workspace's shelf of tag/repo folders, and one of those holding a
    landing page (a `showcase/index.html`) must not delete every app underneath
    it. So a top-level folder with a page lists AND its children still list.

    `tag` — the page's "Repo" facet — is THE FIRST PATH SEGMENT at every depth,
    so `showcase/sub/bar` files under `showcase` exactly as `showcase/bar` does.
    A depth-1 app IS that segment, so it is its own tag (an empty tag would add a
    nameless chip to the facet list and a `?tag=` that filters on "").

    NOT WALKED INTO, at any level: vendor/build dirs by name (`PRUNE_DIR_NAMES`),
    opaque containers by suffix (`OPAQUE_DIR_SUFFIXES`), macOS packages
    (`PACKAGE_DIR_SUFFIXES` — listed if they hold a page, never descended), and
    SYMLINKED directories. A symlink still LISTS if it resolves to a folder with a
    page (it did under the two-level walk, and a linked-in app folder is a
    reasonable thing to have), but the walk stops there: it is now three levels
    deep, so `<ws>/loop -> <ws>` would otherwise duplicate the entire listing
    under a bogus tag, and a link into a remote mount would pay three levels of
    kernel listing on a page load. `MountGuard` is the structural half of that
    second one and is consulted BEFORE any syscall on a candidate path — a `stat`
    under a wedged rclone mount blocks the serving thread, so the guard's pure
    string comparison has to come first (the ordering `index/freshness.py` uses).

    Skips whatever it cannot read at every level and returns [] for a root that
    isn't listable (no workspace yet, on a first run) — a listing degrades, it
    never fails. Names are sorted at every level, so a partial result is stable.
    """
    apps: list[dict] = []
    guard = MountGuard()
    if guard.blocks(root):
        # A workspace pointed at a mount is not walked at all, rather than
        # walked carefully: see the guard's own docstring.
        return apps
    _walk_apps(root, root, 1, apps, guard)
    return apps


def _walk_apps(dir_path: str, root: str, depth: int, apps: list[dict],
               guard: MountGuard) -> None:
    """Collect the apps among `dir_path`'s children, which sit at `depth`, and
    recurse where the rules in `workspace_apps` allow."""
    try:
        names = os.listdir(dir_path)
    except OSError:
        return  # unreadable/racing dir: skip, never fail the listing
    for name in sorted(names):
        lowered = name.lower()
        if (name.startswith(".") or lowered in PRUNE_DIR_NAMES
                or lowered.endswith(OPAQUE_DIR_SUFFIXES)):
            continue
        path = os.path.join(dir_path, name)
        # BEFORE the first syscall on this path, deliberately: a stat under a
        # wedged rclone mount blocks the serving thread, and the guard answers
        # from mount records with pure string work.
        if guard.blocks(path):
            continue
        try:
            if not os.path.isdir(path):
                continue
            # Followed for classification (as `isdir` already does), never for
            # descent — see `workspace_apps`.
            is_link = os.path.islink(path)
            entry_html = app_entry(path)
        except OSError:
            # Unreadable or racing: SKIPPED, not listed as an entry-less card.
            # Resolving the entry inside this guard is what makes that
            # distinction — see `app_dict`.
            continue
        is_package = lowered.endswith(PACKAGE_DIR_SUFFIXES)
        if is_package and entry_html is None:
            continue  # a real macOS bundle: no page, so no card and no descent
        # The first segment of the path below the root, which for a depth-1
        # folder is the folder itself.
        tag = os.path.relpath(path, root).replace(os.sep, "/").split("/")[0]
        # The one name that makes a folder declare itself a page — the depth-3
        # requirement, and the only entry that stops the walk descending.
        is_index = bool(entry_html) and os.path.basename(entry_html).lower() == "index.html"
        if depth >= MAX_APP_DEPTH:
            # An explicit index.html, nothing else, and no descent past here.
            if is_index:
                apps.append(app_dict(path, name, tag, entry_html))
            continue
        # A PAGE is what makes a folder an app, at every level. An entry-less one
        # is a shelf the apps sit on, and it is still WALKED (below) — that is how
        # the apps under it are found.
        if entry_html is not None:
            apps.append(app_dict(path, name, tag, entry_html))
        # Descent is a separate question from emission. A symlink and a package
        # are never descended; below the top level, a self-declared page
        # (`index.html`) owns its subtree and any other `.html` does not.
        if is_link or is_package:
            continue
        if depth == 1 or not is_index:
            _walk_apps(path, root, depth + 1, apps, guard)


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
