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
   call), and recorded as a GitHub-link copy outside the folder (contrib/
   classify.record_install, whatever the feature flags): the contribution
   engine keeps bringing the author's latest in, and the copy never pushes on
   its own (docs/CONTRIB_LOOP.md §16.0 P2) — the user's changes leave the
   machine only as a proposal. (The author's own repo, once signed in,
   reclassifies as an own copy and auto-syncs as ever.)

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


#: stderr fragments meaning "https auth did not happen": the cue to retry the
#: clone over ssh (many dev machines authenticate to GitHub by key only).
_AUTH_FAILURE_MARKERS = (
    "could not read username",
    "authentication failed",
    "repository not found",
    "terminal prompts disabled",
)


def _env() -> dict:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    # No interactive ssh prompts either (passphrase, unknown host key).
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    # A Finder-launched .app gets a minimal PATH (/usr/bin:/bin): credential
    # helpers configured as PATH commands (gh's `!gh auth git-credential`, a
    # Homebrew git-credential-manager) silently fail there and https auth
    # never happens. Widen with the usual install locations.
    extra = ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")]
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    env["PATH"] = os.pathsep.join(parts + [p for p in extra if p not in parts])
    return env


def _git(*args: str, cwd: str | None = None, timeout: int = TIMEOUT_S) -> subprocess.CompletedProcess:
    """One non-interactive git call. `close_fds=False` + an absolute git path
    keep CPython on posix_spawn (app_git's module docstring has the SIGSEGV
    story). A missing git raises GitInstallError naming the fix."""
    from fused_render.shell import git_tools

    env = _env()
    cmd = [git_tools.git_bin()] + (["-C", cwd] if cwd else []) + list(args)
    try:
        # posix-spawn-exempt: argv is git_tools.git_bin() (absolute, the one
        # resolver every git spawn shares) plus `-C <cwd>` and the args —
        # close_fds=False and no cwd= keep CPython on the posix_spawn path.
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env,
                              timeout=timeout,
                              close_fds=False)
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


def clone(url: str, repo: str, *, commit: str | None = None) -> tuple[str, str, str | None]:
    """Fresh clone of ``url`` into its staging dir; answers ``(dir, commit,
    commit time as an exported_at stamp)``. A previous staging clone for the
    same URL is replaced (the link always shows the newest commit).

    ``commit`` (a Marketplace install, docs/CONTRIB_LOOP.md §18.4): the
    approved commit of the app's index row. The clone is made WITHOUT a
    checkout (the author's tip is never written out, M1), the commit is
    fetched by sha, it must be on the repo's default branch (a commit only
    a fork has is refused — GitHub serves it under the repo's address
    anyway), and HEAD is detached there; `_home_branch` then makes
    `fused/mine` at it, with the mirror `refs/heads/<b>` = the commit."""
    if commit is not None and not _SHA_RE.match(commit or ""):
        raise GitInstallError("the Marketplace named a version fused can't read")
    root = _staging_root(url)
    os.makedirs(os.path.dirname(root), exist_ok=True)
    work = tempfile.mkdtemp(dir=os.path.dirname(root), prefix=".clone-")
    pinned = ("--no-checkout",) if commit else ()
    try:
        dest = os.path.join(work, repo)
        r = _git("clone", "-q", "--filter=blob:none", *pinned, "--", url, dest)
        if r.returncode != 0:
            https_msg = _err(r)
            if not any(m in https_msg.lower() for m in _AUTH_FAILURE_MARKERS):
                raise GitInstallError(f"git clone failed: {https_msg}")
            # https could not authenticate: retry over ssh. The origin is then
            # the ssh form, which normalize_url folds onto the same repo.
            shutil.rmtree(dest, ignore_errors=True)
            ssh_url = "git@github.com:" + url.split("github.com/", 1)[1] + ".git"
            r = _git("clone", "-q", "--filter=blob:none", *pinned, "--", ssh_url, dest)
            if r.returncode != 0:
                raise GitInstallError(
                    f"could not clone {url}: it does not exist, or it is private and "
                    "your git has no access (sign in with `gh auth login`, or add an ssh "
                    f"key to GitHub). https: {https_msg} — ssh: {_err(r)}")
        if commit:
            _pin_clone(dest, url, commit)
        commit = _out(_git("rev-parse", "HEAD", cwd=dest))
        if not _SHA_RE.match(commit):
            raise GitInstallError(f"{url} has no commits to install")
        ct = _out(_git("log", "-1", "--format=%ct", "HEAD", cwd=dest))
        at = (datetime.fromtimestamp(int(ct), timezone.utc).isoformat(timespec="seconds")
              .replace("+00:00", "Z") if ct.isdigit() else None)
        shutil.rmtree(root, ignore_errors=True)
        try:
            os.rename(work, root)
        except OSError:
            # Another click on the same link landed its clone first.
            raise GitInstallError("this repository is already being downloaded; "
                                  "open the link again in a moment")
        work = None
        return os.path.join(root, repo), commit, at
    except OSError as exc:
        raise GitInstallError(f"could not download the repository: {exc}")
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)


