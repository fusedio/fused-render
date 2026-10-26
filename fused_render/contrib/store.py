"""The contribution loop's record store (docs/CONTRIB_LOOP.md §3, §16.1, §17.6).

ONE FILE, OUTSIDE EVERY APP: `<home>/contrib/apps.json`,
`{"version": 2, "apps": {<key>: Record}}`. What a copy follows, who owns it
and the proposal it has open are facts about THIS MACHINE'S copy, decided by
fused — never by a file in the repo. The repo is the author's to write (a
committed `.fused/` included), and anything that read its own install source
out of the folder would let the app (or the author) re-point where Bob's
changes go. Nothing here reads the app folder beyond `os.path.isdir` and
one git question, the binding below.

BOUND TO THE REPO AT THE PATH (§17.6). A `github`/`own` record is about
the repository whose `origin` names the record's repo. `get` treats a
record as absent — and drops it — when the folder's origin names another
repo AND the folder does not carry the record's own copy id (`fused.copyid`,
written into `.git/config` by `classify._stamp_copy`): a different
repository now lives there (deleted and re-cloned from elsewhere, replaced,
a new app of the same name), and everything the record says about the old
one would be applied to a stranger. The copy id is what keeps a record whose
origin was merely RE-POINTED (`git remote set-url` by an app, a README, a
Claude session — `.git/config` is app-writable): that copy is still this
record's, and `classify._point_back` sets its origin back (audit O5); a
fresh clone carries no copy id. Only a definite answer drops: git not
running, or the folder no longer a repo at all, keeps the row. v1/v2's
root-commit binding is gone (v3). The answer is memoised per folder on the
folder's, its `.git`'s and its `.git/config`'s stat, so a re-clone or a
re-pointed origin is seen at once and an unchanged repo costs a stat.

KEYED BY FOLDER, realpath'd. Two copies of one repo on one machine are two
records (`find_by_repo` lists both), and every caller — git_upstream's repo
roots (already realpath'd), the routers' workspace spellings, the
move/remove hooks — lands on the same key whichever spelling it holds
(macOS's `/var` → `/private/var`, a symlinked `~/Fused`).

SELF-HEALING, NOT SELF-DELETING. `get` on a key whose folder is gone answers
None, but the row stays: a Finder move leaves the record at the old key until
`classify.ensure` at the new folder adopts it (same repo, old folder gone),
which is how a proposal survives a move nobody told the server about.
`app_state_move.rewrite_stores` (`move`) and `app_state_remove.prune_stores`
(`prune`) handle the moves and deletes the server does see.

One module-level lock around every read-modify-write; atomic writes via
`storage.write_json`. Every function swallows I/O errors (None/False/0): a
broken store must never break a render, an install or a sync — the engine
reads a missing record as "not mine" and today's behaviour runs.
"""
from __future__ import annotations

import copy
import logging
import os
import secrets
import threading
import time

from fused_render.shell import storage

logger = logging.getLogger(__name__)

VERSION = 2
#: The record schema stamp (§17.6): a record without it is a v1 record, one
#: with `v: 2` a v2 record; `migrate_v3` converts both (the git side of the
#: move is `classify.settle_locked`'s).
RECORD_V = 3
#: v2's stamp, which `migrate_v2` writes (the first step of `migrate_v3`).
_V2 = 2

#: Newest last; the page says each once. Ten is far more than one copy
#: accumulates between two visits.
EVENTS_KEEP = 10
#: Activity lines kept (§16.9) — newest last on disk, newest first served.
#: A backup ref only a dropped line named is the engine's to prune later.
ACTIVITY_KEEP = 50

EVENT_KINDS = frozenset({
    "updated", "updated-kept", "merged-back", "declined", "merge-accepted",
    "discarded", "remixed", "original-brought-in",
})

#: The kinds the app page draws as a one-time banner (`engine.status`'s
#: `event`). Every other kind is a user action the page already confirmed,
#: or a state the card draws itself: stored for the record, but born seen,
#: so it can never hide a newer banner-worthy one.
RENDERED_EVENTS = frozenset({"updated-kept", "merged-back"})

