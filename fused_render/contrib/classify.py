"""Own app vs GitHub-link copy (docs/CONTRIB_LOOP.md §4) — the ownership
rule, applied once per copy and recorded outside the folder (`store`).

THE RULE (owner-approved handoff, "People and words"): a copy is YOURS when
its folder heads its own repository whose `origin` is a GitHub repo owned by
the signed-in GitHub account, or by an org where that account is an OWNER
(`gh.org_role(org) == "admin"`) — or when it is not a GitHub clone at all (an
app made here, the shared `local` repo, a non-GitHub remote). Every other
GitHub clone is a GitHub-link copy, collaborators included: push rights alone
never make an app yours.

UNKNOWN OWNERSHIP READS AS "GITHUB", NEVER "OWN". Without a GitHub identity
(signed out, gh missing, GitHub unreachable) the copy is classified `github`
with `owner_checked: False`, and the first `ensure` that has an identity
re-checks it ONCE — possibly upgrading to `own`, never the reverse
automatically. The asymmetry is the safe direction: a mistaken `github` keeps
the author's edits local as "your changes" until they sign in (nothing lost,
nothing pushed); a mistaken `own` would push a contributor's edits straight
to someone else's main.

NOTHING HERE READS `.fused/`. The origin URL and the repo-root test are git
facts; the record is ours. `.fused/` is only ever WRITTEN to by fused, and
`protect_fused_dir` is how a repo that commits one is kept from turning
fused's own writes into "edits" that block every update.

Who calls what: `record_install` from `appfile_git.install` (inline, the
install is already a slow request); `ensure` from the routers and from the
engine's background classification (never under the mutation slot — it may
ask GitHub who the user is, and §0.9 keeps network calls off the slot).
"""
from __future__ import annotations

import logging
import os
import re
import threading

from fused_render import git_upstream as gu
from fused_render.contrib import follow, replay, store

logger = logging.getLogger(__name__)

_GITHUB_REMOTE = re.compile(
    r"^(?:git@github\.com:|(?:https?|ssh)://(?:[^@/]+@)?(?:www\.)?github\.com/)"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+?)(?:\.git)?/?$", re.I)


class _Unknown(Exception):
    """GitHub could not answer the ownership question right now."""