def _default_branch_of(clone_dir: str) -> str | None:
    """The cloned repo's default branch (`origin/HEAD`, which `git clone`
    sets), else the local branch the clone created."""
    short = _out(_git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=clone_dir,
                      timeout=20))
    if "/" in short:
        return short.split("/", 1)[1]
    return _out(_git("symbolic-ref", "--short", "HEAD", cwd=clone_dir, timeout=20)) or None


def _pin_clone(dest: str, url: str, commit: str) -> None:
    """`clone(commit=)`'s second half: the approved commit fetched by sha,
    checked to be on the default branch, checked out DETACHED (the first
    and only checkout this clone gets)."""
    branch = _default_branch_of(dest)
    if not branch:
        raise GitInstallError(f"{url} has no default branch to check the reviewed version against")
    if _git("cat-file", "-e", commit + "^{commit}", cwd=dest, timeout=20).returncode != 0:
        r = _git("fetch", "-q", "--no-tags", "origin", commit, cwd=dest)
        if r.returncode != 0:
            raise GitInstallError("couldn't download the reviewed version of this app; check "
                                  "your connection and try again")
    tip = f"refs/remotes/origin/{branch}"
    if _git("merge-base", "--is-ancestor", commit, tip, cwd=dest, timeout=60).returncode != 0:
        repo = url.split("github.com/", 1)[-1]
        raise GitInstallError(f"The reviewed version isn't in {repo}'s {branch}")
    r = _git("checkout", "-q", "--detach", commit, cwd=dest)
    if r.returncode != 0:
        raise GitInstallError(f"couldn't check out the reviewed version: {_err(r)}")


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
    """The workspace app folder that is a clone of ``url``, or None — the
    same walk `appfile.workspace_app_with_id` uses, so a clone the user moved
    out of ``app/`` is still this app's copy. Only folders heading their own
    ``.git`` cost a git call."""
    from fused_render import app_listing, appfile
    from fused_render.shell.seed import fused_dir

    try:
        rows = app_listing.discover_workspace_apps(fused_dir())
    except Exception:  # noqa: BLE001 — an unwalkable workspace holds no copy we can name
        return None
    for row in rows:
        p = row.get("path")
        if p and os.path.isdir(os.path.join(p, ".git")) and is_clone_of(p, url):
            return appfile._workspace_spelling(p)
    return None


def _dirty(repo_dir: str) -> bool:
    r = _git("status", "--porcelain", "--untracked-files=no", cwd=repo_dir, timeout=60)
    return r.returncode != 0 or bool(_out(r))


def _kind(app_dir: str) -> str | None:
    """The contribution loop's recorded kind of the copy (docs/CONTRIB_LOOP.md
    §16.1) — None when there is no record, or it cannot be read."""
    try:
        from fused_render.contrib import classify

        return classify.kind_of(os.path.realpath(app_dir))
    except Exception:  # noqa: BLE001 — no record readable: today's install
        return None


