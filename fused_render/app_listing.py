"""The entry contract behind `GET /api/apps`, extracted from its router.

An app is a folder in the workspace, one to three levels down, whose entry is
its first non-hidden direct-child `.html` carrying the `<meta name="fused-app">`
marker (`app_entry`, the shared rule — D269 for the sharing, D301 for the
marker). THE MARKER IS THE ONLY SIGNAL: filenames — `index.html` included —
declare nothing (D301 removed the name rules; a one-time migration stamped the
existing workspace apps, and the managed pipelines stamp what they write). The
walk that finds them (`workspace_apps`, which is where the per-level rules are
written down), and the facts reported about each one — a title read out of the
entry, an authored `preview.png` thumbnail if there is one, and when the folder
was last touched — live here rather than inside the route handler, so they can
be tested and reused without a `TestClient`.

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

# The declarative app marker: `<meta name="fused-app">` in a page's head. A page
# carrying it is the author saying "this file is a fused app's entry" — the ONE
# signal that makes a folder an app (D301: filenames, `index.html` included,
# declare nothing). Matched from the head bytes only (same budget as
# `entry_title`): the tag belongs at the top of the document, and an unbounded
# read per candidate would turn the workspace walk into a full-file scan of
# every page on every listing.
FUSED_META_NAME = "fused-app"
_FUSED_META_RE = re.compile(
    rb"<meta\s[^>]*name\s*=\s*[\"']?fused-app[\"']?", re.IGNORECASE)
_META_SCAN_BYTES = 4096


def has_fused_meta(html_path: str) -> bool:
    """True when the page declares itself a fused app via
    `<meta name="fused-app">` in its first 4 KiB. Never raises — an unreadable
    page simply doesn't carry the marker."""
    try:
        with open(html_path, "rb") as fh:
            head = fh.read(_META_SCAN_BYTES)
    except OSError:
        return False
    return _FUSED_META_RE.search(head) is not None


def text_has_fused_meta(text: str) -> bool:
    """The marker check for a page already held as text (GET /render reads the
    file — or the mount serve — before asking), same head-bytes budget."""
    return _FUSED_META_RE.search(
        text[:_META_SCAN_BYTES].encode("utf-8", "ignore")) is not None


def app_entry(dir_path: str) -> str | None:
    """An app folder's entry page: the FIRST non-hidden direct-child `.html`
    (name order) carrying `<meta name="fused-app">`. None when no page in the
    folder declares itself — a folder full of untagged html is NOT an app.

    Raises OSError when the dir can't be listed; every caller skips those.

    THE MARKER IS THE ONLY SIGNAL (D301). The rule used to be name-based —
    `index.html`, else the first `.html` in name order (D269) — which was a
    guess about intent read off a filename: any checked-out repo full of html
    became a grid of cards, and an app was invisible the moment its entry wasn't
    named the blessed way. The marker is the author stating the intent, so
    `index.html` now has ZERO special status — not even as a tiebreaker among
    tagged pages, because a name rule kept "just in case" is the old guess
    sneaking back in. Existing workspace apps were stamped by the one-time
    migration (`meta_migration`); apps elsewhere are the user's to stamp (the
    authoring skill carries the instruction).

    THE SAME RULE AS `templates/shared/app_entry.py::entry_html`, deliberately
    and to the letter — that module's docstring carries the reasoning, and
    `tests/test_shared_app_entry.py` and `tests/test_app_listing.py` ask both the
    same questions. A folder resolving to one page for the card that opens it and
    another for the template that renders it is the failure this parity prevents.

    The ONE divergence that remains is the OSError: this raises where the shared
    copy swallows, so `workspace_apps` can tell "unreadable, skip this folder"
    from "no entry, list it as a folder" (see `app_dict`). A template has no such
    distinction to draw — it renders a notice either way.

    Still a COPY rather than an import: a template must not import `fused_render`
    (SPEC PY-15 / D166), and `templates/` is packaged data, not an importable
    package, so neither side can reach the other. The parity is held by tests.
    """
    children = os.listdir(dir_path)
    # `sorted` is what makes "the first" a fact rather than whatever order the
    # filesystem handed back — the shell and the templates read the same folder
    # and must land on the same page.
    for c in sorted(children):
        if c.startswith(".") or not c.lower().endswith(".html"):
            continue
        p = os.path.join(dir_path, c)
        if os.path.isfile(p) and has_fused_meta(p):
            return os.path.abspath(p)
    return None


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
    in the shared shape keeps callers from having to remember it separately.
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


