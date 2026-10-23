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
store read plus the record's binding (store.py), no network, any
flags, memoised 30 s per folder. git_upstream asks it on every sync: a
`github` copy goes to the engine and never to the plain push. Flags only
decide whether a folder WITHOUT a record is classified at all (A1, as v1) —
a link install records at install whatever the flags.

THE GUARDS (`settle_locked`, every sync of a recorded copy, slot held):
- the v1/v2 → v3 move (§17.6), once per record: a `github` copy's work
  moves onto its own branch `fused/mine` (`HOME_BRANCH`, I4), its
  `refs/heads/<b>` becomes a mirror of the author's branch (I3), v1's
  private refs go, and the record is converted (`store.migrate_v3`) LAST,
  so a git step that failed is retried by the next sync;
- a `github` copy's home branch whenever it has none and HEAD is on the
  author's branch (a fresh `git clone`, an install that died half way —
  `ensure_home_locked`); an `own` copy left on `fused/mine` (it was a
  `github` copy until sign-in said it is the author's) goes back to its
  default branch, its work carried (`_unhome_locked`);
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

NOTHING HERE READS `.fused/`. The origin URL, the copy id and the
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
import secrets
import threading
import time

from fused_render import git_upstream as gu
from fused_render.contrib import follow, replay, store

logger = logging.getLogger(__name__)

KINDS = ("github", "own", "scratch")

def _load_guard():
    """`templates/shared/contrib_guard.py`, by path (templates/shared is not a
    package; the Git panel's ops.py/log.py load the same file) — the one copy
    of the guard's constants (§17.8)."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "shared", "contrib_guard.py")
    spec = importlib.util.spec_from_file_location("fused_render_contrib_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_guard = _load_guard()

#: A `github` copy's own branch (§17.1 I4): HEAD is always here; the
#: author's branch is a fetch-only mirror (I3). One value, the guard's.
HOME_BRANCH = _guard.HOME_BRANCH
HOME_REF = "refs/heads/" + HOME_BRANCH

#: The push guard's value (§16.0 P2): not a URL any git transport speaks, so
#: every push by remote name fails before it leaves the machine.
NO_PUSH_URL = _guard.NO_PUSH_URL

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


#: Every fused git call on a contrib repo (§16.12): no repo-configured
#: fsmonitor command and no hooks run — `.git/config` and `.git/hooks` are
#: writable by the app and by Claude sessions in the folder.
SAFE_ARGS = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")


def _g(root, *args, timeout=gu.TIMEOUT_S):
    return gu._run(root, *SAFE_ARGS, *args, timeout=timeout)


class _Unknown(Exception):
    """GitHub could not answer the ownership question right now."""


def repo_of_url(url: str | None) -> str | None:
    """`owner/name` for any GitHub spelling `appfile_git.normalize_url`
    folds (https, ssh, `.git`, `www.`, trailing slash), case as written."""
    m = _GITHUB_REMOTE.match((url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _origin_url(root) -> str | None:
    r = _g(root, "config", "--get", "remote.origin.url")
    return gu._out(r) or None if gu._ok(r) else None


def _local_default_branch(root) -> str | None:
    """The remote's default branch from local refs only (the clone's
    `origin/HEAD`), else `main`/`master` when that tracking ref exists, else
    the checked-out branch — `share_app._origin_default_branch`'s rule plus a
    last resort, with no `remote set-head --auto` (that asks the network)."""
    r = _g(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    short = gu._out(r) if gu._ok(r) else ""
    if "/" in short:
        return short.split("/", 1)[1]
    for name in ("main", "master"):
        if replay.rev(root, f"refs/remotes/origin/{name}"):
            return name
    cur = gu._current_branch(root)
    if cur == HOME_BRANCH:  # a v3 copy: its upstream names the author's branch
        ref = replay.mirror_ref(root)
        return ref[len("refs/heads/"):] if ref else None
    return cur


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


#: `gh.repo(...).permissions.push` per (login, repo): whether this account
#: may publish straight to the author's branch (a collaborator, §16.6). Asked
#: once per classification with an identity; only definite answers cached.
_push_cache: dict = {}


def _can_push(repo: str | None, ident) -> bool | None:
    """Whether the signed-in account has push rights on `repo` — the fact
    behind the page's "Update <who>'s app directly" button (`push_to_main`
    re-checks with GitHub before it pushes). None when unknown: no
    identity, or GitHub could not say."""
    if not repo or not ident or not ident.get("login"):
        return None
    gh = _gh()
    if gh is None:
        return None
    ck = (str(ident.get("login")).lower(), repo.lower())
    now = time.time()
    with _role_lock:
        hit = _push_cache.get(ck)
    if hit and now - hit[0] < ROLE_TTL_S:
        return hit[1]
    try:
        info = gh.repo(repo)
    except Exception as exc:  # noqa: BLE001 — unknown, asked again next time
        if getattr(exc, "reason", None) in ("not-found", "forbidden"):
            value = False
        else:
            return None
    else:
        perms = info.get("permissions") if isinstance(info, dict) else None
        value = bool(perms.get("push")) if isinstance(perms, dict) else None
    if value is not None:
        with _role_lock:
            _push_cache[ck] = (now, value)
    return value


def _with_push_rights(rec: dict, ident) -> dict:
    """`rec` with `can_push` noted for a checked `github` copy (§16.6): the
    one fact `actions.for_app` needs to offer a collaborator the direct
    publish. An own copy has no such button; the field stays None."""
    if rec.get("kind") == "github" and rec.get("owner_checked") and ident:
        rec["can_push"] = _can_push(rec.get("repo"), ident)
    return rec


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
            _g(root, "config", "--local", "user.name", name)
            _g(root, "config", "--local", "user.email", email)
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
    r = _g(root, "config", "--local", "--get", name)
    return (gu._out(r) or None) if gu._ok(r) else None


# ---------------------------------------------------------------- kind_of


def _kind_stamp(k: str):
    try:
        st = os.stat(store.path())
        s = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        s = None
    return (s, store._folder_stamp(k))


def kind_of(root, *, kick: bool = False, strict: bool = False) -> str | None:
    """`"github" | "own" | "scratch" | None` for the folder `root` (§16.1):
    the record's kind — an unchecked one counts as `github` — when the
    record is still bound to the repo there, else None. Store read +
    binding, no network, any flags. Never raises, unless `strict`
    (git_upstream's sync: an error deciding the kind must never read as "no
    record" there — that is the plain, pushing sync). `kick=True` also
    starts the background classification a folder without a record (A1 on,
    workspace app folders only, as v1) or with an unchecked owner needs."""
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
        if strict:
            raise
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
    if root is not None:
        marked = claim_marked(root)
        if marked is not None:
            _guard_soon(app_dir, identity)
            return store.get(app_dir) or marked
    repo = repo_of_url(_origin_url(root)) if root else None
    if repo and root:
        adopted = _adopt(app_dir, repo)
        if adopted is not None:
            _guard_soon(app_dir, identity)
            return store.get(app_dir) or adopted
    ident = identity if identity is not None else _identity()
    kind, checked = _classify(repo, ident)
    if not checked:
        # A pre-loop clone and no GitHub identity to ask (§16.12): NOT
        # classified yet. It may be the author's own app, auto-pushing as
        # ever; recording it `github` would silently stop that. No record =
        # today's plain sync; the next `ensure` with an identity decides.
        return None
    rec = store.new_record(kind, repo=repo, branch=_local_default_branch(root) if root else None,
                           classified_by="migration", owner_checked=checked)
    _with_push_rights(rec, ident)
    if not store.create(app_dir, rec):
        return store.get(app_dir)
    _forget_kind(app_dir)
    _guard_soon(app_dir, identity)
    return store.get(app_dir) or rec


COPY_ID_CONFIG = "fused.copyid"


def claim_marked(root) -> dict | None:
    """A folder with no record that fused marked as a GitHub-link copy
    (`fused.kind=github` in its own config) — a Finder DUPLICATE of a live
    copy, or one whose record was lost (audit R4). It must never get the
    plain, pushing sync: it gets its own `github` record, whatever the
    identity state (round 2) — a move's orphan adopted when its copy id
    matches, else a new record cloned from the live copy whose `fused.copyid`
    it carries (repo, branch, ownership; no proposal or activity), else one
    built from its origin, owner unchecked. The new record's own copy id is
    written by the next `settle_locked`. No network; answers the record, or
    None when nothing can be built (the caller then skips — never pushes)."""
    try:
        if _local_config(root, KIND_CONFIG) != "github" or store.get(root) is not None:
            return None
        k = store.key(root)
        cid = _local_config(root, COPY_ID_CONFIG)
        live = None
        if cid:
            for other, r in store.all().items():
                if other != k and isinstance(r, dict) and r.get("copy_id") == cid:
                    if os.path.isdir(other):
                        live = r
                        break
                    if r.get("repo"):
                        adopted = _adopt(root, r["repo"])
                        if adopted is not None:
                            return adopted
        repo = (live or {}).get("repo") or repo_of_url(_origin_url(root))
        if not repo:
            return None
        rec = store.new_record(
            "github", repo=repo,
            branch=(live or {}).get("branch") or _local_default_branch(root),
            classified_by="duplicate" if live else "marked",
            owner_checked=bool((live or {}).get("owner_checked")) and live.get("kind") == "github")
        if not store.create(root, rec):
            return store.get(root)
        _forget_kind(root)
        logger.info("contrib: %s carries a GitHub-link copy's marker; recorded it as one", root)
        return store.get(root) or rec
    except Exception:  # noqa: BLE001 — the caller skips (never pushes)
        logger.exception("contrib: claim_marked failed for %s", root)
        return None


def is_marked(root) -> bool:
    """Whether fused marked `root` as a GitHub-link copy (`fused.kind`)."""
    return _local_config(root, KIND_CONFIG) == "github"


def _stamp_copy(root, rec) -> None:
    """Write the record's `copy_id` into the repo's own config (a Finder move
    carries `.git` along; a fresh clone has none — `_adopt`), and remember
    the HEAD the copy is at (`last_head`). The caller holds the slot."""
    cid = rec.get("copy_id")
    if not cid:
        cid = secrets.token_hex(8)
        store.update(root, lambda r: r.__setitem__("copy_id", r.get("copy_id") or cid))
        cid = (store.get(root) or {}).get("copy_id") or cid
    if _local_config(root, COPY_ID_CONFIG) != cid:
        _g(root, "config", "--local", COPY_ID_CONFIG, cid)
    store.note_head(root, replay.rev(root, "HEAD"))


def _adopt(app_dir, repo) -> dict | None:
    """A Finder move the server never saw (§3 self-healing): an orphaned row
    for the same repo whose folder is gone is THIS copy's — and ONLY this
    copy's (audit V10/V12: a fresh clone into a new folder took a deleted
    copy's open proposal; of two copies of one repo the newest orphan won,
    the deleted sibling's). An orphan of the same repo qualifies when it
    either names the folder's own `fused.copyid` (written
    into `.git/config` by `_stamp_copy`, so it moves with the folder and a
    fresh clone has none), or — an orphan from before copy ids — the
    folder's HEAD is or contains its `last_head` (and its sent proposal, if
    any). Among those, `last_head == HEAD` first, then the newest. A v1
    orphan with neither a copy id nor a last HEAD is adopted only when it is
    the ONLY orphan of the repo and the folder carries no copy id."""
    orphans = [(k, r) for k, r in store.find_by_repo(repo) if not os.path.isdir(k)]
    root = follow.is_repo_root(app_dir)
    mine = _local_config(root, COPY_ID_CONFIG) if root else None
    head = replay.rev(app_dir, "HEAD")

    def contains(sha):
        return isinstance(sha, str) and bool(sha) and bool(head) and \
            replay.rev(app_dir, sha) is not None and replay.is_ancestor(app_dir, sha, head)

    def qualifies(r):
        if r.get("copy_id") or mine:
            return bool(mine) and r.get("copy_id") == mine
        p = r.get("proposal") if isinstance(r.get("proposal"), dict) else {}
        sent = p.get("sent") or p.get("pushed_head") or p.get("proposed_head")
        if sent and not contains(sent):
            return False
        if r.get("last_head"):
            return contains(r["last_head"])
        return len(orphans) == 1

    cands = [(k, r) for k, r in orphans if qualifies(r)]
    cands.sort(key=lambda kr: kr[1].get("last_head") != head)  # stable: newest first within
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

    upgrade = mine and rec.get("kind") == "github"
    root = follow.is_repo_root(app_dir) if upgrade else None
    # Work made while the copy was `github` was told it stays on this
    # computer: an upgrade never auto-publishes it (§16.12). It is HELD —
    # git_upstream skips the push — until the person presses Publish now.
    # Only with A1 on, where that button lives (round 2): with it off the
    # page never said anything, and their own repo auto-pushes as today.
    unpublished = _unpublished(root) if root is not None and _sources_on() else 0
    can_push = None if mine else _can_push(repo, ident)

    def fn(r):
        r["owner_checked"] = True
        if mine and r.get("kind") == "github":
            r["kind"] = "own"
            if unpublished:
                r["hold"] = {"why": "upgrade", "at": time.time(), "count": unpublished}
        elif r.get("kind") == "github" and can_push is not None:
            r["can_push"] = can_push

    out = store.update(app_dir, fn)
    _forget_kind(app_dir)
    if out and upgrade and unpublished:
        n = unpublished
        store.add_activity(app_dir, HELD, f"This is your app. {n} edit{'' if n == 1 else 's'} "
                                          f"{'is' if n == 1 else 'are'} ready to publish.")
    elif out and upgrade:
        # Every upgrade says so once: the page (or the git panel) had said
        # "your changes stay on this computer" until now.
        store.add_activity(app_dir, "yours", PUBLISHED_AS_USUAL)
    if out and upgrade and root is not None:
        try:
            with replay.slot(block=block) as held:
                if held:
                    ensure_push_guard(root, "own")
                    _mark_unchecked(root, False)
        except Exception:  # noqa: BLE001 — the next sync settles it
            logger.exception("contrib: unguarding %s failed", app_dir)
    return out


#: The activity kind of the upgrade hold line (§16.12); `release_hold`
#: clears the record's `hold`.
HELD = "held"
PUBLISHED_AS_USUAL = "This is your app — your edits will be published as usual."


def _unpublished(root) -> int:
    """Commits on HEAD no `origin` ref has (local refs only)."""
    r = _g(root, "rev-list", "--count", "HEAD", "--not", "--remotes=origin")
    out = gu._out(r) if gu._ok(r) else ""
    return int(out) if out.isdigit() else 0


def is_held(root) -> bool:
    """An own copy whose automatic syncs must not push (the record's `hold`,
    §16.12) — only ever set with A1 on, and read only then."""
    if not _sources_on():
        return False
    return bool((store.get(root) or {}).get("hold"))


def release_hold(root) -> bool:
    """Publish now was pressed: the held work may go (§16.12)."""
    if not (store.get(root) or {}).get("hold"):
        return False
    return store.update(root, lambda r: r.__setitem__("hold", None)) is not None


def settle_stale_hold(root) -> bool:
    """A1 was switched off while a copy was held: nothing shows the hold any
    more and the next push publishes the held edits — say so in one
    activity line and drop the hold (final gate). Answers whether it did."""
    if _sources_on() or not (store.get(root) or {}).get("hold"):
        return False
    store.add_activity(root, "yours", PUBLISHED_AS_USUAL)
    return release_hold(root)


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
                           classified_by="install", owner_checked=checked)
    _with_push_rights(rec, ident)
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
        r = _g(root, "config", "--get-all", "remote.origin.pushurl")
        have = gu._out(r).split("\n") if gu._ok(r) and gu._out(r) else []
        marker = _local_config(root, KIND_CONFIG)
        changed = False
        if kind == "github":
            if _origin_url(root) and have != [NO_PUSH_URL]:
                _g(root, "config", "--replace-all", "remote.origin.pushurl", NO_PUSH_URL)
                changed = True
            if marker != "github":
                _g(root, "config", "--local", KIND_CONFIG, "github")
                changed = True
            return _set_hook(root, True) or changed
        if NO_PUSH_URL in have:
            _g(root, "config", "--unset-all", "remote.origin.pushurl",
                    "^" + re.escape(NO_PUSH_URL) + "$")
            changed = True
        if marker is not None:
            _g(root, "config", "--local", "--unset-all", KIND_CONFIG)
            changed = True
        return _set_hook(root, False) or changed
    except Exception:  # noqa: BLE001
        logger.exception("contrib: push guard failed for %s", root)
        return False


#: Repo-local marker: a `github` copy whose owner fused could not ask about
#: yet — the git panel (PY-15, cannot read the store) words its Push
#: sentence as "until fused knows whether this app is yours".
UNCHECKED_CONFIG = "fused.ownerunknown"


def _mark_unchecked(root, unchecked: bool) -> None:
    have = _local_config(root, UNCHECKED_CONFIG) == "true"
    if unchecked and not have:
        _g(root, "config", "--local", UNCHECKED_CONFIG, "true")
    elif have and not unchecked:
        _g(root, "config", "--local", "--unset-all", UNCHECKED_CONFIG)


def is_push_guarded(root) -> bool:
    """Whether `root`'s origin carries the P2 push guard."""
    r = _g(root, "config", "--get-all", "remote.origin.pushurl")
    return gu._ok(r) and NO_PUSH_URL in gu._out(r).split("\n")