def _link_collisions(app_dir: str, src: dict | None) -> list[str]:
    """The user's own files (untracked or ignored, on disk) at paths the
    link's commit tracks and the copy's HEAD does not — what would make
    "Get the latest" refuse. Asked of the STAGED clone, which has the new
    commit even when the copy has not fetched it yet; else of the copy
    itself when it knows the commit. [] when neither can say."""
    if not src:
        return []
    ask = staged_clone(src)
    if ask is None:
        if _git("cat-file", "-e", src["commit"] + "^{commit}", cwd=app_dir, timeout=20).returncode != 0:
            return []
        ask = app_dir
    r = _git("ls-tree", "-r", "-z", "--name-only", src["commit"], cwd=ask, timeout=60)
    mine = _git("ls-files", "-z", cwd=app_dir, timeout=60)
    if r.returncode != 0 or mine.returncode != 0:
        return []
    tracked = set(mine.stdout.split("\0"))
    hits = [p for p in r.stdout.split("\0") if p and p not in tracked
            and not p.startswith(".fused/")
            and os.path.lexists(os.path.join(app_dir, *p.split("/")))]
    return sorted(hits)[:12]


def copy_view(app_dir: str, src: dict | None = None) -> dict | None:
    """For a GitHub-link copy (§16.8): what the install page says about it,
    off `engine.status` (no network) — ``{who, changes: bool, collision:
    [paths of the user's own files the update would land on], app_page:
    bool}``, merged into the probe's ``git``; None for any other folder.
    `collision` also counts the LINK's commit (`_link_collisions`), so the
    page never offers "Get the latest" that will refuse; `app_page` is
    whether the app page has the panel to point at (A1, §16.12). A
    marketplace copy (§18.4) answers `{who, kind: "marketplace", switch:
    "Follow <who> directly? …"}` — the `switch-link` prompt's words."""
    if _kind(app_dir) == "marketplace":
        try:
            from fused_render.contrib import store

            repo = (store.get(os.path.realpath(app_dir)) or {}).get("repo") or ""
        except Exception:  # noqa: BLE001
            repo = ""
        who = repo.split("/", 1)[0] if "/" in repo else "the author"
        return {"who": who, "kind": "marketplace", "changes": False, "collision": [],
                "app_page": _contrib_sources(), "switch": SWITCH_LINK_SENTENCE.format(who=who)}
    if _kind(app_dir) != "github":
        return None
    try:
        from fused_render.contrib import engine

        st = engine.status(os.path.realpath(app_dir))
        collision = sorted(set(st.get("blocked_files") or []) | set(_link_collisions(app_dir, src)))
    except Exception:  # noqa: BLE001 — the page falls back to the plain words
        logger.exception("install page: copy status failed for %s", app_dir)
        return None
    return {"who": st.get("who") or None,
            "changes": bool(st.get("changes_files") or st.get("needs_merge")),
            "collision": collision[:12],
            "app_page": _contrib_sources()}


def _probe_github(app_dir: str, src: dict, head: str) -> str:
    """`probe` for a GitHub-link copy: the engine's view decides (§16.8) —
    any change of the user's (or an overlap waiting) is ``local``, which the
    page answers with "open it"; otherwise ``update`` when the author's
    latest (or the link's commit) is not in the copy yet."""
    from fused_render.contrib import engine

    st = engine.status(os.path.realpath(app_dir))
    if (st.get("changes") or st.get("needs_merge") or st.get("changes_files")
            or st.get("blocked_files") or _link_collisions(app_dir, src)):
        return "local"
    if st.get("behind"):
        return "update"
    if _git("merge-base", "--is-ancestor", src["commit"], head, cwd=app_dir, timeout=60).returncode == 0:
        return "same"
    return "update"


