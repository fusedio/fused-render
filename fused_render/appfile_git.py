"""Git-source installs of a `.fused` (SPEC §26 DL-9): the app behind a
``fused-render://open?git=https://github.com/<owner>/<repo>`` link installs
as a real CLONE at ``<workspace>/app/<slug>`` — ``.git`` and ``origin``
kept — so the user can push their edits and open a pull request against the
source.

The flow keeps the install page as the one gate (AF-14):

1. Link click (appfetch.download_github_app): ``clone`` makes a fresh
   ``git clone --filter=blob:none`` into a STAGING dir under the downloads
   dir, ``<downloads>/git/<sha256(url)[:16]>/<repo>``, and a `.fused` is
   packed from it whose index carries ``git: {url, commit}`` — only to drive
   the install page (name, readme, icon, state).
2. Install (appfile.install_app_file → ``install``): the staged clone is
   MOVED into ``app/<slug>``, still on the repo's default branch (owner
   call): git_upstream's auto-sync keeps fast-forwarding it, so the user
   keeps pulling the author's changes. Claude's per-turn commits land on that
   branch too and auto-sync tries to push them; without push rights that
   fails as a standing sync failure, and setting up a fork + PR from there is
   Claude's to help with. (With push rights the push goes straight to the
   source — accepted.)

The repo's own files are never edited: the app id (the page's
``fused-app-id``, else one derived from the repo by appfetch) lives in the
`.fused` manifest and the install stamp, and an installed clone is found by
its ``origin`` URL (``installed_clone_of``). Bookkeeping stays out of git:
``.fused/`` and ``.venv/`` go to ``.git/info/exclude``, and no local
"Installed from" commit is made.

States (``probe``) for a clone already installed: ``same`` (HEAD is the
link's commit), ``update`` (HEAD is an ancestor of it and nothing tracked is
modified — a fast-forward), ``local`` (the copy has commits or edits of its
own: never merged on the user's behalf, they update with git). A copy of the
same app installed from a FILE is ``replace`` when unchanged since install
(the clone takes its place, keeping ``.fused/`` and ``.venv``), ``local``
when it was edited.

A `.fused` claiming a git source is only installed as a clone when the
staged clone for that URL exists and sits at exactly that commit
(``staged_clone``): a hand-made file cannot point the installer at an
arbitrary directory, only at a clone this machine made from the same link.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

TIMEOUT_S = 300

_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GitInstallError(Exception):
    """User-reportable; the message goes verbatim to the page."""


def _git(*args: str, cwd: str | None = None, timeout: int = TIMEOUT_S) -> subprocess.CompletedProcess:
    """One non-interactive git call. `close_fds=False` + an absolute git path
    keep CPython on posix_spawn (app_git's module docstring has the SIGSEGV
    story). A missing git raises GitInstallError naming the fix."""
    from fused_render.shell import git_tools

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    cmd = [git_tools.git_bin()] + (["-C", cwd] if cwd else []) + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=timeout, close_fds=False)
    except FileNotFoundError:
        raise GitInstallError("git is required to install from a GitHub link; "
                              "install it from the Tools step in onboarding")
    except subprocess.TimeoutExpired:
        raise GitInstallError("git took too long; check your connection and open the link again")


def _out(r: subprocess.CompletedProcess) -> str:
    return (r.stdout or "").strip()


def _err(r: subprocess.CompletedProcess) -> str:
    return (r.stderr or r.stdout or "").strip() or f"git exited {r.returncode}"


def normalize_url(url: str) -> str:
    """``https://github.com/owner/repo``, lowercased, for comparing remotes:
    ``.git``, a trailing slash, ``www.`` and the ssh form all fold in."""
    u = (url or "").strip()
    m = re.match(r"^git@github\.com:(.+)$", u, re.I)
    if m:
        u = "https://github.com/" + m.group(1)
    u = re.sub(r"^(?:https?|ssh)://(?:git@)?(?:www\.)?github\.com/", "https://github.com/", u, flags=re.I)
    u = u.rstrip("/")
    if u.lower().endswith(".git"):
        u = u[:-4]
    return u.lower()


def _staging_root(url: str) -> str:
    from fused_render import appfetch

    key = hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:16]
    return os.path.join(appfetch.downloads_dir(), "git", key)


def clone(url: str, repo: str) -> tuple[str, str, str | None]:
    """Fresh clone of ``url`` into its staging dir; answers ``(dir, commit,
    commit time as an exported_at stamp)``. A previous staging clone for the
    same URL is replaced (the link always shows the newest commit)."""
    root = _staging_root(url)
    os.makedirs(os.path.dirname(root), exist_ok=True)
    work = tempfile.mkdtemp(dir=os.path.dirname(root), prefix=".clone-")
    try:
        dest = os.path.join(work, repo)
        r = _git("clone", "-q", "--filter=blob:none", "--", url, dest)
        if r.returncode != 0:
            msg = _err(r)
            if "not found" in msg.lower() or "could not read username" in msg.lower():
                raise GitInstallError(
                    f"GitHub has no repository at {url} that your git can read "
                    "(a private repo needs your git credentials, e.g. `gh auth login`)")
            raise GitInstallError(f"git clone failed: {msg}")
        commit = _out(_git("rev-parse", "HEAD", cwd=dest))
        if not _SHA_RE.match(commit):
            raise GitInstallError(f"{url} has no commits to install")
        ct = _out(_git("log", "-1", "--format=%ct", "HEAD", cwd=dest))
        at = (datetime.fromtimestamp(int(ct), timezone.utc).isoformat(timespec="seconds")
              .replace("+00:00", "Z") if ct.isdigit() else None)
        shutil.rmtree(root, ignore_errors=True)
        os.rename(work, root)
        work = None
        return os.path.join(root, repo), commit, at
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)


def source_of(manifest: dict) -> dict | None:
    """The manifest's validated git source ``{url, commit}``, or None — a
    value out of an untrusted file, so shape-checked and never a path."""
    g = manifest.get("git")
    if not isinstance(g, dict):
        return None
    url, commit = g.get("url"), g.get("commit")
    if isinstance(url, str) and _URL_RE.match(url) and isinstance(commit, str) and _SHA_RE.match(commit):
        return {"url": url, "commit": commit}
    return None


def _origin(repo_dir: str) -> str | None:
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return None
    try:
        r = _git("config", "--get", "remote.origin.url", cwd=repo_dir, timeout=20)
    except GitInstallError:
        return None
    return normalize_url(_out(r)) if r.returncode == 0 and _out(r) else None


def _head(repo_dir: str) -> str | None:
    try:
        r = _git("rev-parse", "HEAD", cwd=repo_dir, timeout=20)
    except GitInstallError:
        return None
    return _out(r) if r.returncode == 0 else None


def staged_clone(src: dict) -> str | None:
    """The staging clone made for ``src['url']`` when it is still there AND
    at exactly ``src['commit']``; else None."""
    root = _staging_root(src["url"])
    try:
        names = [n for n in os.listdir(root) if not n.startswith(".")]
    except OSError:
        return None
    if len(names) != 1:
        return None
    d = os.path.join(root, names[0])
    if _origin(d) != normalize_url(src["url"]) or _head(d) != src["commit"]:
        return None
    return d


def is_clone_of(app_dir: str, url: str) -> bool:
    return _origin(app_dir) == normalize_url(url)


def installed_clone_of(url: str) -> str | None:
    """The folder under ``app/`` that is a clone of ``url``, or None."""
    from fused_render import appfile

    root = appfile.install_dir()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for n in names:
        p = os.path.join(root, n)
        if n.startswith(".") or os.path.islink(p) or not os.path.isdir(os.path.join(p, ".git")):
            continue
        if is_clone_of(p, url):
            return p
    return None


def _dirty(repo_dir: str) -> bool:
    r = _git("status", "--porcelain", "--untracked-files=no", cwd=repo_dir, timeout=60)
    return r.returncode != 0 or bool(_out(r))


def probe(app_dir: str, src: dict) -> str:
    """``same`` | ``update`` | ``local`` for the clone at ``app_dir`` against
    the link's commit. Read-only: the ancestry is asked of the staged clone
    when it is there (it has the new commit), else of the installed one."""
    head = _head(app_dir)
    if head is None:
        return "local"
    if head == src["commit"]:
        return "same"
    # Auto-sync fast-forwards the clone on its own, so it may already be past
    # the link's commit: nothing to install then either.
    if _git("merge-base", "--is-ancestor", src["commit"], head, cwd=app_dir, timeout=60).returncode == 0:
        return "same"
    if _dirty(app_dir):
        return "local"
    ask = staged_clone(src) or app_dir
    r = _git("merge-base", "--is-ancestor", head, src["commit"], cwd=ask, timeout=60)
    return "update" if r.returncode == 0 else "local"


def _place(staged: str, dest: str, *, husk: bool) -> None:
    """Move the staged clone to ``dest``: a rename (a move across
    filesystems), or into a husk entry by entry beside its ``.fused/``."""
    if husk:
        for n in os.listdir(staged):
            if os.path.exists(os.path.join(dest, n)):
                continue
            shutil.move(os.path.join(staged, n), os.path.join(dest, n))
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(staged, dest)


def _replace(staged: str, dest: str) -> None:
    """Swap a file-installed copy for the clone, carrying ``.fused/`` (the
    app's data) and ``.venv`` over. The old copy is parked beside ``app/``
    first and restored if the clone cannot be placed."""
    from fused_render import appfile

    aside = tempfile.mkdtemp(dir=appfile.install_dir(), prefix=".install-replace-")
    old = os.path.join(aside, "old")
    os.rename(dest, old)
    try:
        shutil.move(staged, dest)
    except OSError:
        if not os.path.exists(dest):
            os.rename(old, dest)
        shutil.rmtree(aside, ignore_errors=True)
        raise
    for keep in (".fused", ".venv"):
        src = os.path.join(old, keep)
        if os.path.isdir(src) and not os.path.exists(os.path.join(dest, keep)):
            shutil.move(src, os.path.join(dest, keep))
    shutil.rmtree(aside, ignore_errors=True)


def install(fused_path: str, target: dict, manifest: dict, *, force: bool) -> dict:
    """Install or update the clone ``target`` (appfile.install_target's
    answer for a git-source file) describes. Answers the action taken."""
    from fused_render import app_fused_dir, app_git, appfile

    src = target["git"]
    state = target["state"]
    dest = os.path.normpath(target["path"])
    if state == appfile.STATE_CLASH:
        raise GitInstallError(f"{target['path']} already holds a different app "
                              f"({target['other_name']}); rename or uninstall it first")
    if state == appfile.STATE_LOCAL:
        raise GitInstallError(f"{target['path']} has changes of its own; update it with git "
                              "(git pull)")
    if state == appfile.STATE_SAME:
        raise GitInstallError("this commit is already installed")
    if state == appfile.STATE_REPLACE and not force:
        raise appfile.InstallNeedsConfirm(target)

    if state == appfile.STATE_UPDATE:
        for args in (("fetch", "-q", "origin"), ("merge", "-q", "--ff-only", src["commit"])):
            r = _git(*args, cwd=dest)
            if r.returncode != 0:
                raise GitInstallError(f"git {args[0]} failed: {_err(r)}")
        action = "updated"
    else:
        staged = staged_clone(src)
        if staged is None:
            raise GitInstallError("the downloaded clone is gone or out of date; open the link again")
        if state == appfile.STATE_REPLACE:
            _replace(staged, dest)
        else:
            _place(staged, dest, husk=bool(target.get("husk")))
        action = "installed"
    shutil.rmtree(_staging_root(src["url"]), ignore_errors=True)

    app_git._ensure_excludes(dest)
    app_fused_dir.ensure(dest)
    appfile.write_stamp(dest, manifest, fused_path, git=src)
    entry = os.path.join(dest, *manifest["entry"].split("/"))
    return {"action": action, "entry": entry.replace(os.sep, "/"), "pruned": [], "adopted_state": None}


def sweep_staging(ttl_s: float = 24 * 3600) -> None:
    """Drop staging clones older than ``ttl_s`` — a link opened and never
    installed. Best-effort."""
    from fused_render import appfetch

    root = os.path.join(appfetch.downloads_dir(), "git")
    cutoff = time.time() - ttl_s
    try:
        names = os.listdir(root)
    except OSError:
        return
    for n in names:
        p = os.path.join(root, n)
        try:
            if os.path.isdir(p) and not os.path.islink(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            continue
