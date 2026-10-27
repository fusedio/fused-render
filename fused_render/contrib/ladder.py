"""The Community tab's ladder and its one door (docs/CONTRIB_LOOP.md §19.2).

`status(app_dir)` is ONE composition read for an app's Community tab and
Share sheet: which rung the app stands on (1 On this Mac, 2 On GitHub, 3 In
the Marketplace), whose app it is, the GitHub facts the rung-1 checklist and
the rung-2 strip draw, the Marketplace listing (§18.6, the ListingStrip's
own answer), the App Doctor verdict the listing gate applies, and the flags.
Nothing new is decided here: kind/mine come from the record (`store`, the
ownership rule `classify` applies), the link from `share_app`, the listing
from `listing.view` + `actions_listing.decide`, the doctor from
`listing.doctor_blockers` — the same gate Publish refuses on.

A PAGE READ NEVER WAITS ON GITHUB. GitHub facts are cached reads
(`refresh.github_identity`, `gh.cached_identity`); the repository's
visibility is cached 10 min and, on a miss, looked up on the contrib
background lane (`refresh._submit`) while this read answers `null`. The rung
is read LIVE from the folder (origin on disk), not from the record: a
rebind running in the background must not make the ladder lag.

`publish_github(app_dir, name, visibility)` is rung 1 → 2 in one call:
`git init` when the folder is not a repository (an app inside the shared,
remote-less `<workspace>/local` repo gets one of its own: the folder leaves
the shared repo's index in one commit there, the nested state
`local_monorepo` already keeps for apps with a remote — `app_git._repo_scope`
commits into the nested repo from then on); refused when nested inside any
other repository; the commit identity from git config, else the gh
account's name and `<login>@users.noreply.github.com`; a first commit
"Initial commit" when there are none; then `github_setup.publish_start`,
whose record the client polls on `GET /api/github/publish`. Its done path
rebinds the record (`github_setup._after_publish` → `classify.rebind`).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time

from fused_render.contrib.jobs import PlainError

logger = logging.getLogger(__name__)

#: How long a repository's visibility is trusted (§19.2: 10 min).
VISIBILITY_TTL_S = 600.0
#: How long one App Doctor verdict is reused for an unchanged HEAD/tip.
DOCTOR_TTL_S = 30.0

NESTED = ("This app lives inside a bigger project on GitHub, so it can't have a "
          "repository of its own.")
NO_GH = "Install the GitHub CLI first."
SIGNED_OUT = "Sign in to GitHub first."
OFFLINE = "GitHub can't be reached right now. Try again in a moment."
HAS_REMOTE = "This app is already connected to a repository."
BUSY_PUBLISH = "A GitHub publish is already running. Wait for it to finish."
BUSY_SYNC = "Fused is busy saving this app. Try again in a moment."
BAD_VISIBILITY = "Choose public or private."
BAD_NAME = "Give the repository a name made of letters, numbers and dashes."
FIRST_COMMIT = "Initial commit"

#: The listing states that put an app on rung 3 besides `live`: a request
#: open on the index (`actions_listing.decide`).
RUNG3_STATES = ("live", "in-review", "needs-confirmation", "removing")

_vis_lock = threading.Lock()
_vis_cache: dict = {}  # repo.lower() -> (at, "public"|"private")
_doc_lock = threading.Lock()
_doc_cache: dict = {}  # key -> (at, stamp, {clear, failing})


# ------------------------------------------------------------------ helpers


def _git_bin() -> str:
    from fused_render.shell import git_tools

    return git_tools.git_bin()


def _git(root: str, *args: str, env: dict | None = None, identity: bool = False,
         timeout: float = 30.0):
    """One local git call, no hooks or fsmonitor; `(rc, out, err)` text, or
    None when git could not run. `identity`: Fused's own commit identity —
    the shared `local` repo's (app_git)."""
    from fused_render import git_upstream as gu

    ident = ("-c", "user.name=Fused", "-c", "user.email=apps@fused.io") if identity else ()
    try:
        p = subprocess.run(
            [_git_bin(), "-C", root, "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
             *ident, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, close_fds=False, stdin=subprocess.DEVNULL,
            env={**os.environ, **gu._ENV, **(env or {})})
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def _ok(r) -> bool:
    return r is not None and r[0] == 0


def _must(r, what: str):
    if not _ok(r):
        logger.warning("ladder: git %s failed: %s", what, r[2] if r else "git did not run")
        raise PlainError(f"Couldn't {what}. Try again.", reason="failed")
    return r[1]


def slug(name: str) -> str:
    """A repository name from a folder name: lowercase, every run of other
    characters one `-`, no leading/trailing dash (§19.3's default)."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:100].strip("-")


def _flags() -> dict:
    """`{sources, marketplace, github_link}`. `marketplace` is the switch the
    listing routes gate on (`listing._enabled`, §18.8), so rung 3 is offered
    exactly when its buttons work."""
    from fused_render.contrib import flags, listing

    try:
        from fused_render.shell import prefs

        # Owner 2026-10-27: one switch for Community/GitHub sharing — the
        # install link is part of it (the old github_link_enabled pref still
        # counts, for a stored value, but has no row of its own any more).
        link = bool(prefs.github_link_enabled() or prefs.contrib_enabled())
    except Exception:  # noqa: BLE001 — an unreadable pref reads as off
        link = False
    return {"sources": bool(flags.sources()), "marketplace": bool(listing._enabled()),
            "github_link": link}


def _gh_installed() -> bool:
    """Whether a runnable `gh` is on this machine: the cached health
    snapshot when there is one, else path resolution (stat calls, no
    spawn)."""
    from fused_render import github_setup

    try:
        cached = github_setup.cached_summary()
    except Exception:  # noqa: BLE001
        cached = None
    if isinstance(cached, dict) and "found" in cached and not cached.get("stale"):
        return bool(cached.get("found"))
    try:
        path, _src = github_setup.resolve()
        return bool(path) and github_setup.executable(path)
    except Exception:  # noqa: BLE001
        return False


def _owner_of(repo: str | None) -> str:
    return (repo or "").split("/", 1)[0]


def _owns_cached(owner: str, login: str | None) -> bool | None:
    """Whether `login` owns repos under `owner`, from what is known without
    asking GitHub: the login itself, or `classify`'s org-role cache. None
    when unknown (the background lane then asks)."""
    from fused_render.contrib import classify

    if not owner or not login:
        return None
    if owner.lower() == login.lower():
        return True
    with classify._role_lock:
        hit = classify._role_cache.get((login.lower(), owner.lower()))
    if hit and time.time() - hit[0] < classify.ROLE_TTL_S:
        return bool(hit[1])
    return None


def _ask_owner(owner: str) -> None:
    """Fill `classify`'s org-role cache for `owner` on the background lane."""
    from fused_render.contrib import classify, refresh

    def run():
        try:
            classify._owns(owner, classify._identity())
        except classify._Unknown:
            pass
    refresh._submit(refresh._P_OPEN, "ladder-owner", owner.lower(), run)


# --------------------------------------------------------------- visibility


def visibility(repo: str | None, *, fresh: bool = False) -> str | None:
    """"public" | "private" for `repo` from the 10-minute cache; on a miss
    (or a stale entry) the lookup is queued on the background lane and this
    answers None — or the stale value while the new one comes."""
    if not repo:
        return None
    k = repo.lower()
    with _vis_lock:
        hit = _vis_cache.get(k)
    if hit and time.time() - hit[0] < VISIBILITY_TTL_S and not fresh:
        return hit[1]
    from fused_render.contrib import refresh

    refresh._submit(refresh._P_OPEN, "visibility", k, lambda: _fetch_visibility(repo))
    with _vis_lock:  # an inline lane (tests) has answered already
        hit = _vis_cache.get(k) or hit
    return hit[1] if hit else None


def _fetch_visibility(repo: str) -> str | None:
    """`gh.repo(repo)`'s `private` (the existing helper — `gh api
    repos/<repo>`, the same fact `gh repo view --json visibility` reads).
    Only a definite answer is cached."""
    from fused_render.contrib import gh

    try:
        info = gh.repo(repo)
    except Exception:  # noqa: BLE001 — unknown, asked again next read
        return None
    if not isinstance(info, dict) or not isinstance(info.get("private"), bool):
        return None
    value = "private" if info["private"] else "public"
    note_visibility(repo, value)
    return value


def note_visibility(repo: str, value: str) -> None:
    """Remember `repo`'s visibility (publish-github knows it: it chose it)."""
    if repo and value in ("public", "private"):
        with _vis_lock:
            _vis_cache[repo.lower()] = (time.time(), value)


def forget_caches() -> None:
    """Test seam."""
    with _vis_lock:
        _vis_cache.clear()
    with _doc_lock:
        _doc_cache.clear()


# ------------------------------------------------------------------- doctor


def _doctor(app_dir: str, root: str | None, unpushed: int = 0, *, fresh: bool = False) -> dict:
    """`{clear, failing}` — `listing.doctor_blockers`, the gate Publish to
    the Marketplace refuses on (sharing rows of severity critical/warning,
    plus secrets; fails closed). Reused for DOCTOR_TTL_S while HEAD and the
    pushed tip are unchanged."""
    from fused_render.contrib import listing, replay, store

    k = store.key(app_dir)
    stamp = (replay.rev(root, "HEAD") if root else None, unpushed)
    now = time.time()
    with _doc_lock:
        hit = _doc_cache.get(k)
    if hit and hit[1] == stamp and now - hit[0] < DOCTOR_TTL_S and not fresh:
        return dict(hit[2])
    try:
        blockers = listing.doctor_blockers(app_dir)
    except Exception:  # noqa: BLE001 — unknown, not a verdict
        logger.warning("ladder: doctor failed for %s", app_dir, exc_info=True)
        return {"clear": None, "failing": None}
    out = {"clear": not blockers, "failing": len(blockers)}
    with _doc_lock:
        _doc_cache[k] = (now, stamp, out)
    return dict(out)


# ------------------------------------------------------------------- status


def _who(app_dir: str, root: str | None, origin: str | None, login: str | None) -> dict:
    """`{kind, mine, repo, remix_of, pending, recorded}` from the record
    (`recorded`: the record's own repo) — or, for a folder with no record
    yet, the ownership rule applied to what is known without asking GitHub
    (`pending: True` while that is not enough)."""
    from fused_render.contrib import classify, flags, store

    rec = store.get(app_dir)
    if rec is None:
        # The background classification a folder with no record gets (A1 on,
        # workspace folders only — `_kick`'s rule; nothing new here).
        if flags.sources():
            classify.kind_of(app_dir, kick=True)
        if not origin:
            return {"kind": "own", "mine": True, "repo": None, "remix_of": None,
                    "pending": False, "recorded": None}
        owned = _owns_cached(_owner_of(origin), login)
        if owned is None and login:
            _ask_owner(_owner_of(origin))
        return {"kind": None if owned is None else ("own" if owned else "github"),
                "mine": bool(owned), "repo": origin, "remix_of": None,
                "pending": owned is None, "recorded": None}
    kind = rec.get("kind")
    mine = kind == "own" or (kind == "scratch" and not rec.get("try_of"))
    pending = kind == "github" and not rec.get("owner_checked")
    if mine and classify._needs_rebind(app_dir, rec):
        classify.rebind_in_background(app_dir)
        pending = True
    repo = (rec.get("repo") or origin) if mine else rec.get("repo")
    return {"kind": kind, "mine": mine, "repo": repo, "remix_of": rec.get("remix_of"),
            "pending": pending, "recorded": rec.get("repo")}


def _blocked(app_dir: str, root: str | None, top: str | None) -> str | None:
    """The sentence publish-github would refuse with for this folder's
    shape (nested elsewhere, a remote already there) — None when the door
    is open as far as the folder goes. Local git only."""
    from fused_render.contrib import classify

    if root is not None:
        return HAS_REMOTE if classify._origin_url(root) or _has_any_remote(root) else None
    if top is not None and not _splittable(app_dir, top):
        return NESTED
    return None


def _has_any_remote(root: str) -> bool:
    r = _git(root, "remote")
    return bool(_ok(r) and r[1])


def _splittable(app_dir: str, top: str) -> bool:
    """Whether `app_dir`, nested in the repo at `top`, may leave it for a
    repository of its own: only an app directly under the shared, remote-less
    `<workspace>/local` repo (app_git's), never any other project."""
    from fused_render import app_git

    try:
        local = os.path.realpath(app_git.local_repo_root())
    except Exception:  # noqa: BLE001
        return False
    real = os.path.realpath(app_dir)
    return (os.path.realpath(top) == local and os.path.dirname(real) == local
            and not _has_any_remote(local))


def status(app_dir: str, *, fresh: bool = False) -> dict:
    """The ladder (§19.2) — see the module docstring. `app_dir` is a
    workspace app folder (the router checked)."""
    from fused_render import git_upstream as gu
    from fused_render.contrib import classify, follow, refresh, store

    app_dir = store.key(app_dir)
    fl = _flags()
    ident = refresh.github_identity()
    login = ident.get("login")
    if not login:
        from fused_render.contrib import gh

        login = (gh.cached_identity() or {}).get("login")
    root = follow.is_repo_root(app_dir)
    top = root or gu.repo_root(app_dir)
    origin = classify._origin_repo(root)
    who = _who(app_dir, root, origin, login)
    mine = who["mine"]
    github = {"installed": _gh_installed(), "signed_in": bool(ident.get("signed_in")),
              "checking": bool(ident.get("checking")), "login": login,
              "repo": who["repo"], "visibility": None, "unpushed": 0, "link": None,
              "blocked": None}
    out = {"ok": True, "rung": None, "kind": who["kind"], "mine": mine,
           "pending": who["pending"], "remix_of": who["remix_of"], "github": github,
           "listing": None, "doctor": {"clear": None, "failing": None}, "flags": fl}
    if not mine:
        return out
    on_github = bool(root and origin)
    if on_github:
        github["repo"] = who["repo"] or origin
        github["visibility"] = visibility(github["repo"], fresh=fresh)
        github["unpushed"] = classify._unpublished(root)
        if fl["github_link"]:
            from fused_render import share_app

            try:
                state = share_app.github_link_state(app_dir)
            except Exception:  # noqa: BLE001
                logger.warning("ladder: link state failed for %s", app_dir, exc_info=True)
                state = {}
            github["link"] = state.get("url") if state.get("ok") else None
    else:
        github["blocked"] = _blocked(app_dir, root, None if root else top)
    rung = 2 if on_github else 1
    # The listing needs the record's repo (the listing routes refuse without
    # one): an unrecorded folder, or one whose rebind is still running, waits.
    if on_github and fl["marketplace"] and who["kind"] == "own" and who["recorded"]:
        out["listing"] = _listing(app_dir, signed_in=github["signed_in"], fresh=fresh)
        state = (out["listing"] or {}).get("state")
        if state in RUNG3_STATES:
            rung = 3
    if on_github:
        out["doctor"] = _doctor(app_dir, root, github["unpushed"], fresh=fresh)
    out["rung"] = rung
    return out


def _listing(app_dir: str, *, signed_in: bool, fresh: bool = False) -> dict | None:
    """The ListingStrip's own answer (`GET /api/contrib/listing`, §18.6):
    `{listing, state, sentence, actions, job}` — a refresh job started when
    the last read is stale and someone is signed in, never waited for."""
    from fused_render.contrib import actions_listing, jobs, listing, store

    try:
        job = None
        running = jobs.latest("listing-check", store.key(app_dir))
        if running and running.get("status") == "running":
            job = running.get("id")
        elif signed_in and (fresh or listing.stale(app_dir)):
            job = listing.start_refresh(app_dir)
        lst = listing.view(app_dir)
        decided = actions_listing.decide(lst, signed_in=signed_in, refreshing=bool(job))
    except Exception:  # noqa: BLE001 — one piece failing must not blank the tab
        logger.warning("ladder: listing failed for %s", app_dir, exc_info=True)
        return None
    version = _listed_version(app_dir)
    if isinstance(lst.get("approved"), dict):
        lst = {**lst, "approved": {**lst["approved"], "version": version}}
    return {"ok": True, "listing": lst, "state": decided["state"],
            "sentence": decided["sentence"], "actions": decided["actions"], "job": job,
            "version": version}


def _listed_version(app_dir: str) -> str | None:
    """The version of the listed release: the index row's `version` from
    the last listing read (`listing.view` does not carry it)."""
    from fused_render.contrib import listing

    try:
        row = (listing.cached(app_dir) or {}).get("row")
    except Exception:  # noqa: BLE001
        return None
    v = row.get("version") if isinstance(row, dict) else None
    return v if isinstance(v, str) and v else None


# ---------------------------------------------------------- publish-github


def _identity_ok(root: str) -> bool:
    """Whether git already has a commit identity for `root` (global, system
    or repo-local config)."""
    name, email = _git(root, "config", "--get", "user.name"), _git(root, "config", "--get",
                                                                     "user.email")
    return bool(_ok(name) and name[1] and _ok(email) and email[1])


def _set_identity(root: str, ident: dict) -> None:
    """The gh account's name and `<login>@users.noreply.github.com`, as this
    repository's own (`--local`) identity — never a dialog (§19.2); the Git
    panel can change it later."""
    login = ident["login"]
    name = ident.get("name") if isinstance(ident.get("name"), str) and ident["name"].strip() \
        else login
    _must(_git(root, "config", "--local", "user.name", str(name).strip()), "set your name")
    _must(_git(root, "config", "--local", "user.email", f"{login}@users.noreply.github.com"),
          "set your email")


def _ignore_files(root: str) -> None:
    """What `app_git.init_repo` gives an app's own repository: the ignore
    rules in `.git/info/exclude`, and a `.gitignore` when there is none —
    so the first commit never sweeps `.venv/` or `.fused/` in."""
    from fused_render import app_git

    app_git._ensure_excludes(root)
    gi = os.path.join(root, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8") as fh:
            fh.write(app_git._GITIGNORE)


def _leave_shared_repo(app_dir: str, local: str) -> None:
    """Take `app_dir` out of the shared repo's tracking in ONE commit there,
    built on a private index so nothing another app has staged rides along;
    then the same removal in the shared repo's real index. The files stay
    on disk (the app's own repo is next to be made)."""
    rel = os.path.basename(app_dir)
    head = _git(local, "rev-parse", "--verify", "-q", "HEAD")
    if _ok(head) and head[1]:
        listed = _git(local, "ls-tree", "--name-only", "HEAD", "--", rel)
        if _ok(listed) and listed[1]:
            fd, idx = tempfile.mkstemp(prefix="ladder-index-")
            os.close(fd)
            os.unlink(idx)
            env = {"GIT_INDEX_FILE": idx}
            try:
                _must(_git(local, "read-tree", head[1], env=env), "read the workspace repository")
                _must(_git(local, "rm", "-r", "--cached", "-q", "--", rel, env=env),
                      "move the app out of the workspace repository")
                tree = _must(_git(local, "write-tree", env=env), "write the workspace repository")
                commit = _must(_git(local, "commit-tree", tree, "-p", head[1], "-m",
                                    f"Move {rel} to its own repository", identity=True),
                               "commit to the workspace repository")
                _must(_git(local, "update-ref", "-m", f"fused: move {rel} out", "HEAD", commit,
                           head[1]), "update the workspace repository")
            finally:
                try:
                    os.unlink(idx)
                except OSError:
                    pass
    for _attempt in range(3):  # an app_git commit in `local` may hold index.lock
        r = _git(local, "rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", rel)
        if _ok(r) or "index.lock" not in (r[2] if r else ""):
            break
        time.sleep(0.2)
    if not _ok(r):
        logger.warning("ladder: could not untrack %s in the workspace repository: %s",
                       rel, r[2] if r else "git did not run")


def _make_root(app_dir: str) -> str:
    """`app_dir` as the root of a repository of its own — made when it is
    not one — or a PlainError. Caller holds the slot."""
    from fused_render import git_upstream as gu
    from fused_render.contrib import follow

    real = os.path.realpath(app_dir)
    root = follow.is_repo_root(real)
    if root is not None:
        if _has_any_remote(root):
            raise PlainError(HAS_REMOTE, reason="has-remote")
        return root
    top = gu.repo_root(real)
    if top is not None:
        if not _splittable(real, top):
            raise PlainError(NESTED, reason="nested")
        _leave_shared_repo(real, os.path.realpath(top))
    _must(_git(real, "init", "-q"), "make a repository for this app")
    root = follow.is_repo_root(real)
    if root is None or os.path.realpath(root) != real:
        raise PlainError("Couldn't make a repository for this app. Try again.", reason="failed")
    _ignore_files(root)
    return root


def _first_commit(root: str) -> None:
    """`stage_all` + "Initial commit" (`.fused/` taken back out, as
    app_git's scoped add does)."""
    _must(_git(root, "add", "-A", "--", "."), "stage the app's files")
    _git(root, "reset", "-q", "--", ":(literal).fused")
    _must(_git(root, "commit", "-q", "--allow-empty", "-m", FIRST_COMMIT), "make the first commit")


def publish_github(app_dir: str, name: str | None = None,
                   visibility: str | None = None) -> dict:
    """Rung 1 → 2 (§19.2): see the module docstring. Answers
    `github_setup`'s publish record (`state: "running"`); every refusal is
    a `PlainError` with a plain sentence and a `reason` (`no-gh`,
    `signed-out`, `offline`, `busy`, `nested`, `has-remote`, `invalid`,
    `failed`)."""
    from fused_render import github_setup
    from fused_render.contrib import classify, follow, gh, replay, store

    vis = (visibility or "public").strip().lower()
    if vis not in ("public", "private"):
        raise PlainError(BAD_VISIBILITY, reason="invalid")
    repo_name = slug(name if isinstance(name, str) and name.strip()
                     else os.path.basename(os.path.realpath(app_dir)))
    if not repo_name:
        raise PlainError(BAD_NAME, reason="invalid")
    if not _gh_installed():
        raise PlainError(NO_GH, reason="no-gh")
    try:
        ident = gh.identity(strict=True)
    except Exception:  # noqa: BLE001 — offline / rate-limited: not "signed out"
        raise PlainError(OFFLINE, reason="offline")
    if not ident or not ident.get("login"):
        raise PlainError(SIGNED_OUT, reason="signed-out")
    if github_setup.publish_running():
        raise PlainError(BUSY_PUBLISH, reason="busy")
    real = os.path.realpath(app_dir)
    # Refuse what the folder's shape refuses BEFORE anything is written.
    root0 = follow.is_repo_root(real)
    from fused_render import git_upstream as gu

    blocked = _blocked(real, root0, None if root0 else gu.repo_root(real))
    if blocked == NESTED:
        raise PlainError(NESTED, reason="nested")
    if blocked == HAS_REMOTE:
        raise PlainError(HAS_REMOTE, reason="has-remote")
    # The record rebind will find (an app made here is `own`, repo null).
    # Never under the slot: `ensure` may ask GitHub.
    classify.ensure(real, identity=ident)
    with replay.slot(block=True) as held:
        if not held:
            raise PlainError(BUSY_SYNC, reason="busy")
        root = _make_root(real)
        if not _identity_ok(root):
            _set_identity(root, ident)
        if not github_setup._has_commits(root):
            _first_commit(root)
        rec = store.get_raw(store.key(root))
        if isinstance(rec, dict) and rec.get("kind") in ("own", "scratch"):
            # The record's copy id in `.git/config` keeps it bound once
            # `origin` names a repo it does not yet (store "BOUND TO THE
            # REPO AT THE PATH") — so rebind finds it instead of a dropped row.
            classify._stamp_copy(root, rec)
    if follow.is_repo_root(root) != real:  # never hand gh a bigger repo
        raise PlainError(NESTED, reason="nested")
    try:
        record = github_setup.publish_start(root, repo_name, vis)
    except github_setup.PublishError as e:
        raise PlainError(_sentence(str(e)), reason="refused")
    note_visibility(f"{ident['login']}/{repo_name}", vis)
    return record


def _sentence(text: str) -> str:
    text = (text or "").strip()
    return (text[:1].upper() + text[1:] + ("" if text.endswith((".", "!", "?")) else ".")) \
        if text else "Couldn't put this app on GitHub. Try again."

