"""Git backend for the `versions` template: history of a *fused app*, of a
single *file*, or of an ordinary *directory* — anywhere git works.

There are exactly three kinds of target, resolved once per call by
`_resolve_target`, and the difference between them is the whole shape of this
module:

* **An app** — any file or directory inside an app folder
  (`<workspace>/<tag>/<name>`, see `condition.py`), where the module operates on
  the WHOLE app: the repo is the app, not the file. Writable (`revert`).
* **A file** — a single tracked file in whatever repository it happens to live
  in, scoped to that one path. **Read-only**: `revert` is refused, because the
  repository is the user's own and a revert commit carries the Fused identity
  (the rule linked apps already live by).
* **A directory** — an ordinary folder inside any git work tree, scoped to its
  own subtree. Read-only for the same reason. Its snapshot is the subtree at
  that commit, archived exactly as an app's is, and shown exactly as an app's
  is: its **page** when the extracted tree has one (the shared entry rule — the
  same predicate the gate admits the folder by), else the tree itself to
  **browse** (`browse`).

Everything outside a fused app is resolved by asking GIT where the work tree is
(`rev-parse --show-toplevel`), never by workspace-relative path arithmetic —
the discipline `git/log.py` follows, and the only one that answers correctly for
nested repos, worktrees and submodules.

Three actions:

* `log`      — one PAGE of the commit list, newest first, scoped to the target:
               `skip` rows in, `PAGE_SIZE` (20) rows long, with `more` saying
               whether history continues past it. Never unbounded — outside a
               fused app the repository is the user's own and its subtree may
               carry decades of commits, all of which used to be formatted and
               shipped for a spine whose first screen is twenty rows.
* `snapshot` — materialise one commit as a plain folder so the explorer can
               render the app, the file, or the folder *as it was*. `git archive <sha>` is
               extracted into a per-target, per-commit dir under the shell home
               (`~/.fused-render/app-versions/<key>/<sha>/`); a commit is
               immutable, so an existing complete snapshot is reused as-is.
               A TREE (an app or a directory) reports its `entry` page for
               `/render?path=` when the extracted tree has one; a file reports
               the materialised `file` (plus `entry` when the file is itself a
               page), and the view frames non-page files through their own
               default template. A tree with NO page reports `browse` — the
               extracted tree, framed through `/explorer/embed/<path>`, the
               shell's chrome-free listing. Which of the two a tree gets is a
               fact about the COMMIT, not the target: a revision predating the
               page browses, the next one renders.
* `revert`   — restore the working tree AND index to the selected commit and
               record that as a NEW commit on top ("Reverted to <sha> — …").
               History is never rewritten: revert of a revert works, and
               nothing ever moves refs backwards. The commit object is built
               with `commit-tree` BEFORE anything touches disk: `read-tree
               -u --reset` (the destructive step — unlike `checkout <sha> --
               .` it also *deletes* files added after the selected commit)
               only runs once that commit exists, so a failure never leaves
               the working tree changed with nothing recorded to show for it.

Every action re-derives its target from the path it is given — the app-dir rule
is the same one as `app_git.app_dir_for` (mirrored here, because templates must
not import fused_render, SPEC PY-15/D166) — so a hand-crafted URL cannot pick a
different scope than the gate offered. What it *can* now reach is a file in the
user's own repository, which is exactly why the write path is fenced by kind
rather than by the gate: this module never commits to, or resets, anything but a
workspace app.

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
from appenv import (  # noqa: E402
    home_dir, is_mount_backed, linked_app_dir_for, workspace_dir)

# Mirrors fused_render/app_git.py `_IDENTITY`; keep in step.
_IDENTITY = ["-c", "user.name=Fused", "-c", "user.email=apps@fused.io"]

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# Unit separator: cannot appear in a commit subject, so a plain split is safe.
_US = "\x1f"

# One page of history. Defined once and read by nothing else: the view asks for
# a page by `skip` alone and lets the payload's own `more` flag decide whether
# there is another, so the page size is never duplicated in the template.
PAGE_SIZE = 20


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


def _resolve_target(file: str):
    """(target, None) or (None, error payload) — which of the two things this
    module can be asked about `file` is (D235).

    A target is `(kind, cwd, pathspec, name)`, and every git invocation below is
    built from it: `-C cwd` and `-- <pathspec>`. `name` is the file's basename for
    a file target and "" for the two whole-subtree ones — the pathspec is for
    git, `name` is for finding the file again inside an extracted snapshot.

      ("app",  <app dir>,        ".",                     "")
          A fused app or a git-backed linked app, scoped to the app's own
          subtree. Writable: `revert` is offered for a workspace app.
      ("file", <the file's dir>, ":(literal)<basename>", <basename>)
          A single tracked file in whatever repository it happens to live in —
          the file-side history view. READ-ONLY, always: `main` refuses `revert`
          for this kind, because the repository is the user's own and a revert
          commit carries the Fused identity. Same rule, same reason, as a linked
          app.
      ("dir",  <the directory>,  ".",                     "")
          An ordinary directory inside any git work tree — the folder-side
          history view, and the one this module used to have no answer for at
          all. `condition.py` was widened to offer `versions` on any path in a
          work tree (the `git` mode is the WORKING TREE view now and draws no
          history), and this was left behind: the gate said yes and the log said
          "not inside a fused app folder". READ-ONLY like "file", and for the
          same reason. Membership is asked of GIT (`_repo_root`), never of
          workspace-relative path arithmetic — the discipline `git/log.py`
          follows, and the only one that gets nested repos, worktrees and
          submodules right.

    App-ness is asked FIRST, so a file or folder inside an app keeps the app's
    history (the timeline the auto-commits actually produced) rather than being
    demoted to its own log. The pathspec is `:(literal)`-wrapped so a filename
    holding `*`, `?`, `[` or a leading `:` is matched as itself rather than as a
    glob or as pathspec magic — the discipline `git/log.py` documents.

    A MOUNT-BACKED path is refused before anything stats it, matching the gate:
    git over an rclone-NFS mount stats and lists its way through the work tree,
    which is the exact pattern that wedges a flat million-key S3 prefix.
    """
    if not file:
        return None, {"error": "no target (missing _file param?)"}
    try:
        mounted = is_mount_backed(file)
    except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
        return None, {"error": "cannot tell whether this path is on a remote "
                               "mount, so git history is not offered here"}
    if mounted:
        return None, {"error": "this path is on a remote mount, where git "
                               "history is not offered"}

    app, _err = _require_app(file)
    if app:
        return ("app", app, ".", ""), None

    # Not an app target. Both remaining kinds are somebody else's repository,
    # and git's own ascent is what decides whether there is one.
    path = os.path.abspath(file)
    if os.path.isdir(path):
        if not _repo_root(path):
            return None, {"error": "this folder is not in a git repository"}
        return ("dir", path, ".", ""), None
    # An EXISTING regular file, the same `isfile` the gate insists on rather than
    # `not isdir`. Without it a missing name inside a repository resolves to a
    # perfectly valid file target, and `log` answers with an empty-but-successful
    # history — a view that says "no versions yet" about a file that does not
    # exist, which is a worse answer than the refusal this error vocabulary
    # already has a word for.
    if not os.path.isfile(path):
        return None, {"error": "no such file"}
    cwd = os.path.dirname(path)
    if not os.path.isdir(cwd):
        return None, {"error": "no such file"}
    if not _repo_root(cwd):
        return None, {"error": "this file is not in a git repository"}
    base = os.path.basename(path)
    return ("file", cwd, ":(literal)" + base, base), None


def _resolve_sha(app: str, sha: str):
    """Validated, full commit id — or None. The regex check comes first so a
    client-supplied value can never reach git as an option or a path."""
    sha = (sha or "").strip().lower()
    if not _SHA_RE.match(sha):
        return None
    r = _git(app, "rev-parse", "--verify", "--quiet", sha + "^{commit}")
    out = r.stdout.strip()
    return out if r.returncode == 0 and _SHA_RE.match(out) else None


def _log(target, skip: int = 0):
    kind, app, pathspec, _name = target
    # The pathspec is what scopes the log (pathspecs are relative to `-C app`):
    # `.` is the app's own subtree — for a workspace app the repo root IS the app
    # dir so it changes nothing, for a linked app inside a larger repository it
    # is what makes the list "this app's history" rather than the whole repo's —
    # and for a file target it is that one file, so the list is that file's
    # commits and nothing else (D235).
    #
    # PAGED, and unconditionally so: this used to be an unbounded `git log`, and
    # outside a fused app the repository is the user's own — a directory target
    # in a long-lived repo formats and ships every commit that ever touched the
    # subtree, for a spine the user reads the top twenty rows of. The page is
    # `--skip=N --max-count=PAGE+1`: the +1 is the PROBE, one process instead of
    # a second `rev-list --count`, and it is dropped before the payload is built
    # so `more` is the only thing it is ever visible as. Newest-first is git's
    # own default order, which is what makes skip/limit a stable cursor here:
    # pages are only ever appended below what is already drawn, and any commit
    # landing on top while the user pages would shift the window by one — an
    # accepted duplicate row, not a torn timeline, and the view reloads from the
    # first page after the one action (revert) that can add one.
    try:
        skip = max(0, int(skip))
    except (TypeError, ValueError):
        skip = 0
    r = _git(app, "log", f"--format=%H{_US}%ct{_US}%s",
             f"--skip={skip}", f"--max-count={PAGE_SIZE + 1}", "--", pathspec)
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
    more = len(commits) > PAGE_SIZE
    del commits[PAGE_SIZE:]
    # `can_revert` drives the UI: revert is refused server-side for linked apps
    # and for file and dir targets (see main), so the button must not be offered
    # either. `kind` rides along as the target's own name for itself — every
    # kind previews now, so there is deliberately no second "can this preview"
    # flag: a field that is true for every caller is one nobody reads, and the
    # view branches on the SNAPSHOT payload (`browse` vs `entry` vs `file`),
    # which is the thing that actually differs.
    return {"app": app, "commits": commits, "kind": kind,
            "skip": skip, "more": more,
            "can_revert": kind == "app" and not _is_linked(app)}


def _snapshot(target, sha: str):
    kind, app, pathspec, name = target
    full = _resolve_sha(app, sha)
    if full is None:
        return {"error": "unknown revision"}

    # Per-target key: path hash, not name — two apps may share a basename, and
    # the hash also keeps the snapshot root flat and filename-safe. A FILE target
    # folds its pathspec in, so its one-file snapshot can never collide with a
    # sibling file's or with its directory's app snapshot; an app target hashes
    # the dir alone, exactly as before, so snapshots already on disk stay reused
    # instead of being orphaned by this change.
    key_src = app if kind == "app" else app + "\0" + pathspec
    key = hashlib.sha1(key_src.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    snap = os.path.join(home_dir(), "app-versions", key, full)
    # The completion marker sits BESIDE the extracted tree, not inside it:
    # anything inside is content the snapshot's own listing shows, and a
    # `.fused-snapshot-complete` row in a browsable historical tree is a file the
    # user never wrote and cannot explain. (It only became visible when a
    # directory snapshot started being LISTED — an app snapshot frames its entry
    # page, which never shows its siblings.)
    #
    # Both locations are READ, because snapshots already on disk carry the old
    # in-tree marker and re-extracting them would be a pointless cache wipe; only
    # the new one is ever written.
    marker = snap + ".complete"
    legacy_marker = os.path.join(snap, ".fused-snapshot-complete")

    if not (os.path.isfile(marker) or os.path.isfile(legacy_marker)):
        # `-C app` scopes this for free: git archive run from a subdirectory
        # of the work tree archives only that subtree, with entry names
        # relative to it — so a linked app nested in a larger repository gets
        # its own index.html at the top of the snapshot, and a workspace app
        # (where the app IS the repo root) is the degenerate same case.
        # Verified against git 2.x; a commit that predates the folder just
        # produces an empty tar, which lands in the no-entry notice below.
        # Only a FILE target narrows the archive, to its own pathspec, so the
        # snapshot holds exactly that one file at that revision rather than the
        # whole surrounding directory. A "dir" target takes the SAME
        # pathspec-free call an app does, because `-C` has already scoped it:
        # the directory IS the subtree being asked about, and a workspace app
        # (where the app is the repo root) is that same call, degenerately.
        #
        # The cost is real and is accepted here rather than refused: this is the
        # user's own repository, so the subtree can be large. It is paid LAZILY
        # — only for a commit the user actually clicks — and at most once per
        # commit, because a commit is immutable and the completion marker below
        # makes every later click a no-op. No size cap, deliberately: app
        # snapshots have never had one, and inventing a limit here would mean a
        # folder whose history silently stops previewing at some size nobody
        # can see.
        narrow = ["--", pathspec] if kind == "file" else []
        # Does this commit have ANYTHING at this path? Asked before the archive,
        # because an archive of nothing is not an empty tar this code can
        # extract — it is a lone `pax_global_header` and tar's EOF blocks, and
        # `tarfile.open` REFUSES that ("end of file header"), raising ReadError.
        # The comment further down used to say a commit predating the folder
        # "just produces an empty tar, which lands in the no-entry notice"; it
        # did not, it raised, and the red /api/run traceback overlay was the
        # answer the user got. Latent while only apps had snapshots (an app's
        # folder exists in every commit that built it), and reachable the moment
        # any directory in the user's own repository could be a target.
        #
        # `ls-tree` rather than a guess at the archive's bytes: it answers the
        # question being asked, and a genuinely unreadable archive still reports
        # as one instead of being explained away as an empty revision. `.` is
        # resolved against `-C app`, exactly as the archive's own scoping is.
        #
        # TREE kinds only. A file target that the commit does not contain makes
        # `git archive` fail outright (`fatal: pathspec ... did not match any
        # files`, exit 128), so it is already answered by the branch below — in
        # the file's own words, which are more use than a sentence about a
        # folder.
        if kind != "file":
            listing = _git(app, "ls-tree", "--name-only", full, ".")
            if listing.returncode == 0 and not listing.stdout.strip():
                return {"error": "this revision has nothing under this folder"}
        r = _git(app, "archive", "--format=tar", full, *narrow, binary=True)
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

    # A FILE target's snapshot is that one file, so there is nothing to discover:
    # `file` is the materialised path, and `entry` is it only when the file is
    # itself a page. For anything else the view frames it through its own default
    # template (the same resolution the claude pane does), which is why
    # `file` is reported separately rather than squeezed into `entry` — `entry`
    # means "a document /render can serve directly".
    if kind == "file":
        out = os.path.join(snap, name)
        if not os.path.isfile(out):
            return {"error": "this revision does not contain that file"}
        is_page = out.lower().endswith((".html", ".htm"))
        return {"app": app, "sha": full, "dir": snap, "file": out,
                "browse": None, "entry": out if is_page else None}

    # Both remaining kinds materialise a TREE, and ONE rule decides how it is
    # shown — the shared app-entry rule, asked of the EXTRACTED tree:
    #
    #   `entry`  — the page it resolves (index.html first, else the first
    #              top-level .html — shared/app_entry.py), served directly by
    #              /render. Never a hardcoded index.html: a folder whose page is
    #              `main.html` must preview its history exactly like it renders
    #              live.
    #   `browse` — no page: the extracted tree itself, framed by the view
    #              through `/explorer/embed/<path>`, the shell's own chrome-free
    #              directory listing.
    #
    # The rule is shared with the GATE deliberately, and that is not a tidiness
    # argument — it is the same predicate `versions/condition.py` uses to decide
    # a folder is worth offering this mode at all. Resolving the page for an app
    # and not for a directory made the gate and the view disagree about the very
    # folders the gate had just admitted: they were offered `versions` BECAUSE
    # they have a page, and then previewed as a file listing of themselves.
    # (Which is exactly how it was found — "the versions template does not
    # render the comfy.html file and instead shows me a file explorer".)
    #
    # Asked per COMMIT, of the extracted tree rather than of the live folder, so
    # a revision that predates the page browses and the one after it renders.
    # That is also why the shape rides on the SNAPSHOT payload and not on the
    # target's kind: within one timeline it changes.
    #
    # A tree with no page is not a dead end either. It used to answer
    # `entry: None` and the view drew "this revision has no entry page —
    # nothing to render" over a tree full of files the user could perfectly well
    # have looked at.
    #
    # Two fields rather than one overloaded key, because the two are framed by
    # DIFFERENT routes; one key meaning both is how a folder ends up handed to a
    # document renderer.
    from app_entry import entry_html

    entry = entry_html(snap)
    return {"app": app, "sha": full, "dir": snap, "file": None,
            "browse": None if entry else snap, "entry": entry}


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


def main(action: str = "log", file: str = "", sha: str = "", skip: int = 0):
    target, err = _resolve_target(file)
    if target is None:
        return err
    kind, app, _pathspec, _name = target
    try:
        if action == "log":
            return _log(target, skip)
        if action == "snapshot":
            return _snapshot(target, sha)
        if action == "revert":
            # The security boundary, not just UI politeness: a revert records a
            # commit with the Fused identity and resets the working tree. Only a
            # WORKSPACE app is ours to do that to. A linked app's repo, a
            # standalone file's and a plain folder's are all the user's own —
            # history is view-only there, and this refusal is what makes the
            # read-only promise in condition.py true even for a hand-crafted
            # call, now that the gate offers every path in a work tree.
            if kind != "app":
                return {"error": "revert is disabled outside fused apps — this "
                                 "git history is managed by you"}
            if _is_linked(app):
                return {"error": "revert is disabled for linked apps — "
                                 "this folder's git history is managed by you"}
            return _revert(app, sha)
    except subprocess.TimeoutExpired:
        return {"error": "git timed out"}
    return {"error": f"unknown action: {action}"}
