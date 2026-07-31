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
               nothing ever moves refs backwards. `git read-tree -u --reset`
               is the mechanism — unlike `checkout <sha> -- .` it also
               *deletes* files that were added after the selected commit.

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

_SHARED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)
from appenv import home_dir, workspace_dir  # noqa: E402

# Mirrors fused_render/app_git.py `_IDENTITY`; keep in step.
_IDENTITY = ["-c", "user.name=Fused", "-c", "user.email=apps@fused.io"]

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# Unit separator: cannot appear in a commit subject, so a plain split is safe.
_US = "\x1f"


def _app_dir_for(path: str) -> str:
    """The app dir containing `path`, or "" when the path is not inside an
    app. Mirrors `app_git.app_dir_for` (and `claude/agent.py`); keep in step.
    """
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


def _git(app_dir: str, *args, binary: bool = False):
    """One git invocation against the app repo. `-C` instead of `cwd=` and
    `close_fds=False` — the posix_spawn discipline every subprocess in this
    codebase follows (a plain fork trips PROJ's atfork handler, SIGSEGV).
    """
    return subprocess.run(
        ["git", "-C", app_dir, *_IDENTITY, *args],
        capture_output=True, text=not binary, timeout=60, close_fds=False,
    )


def _require_app(file: str):
    """(app_dir, None) for a target inside a git-backed app, else (None, error
    payload). The refusal is the security boundary described in the module
    docstring — everything else in this file assumes it already ran.
    """
    app = _app_dir_for(file)
    if not app:
        return None, {"error": "not inside a fused app folder"}
    if not os.path.isdir(os.path.join(app, ".git")):
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
    r = _git(app, "log", f"--format=%H{_US}%ct{_US}%s")
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
    return {"app": app, "commits": commits}


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
        r = _git(app, "archive", "--format=tar", full, binary=True)
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace") if r.stderr else ""
            return {"error": "git archive failed: " + err.strip()[:200]}
        os.makedirs(snap, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
            # `data` filter (3.12+): refuses absolute names and `..` traversal
            # in the archive — defence in depth; our own git wrote the tar.
            tf.extractall(snap, filter="data")
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
    if _head(app) == full:
        return {"noop": True, "reason": "already at this revision"}

    # Working tree + index become exactly the selected commit's tree; files
    # added since are deleted, ignored/untracked files are left alone.
    r = _git(app, "read-tree", "-u", "--reset", full)
    if r.returncode != 0:
        return {"error": "git read-tree failed: " + (r.stderr or "").strip()[:200]}

    # Same tree as HEAD (e.g. reverting to a commit whose content later
    # commits already restored): nothing to record.
    if _git(app, "diff", "--cached", "--quiet", "HEAD").returncode == 0:
        return {"noop": True, "reason": "tree already matches this revision"}

    subj = _git(app, "log", "-1", "--format=%s", full).stdout.strip()
    msg = f"Reverted to {full[:7]}" + (f" — {subj}" if subj else "")
    r = _git(app, "commit", "-q", "-m", msg)
    if r.returncode != 0:
        return {"error": "git commit failed: " + (r.stderr or "").strip()[:200]}
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
            return _revert(app, sha)
    except subprocess.TimeoutExpired:
        return {"error": "git timed out"}
    return {"error": f"unknown action: {action}"}