def probe(app_dir: str, src: dict, *, light: bool = False) -> str:
    """``same`` | ``update`` | ``local`` for the clone at ``app_dir`` against
    the link's commit. Read-only: the ancestry is asked of the staged clone
    when it is there (it has the new commit), else of the installed one.
    ``light`` (a card peek, D396) answers off HEAD alone: one git call. A
    GitHub-link copy's answer comes from the engine (`_probe_github`)."""
    head = _head(app_dir)
    if head is None:
        return "local"
    if _kind(app_dir) == "marketplace":
        # §18.4: a GitHub link opened for a copy installed from the
        # Marketplace never moves it — the page asks "Follow <who>
        # directly?" (`install` → `market.switch_to_link` on yes).
        from fused_render import appfile

        return appfile.STATE_SWITCH_LINK
    if head == src["commit"]:
        return "same"
    if light:
        return "update"
    if _kind(app_dir) == "github":
        try:
            return _probe_github(app_dir, src, head)
        except Exception:  # noqa: BLE001 — unknown reads as "open it"
            logger.exception("install page: probe failed for %s", app_dir)
            return "local"
    # Auto-sync fast-forwards the clone on its own, so it may already be past
    # the link's commit: nothing to install then either.
    if _git("merge-base", "--is-ancestor", src["commit"], head, cwd=app_dir, timeout=60).returncode == 0:
        return "same"
    if _dirty(app_dir):
        return "local"
    ask = staged_clone(src)
    if ask is None:
        if _git("cat-file", "-e", src["commit"] + "^{commit}", cwd=app_dir, timeout=20).returncode != 0:
            # Neither repo here knows the link's commit (the staged clone was
            # swept): clean and behind as far as can be told, so offer the
            # update; its `merge --ff-only` reports a real divergence.
            return "update"
        ask = app_dir
    r = _git("merge-base", "--is-ancestor", head, src["commit"], cwd=ask, timeout=60)
    return "update" if r.returncode == 0 else "local"


def expected_identity(clone_dir: str, url: str) -> tuple[str, str | None]:
    """``(name, app_id)`` a build of ``clone_dir`` must carry: the repo's
    folder name, and the entry page's own ``fused-app-id`` — else the id
    derived from the repo URL. appfetch packs with exactly these, and
    ``install`` refuses a file whose manifest disagrees, so a hand-made
    `.fused` cannot steer the clone to another slug or another app's folder."""
    from fused_render import app_listing
    from fused_render import app_id as app_identity

    name = os.path.basename(clone_dir)
    try:
        entry = app_listing.app_entry(clone_dir)
    except OSError:
        entry = None
    own = app_identity.app_id(entry) if entry else None
    if own:
        return name, own
    key = (normalize_url(url).removeprefix("https://") + "/").encode("utf-8")
    return name, f"{app_identity.kebab(name)}-{hashlib.sha256(key).hexdigest()[:8]}"


# ---- placing the clone ---------------------------------------------------------
#
# ONE model for every way a clone lands (new, into a husk, replacing a
# file-installed copy), so no step can leave the user's folder half-written:
#
#   1. STAGE: the downloaded clone is moved into `app/.install-<slug>-*/` —
#      the only step that may copy (downloads and the workspace can be
#      different filesystems). A failure here touches nothing at `dest`.
#   2. SWAP: from then on only same-filesystem `os.rename`s, each undone in
#      reverse if a later one fails: whatever sits at `dest` (a husk, the old
#      copy) is parked inside the staging dir, the clone is renamed onto
#      `dest` (which must not exist — a folder that appeared since the probe
#      fails the rename and is left alone), and `.fused/` / `.venv` are
#      renamed from the parked copy into the clone.
#   3. RETIRE: what is left of the parked copy goes to the OS Trash
#      (recoverable — the user's own files beside the app included). Where
#      there is no bin it stays parked as `app/.replaced-<slug>-<time>/`,
#      never deleted.
#
# The `app/.install-*` staging dir is swept after 24 h like the file
# installer's (appfile._sweep_install_staging).

_CARRY = (".fused", ".venv")