#: An orphaned row (its folder gone and no copy adopted it) is dropped once
#: this long has passed since a sweep first found it so.
ORPHAN_TTL_S = 30 * 24 * 3600

#: The record fields `adopt` carries to a new folder.
ADOPT_KEEP = ("kind", "repo", "branch", "remix_of", "proposal", "classified_by",
              "classified_at", "owner_checked", "copy_id", "last_head",
              "seen_tips", "hold")

#: How long a binding answer is trusted for an unchanged folder (its memo
#: key already changes on a re-clone; this only bounds a stale object DB).
BIND_TTL_S = 30.0

_lock = threading.Lock()


def path() -> str:
    return os.path.join(storage.home_dir(), "contrib", "apps.json")


def key(app_dir: str) -> str:
    """The record key for `app_dir`: absolute, realpath'd, no trailing slash
    (`app_state_remove.canonical` of the realpath)."""
    from fused_render.app_state_remove import canonical

    return canonical(os.path.realpath(os.path.abspath(app_dir)))


def _is_under(k: str, root_key: str) -> bool:
    return k == root_key or k.startswith(root_key.rstrip("/") + "/")


def _read() -> dict:
    data = storage.read_json(path())
    if not isinstance(data, dict) or not isinstance(data.get("apps"), dict):
        return {"version": VERSION, "apps": {}}
    return data


def _write(data: dict) -> bool:
    try:
        storage.write_json(path(), data)
        return True
    except Exception:  # noqa: BLE001 — never raise on I/O (module docstring)
        logger.exception("contrib store: could not write %s", path())
        return False


def new_record(kind: str, *, repo: str | None = None, branch: str | None = None,
               classified_by: str = "migration", owner_checked: bool = True) -> dict:
    """A fresh Record with every §3/§17.6 field present (callers never have
    to `.get` a field into existence). v2's `root_commit` (v3 binds by
    origin or copy id, §17.6) and `needs_merge` are not written
    (`_view` still shows it as None for one release). `skip_tip` is the
    undo hold (engine: an Undo of an automatic move holds that tip)."""
    return {
        "v": RECORD_V,
        "kind": kind,
        "repo": repo,
        "branch": branch,
        "classified_by": classified_by,
        "classified_at": time.time(),
        "owner_checked": owner_checked,
        "remix_of": None,
        "proposal": None,
        "events": [],
        "original_update": None,
        "activity": [],
        "skip_tip": None,
        "own_state": None,
        # A `github` copy whose account may publish straight to the author's
        # branch (a collaborator, §16.6): True/False once GitHub said, else None.
        "can_push": None,
        # A held own copy (§16.12): automatic syncs never push it until
        # Publish now (`classify.is_held` / `release_hold`, ownsync).
        "hold": None,
        # ownsync's memory of GitHub tips it saw (newest last, capped by it).
        "seen_tips": [],
        # Adoption after a Finder move (`classify._adopt`): the HEAD this
        # copy was last seen at, and the id fused wrote into its own
        # `.git/config` (`fused.copyid`) — a fresh clone has neither.
        "last_head": None,
        "copy_id": secrets.token_hex(8),
    }


def _view(rec: dict) -> dict:
    """What a reader sees: `rec` (a copy) with every field present — v2's
    `root_commit` and `needs_merge` as None when the row has none (old
    readers, one release), `skip_tip` (the undo hold) as None — and an
    older proposal's `returned_at` read as `landed_at`
    (§17.5): no reader has to know which schema wrote the row. Nothing
    persists here; `migrate_v3` does that."""
    for name, default in (("root_commit", None), ("needs_merge", None), ("activity", []),
                          ("skip_tip", None), ("own_state", None), ("hold", None), ("seen_tips", []),
                          ("last_head", None), ("copy_id", None)):
        rec.setdefault(name, default)
    p = rec.get("proposal")
    if isinstance(p, dict) and "landed_at" not in p:
        p["landed_at"] = p.get("returned_at")
    return rec