# The third lock (§16.12): the pushurl only covers pushes BY REMOTE NAME; a
# push by URL (what a Claude session tries next) goes around it. A pre-push
# hook in the repo's own hooks dir refuses every push, by name or URL. Fused's
# own pushes (gh.push, ownsync) run with `core.hooksPath=/dev/null`. A hook
# the person had is kept aside and put back when the copy becomes theirs.
HOOK_MARK = "# fused: no-push guard"
_HOOK = f"""#!/bin/sh
{HOOK_MARK} (docs/CONTRIB_LOOP.md §16.12) — removed when this becomes your own app.
echo "This is a copy of someone else's app: Fused keeps its changes on this computer, so they can't be sent from here." >&2
exit 1
"""


def _hooks_dir(root) -> str | None:
    r = _g(root, "rev-parse", "--git-common-dir")
    d = gu._out(r) if gu._ok(r) else ""
    if not d:
        return None
    return os.path.join(d if os.path.isabs(d) else os.path.join(root, d), "hooks")


def _set_hook(root, on: bool) -> bool:
    """Install (`on`) or remove fused's pre-push hook. Answers whether it
    changed anything."""
    hooks = _hooks_dir(root)
    if hooks is None:
        return False
    path = os.path.join(hooks, "pre-push")
    saved = path + ".before-fused"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            current = f.read()
    except OSError:
        current = None
    ours = current is not None and HOOK_MARK in current
    if on:
        if ours:
            return False
        os.makedirs(hooks, exist_ok=True)
        if current is not None and not os.path.exists(saved):
            os.replace(path, saved)
        tmp = path + ".fused-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_HOOK)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
        return True
    if not ours:
        return False
    if os.path.exists(saved):
        os.replace(saved, path)
    else:
        os.unlink(path)
    return True


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
    r = _g(root, "remote", "set-url" if url is not None else "add", "origin", want)
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
        rec = store.get(root)
        if rec is None:
            return None
        kind = kind or rec.get("kind")
        is_root = follow.is_repo_root(root) is not None
        if rec.get("v") != store.RECORD_V:
            moved = _migrate_v3_git_locked(root, rec, kind) if is_root else True
            if moved:
                rec = store.migrate_v3(root)[0] or rec
        if not is_root:
            return rec
        if kind in ("github", "own"):
            replay.protect_locked(root)
        if kind == "github":
            _point_back(root, rec)
            ensure_home_locked(root, rec.get("branch"))
        elif kind == "own":
            _unhome_locked(root, rec)
        ensure_push_guard(root, kind)
        _mark_unchecked(root, kind == "github" and not rec.get("owner_checked"))
        if kind in ("github", "own"):
            _stamp_copy(root, rec)
        return store.get(root) or rec
    except Exception:  # noqa: BLE001 — a guard failure never costs a sync
        logger.exception("contrib: settle failed for %s", root)
        return None