def _place(staged: str, dest: str, *, park_existing: bool, kind: str = "github") -> None:
    root = os.path.dirname(dest)
    os.makedirs(root, exist_ok=True)
    work = tempfile.mkdtemp(dir=root, prefix=f".install-{os.path.basename(dest)}-")
    try:
        clone_in = os.path.join(work, "clone")
        try:
            shutil.move(staged, clone_in)                       # 1. STAGE
        except OSError as exc:
            raise GitInstallError(f"could not copy the clone into {root}: {exc}")
        _home_branch(clone_in, kind=kind)
        parked = None
        undo: list[tuple[str, str]] = []
        try:
            if park_existing:                                   # 2. SWAP
                parked = os.path.join(work, "old")
                os.rename(dest, parked)
                undo.append((parked, dest))
            elif os.path.lexists(dest):
                raise GitInstallError(f"{dest} appeared during the install; open the link again")
            os.rename(clone_in, dest)
            undo.append((dest, clone_in))
            for name in _CARRY if parked else ():
                src = os.path.join(parked, name)
                if os.path.isdir(src) and not os.path.lexists(os.path.join(dest, name)):
                    os.rename(src, os.path.join(dest, name))
                    undo.append((os.path.join(dest, name), src))
        except BaseException:
            for a, b in reversed(undo):
                try:
                    os.rename(a, b)
                except OSError:
                    logger.error("install rollback: could not move %s back to %s", a, b, exc_info=True)
            raise
        if parked:                                              # 3. RETIRE
            leftovers = [n for n in os.listdir(parked) if n != ".DS_Store"]
            if leftovers:
                try:
                    from fused_render.server import fs_mutate

                    fs_mutate._move_to_trash(parked)
                except Exception:  # noqa: BLE001 — no bin: keep it, never delete
                    keep = os.path.join(root, f".replaced-{os.path.basename(dest)}-{int(time.time())}")
                    os.rename(parked, keep)
                    logger.warning("replace: no Trash for the old copy; kept at %s", keep)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _home_name() -> str:
    """A GitHub-link copy's own branch (docs/CONTRIB_LOOP.md §17.1 I4):
    `classify.HOME_BRANCH`."""
    from fused_render.contrib import classify

    return classify.HOME_BRANCH


def _home_branch_pinned(clone_dir: str) -> None:
    """`_home_branch` for a Marketplace clone (`clone(commit=)`, HEAD
    detached at the approved commit): `fused/mine` created right there (no
    file changes), its upstream the MIRROR (`remote = .`, §18.4), and the
    mirror `refs/heads/<b>` moved from the author's tip the clone made to
    the approved commit — so nothing ever reads the tip as "the author's
    version" in this copy (M1)."""
    _HOME_BRANCH = _home_name()
    branch = _default_branch_of(clone_dir)
    head = _out(_git("rev-parse", "HEAD", cwd=clone_dir, timeout=20))
    if not branch or not _SHA_RE.match(head):
        logger.warning("install: no default branch / HEAD in %s", clone_dir)
        return
    r = _git("switch", "-q", "-c", _HOME_BRANCH, cwd=clone_dir, timeout=20)
    if r.returncode != 0:
        logger.warning("install: could not create %s in %s: %s", _HOME_BRANCH, clone_dir, _err(r))
        return
    _git("update-ref", f"refs/heads/{branch}", head, cwd=clone_dir, timeout=20)
    _git("config", "--local", f"branch.{_HOME_BRANCH}.remote", ".", cwd=clone_dir, timeout=20)
    _git("config", "--local", f"branch.{_HOME_BRANCH}.merge", f"refs/heads/{branch}",
         cwd=clone_dir, timeout=20)