# Deepest level below the workspace root the walk looks at; descent stops
# outright there.
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

    A DECLARED PAGE IS WHAT MAKES A FOLDER AN APP, at every level: the folder's
    entry is a direct-child `.html` carrying `<meta name="fused-app">`
    (`app_entry` — the marker is the only signal, D301). An entry-less folder is
    a SHELF the apps sit on, never a card. The rule is now UNIFORM across depths
    1-3: the old per-depth name rules ("any html" at 1-2, `index.html` at 3)
    existed to bound how far a filename could be trusted as a guess about
    intent; the marker is not a guess, so one rule holds everywhere, and a
    checked-out code repo full of untagged html contributes nothing at any
    depth.

    An earlier walk emitted entry-less folders as cards that opened a
    directory. A real workspace showed why it had to go: `sandbox/` held ten
    PEOPLE's folders with the 14 real apps one level inside them, so the page
    drew a blank, title-less card for every person beside the apps it now also
    found. The shelf and its contents both appearing is the tell. `app_dict`
    still accepts `entry=None` (the dict contract predates the walk's page
    requirement, and a future caller may hold an entry-less folder), so that
    changed what the WALK emits, never the dict contract.

    DESCENT IS A SEPARATE DECISION: an entry-less folder is not a card but it
    IS walked, which is exactly how the apps inside those ten people's folders
    are found. Descent stops (depth 2 onward) at a folder WITH an entry — a
    declared page owns its subtree ("this folder IS the page, what is below it
    is my assets"); without the rule, a multi-page app scatters its own tagged
    sub-pages across the grid as separate cards.

    Depth 1 is descended unconditionally, even when it has an entry: the top
    level is the workspace's shelf of tag/repo folders, and one of those holding
    a landing page (a `showcase/index.html`) must not delete every app
    underneath it. So a top-level folder with a page lists AND its children
    still list.

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
        # A DECLARED page (`<meta name="fused-app">` — `app_entry`'s one rule)
        # is what makes a folder an app, at every level. An entry-less one is a
        # shelf the apps sit on, and it is still WALKED (below, depth
        # permitting) — that is how the apps under it are found.
        if entry_html is not None:
            apps.append(app_dict(path, name, tag, entry_html))
        if depth >= MAX_APP_DEPTH:
            continue  # the walk never looks past depth 3
        # Descent is a separate question from emission. A symlink and a package
        # are never descended; below the top level, a declared page owns its
        # subtree. Depth 1 descends unconditionally — see `workspace_apps`.
        if is_link or is_package:
            continue
        if depth == 1 or entry_html is None:
            _walk_apps(path, root, depth + 1, apps, guard)


def is_workspace_app_entry(fs_path: str, root: str) -> bool:
    """Whether `fs_path` is the ENTRY PAGE of a listed workspace app — i.e.
    whether `workspace_apps(root)` would report it as some app's `entry`.

    The explorer's recents filter (shell/recents.py) asks this at record time
    and on every GET: opening an app's entry page is already recorded in the
    APP recents store (server/routers/apps.py), and the same open landing in
    the file recents too put every app in both sidebar lists.

    A targeted re-derivation of the walk, not a `workspace_apps` membership
    test: the walk pays `entry_title`/`preview_image`/`metadata.json` reads
    per app on every call, and this runs per recents record (including the
    500 ms param-churn re-records) and per GET entry. Cost here is bounded
    string work plus at most three listdirs (and `app_entry`'s marker sniffs)
    on the LOCAL workspace.

    The error asymmetry decides every ambiguous case: True hides the file
    from the file recents, so anything indeterminate (OSError, mount-backed
    root, outside the workspace) answers False — a duplicate row is today's
    behavior, a file missing from both lists is a new bug. Parity with the
    walk is held by tests (test_app_listing.py), same as the shared-template
    copy of `app_entry`.
    """
    guard = MountGuard()
    root = os.path.abspath(root)
    if guard.blocks(root):
        return False  # workspace_apps lists nothing under a mount
    # The walk's first act is listdir(root), and an unlistable root lists
    # nothing — the same probe here keeps parity (an execute-only root must
    # not make a descendant "an app's entry" the walk would never emit).
    try:
        os.listdir(root)
    except OSError:
        return False
    path = os.path.abspath(fs_path)
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return False  # Windows cross-drive: not inside the workspace
    if rel == "." or rel.startswith(".."):
        return False
    segments = rel.split(os.sep)
    # The file sits directly in an app folder, so its own depth is the
    # folder's + 1; folders list at depths 1..MAX_APP_DEPTH only.
    parent_depth = len(segments) - 1
    if not 1 <= parent_depth <= MAX_APP_DEPTH:
        return False
    # Reachability: every ancestor DIRECTORY must be one the walk descends
    # into (or, for the app folder itself, one it lists). The name rules
    # apply to all of them; descent rules apply to the ancestors ABOVE the
    # app folder — a symlinked or `.app`-package app folder still LISTS, but
    # a symlinked/package ancestor is never walked past.
    for i, seg in enumerate(segments[:-1]):
        lowered = seg.lower()
        if (seg.startswith(".") or lowered in PRUNE_DIR_NAMES
                or lowered.endswith(OPAQUE_DIR_SUFFIXES)):
            return False
        is_app_folder = i == parent_depth - 1
        dir_path = os.path.join(root, *segments[:i + 1])
        if guard.blocks(dir_path):
            return False
        try:
            if not is_app_folder:
                # An ancestor the walk must descend THROUGH: never a symlink
                # or a package, and (below depth 1) never a folder with an
                # entry of its own — a declared page owns its subtree. The
                # entry probe runs at EVERY depth, depth 1 included, because
                # the walk resolves each candidate's entry before descending
                # and an unlistable folder ends its subtree there — only the
                # ownership rule is depth-gated (depth 1 descends
                # unconditionally, landing page or not).
                if os.path.islink(dir_path) or lowered.endswith(PACKAGE_DIR_SUFFIXES):
                    return False
                ancestor_entry = app_entry(dir_path)
                if i + 1 >= 2 and ancestor_entry is not None:
                    return False
            else:
                # The app folder itself: a symlink or `.app` package here
                # still LISTS when it holds a page, so only the entry rule
                # applies.
                entry = app_entry(dir_path)
        except OSError:
            return False  # unreadable anywhere: the walk skips it — record
    # The entry rule is `app_entry`'s alone (the first tagged direct-child
    # page — the marker is the only signal, D301, uniform across depths 1-3),
    # so matching it is the whole remaining question.
    if entry is None:
        return False
    return os.path.normcase(entry) == os.path.normcase(path)


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