# --------------------------------------------------------- v3 home branch


def _valid_branch(branch) -> bool:
    return isinstance(branch, str) and bool(branch) and branch != HOME_BRANCH \
        and follow._valid("o/r", branch)


def _set_upstream(root, branch) -> None:
    """`fused/mine` tracks the author's branch (`origin/<b>`), so every git
    tool — the Git panel, a terminal's `git status` — reads "ahead/behind"
    against the author's version."""
    if _local_config(root, f"branch.{HOME_BRANCH}.remote") != "origin":
        _g(root, "config", "--local", f"branch.{HOME_BRANCH}.remote", "origin")
    if _local_config(root, f"branch.{HOME_BRANCH}.merge") != f"refs/heads/{branch}":
        _g(root, "config", "--local", f"branch.{HOME_BRANCH}.merge", f"refs/heads/{branch}")


def ensure_home_locked(root, branch) -> bool:
    """I4 for a `github` copy (§17.6): HEAD on `fused/mine`, tracking the
    author's `branch`. When the branch is missing and HEAD is on the
    author's branch (a fresh clone, an install or a move that died half
    way) it is created right there — `switch -c`, same commit: no file
    changes, uncommitted edits carried. When `fused/mine` exists and HEAD
    is elsewhere, nothing moves: that is the `other-branch` state (Go back).
    The caller holds the slot. Answers whether HEAD is on `fused/mine`."""
    if not _valid_branch(branch):
        return False
    cur = gu._current_branch(root)
    if cur == HOME_BRANCH:
        _set_upstream(root, branch)
        return True
    if cur != branch or replay.rev(root, HOME_REF) or gu._operation_in_flight(root):
        return False
    r = _g(root, "switch", "-q", "-c", HOME_BRANCH)
    if not gu._ok(r) or gu._current_branch(root) != HOME_BRANCH:
        logger.warning("contrib: could not create %s in %s: %s", HOME_BRANCH, root, gu._text(r))
        return False
    _set_upstream(root, branch)
    # The author's branch is no longer checked out: from here on it is the
    # mirror of origin's (I3). What it held is on fused/mine now.
    tip = replay.rev(root, f"refs/remotes/origin/{branch}")
    old = replay.rev(root, f"refs/heads/{branch}")
    if tip and old and tip != old:
        _g(root, "update-ref", f"refs/heads/{branch}", tip, old)
    return True


