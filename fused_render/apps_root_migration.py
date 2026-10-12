"""One-time migration: the apps under `<workspace>/local` move to `<workspace>/app`.

New apps have landed in `app/` since 2026-10-11 (`routers/apps.py`'s create
route), beside the bots' builds; everything made before that still sits in
`local/`. The owner's rule for the old ones (2026-10-12): move every folder,
with its `.fused/` state (data, cache, meta — it is INSIDE the folder, so the
move carries it), unless `app/<name>` already exists — a name clash is skipped
and the `local/` copy left exactly where it was. When nothing is left under
`local/` afterwards (only `.git`, `.gitignore`, `.DS_Store`), the folder is
deleted, shared repo included — the owner's call: that history was scoped
undo for folders that no longer live there.

Each move settles the way `routers/current_apps.rename` and
`fs_mutate._fs_rename` settle one: `.fused/meta.json` witnesses the OLD path
before the rename (`app_fused_dir.ensure`), and `ensure` at the NEW path fires
`_after_move` — the server's stores (bookmarks, recents, desk, registered
apps, app_recents) are re-rooted and the app's Claude sessions are carried
to the new cwd bucket, with a live session recorded as pending so the next
open retries. A folder that could not take the witness (read-only) settles
by hand through the same two calls.

Git, both sides, through app_git's existing doors: the shared `local` repo
(D626) records the folder's departure as one scoped commit ("Move app X to
app/"), exactly as fs_mutate's rename hook does when an app is dragged out of
the tag; and at the new path the app heads its OWN repository, the same
posture a newly created `app/` app has (`init_repo`'s per-app branch). An app
that already heads its own repo (one local_monorepo skipped for having a
remote, or one on a machine that never ran that migration) moves with its
`.git` intact and is not re-initialised — which is why this runs BEFORE
local_monorepo: that migration would delete the per-app `.git` for folders
that are about to leave the shared repo anyway.

Two phases, because the server starts right after the startup hook calls
this: the RENAMES run synchronously (`move_all`, milliseconds for a few dozen
same-volume renames — the listing must never see a folder mid-move), and the
settle + git trail + stamp run on a daemon thread (`finish_in_background`,
which then chains `local_monorepo.run_once` so the two never race on
`local/.git`). Runs once per machine, recorded in a stamp file under
~/.fused-render (the local_monorepo idiom). A rename that raised is NOT
stamped, so the next start retries; a retry that finds `local/<name>` gone
treats it as done. Every step is idempotent. Never raises past run_once.
"""
import json
import logging
import os
import shutil
import stat
import threading

logger = logging.getLogger(__name__)

_STAMP_NAME = "apps_root_migration.json"
OLD_TAG = "local"
NEW_TAG = "app"

# What may remain under `local/` for it to count as empty: the shared repo's
# own files (D626) and the one file macOS drops into any folder it has shown.
_HOUSEKEEPING = {".git", ".gitignore", ".DS_Store"}


def _stamp_path() -> str:
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(base, _STAMP_NAME)


def _force_rm(func, path, _exc):
    """rmtree onerror hook: `.git` objects are read-only (0444) by design, and
    on Windows that alone fails the unlink — lift the bit and retry once."""
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    func(path)


def _settle(old: str, new: str, witnessed: bool) -> None:
    """Re-root stores and carry Claude sessions for one moved folder — the
    witness path when `ensure(old)` wrote meta before the move, else by hand
    (the same two doors `routers/current_apps.rename` uses)."""
    from fused_render import app_fused_dir, app_state_move, claude_session_move

    recorded = app_fused_dir.recorded_app_dir(new)
    if witnessed and recorded and (os.path.normcase(os.path.abspath(recorded))
                                   == os.path.normcase(old)):
        app_fused_dir.ensure(new)
        return
    app_state_move.rewrite_stores(old, new)
    claude_session_move.relocate(old, new)
    if recorded is not None and (os.path.normcase(os.path.abspath(recorded))
                                 != os.path.normcase(old)):
        _own_witness(new, old, recorded)


