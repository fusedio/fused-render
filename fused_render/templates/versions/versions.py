"""Git backend for the `versions` template: history of a *fused app*.

The template targets any file or directory inside an app folder
(`<workspace>/<tag>/<name>`, see `condition.py`) but always operates on the
WHOLE app — the repo is the app, not the file. Three actions:

* `log`      — the commit list, newest first.
* `snapshot` — materialise one commit as a plain folder so the explorer can
               render the app *as it was*. `git archive <sha>` is extracted
               into a per-app, per-commit dir under the shell home
               (`~/.fused-render/app-versions/<app-key>/<sha>/`); a commit is
               immutable, so an existing complete snapshot is reused as-is.
               The caller iframes `/render?path=<snapshot>/index.html`.
* `revert`   — restore the working tree AND index to the selected commit and
               record that as a NEW commit on top ("Reverted to <sha> — …").
               History is never rewritten: revert of a revert works, and
               nothing ever moves refs backwards. The commit object is built
               with `commit-tree` BEFORE anything touches disk: `read-tree
               -u --reset` (the destructive step — unlike `checkout <sha> --
               .` it also *deletes* files added after the selected commit)
               only runs once that commit exists, so a failure never leaves
               the working tree changed with nothing recorded to show for it.

Scoped hard to app dirs: every action re-derives the app dir from the target
path with the same rule as `app_git.app_dir_for` (mirrored here — templates
must not import fused_render, SPEC PY-15/D166) and refuses anything else, so
a hand-crafted URL can never make this module commit to, or archive from, a
user's real repository.

Subprocess discipline matches `app_git`/`claude/agent.py`: `git -C <dir>`
(never `cwd=`), `close_fds=False`, bounded timeout, and a fixed per-invocation
identity for the revert commit so a missing global git config can't fail it.

Stdlib only — snapshots are unpacked with `tarfile` from `git archive`'s
stdout, no `tar` binary involved.
"""
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "versions.py")

_SHARED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)
from appenv import home_dir, linked_app_dir_for, workspace_dir  # noqa: E402

# Mirrors fused_render/app_git.py `_IDENTITY`; keep in step.
_IDENTITY = ["-c", "user.name=Fused", "-c", "user.email=apps@fused.io"]

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# Unit separator: cannot appear in a commit subject, so a plain split is safe.
_US = "\x1f"


def _app_dir_for(path: str) -> str:
    """The app dir containing `path`, or "" when the path is not inside an
    app. Mirrors `app_git.app_dir_for` (and `claude/agent.py`); keep in step.
    Linked apps (registry folders outside the workspace) resolve too — but
    read-only; see `_is_linked` / `main`'s revert refusal.
    """
    linked = linked_app_dir_for(path)
    if linked:
        return linked
    try:
        root = workspace_dir()
        rel = os.path.relpath(os.path.abspath(path), root)
    except (ValueError, OSError):  # different drive on Windows
        return ""
    if rel == os.curdir or rel.split(os.sep, 1)[0] == os.pardir:
        return ""
    parts = rel.split(os.sep)
    if len(parts) < 2 or parts[0].startswith(".") or parts[1].startswith("."):
        return ""
    return os.path.join(root, parts[0], parts[1])


def _is_linked(app_dir: str) -> bool:
    """A linked app's repo is the USER'S OWN repository: fused-render shows
    its history but never writes into it — no revert commit, no Fused
    identity in their log (fused_render/linked_apps.py). Read actions (log,
    snapshot) are fine: snapshot archives into the shell home, not the repo."""
    from appenv import is_linked_app_dir

    return is_linked_app_dir(app_dir)


def _git(app_dir: str, *args, binary: bool = False):
    """One git invocation against the app repo. `-C` instead of `cwd=` and
    `close_fds=False` — the posix_spawn discipline every subprocess in this
    codebase follows (a plain fork trips PROJ's atfork handler, SIGSEGV).
    """
    return subprocess.run(
        ["git", "-C", app_dir, *_IDENTITY, *args],
        capture_output=True, text=not binary, timeout=60, close_fds=False,
    )


