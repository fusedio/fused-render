"""One-time migration: `<workspace>/local` becomes ONE git repository (D626).

Before this, every app under the `local` tag headed its own repo
(app_git.init_repo's old behaviour). This migration folds them into a single
repo at the tag root: it creates `<workspace>/local/.git` (with the root
`.gitignore`), then for each app folder directly under the tag it carries the
app's repo-local `.git/info/exclude` patterns up to the shared repo, DELETES
the app's own `.git` (per-app history is discarded — owner's call: those
repos were scaffold-plus-turn undo logs, none with a remote), and lands the
folder as one "Adopt <name> into the workspace repo" commit. A dirty tree is
adopted exactly as it stands — the adopt commit IS its new baseline.

Deliberately skipped, and left heading their own repo: an app whose repo has
a REMOTE. A remote means the tree is externally synced (meta_migration's
discriminator), and deleting its `.git` would destroy a clone the user can
push. git itself keeps such a nested repo shadowing the shared one, and
app_git._repo_scope keeps committing into it — nothing breaks, it just stays
its own repo.

Runs once per machine, recording completion in a stamp file under
~/.fused-render (the meta_migration/bookmarks D97 idiom). A run where ANY app
failed to adopt does NOT stamp, so the next start retries — every step here
is idempotent (the exclude merge is append-only, rmtree of a half-deleted
`.git` finishes the job, a re-`add` of an adopted folder stages nothing).
Never raises past run_once; the workspace must open whether or not this ran.
"""
import json
import logging
import os
import shutil
import stat
import threading

from fused_render import app_git

logger = logging.getLogger(__name__)

_STAMP_NAME = "local_monorepo.json"


def _stamp_path() -> str:
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(base, _STAMP_NAME)


def _force_rm(func, path, _exc):
    """rmtree onerror hook: `.git` objects are read-only (0444) by design, and
    on Windows that alone fails the unlink — lift the bit and retry once."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        raise


def _has_remote(app_dir: str) -> bool:
    """Whether the app's own repo has any remote. A git that cannot answer
    reads as "has one" — when in doubt, the safe direction is not deleting."""
    r = app_git._git(app_dir, "remote")
    return r.returncode != 0 or bool((r.stdout or "").strip())


def _merge_excludes(app_dir: str, local: str) -> None:
    """Carry the app repo's `.git/info/exclude` lines up to the shared repo's,
    append-only, before the app's `.git` is deleted — those patterns were
    added by app_git._ensure_excludes (or the user) to keep bookkeeping files
    out of history, and the adopt commit's `add -A` must not sweep them in."""
    src = os.path.join(app_dir, ".git", "info", "exclude")
    try:
        with open(src, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()
                     and not ln.strip().startswith("#")]
    except OSError:
        return
    if not lines:
        return
    dst = os.path.join(local, ".git", "info", "exclude")
    try:
        try:
            with open(dst, encoding="utf-8") as f:
                have = {ln.strip() for ln in f}
        except OSError:
            have = set()
        missing = [ln for ln in lines if ln not in have]
        if missing:
            with open(dst, "a", encoding="utf-8") as f:
                f.write("\n".join(missing) + "\n")
    except OSError:
        logger.warning("exclude merge failed for %s", app_dir, exc_info=True)


def migrate(root: str) -> tuple[int, bool]:
    """Fold every per-app repo under `<root>/local` into the shared repo.
    Returns `(adopted, complete)` — `complete` False when any app failed and
    the run should not be stamped. A workspace with no `local` tag at all is
    complete with nothing adopted."""
    local = os.path.join(os.path.abspath(root), app_git.LOCAL_TAG)
    if not os.path.isdir(local):
        return 0, True
    if not app_git.ensure_local_repo():
        # No git on the machine (or init failed): nothing to migrate INTO.
        # Not stamped — a later start with git present gets another chance.
        return 0, False
    adopted, complete = 0, True
    for name in sorted(os.listdir(local)):
        app_dir = os.path.join(local, name)
        if name.startswith(".") or not os.path.isdir(app_dir):
            continue
        try:
            git_dir = os.path.join(app_dir, ".git")
            if os.path.exists(git_dir):
                if _has_remote(app_dir):
                    logger.info("local monorepo: %s has a remote — left as "
                                "its own repository", name)
                    continue
                _merge_excludes(app_dir, local)
                shutil.rmtree(git_dir, onerror=_force_rm)
            # Adopt whatever the folder holds — a dirty tree as-is (its adopt
            # commit is the new baseline), a repo-less folder the same way.
            if app_git._git(local, "add", "-A", "--", name).returncode != 0:
                complete = False
                continue
            if app_git._git(local, "diff", "--cached", "--quiet",
                            "--", name).returncode == 0:
                continue  # already tracked and clean (e.g. a retried run)
            if app_git._git(local, "commit", "-q", "-m",
                            f"Adopt {name} into the workspace repo",
                            "--", name).returncode != 0:
                complete = False
                continue
            adopted += 1
        except Exception:
            logger.warning("local monorepo: adopting %s failed", name,
                           exc_info=True)
            complete = False
    return adopted, complete


def run_once(root: str) -> None:
    """The startup entry point: run `migrate` once per machine, recording
    completion in the stamp file. Never raises."""
    stamp = _stamp_path()
    try:
        if os.path.exists(stamp):
            return
        adopted, complete = migrate(root)
        if not complete:
            logger.warning("local monorepo migration incomplete "
                           "(%d adopted) — will retry next start", adopted)
            return
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as fh:
            json.dump({"done": True, "adopted": adopted}, fh)
        if adopted:
            logger.info("local monorepo: adopted %d app folder(s)", adopted)
    except Exception:
        logger.warning("local monorepo migration failed", exc_info=True)


def run_once_in_background(root: str) -> None:
    """`run_once` on a daemon thread — startup must not wait on git calls
    across every app folder."""
    threading.Thread(target=run_once, args=(root,),
                     name="local-monorepo-migration", daemon=True).start()