def _home_branch(clone_dir: str, *, kind: str = "github") -> None:
    """Right after the clone, before any hook or sync can see it (§17.6): HEAD
    on `fused/mine` at the clone's commit (no file changes), tracking the
    author's branch, which stays behind as the fetch-only mirror. Whose app
    it is is not known yet: `classify.record_install` puts an `own` copy back
    on its default branch. A failure is logged, never fatal — the first
    settle creates the branch (`classify.ensure_home_locked`)."""
    _HOME_BRANCH = _home_name()
    try:
        branch = _out(_git("symbolic-ref", "--short", "HEAD", cwd=clone_dir, timeout=20))
        if not branch and kind == "marketplace":
            _home_branch_pinned(clone_dir)
            return
        if not branch or branch == _HOME_BRANCH:
            return
        r = _git("switch", "-q", "-c", _HOME_BRANCH, cwd=clone_dir, timeout=20)
        if r.returncode != 0:
            logger.warning("install: could not create %s in %s: %s", _HOME_BRANCH, clone_dir, _err(r))
            return
        _git("config", "--local", f"branch.{_HOME_BRANCH}.remote", "origin", cwd=clone_dir, timeout=20)
        _git("config", "--local", f"branch.{_HOME_BRANCH}.merge", f"refs/heads/{branch}",
             cwd=clone_dir, timeout=20)
    except GitInstallError:
        logger.warning("install: could not set up %s in %s", _HOME_BRANCH, clone_dir, exc_info=True)


