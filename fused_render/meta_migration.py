"""One-time migration: stamp `<meta name="fused-app">` into existing apps.

The marker (see `app_listing.has_fused_meta`) is the ONLY thing that makes a
folder an app now (D301); apps created from the starter carry it, but every app
authored before the marker existed does not — and under meta-only detection
those are invisible until stamped. This walks the workspace ONCE under the OLD
name-based rules (frozen below as `_legacy_entry`/`_legacy_apps` — the live
walk can no longer see untagged apps) and inserts the tag into each entry page
it finds, so everything the old rules called an app survives the switch. Apps
OUTSIDE the workspace are deliberately not touched: the migration edits only
files under the folder the user was told fused-render manages; external apps
are the user's to stamp (the authoring skill carries the instruction).

EXTERNALLY SYNCED trees — any git repo with a remote: the showcase clone,
deeplink clones, the user's own checkouts — are skipped outright and never
descended (`_has_remote`): stamping a tracked file there leaves the repo
permanently dirty and breaks its `--ff-only` pull, so those trees must carry
the tag UPSTREAM instead. Community INSTALLS are different — the install
copies into a fresh remote-less repo, and that pipeline stamps its own copies
(install and update both call `stamp_entry`), which is why this migration can
stay one-time rather than every-startup.

Runs at server startup, once per machine: a stamp file under the fused-render
home dir (`fused_meta_migration.json`) records completion, following the
bookmarks D97 "migrate once, record that you did" shape. Everything here is
best-effort — a page it cannot read, decode, or place the tag into is skipped
and left exactly as it was (the name-based detection rules still cover it),
because a migration that damages a user's page is strictly worse than one that
misses it.

Each app folder is a local git repo (`app_git.init_repo`). A stamped page in a
CLEAN repo is committed with a fixed message, so the change is honest history
rather than surprise dirt; a repo that was already dirty is left dirty — the
migration must never sweep a user's in-progress edits into a commit of its own.
"""
import json
import logging
import os
import re
import threading

from fused_render import app_git
from fused_render.app_listing import (
    MAX_APP_DEPTH,
    OPAQUE_DIR_SUFFIXES,
    PACKAGE_DIR_SUFFIXES,
    PRUNE_DIR_NAMES,
    has_fused_meta,
)
from fused_render.index.ignore import MountGuard

logger = logging.getLogger(__name__)

_STAMP_NAME = "fused_meta_migration.json"

# Insertion points, tried in order: right after `<meta charset...>` (the tag
# belongs at the top of head, and detection reads only the first 4 KiB), else
# right after the opening `<head...>` tag. A page with neither is skipped —
# guessing a position inside markup this module doesn't understand is how a
# migration breaks a page.
_CHARSET_RE = re.compile(r"<meta\s[^>]*charset[^>]*>", re.IGNORECASE)
_HEAD_RE = re.compile(r"<head[^>]*>", re.IGNORECASE)

META_TAG = '<meta name="fused-app" />'


def _stamp_path() -> str:
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(base, _STAMP_NAME)


def _insert_meta(text: str) -> str | None:
    """`text` with the marker inserted after `<meta charset>` (else after
    `<head>`), or None when neither anchor exists."""
    m = _CHARSET_RE.search(text) or _HEAD_RE.search(text)
    if not m:
        return None
    at = m.end()
    return text[:at] + "\n" + META_TAG + text[at:]


def _has_remote(dir_path: str) -> bool:
    """Does `dir_path` head a git repo with a remote? A remote means the tree
    is EXTERNALLY SYNCED — the showcase clone, a deeplink clone, the user's own
    checkout — and stamping a tracked file there leaves the repo permanently
    dirty and breaks its `--ff-only` pull. Managed app repos never have one
    (`app_git.init_repo` and the community install both `git init` fresh and
    add no remote), so "has a remote" is the one discriminator that separates
    hands-off trees from ours without hardcoding paths."""
    if not os.path.isdir(os.path.join(dir_path, ".git")):
        return False
    r = app_git._git(dir_path, "remote")
    # A failing `git remote` reads as "has one": when git can't answer, the
    # safe direction is not stamping.
    return r.returncode != 0 or bool((r.stdout or "").strip())


def _repo_clean(app_dir: str) -> bool:
    if not os.path.isdir(os.path.join(app_dir, ".git")):
        return False
    r = app_git._git(app_dir, "status", "--porcelain")
    return r.returncode == 0 and not (r.stdout or "").strip()