#: The Activity line of the v2 → v3 move, written only when the copy had
#: work of its own (§17.6, Q5). No git words (§0.1).
MOVED_TEXT = "Your changes now have a place of their own in this app. Nothing in your files changed."


def _migrate_v3_git_locked(root, rec, kind) -> bool:
    """The git side of the v1/v2 → v3 move (§17.6); slot held. A `github`
    copy on its author's branch (v2's only shape) gets `fused/mine` there
    and the branch becomes the mirror, its old tip pinned when it carried
    work of Bob's; a copy on some other version keeps HEAD where it is
    (`other-branch`, Go back) and gets `fused/mine` at the author's
    branch's tip, which is where v2 kept Bob's work. v1's private refs go
    for every kind. Answers whether the record may be stamped `v: 3` (False
    = try again next sync)."""
    for ref in store.V1_REFS:
        if replay.rev(root, ref):
            _g(root, "update-ref", "-d", ref)
    branch = rec.get("branch")
    if kind != "github" or not _valid_branch(branch):
        return True
    main_ref = f"refs/heads/{branch}"
    origin = replay.rev(root, f"refs/remotes/origin/{branch}")
    main = replay.rev(root, main_ref)
    cur = gu._current_branch(root)
    ahead = bool(main and origin and not replay.is_ancestor(root, main, origin))
    if cur == branch:
        if ahead:
            replay.backup_locked(root, main, why="v3-move")
        if not ensure_home_locked(root, branch):
            return False
    elif not replay.rev(root, HOME_REF) and main:
        # HEAD on some other version (or detached): Bob's v2 work is on the
        # author's branch — it becomes his branch; HEAD stays put.
        if not gu._ok(_g(root, "update-ref", HOME_REF, main, "")):
            return False
        _set_upstream(root, branch)
        if ahead:
            replay.backup_locked(root, main, why="v3-move")
        if origin and gu._current_branch(root) != branch:
            _g(root, "update-ref", main_ref, origin, main)
    if ahead:
        store.add_activity(root, "moved", MOVED_TEXT)
    return True