def install(fused_path: str, target: dict, manifest: dict, *, force: bool,
            kind: str = "github", market: dict | None = None) -> dict:
    """Install or update the clone ``target`` (appfile.install_target's
    answer for a git-source file) describes. Answers the action taken.

    ``kind="marketplace"`` (docs/CONTRIB_LOOP.md §18.4): a Marketplace
    install of a clone made at the approved commit (`clone(commit=)`),
    recorded as a marketplace copy with ``market = {slug, approved}``. An
    existing copy of the repo is never moved from here: a marketplace copy
    opened by GitHub link is the `switch-link` prompt (``force`` = the user
    said yes → `market.switch_to_link`), and every other update of a
    marketplace copy is the engine's (`engine.bring_in`)."""
    from fused_render import app_fused_dir, app_git, appfile

    # Only the link's own facts: `install_target` merges the copy's page
    # words (who/changes/collision) into `git`, and `src` is stamped.
    src = {"url": target["git"]["url"], "commit": target["git"]["commit"]}
    state = target["state"]
    dest = os.path.normpath(target["path"])
    if state == appfile.STATE_CLASH:
        raise GitInstallError(f"{target['path']} already holds a different app "
                              f"({target['other_name']}); rename or uninstall it first")
    installed_kind = kind
    kind = _kind(dest) if target.get("found_by") == "git" else None
    if kind == "marketplace" or state == appfile.STATE_SWITCH_LINK:
        return _install_on_market_copy(dest, target, manifest, force=force,
                                       wanted=installed_kind)
    if installed_kind == "marketplace" and kind == "github":
        # D13: the trust level changes only by the user's explicit switch
        # (`market.switch_to_reviewed`, marketplace.start_install's confirm).
        raise GitInstallError("You follow this app by its GitHub link. Switch it to reviewed "
                              "releases from its app page.")
    if state == appfile.STATE_LOCAL:
        if target.get("found_by") == "git":
            if kind == "github":
                # The engine keeps the copy on the author's latest with the
                # user's changes kept (§16.8); the app page is where to look —
                # when it has the panel (A1, §16.12). Plain words either way.
                view = copy_view(dest, src) or {}
                who = view.get("who") or "the author"
                if view.get("collision"):
                    raise GitInstallError(
                        f"{who}'s latest adds {view['collision'][0]}, which you already have "
                        "here, so this app isn't updated from here."
                        + (" Open it to move yours aside." if view.get("app_page") else ""))
                if view.get("app_page"):
                    raise GitInstallError("You have your own changes in this app. Open it to "
                                          f"bring in {who}'s latest.")
                raise GitInstallError("This app has changes of its own, so it isn't updated "
                                      "from here.")
            if _contrib_sources():
                raise GitInstallError("You already have this app with your own changes. "
                                      "Open it to see them.")
            raise GitInstallError(f"{target['path']} has changes of its own; update it with git "
                                  "(git pull)")
        raise GitInstallError(f"{target['path']} was edited since it was installed from a file; "
                              "rename or uninstall it, then open the link again")
    if state == appfile.STATE_SAME:
        raise GitInstallError("this commit is already installed")
    if state == appfile.STATE_REPLACE and not force:
        raise appfile.InstallNeedsConfirm(target)

    if state == appfile.STATE_UPDATE and kind == "github":
        # "Get the latest" on a GitHub-link copy IS the engine's Bring in
        # (§16.8): the same table as every automatic update, its words plain.
        from fused_render.contrib import engine

        res = engine.bring_in(os.path.realpath(dest))
        if not res.get("ok") or res.get("needs_merge"):
            raise GitInstallError(res.get("message") or "Couldn't update this app right now. "
                                  "Open it to see why.")
        action = "updated"
    elif state == appfile.STATE_UPDATE:
        if kind is not None or _contrib_sources():
            # A recorded copy (or the loop on): this update is one more
            # writer of a repo the loop also touches — it holds the same slot,
            # and a committed `.fused/` the loop marked skip-worktree (any
            # recorded copy, §16.1) needs fused's local copies set aside to
            # let the update through.
            from fused_render.contrib import replay

            with replay.slot() as held:
                if not held:
                    raise GitInstallError("this app is busy updating; open the link again in a moment")
                _update_clone(dest, src, aside=lambda: replay.fused_aside(
                    os.path.realpath(dest), src["commit"]))
        else:
            _update_clone(dest, src, aside=None)
        action = "updated"
    else:
        staged = staged_clone(src)
        if staged is None:
            raise GitInstallError("the downloaded clone is gone or out of date; open the link again")
        if (manifest.get("name"), appfile.app_id_of(manifest)) != expected_identity(staged, src["url"]):
            raise GitInstallError("this app file does not match the repository it names; "
                                  "open the GitHub link again")
        _place(staged, dest, park_existing=state == appfile.STATE_REPLACE or bool(target.get("husk")),
               kind=installed_kind)
        # A fresh clone at `dest`: whatever record the folder had (a copy
        # deleted in Finder and installed again, a husk's old app) names
        # another repo's history — its proposal, overlap and activity are
        # not this clone's (§16.1).
        _drop_record(dest)
        action = "installed"
    shutil.rmtree(_staging_root(src["url"]), ignore_errors=True)

    app_git._ensure_excludes(dest)
    app_fused_dir.ensure(dest)
    appfile.write_stamp(dest, manifest, fused_path, git=src)
    if installed_kind == "marketplace" and action == "installed":
        _record_install(dest, src["url"], kind="marketplace", market=market)
    else:
        _record_install(dest, src["url"])
    entry = os.path.join(dest, *manifest["entry"].split("/"))
    return {"action": action, "entry": entry.replace(os.sep, "/"), "pruned": [], "adopted_state": None}


SWITCH_LINK_SENTENCE = "Follow {who} directly? You'll get every change, unreviewed."


def _install_on_market_copy(dest: str, target: dict, manifest: dict, *, force: bool,
                            wanted: str) -> dict:
    """`install` onto an existing MARKETPLACE copy (§18.4) — never a move
    from here (M1): a Marketplace install of it is the engine's Bring in
    (the approved release); a GitHub link opened for it is the `switch-link`
    prompt — refused without the user's yes (`InstallNeedsConfirm`, the
    page asks "Follow <who> directly?"), and with it the explicit switch
    (`market.switch_to_link`, one Activity line with Undo)."""
    from fused_render import appfile

    root = os.path.realpath(dest)
    entry = os.path.join(dest, *manifest["entry"].split("/")).replace(os.sep, "/")
    if wanted == "marketplace":
        from fused_render.contrib import engine

        res = engine.bring_in(root)
        if not res.get("ok") or res.get("needs_merge"):
            raise GitInstallError(res.get("message") or "Couldn't update this app right now. "
                                  "Open it to see why.")
        return {"action": "updated", "entry": entry, "pruned": [], "adopted_state": None}
    if not force:
        raise appfile.InstallNeedsConfirm(target)
    from fused_render.contrib import market

    res = market.switch_to_link(root)
    if not res.get("ok"):
        raise GitInstallError(res.get("message") or "Couldn't switch this app right now.")
    return {"action": "switched", "entry": entry, "pruned": [], "adopted_state": None,
            "message": res.get("message")}