def _repo_root(app: str) -> str:
    """The work-tree root of the repo containing `app`, or "". Git's own
    ascent (`rev-parse --show-toplevel`) — a linked app is often a subfolder
    of the user's repository, so its `.git` lives at an ancestor, and git
    handles the `.git`-file shapes (worktree, submodule) a stat can't."""
    try:
        r = _git(app, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _require_app(file: str):
    """(app_dir, None) for a target inside a git-backed app, else (None, error
    payload). The refusal is the security boundary described in the module
    docstring — everything else in this file assumes it already ran.

    A workspace app must carry its OWN `.git` (app_git.init_repo puts it
    there; the workspace sitting inside some larger repo must not leak that
    repo's history into every app). A linked app is the opposite case: its
    `.git` is routinely at an ancestor, so git's ascent is the authority —
    matching its gate (condition.py).
    """
    app = _app_dir_for(file)
    if not app:
        return None, {"error": "not inside a fused app folder"}
    if _is_linked(app):
        if not _repo_root(app):
            return None, {"error": "this app has no git history"}
    elif not os.path.isdir(os.path.join(app, ".git")):
        return None, {"error": "this app has no git history"}
    return app, None


def _resolve_sha(app: str, sha: str):
    """Validated, full commit id — or None. The regex check comes first so a
    client-supplied value can never reach git as an option or a path."""
    sha = (sha or "").strip().lower()
    if not _SHA_RE.match(sha):
        return None
    r = _git(app, "rev-parse", "--verify", "--quiet", sha + "^{commit}")
    out = r.stdout.strip()
    return out if r.returncode == 0 and _SHA_RE.match(out) else None


def _log(app: str):
    # `-- .` scopes the log to the app's own subtree (pathspecs are relative
    # to `-C app`). For a workspace app the repo root IS the app dir, so this
    # changes nothing there; for a linked app inside a larger repository it is
    # what makes the list "this app's history" rather than the whole repo's.
    r = _git(app, "log", f"--format=%H{_US}%ct{_US}%s", "--", ".")
    if r.returncode != 0:
        return {"error": "git log failed: " + (r.stderr or "").strip()[:200]}
    commits = []
    for line in r.stdout.splitlines():
        parts = line.split(_US, 2)
        if len(parts) != 3:
            continue
        sha, ts, subject = parts
        try:
            ts = int(ts)
        except ValueError:
            ts = 0
        commits.append({"sha": sha, "ts": ts, "subject": subject})
    # `can_revert` drives the UI: revert is refused server-side for linked
    # apps (see main), so the button must not be offered either.
    return {"app": app, "commits": commits, "can_revert": not _is_linked(app)}


def _snapshot(app: str, sha: str):
    full = _resolve_sha(app, sha)
    if full is None:
        return {"error": "unknown revision"}

    # Per-app key: path hash, not name — two apps may share a basename, and
    # the hash also keeps the snapshot root flat and filename-safe.
    key = hashlib.sha1(app.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    snap = os.path.join(home_dir(), "app-versions", key, full)
    marker = os.path.join(snap, ".fused-snapshot-complete")

    if not os.path.isfile(marker):
        # `-C app` scopes this for free: git archive run from a subdirectory
        # of the work tree archives only that subtree, with entry names
        # relative to it — so a linked app nested in a larger repository gets
        # its own index.html at the top of the snapshot, and a workspace app
        # (where the app IS the repo root) is the degenerate same case.
        # Verified against git 2.x; a commit that predates the folder just
        # produces an empty tar, which lands in the no-entry notice below.
        r = _git(app, "archive", "--format=tar", full, binary=True)
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace") if r.stderr else ""
            return {"error": "git archive failed: " + err.strip()[:200]}
        os.makedirs(snap, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
            # `data` filter: refuses absolute names and `..` traversal in the
            # archive — defence in depth; our own git wrote the tar. The
            # kwarg itself is 3.12+ (PEP 706), backported to some but not all
            # patch releases of 3.10/3.11 that requires-python still allows;
            # hasattr(tarfile, "data_filter") is the documented feature probe,
            # so an interpreter without the backport just skips the filter
            # instead of raising TypeError and breaking the preview outright.
            if hasattr(tarfile, "data_filter"):
                tf.extractall(snap, filter="data")
            else:
                tf.extractall(snap)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(full + "\n")

    entry = os.path.join(snap, "index.html")
    if not os.path.isfile(entry):
        return {"app": app, "sha": full, "dir": snap, "entry": None}
    return {"app": app, "sha": full, "dir": snap, "entry": entry}


def _revert(app: str, sha: str):
    full = _resolve_sha(app, sha)
    if full is None:
        return {"error": "unknown revision"}
    head = _head(app)
    if head == full:
        return {"noop": True, "reason": "already at this revision"}

    # Tree comparison BEFORE anything touches disk: read-tree's reset is
    # destructive to the working tree, and revert-of-a-revert can leave two
    # different commits with an identical tree — running it first would
    # silently discard uncommitted edits to reach a tree that was already
    # there, then report a no-op with no sign anything happened.
    target_tree = _git(app, "rev-parse", "--verify", "--quiet",
                       full + "^{tree}").stdout.strip()
    head_tree = _git(app, "rev-parse", "--verify", "--quiet",
                     head + "^{tree}").stdout.strip() if head else ""
    if target_tree and target_tree == head_tree:
        return {"noop": True, "reason": "tree already matches this revision"}

    subj = _git(app, "log", "-1", "--format=%s", full).stdout.strip()
    msg = f"Reverted to {full[:7]}" + (f" — {subj}" if subj else "")

    # Build the commit object first — commit-tree writes no working-tree or
    # index state — so a failure here (e.g. a misconfigured commit signing
    # key) leaves disk untouched instead of reporting an error after the
    # revert already landed. Only once the commit exists does HEAD move and
    # the (destructive) working-tree reset run.
    parents = ["-p", head] if head else []
    r = _git(app, "commit-tree", target_tree, *parents, "-m", msg,
             "--no-gpg-sign")
    if r.returncode != 0:
        return {"error": "git commit failed: " + (r.stderr or "").strip()[:200]}
    new_sha = r.stdout.strip()

    r = _git(app, "update-ref", "HEAD", new_sha)
    if r.returncode != 0:
        return {"error": "git update-ref failed: " + (r.stderr or "").strip()[:200]}

    # Working tree + index become exactly the selected commit's tree; files
    # added since are deleted, ignored/untracked files are left alone.
    r = _git(app, "read-tree", "-u", "--reset", new_sha)
    if r.returncode != 0:
        return {"error": "git read-tree failed: " + (r.stderr or "").strip()[:200]}
    return {"reverted": True, "message": msg}


def _head(app: str) -> str:
    r = _git(app, "rev-parse", "--verify", "--quiet", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def main(action: str = "log", file: str = "", sha: str = ""):
    app, err = _require_app(file)
    if err is not None:
        return err
    try:
        if action == "log":
            return _log(app)
        if action == "snapshot":
            return _snapshot(app, sha)
        if action == "revert":
            if _is_linked(app):
                # The security boundary, not just UI politeness: a revert
                # records a commit with the Fused identity, and a linked app's
                # repo belongs to the user. History is view-only here.
                return {"error": "revert is disabled for linked apps — "
                                 "this folder's git history is managed by you"}
            return _revert(app, sha)
    except subprocess.TimeoutExpired:
        return {"error": "git timed out"}
    return {"error": f"unknown action: {action}"}