def _own_witness(new: str, old: str, recorded: str) -> None:
    """A copy's `meta.json` still names the folder it was copied FROM. Left
    like that, it becomes a lie once that folder is gone — the original has
    moved to `app/`, or `local/` itself was deleted — and the witness on the
    copy's next open would read the stale record as a MOVE of the original,
    handing the original's bookmarks, desk row and Claude sessions to the
    copy (bugbot on #1592). So the record is settled here: `app_dir` becomes
    the copy's own path, with the evidence kept in `migrations` — this hop,
    plus `copy_of` naming what the record used to say — the shape
    `_after_move` writes, so later readers see one history."""
    from datetime import datetime, timezone

    from fused_render import app_fused_dir

    meta = app_fused_dir.read_meta(new) or {}
    migrations = meta.get("migrations")
    if not isinstance(migrations, list):
        migrations = []
    migrations.append({"from": old, "to": new,
                       "at": datetime.now(timezone.utc).isoformat(),
                       "sessions": 0, "copy_of": recorded})
    meta.update(app_dir=new, migrations=migrations)
    path = app_fused_dir.meta_path(new)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _git_trail(old: str, new: str) -> None:
    """Record the departure in the shared `local` repo and head the moved
    folder with its own repo unless it already does. Best-effort: git missing
    or failing never un-moves anything."""
    from fused_render import app_git

    name = os.path.basename(new)
    if os.path.isdir(os.path.join(new, ".git")):
        # Heads its own repo: it was never tracked by the shared one (git
        # does not track a nested repo's files), so there is no departure to
        # record and nothing to initialise — the history came along.
        return
    app_git.commit(old, f"Move app {name} to {NEW_TAG}/", only_shared=True,
                   action="Auto-push after app move")
    app_git.init_repo(new, message=f"Moved from {OLD_TAG}/")


def move_all(root: str) -> tuple[list[tuple[str, str, bool]], list[str], bool]:
    """Phase one: rename every app folder under `<root>/local` to
    `<root>/app/<name>`, skipping a name that is already taken there. Returns
    `(moved, skipped, complete)` where `moved` holds `(old, new, witnessed)`
    triples for `finish` and `complete` is False when any rename raised."""
    root = os.path.abspath(root)
    old_root = os.path.join(root, OLD_TAG)
    new_root = os.path.join(root, NEW_TAG)
    moved: list[tuple[str, str, bool]] = []
    skipped: list[str] = []
    complete = True
    if not os.path.isdir(old_root):
        return moved, skipped, complete
    try:
        names = sorted(os.listdir(old_root))
    except OSError:
        logger.warning("apps root migration: cannot list %s", old_root, exc_info=True)
        return moved, skipped, False
    from fused_render import app_fused_dir

    for name in names:
        old = os.path.join(old_root, name)
        # Real app folders only: the shared repo's own entries and any other
        # dotname stay (they are not apps), as does a symlink — it points
        # somewhere the user chose, and moving the link is not moving an app.
        if name.startswith(".") or os.path.islink(old) or not os.path.isdir(old):
            continue
        new = os.path.join(new_root, name)
        if os.path.exists(new):
            logger.info("apps root migration: %s/%s exists — leaving %s in place",
                        NEW_TAG, name, old)
            skipped.append(name)
            continue
        try:
            os.makedirs(new_root, exist_ok=True)
            # Witness the OLD path before the move, so the settle in phase two
            # runs through `_after_move`'s full machinery (pending live
            # sessions included). False when the folder cannot be written to.
            # ONLY when the folder's meta is its own, though: a COPY carries
            # the original's `.fused/` wholesale (`audio-to-text-2` records
            # `audio-to-text`), and `ensure` on it would fire the witness
            # against the original — which, if it moved earlier in this very
            # loop, is "gone" and reads as a MOVE, handing the original's
            # stores and Claude sessions to the copy. Such a folder settles by
            # hand in phase two, and its record is made its own there
            # (`_own_witness`) so the same lie cannot fire on a later open.
            recorded = app_fused_dir.recorded_app_dir(old)
            own_meta = recorded is None or (
                os.path.normcase(os.path.abspath(recorded)) == os.path.normcase(old))
            witnessed = app_fused_dir.ensure(old) if own_meta else False
            shutil.move(old, new)
        except Exception:
            logger.warning("apps root migration: could not move %s -> %s "
                           "— will retry next start", old, new, exc_info=True)
            complete = False
            continue
        logger.info("apps root migration: moved %s -> %s", old, new)
        moved.append((old, new, witnessed))
    return moved, skipped, complete