def _update_clone(dest: str, src: dict, *, aside) -> None:
    """The fast-forward update of an installed clone: fetch, then
    `merge --ff-only` to the link's commit. With the contribution loop on
    (`aside` given): the merge runs inside `aside()`, only the FOLLOWED
    branch is fetched (not every branch the author ever pushed), and git's
    own words go to the log, never to the install page — a plain sentence
    does (§0.1). A copy on `fused/mine` (§17.6) lands the commit there and
    then moves the author's branch — its fetch-only mirror — to what was
    fetched, as a ref update (I3)."""
    if _kind(dest) == "marketplace":
        # M1 (§18.4): a marketplace copy is never fast-forwarded to a link's
        # commit — `install` answers it with the switch-link prompt.
        raise GitInstallError("This app updates from the Marketplace's reviewed versions only.")
    fetch = ("fetch", "-q", "origin")
    branch = None
    if aside is not None:
        from fused_render.contrib import classify

        branch = classify._local_default_branch(os.path.realpath(dest))
        if branch:
            fetch = ("fetch", "-q", "--", "origin", branch)
    for args in (fetch, ("merge", "-q", "--ff-only", src["commit"])):
        if args[0] == "merge" and aside is not None:
            with aside():
                r = _git(*args, cwd=dest)
        else:
            r = _git(*args, cwd=dest)
        if r.returncode != 0:
            if aside is not None:
                logger.warning("install update of %s: git %s failed: %s", dest, args[0], _err(r))
                raise GitInstallError("Couldn't update this app right now. Check your "
                                      "connection and open the link again.")
            raise GitInstallError(f"git {args[0]} failed: {_err(r)}")
    if branch and _out(_git("symbolic-ref", "--short", "HEAD", cwd=dest, timeout=20)) == _home_name():
        tip = _out(_git("rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}^{{commit}}",
                        cwd=dest, timeout=20))
        old = _out(_git("rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}",
                        cwd=dest, timeout=20))
        if tip and old != tip and (not old or _git("merge-base", "--is-ancestor", old, tip,
                                                   cwd=dest, timeout=20).returncode == 0):
            _git("update-ref", f"refs/heads/{branch}", tip, old or "", cwd=dest, timeout=20)


def _contrib_sources() -> bool:
    try:
        from fused_render.contrib import flags

        return flags.sources()
    except Exception:  # noqa: BLE001 — an unreadable flag reads as off
        return False


def _drop_record(dest: str) -> None:
    try:
        from fused_render.contrib import store

        store.drop(dest)
    except Exception:  # noqa: BLE001 — record_install replaces it anyway
        logger.exception("could not drop the old install record of %s", dest)


def _record_install(dest: str, url: str, *, kind: str = "github",
                    market: dict | None = None) -> None:
    """Record the install source OUTSIDE the folder (contrib/store.py,
    docs/CONTRIB_LOOP.md §4, §16.1) and settle the copy's guards — a
    committed `.fused/` marked skip-worktree so the stamp and `meta.json`
    just written never read as edits, and the push guard. Whatever the
    flags; never fails the install that already landed. `kind=
    "marketplace"` + `market` (§18.4): a Marketplace install."""
    try:
        from fused_render.contrib import classify

        if kind == "marketplace":
            classify.record_install(dest, url, kind="marketplace", market=market)
            return
        classify.record_install(dest, url)
    except Exception:  # noqa: BLE001
        logger.exception("could not record the install source of %s", dest)


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