def repo_of_url(url: str | None) -> str | None:
    """`owner/name` for any GitHub spelling `appfile_git.normalize_url`
    folds (https, ssh, `.git`, `www.`, trailing slash), case as written."""
    m = _GITHUB_REMOTE.match((url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _origin_url(root) -> str | None:
    r = gu._run(root, "config", "--get", "remote.origin.url")
    return gu._out(r) or None if gu._ok(r) else None


def _local_default_branch(root) -> str | None:
    """The remote's default branch from local refs only (the clone's
    `origin/HEAD`), else `main`/`master` when that tracking ref exists, else
    the checked-out branch — `share_app._origin_default_branch`'s rule plus a
    last resort, with no `remote set-head --auto` (that asks the network)."""
    r = gu._run(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    short = gu._out(r) if gu._ok(r) else ""
    if "/" in short:
        return short.split("/", 1)[1]
    for name in ("main", "master"):
        if replay.rev(root, f"refs/remotes/origin/{name}"):
            return name
    return gu._current_branch(root)


# ----------------------------------------------------------------- identity


def _gh():
    try:
        from fused_render.contrib import gh  # WS-B; absent → not signed in
    except ImportError:
        return None
    return gh


def _identity():
    gh = _gh()
    if gh is None:
        return None
    try:
        ident = gh.identity()
    except Exception:  # noqa: BLE001 — unreachable/signed out reads as unknown
        return None
    return ident if isinstance(ident, dict) and ident.get("login") else None


def _owns(owner: str, ident: dict | None) -> bool:
    """Whether the signed-in account owns repos under `owner`. Raises
    `_Unknown` when there is no identity or GitHub cannot say."""
    if not ident:
        raise _Unknown()
    if str(ident.get("login") or "").lower() == owner.lower():
        return True
    gh = _gh()
    if gh is None:
        raise _Unknown()
    try:
        role = gh.org_role(owner)
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "reason", None) in ("not-found", "forbidden"):
            return False  # not an org, or not one this account belongs to
        raise _Unknown() from exc
    return role == "admin"


def _classify(repo: str | None, ident) -> tuple[str, bool]:
    """`(kind, owner_checked)` for a folder whose origin names `repo`."""
    if not repo:
        return "own", True
    try:
        return ("own" if _owns(repo.split("/", 1)[0], ident) else "github"), True
    except _Unknown:
        return "github", False


# --------------------------------------------------------------- public API


def ensure(app_dir, *, identity=None) -> dict | None:
    """The record for `app_dir`, creating it once (§4). None when the folder
    is gone or the store cannot be written. May ask GitHub who the user is
    (`gh.identity`, cached by gh.py) — never call this under the slot."""
    if not os.path.isdir(app_dir):
        return None
    rec = store.get(app_dir)
    if rec is not None:
        if rec.get("kind") == "github" and not rec.get("owner_checked"):
            rec = _recheck(app_dir, rec, identity) or rec
        return rec

    root = follow.is_repo_root(app_dir)
    repo = repo_of_url(_origin_url(root)) if root else None
    if repo:
        # A Finder move the server never saw: the same repo's record at a
        # folder that no longer exists is THIS copy's (§3 self-healing).
        for k, orphan in store.find_by_repo(repo):
            if not os.path.isdir(k):
                adopted = store.adopt(k, app_dir)
                if adopted is not None:
                    logger.info("contrib: adopted the record of %s for %s", k, app_dir)
                    protect_fused_dir(app_dir, block=False)
                    return adopted
    kind, checked = _classify(repo, identity if identity is not None else _identity())
    rec = store.new_record(kind, repo=repo, branch=_local_default_branch(root) if root else None,
                           classified_by="migration", owner_checked=checked)
    if not store.create(app_dir, rec):
        return store.get(app_dir)
    protect_fused_dir(app_dir, block=False)
    return store.get(app_dir) or rec


def _recheck(app_dir, rec, identity) -> dict | None:
    """The one deferred ownership check (module docstring): only with an
    identity, only upgrading, then never again."""
    ident = identity if identity is not None else _identity()
    repo = rec.get("repo")
    if not ident or not repo:
        return None
    try:
        mine = _owns(repo.split("/", 1)[0], ident)
    except _Unknown:
        return None

    def fn(r):
        r["owner_checked"] = True
        if mine and r.get("kind") == "github":
            r["kind"] = "own"

    return store.update(app_dir, fn)


def record_install(app_dir, url) -> dict | None:
    """`appfile_git.install` placed or updated the clone of `url` at
    `app_dir`: classify it as an install (§4). An existing record for the
    same repo is kept as it is (an update install must not drop a proposal);
    any other record at this folder is replaced."""
    repo = repo_of_url(url)
    existing = store.get(app_dir)
    if existing is not None and repo and (existing.get("repo") or "").lower() == repo.lower():
        protect_fused_dir(app_dir)
        return existing
    from fused_render.contrib import flags

    root = follow.is_repo_root(app_dir)
    # With the loop off the record is written (it is invisible) but GitHub
    # is not asked anything during an install: ownership then stays
    # unchecked, and the first `ensure` with the loop on settles it.
    kind, checked = _classify(repo, _identity() if flags.sources() else None)
    rec = store.new_record(kind, repo=repo, branch=_local_default_branch(root) if root else None,
                           classified_by="install", owner_checked=checked)
    store.put(app_dir, rec)
    protect_fused_dir(app_dir)
    return store.get(app_dir) or rec


def protect_fused_dir(app_dir, *, block=True) -> int:
    """Skip-worktree every tracked `.fused/` path (§4, replay's module
    docstring), only while `flags.sources()` is on — the bit changes what
    `git status` reports, and with every flag off nothing may differ from
    today (§0.6). Takes the slot (an index write); `block=False` skips when
    it is busy — the engine re-protects after every move. Answers how many
    paths were newly protected."""
    from fused_render.contrib import flags

    try:
        if not flags.sources():
            return 0
        root = follow.is_repo_root(app_dir)
        if root is None:
            return 0
        with replay.slot(block=block) as held:
            if not held:
                return 0
            return replay.protect_locked(root)
    except Exception:  # noqa: BLE001 — housekeeping never fails its caller
        logger.exception("contrib: protect_fused_dir failed for %s", app_dir)
        return 0


# --------------------------------------------- background classification

_kick_lock = threading.Lock()
_kicking: set = set()


def ensure_in_background(app_dir) -> bool:
    """Run `ensure(app_dir)` on a daemon thread, at most one per folder at a
    time — for callers that hold the slot (the engine's `owns`) and so must
    not ask GitHub anything themselves. Answers whether a thread started."""
    k = store.key(app_dir)
    with _kick_lock:
        if k in _kicking:
            return False
        _kicking.add(k)

    def run():
        try:
            ensure(app_dir)
        except Exception:  # noqa: BLE001
            logger.exception("contrib: background classification failed for %s", app_dir)
        finally:
            with _kick_lock:
                _kicking.discard(k)

    try:
        threading.Thread(target=run, daemon=True, name="contrib-classify").start()
    except RuntimeError:
        with _kick_lock:
            _kicking.discard(k)
        return False
    return True