#: Fields `_view` shows as None for old readers that v3 does not store
#: (§17.6): written back only when something set them.
_VIEW_ONLY = ("root_commit", "needs_merge")


def _stored(rec: dict) -> dict:
    """`rec` as persisted: `_VIEW_ONLY` fields left out while None."""
    for name in _VIEW_ONLY:
        if name in rec and rec[name] is None:
            del rec[name]
    return rec


# --------------------------------------------------------------------- binding

_bind_lock = threading.Lock()
_bind_memo: dict = {}  # key -> (stamp, (repo, copy_id), at, bound: bool | None)


def _git(path, *args, timeout=10.0):
    """One bounded local git call (git_upstream's discipline), with lazy
    fetching off: an app installed from a link is a `--filter=blob:none`
    clone, where asking about a missing object would otherwise go to the
    network from inside `get`. (Older git ignores the variable; a root
    commit is never filtered out, so its fetch only ever confirms
    "missing".)"""
    import subprocess

    from fused_render import git_upstream as gu

    try:
        p = subprocess.run([gu._git_bin(), "-C", path, "-c", "core.fsmonitor=",
                            "-c", "core.hooksPath=/dev/null", *args], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout,
                           env={**os.environ, **gu._ENV, "GIT_NO_LAZY_FETCH": "1"},
                           stdin=subprocess.DEVNULL, close_fds=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return p.returncode, p.stdout, p.stderr


def _roots(path: str) -> list | None:
    """HEAD's root commits in the repo at `path`, sorted; None when git
    cannot say (not a repo, no commits, git unavailable)."""
    from fused_render import git_upstream as gu

    r = _git(path, "rev-list", "--max-parents=0", "HEAD")
    if not gu._ok(r):
        return None
    return sorted(ln for ln in gu._out(r).split() if len(ln) >= 40) or None


def _folder_stamp(k: str):
    """What changes when a different repo moves in at `k`: the folder's own
    inode and mtime, and its `.git`'s (dir, or a worktree's file) inode.
    Not `.git`'s mtime: every git write (index.lock) bumps it. The folder's
    mtime is there because inodes are REUSED: on ext4 a folder deleted and
    cloned again within seconds gets the same inode numbers back, and the
    memo would then vouch for the stranger's repo (a replaced folder kept
    the old record for BIND_TTL_S). The top folder's mtime moves only when
    an entry is added or removed at its top level — rare in use, and a miss
    costs one local walk."""
    try:
        st = os.stat(k)
    except OSError:
        return None
    try:
        return (st.st_ino, st.st_mtime_ns, os.stat(os.path.join(k, ".git")).st_ino)
    except OSError:
        return (st.st_ino, st.st_mtime_ns, None)


def _config_stamp(k: str):
    """`.git/config`'s stat (a re-pointed origin, a copy id written)."""
    try:
        st = os.stat(os.path.join(k, ".git", "config"))
        return (st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _bound(k: str, rec: dict) -> bool | None:
    """True when the repo at `k` is still `rec`'s (module docstring "BOUND
    TO THE REPO AT THE PATH"): its origin names the record's repo, or its
    `fused.copyid` is the record's. False when neither holds; None when git
    cannot say (not a repo, git missing, timed out). Memoised."""
    from fused_render import git_upstream as gu
    from fused_render.contrib import classify

    want = ((rec.get("repo") or "").lower(), rec.get("copy_id"))
    stamp = (_folder_stamp(k), _config_stamp(k))
    now = time.time()
    with _bind_lock:
        hit = _bind_memo.get(k)
    if hit and hit[0] == stamp and hit[1] == want and now - hit[2] < BIND_TTL_S:
        return hit[3]
    # Only a folder heading its own repo is judged: one nested in another
    # repo (an app in the shared `local` repo) would read the parent's.
    r = (_git(k, "config", "--local", "--get-regexp", r"^(remote\.origin\.url|fused\.copyid)$")
         if os.path.lexists(os.path.join(k, ".git")) else None)
    if r is None or r[0] not in (0, 1):
        ans = None  # not a repo (any more), or git could not run
    else:
        have = {}
        for line in (gu._out(r) if r[0] == 0 else "").splitlines():
            name, _, value = line.partition(" ")
            have.setdefault(name.lower(), value.strip())
        origin = (classify.repo_of_url(have.get("remote.origin.url")) or "").lower()
        ans = origin == want[0] or (bool(want[1]) and have.get("fused.copyid") == want[1])
    with _bind_lock:
        _bind_memo[k] = (stamp, want, now, ans)
    return ans


def _root_present(k: str, root_commit: str) -> bool | None:
    """v2's binding, for an old row only: whether `root_commit` is in the
    repo at `k` (None: git cannot say)."""
    if root_commit in (_roots(k) or ()):
        return True
    r = _git(k, "cat-file", "-e", root_commit)
    return None if r is None else True if r[0] == 0 else False if r[0] == 1 else None


def _forget_binding(k: str) -> None:
    with _bind_lock:
        _bind_memo.pop(k, None)


def _bind(k: str, rec: dict) -> dict | None:
    """`rec` when it is still about the repo at `k`; None, after dropping
    the row, when it is not (module docstring). Only `github`/`own` rows are
    judged — a Try copy's origin is fused's own scratch remote. Git runs
    OUTSIDE the store lock; the drop is a compare-and-set against the row as
    read."""
    if rec.get("kind") not in ("github", "own"):
        return rec
    if _bound(k, rec) is not False:
        return rec
    if rec.get("kind") == "github" and not rec.get("copy_id") and rec.get("v") != RECORD_V:
        # A v1/v2 row that never got a copy id, over a re-pointed origin
        # (review P2 #19): v2's root-commit binding decides — a definite
        # stranger is dropped; otherwise the row is kept and
        # `classify._point_back` sets origin back.
        rc = rec.get("root_commit")
        if not rc or _root_present(k, rc) is not False:
            return rec
    with _lock:
        data = _read()
        cur = data["apps"].get(k)
        if isinstance(cur, dict) and cur.get("repo") == rec.get("repo") \
                and cur.get("copy_id") == rec.get("copy_id"):
            del data["apps"][k]
            _write(data)
    logger.info("contrib store: %s no longer holds %s; dropped its record", k, rec.get("repo"))
    _forget_binding(k)
    return None


# ----------------------------------------------------------------------- reads


def get(app_dir: str) -> dict | None:
    """The record for `app_dir`, or None — none recorded, the store
    unreadable, the folder no longer exists (the row is kept for adoption;
    module docstring), or a different repository lives there now (the row
    is dropped: "BOUND TO A ROOT COMMIT"). A copy: mutating it changes
    nothing."""
    try:
        k = key(app_dir)
        if not os.path.isdir(k):
            return None
        with _lock:
            rec = _read()["apps"].get(k)
        if not isinstance(rec, dict):
            return None
        rec = _bind(k, copy.deepcopy(rec))
        return _view(rec) if rec is not None else None
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: get failed for %s", app_dir)
        return None


def get_raw(k: str) -> dict | None:
    """The record stored under the exact key `k`, folder present or not —
    for adoption (`classify.ensure`), which needs the orphaned row `get`
    hides."""
    try:
        with _lock:
            rec = _read()["apps"].get(k)
        return copy.deepcopy(rec) if isinstance(rec, dict) else None
    except Exception:  # noqa: BLE001
        return None


def all() -> dict:  # noqa: A001 — the contract's name
    """Every record, `{key: Record}`, including orphans. A copy."""
    try:
        with _lock:
            return copy.deepcopy(_read()["apps"])
    except Exception:  # noqa: BLE001
        return {}


def find_by_repo(repo: str) -> list:
    """`[(key, Record)]` for every record naming `repo` (case-insensitive —
    GitHub's own rule), folder present or not; newest classification first."""
    want = (repo or "").lower()
    if not want:
        return []
    out = [(k, r) for k, r in all().items()
           if isinstance(r, dict) and isinstance(r.get("repo"), str)
           and r["repo"].lower() == want]
    out.sort(key=lambda kr: kr[1].get("classified_at") or 0, reverse=True)
    return out


# ---------------------------------------------------------------------- writes


def put(app_dir: str, record: dict) -> bool:
    try:
        k = key(app_dir)
        _forget_binding(k)
        with _lock:
            data = _read()
            data["apps"][k] = copy.deepcopy(record)
            return _write(data)
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: put failed for %s", app_dir)
        return False


def create(app_dir: str, record: dict) -> bool:
    """`put` only when `app_dir` has no record yet — two classifications
    racing (a router GET and the engine's background one) keep the first.
    False when one existed (or the write failed)."""
    try:
        k = key(app_dir)
        _forget_binding(k)
        with _lock:
            data = _read()
            if isinstance(data["apps"].get(k), dict):
                return False
            data["apps"][k] = copy.deepcopy(record)
            return _write(data)
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: create failed for %s", app_dir)
        return False


def update(app_dir: str, fn) -> dict | None:
    """Apply `fn(record)` (mutating a copy in place) under the lock and
    persist; answers the new record, or None when there is no record (or the
    write failed). `fn` must not do I/O — it runs under the store lock."""
    try:
        k = key(app_dir)
        with _lock:
            data = _read()
            rec = data["apps"].get(k)
            if not isinstance(rec, dict):
                return None
            rec = _view(copy.deepcopy(rec))  # `fn` sees the full shape, like `get`
            fn(rec)
            data["apps"][k] = _stored(copy.deepcopy(rec))
            if not _write(data):
                return None
            return _view(copy.deepcopy(rec))
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: update failed for %s", app_dir)
        return None


def drop(app_dir: str) -> bool:
    try:
        k = key(app_dir)
        _forget_binding(k)
        with _lock:
            data = _read()
            if data["apps"].pop(k, None) is None:
                return False
            return _write(data)
    except Exception:  # noqa: BLE001
        return False


def adopt(old_key: str, app_dir: str) -> dict | None:
    """Re-key the orphaned row at `old_key` onto `app_dir` (a Finder move the
    server never saw — or a fresh clone of the same repo), atomically:
    answers the adopted record, or None when `old_key` vanished meanwhile or
    `app_dir` already has a record. Only what is about the COPY'S SOURCE
    travels (`ADOPT_KEEP`: what it follows, its classification, a proposal,
    a remix's original); everything about the old folder's git state —
    an overlap, backup names, the skip-worktree marker, banners — starts
    fresh, since it may name commits and refs this folder never had."""
    try:
        new_key = key(app_dir)
        with _lock:
            data = _read()
            old = data["apps"].get(old_key)
            if not isinstance(old, dict) or new_key in data["apps"]:
                return None
            del data["apps"][old_key]
            rec = new_record(old.get("kind") or "own")
            rec.update({k: old[k] for k in ADOPT_KEEP if k in old})
            if isinstance(rec.get("proposal"), dict):  # persisted v3-shaped
                _v3_proposal(rec["proposal"])
            data["apps"][new_key] = rec
            if not _write(data):
                return None
            return _view(copy.deepcopy(rec))
    except Exception:  # noqa: BLE001
        return None


def move(old_root: str, new_root: str) -> int:
    """Re-key every record under `old_root` onto `new_root` (an app folder,
    or a folder holding several, moved — `app_state_move.rewrite_stores`).

    Two guards beyond the prefix remap, because the move hook is driven by
    `.fused/meta.json` — a file a repo can commit (§0.5): a row whose
    destination already holds a record is left where it is (an install that
    just classified the folder must not be overwritten by a remap a committed
    `meta.json` named), and nothing moves while `old_root` still exists (a
    copy, not a move — `app_fused_dir._after_move` makes the same test).
    Answers how many rows moved. Idempotent."""
    try:
        old_k, new_k = key(old_root), key(new_root)
        if old_k == new_k or os.path.exists(old_k):
            return 0
        moved = 0
        with _lock:
            data = _read()
            apps = data["apps"]
            for k in [k for k in apps if _is_under(k, old_k)]:
                dest = new_k + k[len(old_k):]
                if dest in apps:
                    continue
                apps[dest] = apps.pop(k)
                moved += 1
            if moved and not _write(data):
                return 0
        return moved
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: move %s -> %s failed", old_root, new_root)
        return 0


def prune(root: str) -> int:
    """Drop every record under `root` (the app was removed). Answers how many."""
    try:
        root_k = key(root)
        with _lock:
            data = _read()
            gone = [k for k in data["apps"] if _is_under(k, root_k)]
            for k in gone:
                del data["apps"][k]
            if gone and not _write(data):
                return 0
        return len(gone)
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: prune failed for %s", root)
        return 0


def sweep_orphans(now: float | None = None) -> int:
    """Housekeeping (the engine's hourly pass): stamp `orphaned_at` on a row
    whose folder is gone, clear it on one whose folder is back, and drop a
    row orphaned for longer than ORPHAN_TTL_S — a copy deleted outside the
    server, or moved somewhere `classify.ensure` never ran. Answers how many
    rows were dropped."""
    now = time.time() if now is None else now
    try:
        with _lock:
            data = _read()
            apps = data["apps"]
            changed = dropped = 0
            for k in list(apps):
                rec = apps[k]
                if not isinstance(rec, dict):
                    continue
                if os.path.isdir(k):
                    if rec.pop("orphaned_at", None) is not None:
                        changed += 1
                elif not rec.get("orphaned_at"):
                    rec["orphaned_at"] = now
                    changed += 1
                elif now - rec["orphaned_at"] > ORPHAN_TTL_S:
                    del apps[k]
                    dropped += 1
            if (changed or dropped) and not _write(data):
                return 0
        return dropped
    except Exception:  # noqa: BLE001
        logger.exception("contrib store: orphan sweep failed")
        return 0


# ------------------------------------------------------------ events / backups


def add_event(app_dir: str, kind: str, who: str | None = None) -> dict | None:
    """Append one Event (newest last, capped at EVENTS_KEEP). `seen` starts
    False; `ack(app_dir, "event")` marks every event seen."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown contrib event kind {kind!r}")
    event = {"kind": kind, "at": time.time(), "who": who,
             "seen": kind not in RENDERED_EVENTS}

    def fn(rec):
        events = rec.get("events") if isinstance(rec.get("events"), list) else []
        events.append(event)
        rec["events"] = events[-EVENTS_KEEP:]

    return update(app_dir, fn)


def ack(app_dir: str, kind: str) -> dict | None:
    """The page acknowledged a banner: `"event"` marks every event seen;
    `"settled"` marks a merged/declined proposal's banner seen
    (`proposal.settled_seen`)."""
    def fn(rec):
        if kind == "event":
            for e in rec.get("events") or []:
                if isinstance(e, dict):
                    e["seen"] = True
        elif kind == "settled":
            if isinstance(rec.get("proposal"), dict):
                rec["proposal"]["settled_seen"] = True
        else:
            raise ValueError(f"unknown ack kind {kind!r}")

    return update(app_dir, fn)


# -------------------------------------------------------------------- activity


def note_head(app_dir: str, head: str | None) -> None:
    """Remember the HEAD this copy was last seen at (`last_head`, for
    adoption after a Finder move). Writes only when it changed."""
    if not head:
        return
    rec = get(app_dir)
    if rec is None or rec.get("last_head") == head:
        return
    update(app_dir, lambda r: r.__setitem__("last_head", head))


def add_activity(app_dir: str, kind: str, text: str, *, backup=None, aside=None,
                 ref=None) -> str | None:
    """Append one Activity line (§16.9) — a plain-words sentence (§0.1) for
    something fused did to this copy — and answer its id (None when there is
    no record, or the write failed). `backup` (a backup ref, or a list of
    them: a tip and its `-wip`) and/or `aside` (where moved-aside files
    went) make the line undoable, and a ref a line names is never pruned
    (`referenced_backups`). `ref`: a pinned ref the line names WITHOUT
    offering Undo (the mirror's old tip after an author rewind — restoring
    it would put the author's old version over Bob's own; review P1 #3);
    never pruned either. Capped at ACTIVITY_KEEP."""
    entry = new_activity(kind, text, backup=backup, aside=aside, ref=ref)
    return entry["id"] if update(app_dir, lambda rec: append_activity(rec, entry)) is not None \
        else None


def new_activity(kind: str, text: str, *, backup=None, aside=None, ref=None) -> dict:
    """One Activity line, not stored yet — for a caller that must write it
    in the SAME `update` as another field (the engine's `landed_at`
    once-stamp, §17.2): `update(app_dir, fn)` with `append_activity(rec,
    line)` inside `fn`."""
    return {"id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}",
            "at": time.time(), "kind": str(kind), "text": str(text),
            "backup": backup, "aside": aside, "ref": ref, "restored_at": None}


def append_activity(rec: dict, entry: dict) -> None:
    """Append `entry` (`new_activity`) to `rec` in place, capped at
    ACTIVITY_KEEP. For use inside an `update` function."""
    lines = rec.get("activity") if isinstance(rec.get("activity"), list) else []
    lines.append(entry)
    rec["activity"] = lines[-ACTIVITY_KEEP:]


def _undoable(e: dict) -> bool:
    return bool(e.get("backup") or e.get("aside")) and not e.get("restored_at")


def activity(app_dir: str) -> list:
    """`[{id, at, kind, text, undo}]`, newest first (§16.9). `undo`: the line
    names a backup or set-aside files and was not restored already."""
    rec = get(app_dir)
    lines = rec.get("activity") if rec and isinstance(rec.get("activity"), list) else []
    return [{"id": e.get("id"), "at": e.get("at"), "kind": e.get("kind"),
             "text": e.get("text"), "undo": _undoable(e)}
            for e in reversed(lines) if isinstance(e, dict)]


def activity_entry(app_dir: str, activity_id: str) -> dict | None:
    """The whole stored line `activity_id` (`backup`, `aside`,
    `restored_at` included) — what `engine.restore` acts on. A copy."""
    rec = get(app_dir)
    for e in (rec or {}).get("activity") or []:
        if isinstance(e, dict) and e.get("id") == activity_id:
            return copy.deepcopy(e)
    return None


def mark_restored(app_dir: str, activity_id: str) -> bool:
    """Line `activity_id` was undone: it stays (it is history), without Undo."""
    hit = []

    def fn(rec):
        for e in rec.get("activity") or []:
            if isinstance(e, dict) and e.get("id") == activity_id:
                e["restored_at"] = time.time()
                hit.append(True)

    return update(app_dir, fn) is not None and bool(hit)


def referenced_backups(app_dir: str) -> set:
    """Every backup ref an activity line still names — what the engine's
    backup pruning must never delete."""
    rec = get(app_dir)
    refs: set = set()
    for e in (rec or {}).get("activity") or []:
        if not isinstance(e, dict):
            continue
        for b in (e.get("backup"), e.get("ref")):
            for r in (b if isinstance(b, list) else [b]):
                if isinstance(r, str) and r:
                    refs.add(r)
    return refs


# ------------------------------------------------------------------ v1 -> v2


#: The private refs v1 kept beside the branch: deleted on a repo's first v2
#: sync (§16.0 P3, `classify.settle_locked`) and never written again.
V1_REFS = ("refs/fused/follow", "refs/fused/base", "refs/fused/proposed",
           "refs/fused/origin-seen")


def migrate_v2(app_dir: str) -> tuple[dict | None, bool]:
    """Convert a v1 record in place (§16.0 P3, §16.1): clear `needs_merge`
    (it named v1's rebase state), turn each `backups` entry into an activity
    line naming the ref, map the proposal's `pushed_head or proposed_head`
    to `sent` (+ `checked_tip`), drop the v1-only fields, stamp `v: 2`.
    Answers `(record, migrated)` — `migrated` is False for a record already
    v2 or newer (or no record). The caller deletes V1_REFS when it is True.
    A newer record costs one read, no write. The first step of `migrate_v3`."""
    cur = get(app_dir)
    if cur is None or cur.get("v") in (_V2, RECORD_V):
        return cur, False
    did = []

    def fn(rec):
        if rec.get("v") in (_V2, RECORD_V):
            return
        did.append(True)
        rec["needs_merge"] = None
        lines = rec.get("activity") if isinstance(rec.get("activity"), list) else []
        for b in rec.pop("backups", None) or []:
            if isinstance(b, dict) and isinstance(b.get("ref"), str):
                at = b.get("at") if isinstance(b.get("at"), (int, float)) else time.time()
                lines.append({"id": f"{int(at * 1000)}-{secrets.token_hex(3)}",
                              "at": at, "kind": "backup",
                              "text": "Saved a copy of your work before an update.",
                              "backup": b["ref"], "aside": None, "restored_at": None})
        lines.sort(key=lambda e: e.get("at") or 0)
        rec["activity"] = lines[-ACTIVITY_KEEP:]
        p = rec.get("proposal")
        if isinstance(p, dict):
            if "sent" not in p:
                p["sent"] = p.get("pushed_head") or p.get("proposed_head")
            p.setdefault("checked_tip", None)
            p.pop("pushed_head", None)
            p.pop("proposed_head", None)
        rec.pop("protected", None)
        for name, default in (("root_commit", None), ("skip_tip", None), ("own_state", None)):
            rec.setdefault(name, default)
        rec["v"] = _V2

    rec = update(app_dir, fn)
    return (_view(rec) if rec is not None else None), bool(did)


# ------------------------------------------------------------------ v2 -> v3

#: Proposal fields v3 no longer keeps (§17.5): what was sent lives on GitHub
#: (the PR head), and the credit rewrite and the tip check are gone.
_V2_PROPOSAL_FIELDS = ("sent", "pushed_head", "proposed_head", "checked_tip", "credit",
                       "returned_at")


def _v3_proposal(p: dict) -> None:
    if "landed_at" not in p:
        p["landed_at"] = p.get("returned_at")
    for name in _V2_PROPOSAL_FIELDS:
        p.pop(name, None)


def migrate_v3(app_dir: str) -> tuple[dict | None, bool]:
    """Convert a v1/v2 record in place to v3 (§17.6) — the RECORD only; the
    git side of the move (`fused/mine`, the mirror, the v1 refs) is
    `classify.settle_locked`'s, which calls this LAST so a git step that
    failed is retried on the next sync. v1 first goes through `migrate_v2`.
    Drops `root_commit`, `needs_merge` and a `github` row's `skip_tip` (v2's
    Keep mine is not an undo hold; an own row's is ownsync's and stays), the proposal's
    `sent`/`pushed_head`/`proposed_head`/`checked_tip`/credit (its
    `returned_at` becomes `landed_at`), stamps `v: 3`. Answers `(record,
    migrated)`; `migrated` is False for a record already v3 (or none) — one
    read, no write."""
    cur = get(app_dir)
    if cur is None or cur.get("v") == RECORD_V:
        return cur, False
    if cur.get("v") != _V2:
        cur, _ = migrate_v2(app_dir)
        if cur is None:
            return None, False
    did = []

    def fn(rec):
        if rec.get("v") == RECORD_V:
            return
        did.append(True)
        rec.pop("root_commit", None)
        rec.pop("needs_merge", None)
        if rec.get("kind") != "own":
            # v2's Keep mine is not v3's undo hold (review P2 #9); an own
            # copy's `skip_tip` is ownsync's and stays.
            rec["skip_tip"] = None
        if isinstance(rec.get("proposal"), dict):
            _v3_proposal(rec["proposal"])
        rec["v"] = RECORD_V

    rec = update(app_dir, fn)
    return (_view(rec) if rec is not None else None), bool(did)