def _remove_empty_old_root(root: str) -> bool:
    """Delete `<root>/local` when only housekeeping entries remain. True when
    it is gone on return (or never existed)."""
    old_root = os.path.join(os.path.abspath(root), OLD_TAG)
    if not os.path.isdir(old_root) or os.path.islink(old_root):
        return not os.path.exists(old_root)
    try:
        leftover = set(os.listdir(old_root)) - _HOUSEKEEPING
    except OSError:
        return False
    if leftover:
        logger.info("apps root migration: %s kept, %d entr%s left in it",
                    old_root, len(leftover), "y" if len(leftover) == 1 else "ies")
        return False
    try:
        shutil.rmtree(old_root, onerror=_force_rm)
    except Exception:
        logger.warning("apps root migration: could not delete empty %s", old_root,
                       exc_info=True)
        return False
    logger.info("apps root migration: deleted empty %s", old_root)
    return True


def finish(root: str, moved: list[tuple[str, str, bool]], skipped: list[str],
           complete: bool) -> None:
    """Phase two: settle stores/sessions and leave the git trail for every
    moved folder, delete an emptied `local/`, and stamp a complete run."""
    for old, new, witnessed in moved:
        try:
            _settle(old, new, witnessed)
        except Exception:
            # The witness refires on the app's next open; nothing is lost.
            logger.warning("apps root migration: settle failed for %s", new, exc_info=True)
        try:
            _git_trail(old, new)
        except Exception:
            logger.warning("apps root migration: git trail failed for %s", new, exc_info=True)
    if moved:
        # The sessions are on disk under the new paths and the stores are
        # re-rooted, but nothing the tasks watcher ticks on changed — the
        # same bump the two rename doors make, so an already-open Tasks
        # listing reloads instead of sitting on dead `local/` rows until its
        # floor refresh. Best-effort: a late listing is not a failed move.
        try:
            from fused_render import tasks_watch

            tasks_watch.notify_all()
        except Exception:
            logger.warning("apps root migration: tasks listing not notified", exc_info=True)
    if not complete:
        logger.warning("apps root migration incomplete (%d moved) — will retry next start",
                       len(moved))
        return
    removed = _remove_empty_old_root(root)
    stamp = _stamp_path()
    os.makedirs(os.path.dirname(stamp), exist_ok=True)
    with open(stamp, "w", encoding="utf-8") as fh:
        json.dump({"done": True, "moved": [n for _, n, _ in moved],
                   "skipped": skipped, "local_removed": removed}, fh, indent=2)
    if moved or skipped:
        logger.info("apps root migration: %d moved, %d skipped (name taken in %s/)",
                    len(moved), len(skipped), NEW_TAG)


def run_once(root: str) -> None:
    """The startup entry point. The renames happen HERE, before the caller
    starts serving; everything after them runs on a daemon thread, which
    then chains `local_monorepo.run_once` (same `local/.git`, so never in
    parallel). Never raises."""
    from fused_render import local_monorepo

    try:
        if os.path.exists(_stamp_path()):
            local_monorepo.run_once_in_background(root)
            return
        moved, skipped, complete = move_all(root)
    except Exception:
        logger.warning("apps root migration failed", exc_info=True)
        local_monorepo.run_once_in_background(root)
        return

    def _background() -> None:
        try:
            finish(root, moved, skipped, complete)
        except Exception:
            logger.warning("apps root migration: finish failed", exc_info=True)
        local_monorepo.run_once(root)

    threading.Thread(target=_background, name="apps-root-migration",
                     daemon=True).start()
