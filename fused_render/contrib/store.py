"""The contribution loop's record store (docs/CONTRIB_LOOP.md §3, §16.1).

ONE FILE, OUTSIDE EVERY APP: `<home>/contrib/apps.json`,
`{"version": 2, "apps": {<key>: Record}}`. What a copy follows, who owns it
and the proposal it has open are facts about THIS MACHINE'S copy, decided by
fused — never by a file in the repo. The repo is the author's to write (a
committed `.fused/` included), and anything that read its own install source
out of the folder would let the app (or the author) re-point where Bob's
changes go. Nothing here reads the app folder beyond `os.path.isdir` and
one git question, the binding below.

BOUND TO A ROOT COMMIT (§16.1). A record names `root_commit`, a root commit
of HEAD when it was classified. `get` treats a record as absent — and drops
it — when that commit is not in the repo at the path any more (`git
cat-file -e` answers "missing" in a repo): a different repository now lives
there (deleted and re-cloned, replaced, a new app of the same name), and
everything the record says about the old one — what it follows, a proposal,
an overlap — would be applied to a stranger. Only a definite "missing"
drops: git not running, or the folder no longer a repo at all, keeps the
row. A v1 record without `root_commit` gets it filled on its first read.
The answer is memoised per folder on the folder's and its `.git`'s inode, so
a re-clone at the same path is seen at once and an unchanged repo costs a
stat.

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
#: The record schema stamp (§16.0 P3): a record without it is a v1 record,
#: which `migrate_v2` converts on its repo's first v2 sync.
RECORD_V = 2

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
              "classified_at", "owner_checked", "root_commit", "copy_id", "last_head",
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
               classified_by: str = "migration", owner_checked: bool = True,
               root_commit: str | None = None) -> dict:
    """A fresh Record with every §3/§16.1 field present (callers never have
    to `.get` a field into existence)."""
    return {
        "v": RECORD_V,
        "kind": kind,
        "repo": repo,
        "branch": branch,
        "classified_by": classified_by,
        "classified_at": time.time(),
        "owner_checked": owner_checked,
        "root_commit": root_commit,
        "remix_of": None,
        "proposal": None,
        "events": [],
        "needs_merge": None,
        "original_update": None,
        "activity": [],
        "skip_tip": None,
        "own_state": None,
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
    """What a reader sees: `rec` (a copy) with every v2 field present, and a
    v1 proposal's `pushed_head or proposed_head` read as `sent` (§16.1) — no
    reader has to know which schema wrote the row. Nothing persists here;
    `migrate_v2` does that."""
    for name, default in (("root_commit", None), ("activity", []),
                          ("skip_tip", None), ("own_state", None), ("hold", None), ("seen_tips", []),
                          ("last_head", None), ("copy_id", None)):
        rec.setdefault(name, default)
    p = rec.get("proposal")
    if isinstance(p, dict):
        if "sent" not in p:
            p["sent"] = p.get("pushed_head") or p.get("proposed_head")
        p.setdefault("checked_tip", None)
    return rec


# --------------------------------------------------------------------- binding

_bind_lock = threading.Lock()
_bind_memo: dict = {}  # key -> (folder stamp, root_commit, at, bound: bool | None)


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


def root_commit_of(path: str) -> str | None:
    """A root commit of HEAD in the repo at `path` (the lowest-sorting one
    when a history has several, so every reader picks the same), or None —
    not a repo, no commits yet, git unavailable."""
    roots = _roots(path)
    return roots[0] if roots else None


def _folder_stamp(k: str):
    """What changes when a different repo moves in at `k`: the folder's own
    inode, and its `.git`'s (dir, or a worktree's file) inode. Not `.git`'s
    mtime: every git write (index.lock) bumps it."""
    try:
        st = os.stat(k)
    except OSError:
        return None
    try:
        return (st.st_ino, os.stat(os.path.join(k, ".git")).st_ino)
    except OSError:
        return (st.st_ino, None)


def _bound(k: str, root_commit: str) -> bool | None:
    """True when `root_commit` is in the repo at `k`, False when that repo
    definitely lacks it (`cat-file -e` exit 1), None when git cannot say
    (not a repo any more, git missing, timed out). Memoised (module
    docstring)."""
    stamp = _folder_stamp(k)
    now = time.time()
    with _bind_lock:
        hit = _bind_memo.get(k)
    if hit and hit[0] == stamp and hit[1] == root_commit and now - hit[2] < BIND_TTL_S:
        return hit[3]
    if root_commit in (_roots(k) or ()):
        ans = True  # the common case, one local walk
    else:
        r = _git(k, "cat-file", "-e", root_commit)
        ans = None if r is None else True if r[0] == 0 else False if r[0] == 1 else None
    with _bind_lock:
        _bind_memo[k] = (stamp, root_commit, now, ans)
    return ans


def _forget_binding(k: str) -> None:
    with _bind_lock:
        _bind_memo.pop(k, None)


def _origin_matches(k: str, rec: dict) -> bool:
    """Whether the folder's `origin` names the record's repo (both None for
    an own app with no GitHub repo). Only `github`/`own` rows are judged — a
    Try copy's origin is fused's own scratch remote."""
    if rec.get("kind") not in ("github", "own"):
        return True
    from fused_render import git_upstream as gu
    from fused_render.contrib import classify

    r = _git(k, "config", "--get", "remote.origin.url")
    url = gu._out(r) if gu._ok(r) else ""
    have = (classify.repo_of_url(url) or "").lower()
    return have == (rec.get("repo") or "").lower()


def _bind(k: str, rec: dict) -> dict | None:
    """`rec` when it is still about the repo at `k`; None, after dropping
    the row, when it is not; a v1 row gets its `root_commit` filled (module
    docstring). Git runs OUTSIDE the store lock; the drop and the fill are
    compare-and-set against the row as read."""
    rc = rec.get("root_commit")
    if rc:
        if _bound(k, rc) is False:
            with _lock:
                data = _read()
                cur = data["apps"].get(k)
                if isinstance(cur, dict) and cur.get("root_commit") == rc:
                    del data["apps"][k]
                    _write(data)
            logger.info("contrib store: %s now holds a different repository; "
                        "dropped its record", k)
            _forget_binding(k)
            return None
        return rec
    found = root_commit_of(k)
    if found and (rec.get("v") != RECORD_V or rec.get("kind") == "github") \
            and not _origin_matches(k, rec):
        # A row with no root commit yet — v1, or a github row made without
        # one — over a repo whose origin is not the record's: a different
        # repository may have moved in before the first bound read (§16.12,
        # audit V11/R8). Binding it now would hand the stranger the old
        # record — drop it instead; the folder is classified afresh. A BOUND
        # row (root commit known) is never dropped over its origin (§16.1: a
        # github copy's is pointed back), and a v2 own row is filled whatever
        # its origin says (its author's to change).
        with _lock:
            data = _read()
            cur = data["apps"].get(k)
            if isinstance(cur, dict) and not cur.get("root_commit"):
                del data["apps"][k]
                _write(data)
        logger.info("contrib store: %s no longer holds %s; dropped its v1 record",
                    k, rec.get("repo"))
        return None
    if found:
        with _lock:
            data = _read()
            cur = data["apps"].get(k)
            if isinstance(cur, dict) and not cur.get("root_commit"):
                cur["root_commit"] = found
                _write(data)
        rec["root_commit"] = found
    return rec


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
            rec = _view(copy.deepcopy(rec))  # `fn` sees the v2 shape, like `get`
            fn(rec)
            data["apps"][k] = rec
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


def drop_key(k: str) -> bool:
    try:
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
            p = rec.get("proposal")
            if isinstance(p, dict):  # a v1 row's proposal, persisted v2-shaped
                p.setdefault("sent", p.get("pushed_head") or p.get("proposed_head"))
                p.setdefault("checked_tip", None)
                p.pop("pushed_head", None)
                p.pop("proposed_head", None)
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


def add_backup(app_dir: str, ref: str, why: str) -> dict | None:
    """v1's backup list, kept only as a shim for callers not yet moved to
    `add_activity`: the ref is named by one activity line instead (§16.1
    "Drop backups list")."""
    add_activity(app_dir, "backup", "Saved a copy of your work before changing it.",
                 backup=ref)
    return get(app_dir)


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


def add_activity(app_dir: str, kind: str, text: str, *, backup=None, aside=None) -> str | None:
    """Append one Activity line (§16.9) — a plain-words sentence (§0.1) for
    something fused did to this copy — and answer its id (None when there is
    no record, or the write failed). `backup` (a backup ref, or a list of
    them: a tip and its `-wip`) and/or `aside` (where moved-aside files
    went) make the line undoable, and a ref a line names is never pruned
    (`referenced_backups`). Capped at ACTIVITY_KEEP."""
    entry = {"id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}",
             "at": time.time(), "kind": str(kind), "text": str(text),
             "backup": backup, "aside": aside, "restored_at": None}

    def fn(rec):
        lines = rec.get("activity") if isinstance(rec.get("activity"), list) else []
        lines.append(entry)
        rec["activity"] = lines[-ACTIVITY_KEEP:]

    return entry["id"] if update(app_dir, fn) is not None else None


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
        b = e.get("backup") if isinstance(e, dict) else None
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
    v2 (or no record). The caller deletes V1_REFS when it is True. A v2
    record costs one read, no write (this runs on every sync)."""
    cur = get(app_dir)
    if cur is None or cur.get("v") == RECORD_V:
        return cur, False
    did = []

    def fn(rec):
        if rec.get("v") == RECORD_V:
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
        rec["v"] = RECORD_V

    rec = update(app_dir, fn)
    return (_view(rec) if rec is not None else None), bool(did)