def stamp_entry(entry_html: str) -> bool:
    """Insert the marker into one entry page. True when the file was changed.
    Skips (returns False) when the tag is already there, the file is not valid
    UTF-8, or no insertion anchor exists — never raises."""
    try:
        if has_fused_meta(entry_html):
            return False
        with open(entry_html, "r", encoding="utf-8") as fh:
            text = fh.read()
        updated = _insert_meta(text)
        if updated is None:
            logger.info("meta migration: no <head>/<meta charset> anchor in %s, skipped",
                        entry_html)
            return False
        with open(entry_html, "w", encoding="utf-8") as fh:
            fh.write(updated)
        return True
    except (OSError, UnicodeError):
        logger.info("meta migration: could not stamp %s, skipped", entry_html,
                    exc_info=True)
        return False


def _legacy_entry(dir_path: str) -> str | None:
    """The pre-D301 NAME-BASED entry rule, frozen here: `index.html` if the
    folder has one, else the first non-hidden direct-child `.html` in name
    order. The migration is the translator from the old world to the new, so it
    is the one place the old rule legitimately lives on — `app_listing` itself
    no longer trusts filenames at all."""
    try:
        children = os.listdir(dir_path)
    except OSError:
        return None
    htmls = [c for c in sorted(children)
             if not c.startswith(".") and c.lower().endswith(".html")
             and os.path.isfile(os.path.join(dir_path, c))]
    for c in htmls:
        if c.lower() == "index.html":
            return os.path.join(dir_path, c)
    return os.path.join(dir_path, htmls[0]) if htmls else None


def _legacy_apps(dir_path: str, depth: int, out: list[str],
                 guard: MountGuard) -> None:
    """The pre-D301 walk, reduced to what stamping needs: collect (app dir,
    entry) candidates under the old per-depth rules — any html at depths 1-2,
    `index.html` at depth 3, an `index.html` below depth 1 owns its subtree.
    Same prune/package/symlink/mount discipline as the live walk."""
    try:
        names = os.listdir(dir_path)
    except OSError:
        return
    for name in sorted(names):
        lowered = name.lower()
        if (name.startswith(".") or lowered in PRUNE_DIR_NAMES
                or lowered.endswith(OPAQUE_DIR_SUFFIXES)):
            continue
        path = os.path.join(dir_path, name)
        if guard.blocks(path):
            continue
        try:
            if not os.path.isdir(path):
                continue
            is_link = os.path.islink(path)
            entry = _legacy_entry(path)
        except OSError:
            continue
        is_package = lowered.endswith(PACKAGE_DIR_SUFFIXES)
        if is_package and entry is None:
            continue
        if _has_remote(path):
            # Externally synced tree: neither stamped nor descended — an app
            # deeper inside a synced repo is that repo's file too.
            continue
        is_index = bool(entry) and os.path.basename(entry).lower() == "index.html"
        if depth >= MAX_APP_DEPTH:
            if is_index:
                out.append(path)
            continue
        if entry is not None:
            out.append(path)
        if is_link or is_package:
            continue
        if depth == 1 or not is_index:
            _legacy_apps(path, depth + 1, out, guard)


def migrate_workspace(root: str) -> int:
    """Stamp the entry page of every folder the OLD name-based rules called an
    app; returns how many files changed. Idempotent — a second run finds
    nothing to do."""
    guard = MountGuard()
    if guard.blocks(root):
        return 0
    dirs: list[str] = []
    _legacy_apps(root, 1, dirs, guard)
    changed = 0
    for app_dir in dirs:
        entry = _legacy_entry(app_dir)
        if not entry:
            continue
        was_clean = _repo_clean(app_dir)
        if not stamp_entry(entry):
            continue
        changed += 1
        if was_clean:
            # Honest history in the app's own undo-log repo; a dirty repo is
            # deliberately left dirty (see module docstring).
            app_git._git(app_dir, "add", "-A")
            app_git._git(app_dir, "commit", "-q", "-m",
                         "Add fused-app meta tag (fused-render migration)")
    return changed


def run_once(root: str) -> None:
    """The startup entry point: run `migrate_workspace` exactly once per
    machine, recording completion in the stamp file. Never raises."""
    stamp = _stamp_path()
    try:
        if os.path.exists(stamp):
            return
        changed = migrate_workspace(root)
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as fh:
            json.dump({"done": True, "stamped": changed}, fh)
        if changed:
            logger.info("meta migration: stamped %d app entry page(s)", changed)
    except Exception:
        # No stamp file is written on failure, so the next startup retries.
        logger.warning("meta migration failed", exc_info=True)


def run_once_in_background(root: str) -> None:
    """`run_once` on a daemon thread — startup must not wait on a workspace
    walk plus git calls."""
    threading.Thread(target=run_once, args=(root,), daemon=True,
                     name="fused-meta-migration").start()