def _unhome_locked(root, rec) -> None:
    """An `own` copy still on `fused/mine` — it was a `github` copy until
    sign-in said it is the author's (`_recheck`), or an install fused put
    there before it knew: back to its default branch (today's plain sync
    and ownsync work there), every commit carried. The default branch
    moves to `fused/mine`'s tip when that has work it lacks (its old tip
    pinned if that drops anything), then it is checked out and `fused/mine`
    deleted. An uncommitted edit in the way: nothing moves; the next sync
    tries again. Slot held."""
    branch = rec.get("branch")
    if gu._current_branch(root) != HOME_BRANCH or not _valid_branch(branch):
        return
    if gu._operation_in_flight(root) is not None:
        return
    home = replay.rev(root, HOME_REF)
    main_ref = f"refs/heads/{branch}"
    main = replay.rev(root, main_ref)
    if not home:
        return
    if main is None:
        if not gu._ok(_g(root, "update-ref", main_ref, home, "")):
            return
    elif not replay.is_ancestor(root, home, main):
        if not replay.is_ancestor(root, main, home) and \
                not replay.backup_locked(root, main, why="own-move"):
            return
        if not gu._ok(_g(root, "update-ref", main_ref, home, main)):
            return
    target = replay.rev(root, main_ref)
    with replay.fused_aside(root, target):
        r = _g(root, "switch", "-q", branch)
    if not gu._ok(r) or gu._current_branch(root) != branch:
        logger.info("contrib: %s stays on %s for now: %s", root, HOME_BRANCH, gu._text(r))
        return
    _g(root, "branch", "-q", "-D", HOME_BRANCH)
    replay.protect_after_move(root)


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
