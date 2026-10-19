"""Own app vs GitHub-link copy (docs/CONTRIB_LOOP.md §4, §16.1) — the
ownership rule, applied once per copy and recorded outside the folder
(`store`), plus the guards that make the recorded kind true in git.

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
to someone else's main. `kind_of` answers `github` for an unchecked record
too (§16.1): an unknown owner never pushes.

KIND DECIDES GIT BEHAVIOUR, WHATEVER THE FLAGS (§16.0 P1). `kind_of` is a
store read plus the record's root-commit binding (store.py), no network, any
flags, memoised 30 s per folder. git_upstream asks it on every sync: a
`github` copy goes to the engine and never to the plain push. Flags only
decide whether a folder WITHOUT a record is classified at all (A1, as v1) —
a link install records at install whatever the flags.

THE GUARDS (`settle_locked`, every sync of a recorded copy, slot held):
- the v1 → v2 conversion of the record and the deletion of v1's private
  refs (§16.0 P3), once;
- skip-worktree on every tracked `.fused/` path for any `github`/`own`
  record — never undone (a repo that commits `.fused/` must not turn fused's
  own writes into edits; and every commit fused makes excludes `.fused` by
  pathspec as well, app_git/agent);
- a `github` copy's `origin` pointed back at the RECORD's repo when the
  folder's config disagrees (`.git/config` is app-writable; the record is
  ours — §5), with one activity line; an own copy's origin is its author's;
- the push guard (P2): `remote.origin.pushurl = no-push://fused` on a
  `github` copy, so nothing that pushes by remote name — a terminal, the git
  panel, a Claude session's `git push` — reaches the author. Removed when
  the kind becomes `own`. Explicit sends push by URL (`gh.push`).
Identity (`apply_identity`) needs GitHub's answer, so it runs on `ensure` /
`record_install` / sign-in, never under the slot.

NOTHING HERE READS `.fused/`. The origin URL, the root commit and the
repo-root test are git facts; the record is ours.

Who calls what: `record_install` from `appfile_git.install` (inline, the
install is already a slow request); `ensure` from the routers and from the
background classification (never under the mutation slot — it may ask
GitHub who the user is, and §0.9 keeps network calls off the slot);
`kind_of` + `settle_locked` from git_upstream's intercept (slot held).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

from fused_render import git_upstream as gu
from fused_render.contrib import follow, replay, store

logger = logging.getLogger(__name__)

KINDS = ("github", "own", "scratch")

#: The push guard's value (§16.0 P2): not a URL any git transport speaks, so
#: every push by remote name fails before it leaves the machine.
NO_PUSH_URL = "no-push://fused"

#: Repo-local marker of the recorded kind, for the one reader that cannot
#: import this module (claude_agent/agent.py's turn commit, D166): it picks
#: the commit identity and the Claude trailer. Never read to decide what a
#: copy follows or where it pushes — that is the record's alone.
KIND_CONFIG = "fused.kind"

_GITHUB_REMOTE = re.compile(
    r"^(?:git@github\.com:|(?:https?|ssh)://(?:[^@/]+@)?(?:www\.)?github\.com/)"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+?)(?:\.git)?/?$", re.I)


#: `gh.org_role` answers per (login, owner), for as long as gh.py trusts the
#: identity itself: classifying every app of one org must not ask GitHub
#: once per app. Only definite answers are cached — an unreachable GitHub is
#: asked again next time.
ROLE_TTL_S = 600.0
_role_lock = threading.Lock()
_role_cache: dict = {}

#: `kind_of`'s memo (§16.1): per folder, keyed also on the store file's and
#: the folder's stat, so a store write or a re-clone is seen at once.
KIND_TTL_S = 30.0
_kind_lock = threading.Lock()
_kind_memo: dict = {}


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


def _in_workspace(root) -> bool:
    """`root` is a workspace app folder, `<workspace>/<tag>/<name>`, by
    realpath (git_upstream's roots are realpath'd; the workspace setting may
    be a symlinked spelling)."""
    try:
        from fused_render.shell.seed import fused_dir

        rel = os.path.relpath(os.path.realpath(root), os.path.realpath(fused_dir()))
    except (ValueError, OSError):
        return False
    parts = rel.split(os.sep)
    return len(parts) == 2 and not any(p.startswith(".") for p in parts)


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
    ck = (str(ident.get("login") or "").lower(), owner.lower())
    now = time.time()
    with _role_lock:
        hit = _role_cache.get(ck)
    if hit and now - hit[0] < ROLE_TTL_S:
        return hit[1]
    try:
        mine = gh.org_role(owner) == "admin"
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "reason", None) not in ("not-found", "forbidden"):
            raise _Unknown() from exc  # unreachable: never cached
        mine = False  # not an org, or not one this account belongs to
    with _role_lock:
        _role_cache[ck] = (now, mine)
    return mine


def _classify(repo: str | None, ident) -> tuple[str, bool]:
    """`(kind, owner_checked)` for a folder whose origin names `repo`."""
    if not repo:
        return "own", True
    try:
        return ("own" if _owns(repo.split("/", 1)[0], ident) else "github"), True
    except _Unknown:
        return "github", False


def _commit_identity(ident: dict | None) -> tuple[str, str] | None:
    """`(name, email)` fused commits as for this GitHub account: its name
    (or login) and its noreply address. None when unknown."""
    if not isinstance(ident, dict) or not ident.get("login"):
        return None
    email = ident.get("email")
    if not isinstance(email, str) or "@" not in email:
        uid = ident.get("id")
        email = (f"{uid}+{ident['login']}@users.noreply.github.com" if uid
                 else f"{ident['login']}@users.noreply.github.com")
    name = ident.get("name") if isinstance(ident.get("name"), str) and ident.get("name").strip() \
        else ident["login"]
    return str(name).strip(), email


def apply_identity(app_dir, ident=None, *, block=False) -> bool:
    """Give a `github` copy the signed-in account's commit identity as
    repo-local `user.name`/`user.email` (§16.1), so app_git and Claude-turn
    commits there are the user's, not `Fused <apps@fused.io>`. No-op for any
    other kind or without an identity. Takes the slot (a config write in a
    real app repo, §0.9); `block=False` skips when busy — the next `ensure`
    redoes it. Answers whether it wrote."""
    try:
        rec = store.get(app_dir)
        root = follow.is_repo_root(app_dir)
        if not rec or rec.get("kind") != "github" or root is None:
            return False
        who = _commit_identity(ident if ident is not None else _identity())
        if who is None:
            return False
        name, email = who
        have = (_local_config(root, "user.name"), _local_config(root, "user.email"))
        if have == (name, email):
            return False
        with replay.slot(block=block) as held:
            if not held:
                return False
            gu._run(root, "config", "--local", "user.name", name)
            gu._run(root, "config", "--local", "user.email", email)
        return True
    except Exception:  # noqa: BLE001 — housekeeping never fails its caller
        logger.exception("contrib: apply_identity failed for %s", app_dir)
        return False


def apply_identity_everywhere(ident=None) -> int:
    """The user just signed in: `apply_identity` on every recorded `github`
    copy. Answers how many were written."""
    ident = ident if ident is not None else _identity()
    if not ident:
        return 0
    n = 0
    for k, rec in store.all().items():
        if isinstance(rec, dict) and rec.get("kind") == "github" and os.path.isdir(k):
            n += apply_identity(k, ident, block=True)
    return n


def _local_config(root, name) -> str | None:
    r = gu._run(root, "config", "--local", "--get", name)
    return (gu._out(r) or None) if gu._ok(r) else None


# ---------------------------------------------------------------- kind_of


def _kind_stamp(k: str):
    try:
        st = os.stat(store.path())
        s = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        s = None
    return (s, store._folder_stamp(k))


def kind_of(root, *, kick: bool = False) -> str | None:
    """`"github" | "own" | "scratch" | None` for the folder `root` (§16.1):
    the record's kind — an unchecked one counts as `github` — when the
    record is still bound to the repo there, else None. Store read +
    binding, no network, any flags; never raises. `kick=True` (git_upstream's
    sync) also starts the background classification a folder without a
    record (A1 on, workspace app folders only, as v1) or with an unchecked
    owner needs."""
    try:
        k = store.key(root)
        now = time.time()
        stamp = _kind_stamp(k)
        with _kind_lock:
            hit = _kind_memo.get(k)
        if hit and hit[1] == stamp and now - hit[0] < KIND_TTL_S and not kick:
            return hit[2]
        rec = store.get(root)
        kind = rec.get("kind") if rec else None
        kind = kind if kind in KINDS else None
        with _kind_lock:
            _kind_memo[k] = (now, _kind_stamp(k), kind)
        if kick:
            _kick(root, rec)
        return kind
    except Exception:  # noqa: BLE001 — unknown kind reads as "no record"
        logger.exception("contrib: kind_of failed for %s", root)
        return None


def _forget_kind(app_dir) -> None:
    try:
        k = store.key(app_dir)
    except Exception:  # noqa: BLE001
        return
    with _kind_lock:
        _kind_memo.pop(k, None)


def _kick(root, rec) -> None:
    """A folder with no record is classified only with A1 on (as v1); an
    unchecked owner is re-checked WHATEVER the flags — it decides whether
    the author's own copy pushes, which is correctness, not UI (TL ruling
    2026-10-19). Both on a background thread: `ensure` may ask GitHub."""
    from fused_render.contrib import flags

    if rec is None:
        if flags.sources() and _in_workspace(root):
            ensure_in_background(root)
    elif rec.get("kind") == "github" and not rec.get("owner_checked"):
        ensure_in_background(root)


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
        _guard_soon(app_dir, identity)
        return store.get(app_dir) or rec

    root = follow.is_repo_root(app_dir)
    repo = repo_of_url(_origin_url(root)) if root else None
    rc = store.root_commit_of(app_dir)
    if repo and rc:
        adopted = _adopt(app_dir, repo, rc)
        if adopted is not None:
            _guard_soon(app_dir, identity)
            return store.get(app_dir) or adopted
    kind, checked = _classify(repo, identity if identity is not None else _identity())
    rec = store.new_record(kind, repo=repo, branch=_local_default_branch(root) if root else None,
                           classified_by="migration", owner_checked=checked, root_commit=rc)
    if not store.create(app_dir, rec):
        return store.get(app_dir)
    _forget_kind(app_dir)
    _guard_soon(app_dir, identity)
    return store.get(app_dir) or rec


def _adopt(app_dir, repo, rc) -> dict | None:
    """A Finder move the server never saw (§3 self-healing): an orphaned row
    for the same repo whose folder is gone is THIS copy's — but only one with
    the SAME root commit (§16.1). Among several, the one whose sent proposal
    this repo has wins (it is the moved copy, not a deleted sibling), else
    the newest. A v1 orphan never got a root commit (its folder was gone
    before any v2 read): it is adopted only when it is the ONLY orphan of
    the repo — nothing to confuse it with."""
    orphans = [(k, r) for k, r in store.find_by_repo(repo) if not os.path.isdir(k)]
    cands = [(k, r) for k, r in orphans if r.get("root_commit") == rc]
    if not cands and len(orphans) == 1 and not orphans[0][1].get("root_commit"):
        cands = orphans

    def has_sent(r):
        p = r.get("proposal")
        sent = (p.get("sent") or p.get("pushed_head") or p.get("proposed_head")) \
            if isinstance(p, dict) else None
        return isinstance(sent, str) and bool(sent) and replay.rev(app_dir, sent) is not None

    cands.sort(key=lambda kr: not has_sent(kr[1]))  # stable: newest first within
    for k, _orphan in cands:
        adopted = store.adopt(k, app_dir)
        if adopted is not None:
            logger.info("contrib: adopted the record of %s for %s", k, app_dir)
            _forget_kind(app_dir)
            return adopted
    return None


def _recheck(app_dir, rec, identity, *, block=False) -> dict | None:
    """The one deferred ownership check (module docstring): only with an
    identity, only upgrading, then never again. An upgrade to `own` takes
    the push guard off (back to today's auto-push) — here when the slot is
    free (`block`), else at the next sync's `settle_locked`."""
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

    out = store.update(app_dir, fn)
    _forget_kind(app_dir)
    root = follow.is_repo_root(app_dir) if out and out.get("kind") == "own" else None
    if root is not None:
        try:
            with replay.slot(block=block) as held:
                if held:
                    ensure_push_guard(root, "own")
        except Exception:  # noqa: BLE001 — the next sync settles it
            logger.exception("contrib: unguarding %s failed", app_dir)
    return out


def after_sign_in(ident=None) -> dict:
    """The user just signed in to GitHub (github_login): re-check every
    unchecked `github` record once — whatever the flags, an author's own
    copy must get its auto-push back — then give the remaining `github`
    copies the account's commit identity. Asks GitHub (fresh, not the
    signed-out cache): never under the slot, never on a request thread.
    Answers `{rechecked, identity}` counts."""
    if ident is None:
        gh = _gh()
        try:
            ident = gh.identity(refresh=True) if gh is not None else None
        except Exception:  # noqa: BLE001 — unreachable: nothing to apply
            ident = None
    if not isinstance(ident, dict) or not ident.get("login"):
        return {"rechecked": 0, "identity": 0}
    rechecked = 0
    for k, rec in store.all().items():
        if (isinstance(rec, dict) and rec.get("kind") == "github"
                and not rec.get("owner_checked") and os.path.isdir(k)
                and store.get(k) is not None):
            rechecked += _recheck(k, rec, ident, block=True) is not None
    return {"rechecked": rechecked, "identity": apply_identity_everywhere(ident)}


def record_install(app_dir, url) -> dict | None:
    """`appfile_git.install` placed or updated the clone of `url` at
    `app_dir`: classify it as an install (§4). An existing record for the
    same repo is kept as it is (an update install must not drop a proposal —
    a fresh place dropped any record first, appfile_git); any other record at
    this folder is replaced. Written whatever the flags (§16.1 Migration)."""
    repo = repo_of_url(url)
    existing = store.get(app_dir)
    if existing is not None and repo and (existing.get("repo") or "").lower() == repo.lower():
        _guard(app_dir, None, block=True, lookup=_sources_on())
        return store.get(app_dir) or existing
    from fused_render.contrib import flags

    root = follow.is_repo_root(app_dir)
    # With the loop off the record is written (it is invisible) but GitHub
    # is not asked anything during an install: ownership then stays
    # unchecked (`github`: never pushes), and the first `ensure` with the
    # loop on settles it.
    ident = _identity() if flags.sources() else None
    kind, checked = _classify(repo, ident)
    rec = store.new_record(kind, repo=repo, branch=_local_default_branch(root) if root else None,
                           classified_by="install", owner_checked=checked,
                           root_commit=store.root_commit_of(app_dir))
    store.put(app_dir, rec)
    _forget_kind(app_dir)
    _guard(app_dir, ident, block=True, lookup=False)
    return store.get(app_dir) or rec


def protect_fused_dir(app_dir, *, block=True) -> int:
    """Skip-worktree every tracked `.fused/` path of a `github`/`own` copy
    (§16.1, replay's module docstring) — whatever the flags, and never
    undone. Takes the slot (an index write); `block=False` skips when it is
    busy — the next sync re-protects. Answers how many paths were newly
    protected."""
    try:
        rec = store.get(app_dir)
        if not rec or rec.get("kind") not in ("github", "own"):
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


# ----------------------------------------------------------------- guards


def ensure_push_guard(root, kind) -> bool:
    """P2's second lock (§16.1): a `github` copy gets `remote.origin.pushurl
    = no-push://fused` (and the `fused.kind` marker); any other kind loses
    exactly that value (a pushurl the author set themselves stays). Only
    when an `origin` remote exists — a config section with a pushurl and no
    url would read as a remote that is not there. The caller holds the slot
    (or is the install/remix that owns the folder). Answers whether it
    changed anything."""
    try:
        r = gu._run(root, "config", "--get-all", "remote.origin.pushurl")
        have = gu._out(r).split("\n") if gu._ok(r) and gu._out(r) else []
        marker = _local_config(root, KIND_CONFIG)
        changed = False
        if kind == "github":
            if _origin_url(root) and have != [NO_PUSH_URL]:
                gu._run(root, "config", "--replace-all", "remote.origin.pushurl", NO_PUSH_URL)
                changed = True
            if marker != "github":
                gu._run(root, "config", "--local", KIND_CONFIG, "github")
                changed = True
            return changed
        if NO_PUSH_URL in have:
            gu._run(root, "config", "--unset-all", "remote.origin.pushurl",
                    "^" + re.escape(NO_PUSH_URL) + "$")
            changed = True
        if marker is not None:
            gu._run(root, "config", "--local", "--unset-all", KIND_CONFIG)
            changed = True
        return changed
    except Exception:  # noqa: BLE001
        logger.exception("contrib: push guard failed for %s", root)
        return False


def is_push_guarded(root) -> bool:
    """Whether `root`'s origin carries the P2 push guard."""
    r = gu._run(root, "config", "--get-all", "remote.origin.pushurl")
    return gu._ok(r) and NO_PUSH_URL in gu._out(r).split("\n")


def _point_back(root, rec) -> bool:
    """A `github` copy whose `origin` no longer names the record's repo is
    pointed back at it (§16.1) — with one activity line. Answers whether it
    moved origin."""
    repo = rec.get("repo")
    if not isinstance(repo, str) or "/" not in repo:
        return False
    url = _origin_url(root)
    if url is not None and (repo_of_url(url) or "").lower() == repo.lower():
        return False
    want = follow.github_url(repo)
    r = gu._run(root, "remote", "set-url" if url is not None else "add", "origin", want)
    if not gu._ok(r):
        logger.warning("contrib: could not point %s back at %s: %s", root, repo, gu._text(r))
        return False
    logger.info("contrib: origin of %s was %r; pointed back at %s", root, url, want)
    store.add_activity(root, "repointed", f"Pointed this copy back at {repo}.")
    return True


def settle_locked(root, kind=None) -> dict | None:
    """Every guard a recorded copy needs before it syncs (module docstring
    "THE GUARDS"); the caller holds the slot. No network. Answers the record
    (None: no record). Never raises."""
    try:
        rec, migrated = store.migrate_v2(root)
        if rec is None:
            return None
        if migrated:
            for ref in store.V1_REFS:
                if replay.rev(root, ref):
                    gu._run(root, "update-ref", "-d", ref)
        kind = kind or rec.get("kind")
        if follow.is_repo_root(root) is None:
            return rec
        if kind in ("github", "own"):
            replay.protect_locked(root)
        if kind == "github":
            _point_back(root, rec)
        ensure_push_guard(root, kind)
        return store.get(root) or rec
    except Exception:  # noqa: BLE001 — a guard failure never costs a sync
        logger.exception("contrib: settle failed for %s", root)
        return None


def _sources_on() -> bool:
    from fused_render.contrib import flags

    return flags.sources()


def _guard(app_dir, ident, *, block, lookup=True) -> None:
    """`settle_locked` under the slot, then the identity — `ident`, else
    (with `lookup`) GitHub's answer, asked outside the slot — for
    `ensure`/`record_install`. An install with the loop off never asks."""
    root = follow.is_repo_root(app_dir)
    if root is None:
        return
    with replay.slot(block=block) as held:
        if held:
            settle_locked(root)
    if ident is not None or lookup:
        apply_identity(app_dir, ident, block=block)


_guard_lock = threading.Lock()
_guarded: dict = {}  # key -> last guard time
GUARD_TTL_S = 30.0


def _guard_soon(app_dir, ident) -> None:
    """`_guard` at most once per GUARD_TTL_S per folder, never waiting for
    the slot — `ensure` runs on page GETs; every sync settles anyway."""
    k = store.key(app_dir)
    now = time.time()
    with _guard_lock:
        if now - _guarded.get(k, 0.0) < GUARD_TTL_S:
            return
        _guarded[k] = now
    _guard(app_dir, ident, block=False)


# --------------------------------------------- background classification

_kick_lock = threading.Lock()
_kicking: set = set()


def ensure_in_background(app_dir) -> bool:
    """Run `ensure(app_dir)` on a daemon thread, at most one per folder at a
    time — for callers that hold the slot (git_upstream's sync, `kind_of`
    with `kick`) and so must not ask GitHub anything themselves. Answers
    whether a thread started."""
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
