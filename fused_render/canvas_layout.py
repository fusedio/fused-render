"""Move canvas clones into the scoped layout, once (D1476).

Until D1476 a clone lived at `~/Fused/canvas/<env>/<name>` (D1382). It now
lives at `~/Fused/canvas/<env>/user/<name>`, beside `team/` and `public/`,
the folders `fused canvas mirror` fills with read-only reference copies.
`ensure_layout(env)` moves every clone still at the old place. It is safe to
call from anywhere and as often as wanted: it does nothing once nothing is
left to move, and remembers that for the life of the process.

WHAT A LEGACY CLONE IS is read off the disk, never off a version stamp: a
direct child `<name>` of `<env>/` whose name is a canvas name and which holds
a `canvas.toml`. `.sync/` and every dot-folder fail the name rule; a scope
folder of the new layout never holds a `canvas.toml` of its own, so a clone
that is literally named `user`, `team` or `public` is told apart from the
scope folder it must make room for.

ONE CLONE, IN ORDER (`_migrate`):

1. Nothing sits at `user/<name>` that is not ours to replace. A reference
   copy there (`canvas_marker.removable_mirror`) is removed; a live clone or
   an unmarked folder is NEVER overwritten: the legacy folder stays where it
   is and the conflict is reported.
2. The clone's writer lock (`.sync/<name>.lock`) is claimed. Another
   fused-render syncing it (an installed app beside a dev server) refuses the
   claim, and the clone is deferred.
3. `claude_session_move.held_sessions`: a Claude session working in the
   clone, or one that finished moments ago, defers it, exactly as it refuses
   a rename. Its transcript could not move and its absolute paths would be
   left pointing at nothing.
4. The move is written to the journal (`.sync/layout-journal.json`), the
   folder is renamed, and everything that names the clone by absolute path
   follows it (`_carry`): the desk row, bookmarks, recents, schedule targets
   and the pin (`app_state_move.rewrite_stores`), and the Claude transcripts
   bucketed by working directory (`claude_session_move.relocate`). Then the
   journal entry is dropped.
5. The seeded files are refreshed and the folder is marked live when its
   collection id is known (`canvases._mark_live`); with an unknown id or an
   external CLI it keeps no marker, which reads as a live clone all the same.

`.sync/` does not move: the merge base, trash, tombstones and lock are keyed
by canvas name under `<env>/.sync/`, as before.

A RESERVED NAME takes two renames, because its target is inside the folder
it occupies: `<env>/user` -> `<env>/.legacy-user` -> `<env>/user/user`. Each
is journaled and carried on its own. A clone named `user` goes first, and
while it cannot move nothing else does (`LayoutReport.blocked`): every other
clone's new home is inside its folder.

CRASH SAFETY. Every step is one `os.rename`. A journal entry is replayed only
while its source path does not exist and its destination does, which is the
one state that says "the rename happened, the carry may not have": the carry
is a prefix rewrite, idempotent, and cannot touch anything else while nothing
lives at the source. An entry whose source is back (never renamed, or
something new is there) is dropped unreplayed. Everything else is found again
by structure, `.legacy-*` folders included.

DEFERRED clones stay at the old path, invisible to the new layout, and are
tried again on a later call (at most every `RETRY_S`, or at once with
`force=True`, which the clone route passes).

Serialised in the process by a lock per canvas root, and across processes by
the same flock the writer lock uses, on `.sync/layout.lock`.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
import threading
import time

from fused_render import canvas_marker, canvas_sync_state, canvases

logger = logging.getLogger(__name__)

#: Canvas names that are also scope folders of the new layout.
RESERVED_NAMES = canvas_marker.SCOPES
#: Where clones live under an environment's root.
CLONES_SCOPE = "user"
#: A reserved-name clone between its two renames. Dot-prefixed: no clone
#: scan, no watcher and no `canvas mirror` run looks at it.
LEGACY_PREFIX = ".legacy-"
JOURNAL_NAME = "layout-journal.json"
LOCK_NAME = "layout.lock"
_JOURNAL_VERSION = 1
#: How soon a call may look again while something is still deferred, in
#: conflict, or owed a replay. The callers include a status poll.
RETRY_S = 15.0

MOVED = "moved"
ABSENT = "absent"


@dataclasses.dataclass
class LayoutReport:
    """What one `ensure_layout` pass did for an environment."""

    env: str
    #: Clones moved into `user/` by this pass.
    moved: list[str] = dataclasses.field(default_factory=list)
    #: name -> why it was not moved YET (retried later).
    deferred: dict[str, str] = dataclasses.field(default_factory=dict)
    #: name -> why it cannot be moved without the user (never overwritten).
    conflicts: dict[str, str] = dataclasses.field(default_factory=dict)
    #: A clone named `user` still sits where the clones folder must go, so
    #: nothing may be created under `user/` either. The reason, or "".
    blocked: str = ""
    #: A clone that DID move still has a session transcript at its old
    #: bucket (a turn was open); the carry is replayed on a later pass.
    carry_owed: bool = False
    #: The pass itself failed (logged); nothing is known about this root.
    error: str = ""

    def pending(self) -> bool:
        return bool(self.deferred or self.conflicts or self.blocked
                    or self.carry_owed or self.error)

    def refusal(self, name: str, *, creating: bool = False) -> str | None:
        """Why canvas `name` must not be used at its new path right now, or
        None: its own clone is still waiting at the old path. `creating` —
        the caller would make a NEW folder under `user/` — is also refused
        while the environment is `blocked`. A conflict is not a refusal: the
        folder at the new path is a clone in its own right."""
        return self.deferred.get(name) or ((self.blocked or None) if creating else None)


@dataclasses.dataclass
class _RootState:
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    done: bool = False
    checked_at: float | None = None
    report: LayoutReport | None = None


_STATE_LOCK = threading.Lock()
_STATE: dict[str, _RootState] = {}
#: (root, name) -> when this process moved that clone (`moved_recently`).
_MOVED_AT: dict[tuple[str, str], float] = {}
#: How long a page's catalog listing may plausibly predate a move.
MOVED_RECENTLY_S = 10 * 60.0
# Set on the thread that is inside a pass. The carry calls into the stores,
# and a store that one day asks "which canvases are cloned?" would re-enter
# `ensure_layout` on this thread, behind a lock it already holds.
_IN_PASS = threading.local()


def _state_for(root: str) -> _RootState:
    key = os.path.normcase(os.path.abspath(root))
    with _STATE_LOCK:
        return _STATE.setdefault(key, _RootState())


def reset() -> None:
    """Forget what this process concluded about every root (tests, and a
    caller that knows the disk changed under it)."""
    with _STATE_LOCK:
        _STATE.clear()
        _MOVED_AT.clear()


def moved_recently(env: str, name: str) -> bool:
    """Whether THIS process moved clone (env, name) into `user/` within
    `MOVED_RECENTLY_S`. A page that listed the catalog before the move still
    shows the canvas as "not cloned" and asks for a clone; the clone route
    answers that with the folder as it is rather than a `pull --force` over
    the user's unpublished work."""
    key = (os.path.normcase(os.path.abspath(canvases.canvases_root(env))), name)
    with _STATE_LOCK:
        at = _MOVED_AT.get(key)
    return at is not None and time.monotonic() - at < MOVED_RECENTLY_S


# -- what is on disk -----------------------------------------------------------


def _has_toml(folder: str) -> bool:
    return os.path.isfile(os.path.join(folder, "canvas.toml"))


def _legacy_rel(root: str, name: str) -> str | None:
    """Where legacy clone `name` sits right now, relative to `root`: its old
    place, its `.legacy-` halfway house (reserved names), or None."""
    if not canvases._NAME_RE.match(name):
        return None
    if _has_toml(os.path.join(root, name)):
        return name
    if name in RESERVED_NAMES and _has_toml(os.path.join(root, LEGACY_PREFIX + name)):
        return LEGACY_PREFIX + name
    return None


def _order(names) -> list[str]:
    """Reserved names first, in scope order (`user` before everything: its
    folder is where every other clone goes), then the rest by name."""
    names = set(names)
    return ([n for n in RESERVED_NAMES if n in names]
            + sorted(names - set(RESERVED_NAMES), key=str.casefold))


def _legacy_names(root: str) -> list[str]:
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    found = set()
    for entry in entries:
        name = (entry[len(LEGACY_PREFIX):]
                if entry.startswith(LEGACY_PREFIX) else entry)
        if entry.startswith(LEGACY_PREFIX) and name not in RESERVED_NAMES:
            continue
        if _legacy_rel(root, name) is not None:
            found.add(name)
    return _order(found)


def legacy_clones(env: str) -> list[str]:
    """The names of `env`'s clones still at the pre-D1476 path, in the order
    they would be moved."""
    return _legacy_names(canvases.canvases_root(env))


def known_collection_id(env: str, name: str) -> str | None:
    """Canvas (env, name)'s collection id as far as this machine knows it,
    with no network call: the clone's sync point (`.sync/<name>.json`,
    written by every sync that ran through the manifest shim), else the
    stored catalog listing. None for a clone that never synced and is in no
    stored listing, and always on the external-CLI path, whose listing
    carries no ids."""
    path = os.path.join(canvases.canvases_root(env), ".sync", f"{name}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        remote = data.get("remote")
        found = data.get("collection_id") or (
            remote.get("id") if isinstance(remote, dict) else None)
        if isinstance(found, str) and found:
            return found
    for entry in canvases._listing_cache_read(env) or ():
        if entry.get("name") == name:
            found = entry.get("id")
            return found if isinstance(found, str) and found else None
    return None


# -- the journal ---------------------------------------------------------------


def _journal_path(root: str) -> str:
    return os.path.join(root, ".sync", JOURNAL_NAME)


def _journal_read(root: str) -> list[dict]:
    """The moves still owed a carry: `{name, src, dst}`, `src` and `dst`
    relative to `root` (the workspace itself can move)."""
    try:
        with open(_journal_path(root), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    moves = data.get("moves") if isinstance(data, dict) else None
    if not isinstance(moves, list):
        return []
    return [m for m in moves
            if isinstance(m, dict)
            and all(isinstance(m.get(k), str) and m[k] for k in ("name", "src", "dst"))]


def _journal_write(root: str, moves: list[dict]) -> None:
    path = _journal_path(root)
    if not moves:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    tmp = f"{path}.{os.getpid()}.tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": _JOURNAL_VERSION, "moves": moves}, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _journal_add(root: str, move: dict) -> None:
    moves = [m for m in _journal_read(root) if (m["src"], m["dst"]) != (move["src"], move["dst"])]
    _journal_write(root, [*moves, move])


def _journal_drop(root: str, move: dict) -> None:
    try:
        _journal_write(root, [m for m in _journal_read(root)
                              if (m["src"], m["dst"]) != (move["src"], move["dst"])])
    except OSError:
        logger.warning("canvas layout: could not update %s", _journal_path(root),
                       exc_info=True)


# -- carrying what names the clone by path -------------------------------------


def _spellings(root: str) -> list[str]:
    """The root as configured and as resolved. Stores hold the configured
    spelling (`canvases_dir + "/" + name`); a task or session started through
    the CLI wrapper's `pwd -P` holds the resolved one."""
    return list(dict.fromkeys((root, os.path.realpath(root))))


def _held(root: str, rel: str) -> list:
    from fused_render import claude_session_move

    held: list = []
    for base in _spellings(root):
        held += claude_session_move.held_sessions(os.path.join(base, rel))
    return list(dict.fromkeys(held))


def _carry(root: str, src: str, dst: str) -> bool:
    """Point everything that names `<root>/<src>` at `<root>/<dst>`. True when
    nothing is left behind (False: a transcript was held; worth a retry).
    Idempotent, and never raises: the folder has already moved."""
    complete = True
    try:
        from fused_render import app_state_move, claude_session_move, tasks_watch

        for base in _spellings(root):
            old, new = os.path.join(base, src), os.path.join(base, dst)
            app_state_move.rewrite_stores(old, new)
            carried = claude_session_move.relocate(old, new)
            if carried.get("pending"):
                complete = False
                logger.warning("canvas layout %s -> %s: sessions left behind %s",
                               old, new, carried["pending"])
        tasks_watch.notify_all()
    except Exception:  # noqa: BLE001 — the move itself is done
        logger.warning("canvas layout: carrying stores/sessions %s -> %s failed",
                       src, dst, exc_info=True)
    return complete


def _move(root: str, name: str, src: str, dst: str) -> None:
    """One journaled rename + carry. OSError when the rename did not happen
    (nothing changed, the journal entry is gone again)."""
    move = {"name": name, "src": src, "dst": dst}
    _journal_add(root, move)
    try:
        os.rename(os.path.join(root, src), os.path.join(root, dst))
    except OSError:
        _journal_drop(root, move)
        raise
    if _carry(root, src, dst):
        _journal_drop(root, move)


def _replay(root: str) -> bool:
    """Finish the carries a crash (or a held session) left owed. True when
    the journal is empty afterwards."""
    owed = False
    for move in _journal_read(root):
        src, dst = os.path.join(root, move["src"]), os.path.join(root, move["dst"])
        # Only this state replays (module docstring): renamed, and nothing
        # has taken the old path since.
        if os.path.lexists(src) or not os.path.lexists(dst):
            _journal_drop(root, move)
            continue
        if _carry(root, move["src"], move["dst"]):
            _journal_drop(root, move)
        else:
            owed = True
    return not owed


# -- one clone -----------------------------------------------------------------


def _foreign_content(folder: str) -> str | None:
    """A canvas folder INSIDE a legacy clone named `user` — a clone of the new
    layout made there by another process. Moving the legacy clone would carry
    it along, so it is a conflict. A clone's own content is flat (every node
    is a top-level file), so nothing legitimate matches."""
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return None
    for entry in entries:
        child = os.path.join(folder, entry)
        if os.path.isdir(child) and (
                _has_toml(child) or os.path.lexists(canvas_marker.marker_path(child))):
            return entry
    return None


def _free_target(root: str, target: str) -> str | None:
    """Make room at `target` when what is there is ours to remove; else say
    what is in the way. None = free."""
    if not os.path.lexists(target):
        return None
    scope_dir = os.path.join(root, CLONES_SCOPE)
    if canvas_marker.removable_mirror(target, scope_dir):
        # A reference copy of the canvas this clone is the live one of.
        try:
            shutil.rmtree(target)
        except OSError as exc:
            return f"the reference copy at {target} could not be removed ({exc})"
        return None
    if os.path.isdir(target) and not os.path.islink(target):
        try:
            os.rmdir(target)  # an empty folder holds nothing to lose
            return None
        except OSError:
            pass
    return f"a folder already exists at {target}"


def _migrate(env: str, root: str, name: str) -> str:
    """Move one legacy clone. "moved", "deferred:<why>" or "conflict:<why>"."""
    rel = _legacy_rel(root, name)
    if rel is None:
        return ABSENT
    new_rel = os.path.join(CLONES_SCOPE, name)
    source, target = os.path.join(root, rel), os.path.join(root, new_rel)
    staged = rel != name  # already at `.legacy-<name>`
    if name == CLONES_SCOPE and not staged:
        inside = _foreign_content(source)
        if inside is not None:
            return (f"conflict:canvas {name!r} is cloned at {source}, which already holds "
                    f"another canvas folder ({inside})")
    elif os.path.lexists(target) and not canvas_marker.removable_mirror(
            target, os.path.join(root, CLONES_SCOPE)):
        # Before any lock or rename: a live clone or an unmarked folder at
        # the new path is never replaced, and the legacy folder stays put.
        try:
            empty = (os.path.isdir(target) and not os.path.islink(target)
                     and not os.listdir(target))
        except OSError:
            empty = False
        if not empty:
            return f"conflict:a folder already exists at {target}"
    key = (env, name)
    with canvases._SYNC_LOCK:
        busy = key in canvases._syncs or key in canvases._DELETING
    if busy:
        return "deferred:it is being synced, renamed or deleted right now"
    lock_path = canvases._lock_path(env, name)
    try:
        outcome, record = canvas_sync_state.claim_lock(lock_path)
    except OSError as exc:
        return f"deferred:could not take the canvas sync lock ({exc})"
    if outcome == "refused":
        return ("deferred:it is synced by another fused-render "
                f"(pid {record.get('pid')}, port {record.get('port')})")
    lock_since = (canvas_sync_state.read_lock(lock_path) or {}).get("since")
    try:
        if _held(root, rel):
            return ("deferred:Claude is working in this canvas, or finished "
                    "moments ago")
        if name in RESERVED_NAMES and not staged:
            via = LEGACY_PREFIX + name
            if os.path.lexists(os.path.join(root, via)):
                return f"conflict:a folder already exists at {os.path.join(root, via)}"
            try:
                _move(root, name, rel, via)
            except OSError as exc:
                return f"deferred:could not move the canvas folder ({exc})"
            rel, source = via, os.path.join(root, via)
        try:
            os.makedirs(os.path.join(root, CLONES_SCOPE), exist_ok=True)
        except OSError as exc:
            return f"deferred:could not create {os.path.join(root, CLONES_SCOPE)} ({exc})"
        blocker = _free_target(root, target)
        if blocker is not None:
            return f"conflict:{blocker}"
        try:
            _move(root, name, rel, new_rel)
        except OSError as exc:
            return f"deferred:could not move the canvas folder ({exc})"
    finally:
        canvas_sync_state.release_lock(lock_path, lock_since)
    # Seeded files first (the marker's `.fusedignore` line among them), then
    # the marker: both no-ops on the external-CLI path, and the marker also
    # when the collection id is unknown — sync start heals it later.
    canvases._seed_clone_claude_files(target, name)
    canvases._mark_live(env, name)
    with _STATE_LOCK:
        _MOVED_AT[(os.path.normcase(os.path.abspath(root)), name)] = time.monotonic()
    logger.info("canvas %s: clone moved to %s", canvases._sync_key(env, name), target)
    return MOVED


def _run(env: str, root: str, only: str | None = None) -> LayoutReport:
    """One pass over `root` under the cross-process layout lock."""
    report = LayoutReport(env=env)
    names = _legacy_names(root)
    if only is not None:
        names = [n for n in names if n == only]
    if not names and not _journal_read(root):
        return report
    lock_path = os.path.join(root, ".sync", LOCK_NAME)
    try:
        outcome, _record = canvas_sync_state.claim_lock(lock_path)
    except OSError as exc:
        outcome = "refused"
        logger.warning("canvas layout: could not take %s (%s)", lock_path, exc)
    if outcome == "refused":
        why = "another fused-render is moving this environment's canvases"
        report.deferred = {n: why for n in names}
        # Whatever the journal holds is that process's to finish; if it does
        # not, a later pass here will.
        report.carry_owed = bool(_journal_read(root))
        return report
    lock_since = (canvas_sync_state.read_lock(lock_path) or {}).get("since")
    try:
        if not _replay(root):
            # A carry is still owed (a held transcript): look again later.
            report.carry_owed = True
        # Re-read under the lock: another process may have just finished.
        names = [n for n in _legacy_names(root) if only is None or n == only]
        for name in names:
            if report.blocked:
                report.deferred[name] = report.blocked
                continue
            outcome = _migrate(env, root, name)
            kind, _, why = outcome.partition(":")
            if kind == MOVED:
                report.moved.append(name)
            elif kind == "deferred":
                report.deferred[name] = why
            elif kind == "conflict":
                report.conflicts[name] = why
            if name == CLONES_SCOPE and kind in ("deferred", "conflict"):
                report.blocked = (f"canvas {name!r} must move out of "
                                  f"{os.path.join(root, CLONES_SCOPE)} first: {why}")
        # A move made on THIS pass whose carry left a transcript behind (a
        # turn opened after the check) is owed a replay as much as one a
        # crash left: without this the pass reads as finished, the answer
        # is remembered, and the journal is never looked at again.
        if _journal_read(root):
            report.carry_owed = True
    finally:
        canvas_sync_state.release_lock(lock_path, lock_since)
    return report


def _log_report(env: str, report: LayoutReport, previous: LayoutReport | None) -> None:
    """Say what a pass left pending, once per reason: the pass repeats every
    `RETRY_S` under a status poll for as long as something is."""
    before_deferred = previous.deferred if previous is not None else {}
    before_conflicts = previous.conflicts if previous is not None else {}
    for name, why in report.deferred.items():
        if before_deferred.get(name) != why:
            logger.info("canvas %s: clone not moved yet: %s",
                        canvases._sync_key(env, name), why)
    for name, why in report.conflicts.items():
        if before_conflicts.get(name) != why:
            logger.warning("canvas %s: clone left at its old path: %s",
                           canvases._sync_key(env, name), why)


def _blocked_by_user_clone(root: str) -> str:
    """The standing form of `LayoutReport.blocked`, for a pass that found no
    work of its own (a `migrate_one` of another name)."""
    if _has_toml(os.path.join(root, CLONES_SCOPE)):
        return (f"canvas {CLONES_SCOPE!r} must move out of "
                f"{os.path.join(root, CLONES_SCOPE)} first")
    return ""


# -- entry points --------------------------------------------------------------


def ensure_layout(env: str, *, force: bool = False) -> LayoutReport:
    """Move every legacy clone of `env` that can move right now, and say what
    could not. Idempotent; never raises.

    Cheap when there is nothing to do: the answer "nothing legacy here" is
    kept for the life of the process, and a pass that left something pending
    is not repeated for `RETRY_S`. `force` skips both."""
    root = canvases.canvases_root(env)
    state = _state_for(root)
    if getattr(_IN_PASS, "active", False):
        return LayoutReport(env=env)
    if not force:
        if state.done:
            return LayoutReport(env=env)
        checked = state.checked_at
        if (checked is not None and state.report is not None
                and time.monotonic() - checked < RETRY_S):
            return state.report
    with state.lock:
        if not force and state.done:
            return LayoutReport(env=env)
        _IN_PASS.active = True
        try:
            report = _run(env, root)
        except Exception:  # noqa: BLE001 — a layout pass must never fail its caller
            logger.warning("canvas layout: pass over %s failed", root, exc_info=True)
            report = LayoutReport(env=env, error="the canvas folders could not be checked")
        finally:
            _IN_PASS.active = False
        _log_report(env, report, state.report)
        state.done = not report.pending()
        state.checked_at = time.monotonic()
        state.report = report
        return report


def migrate_one(env: str, name: str) -> str:
    """Move legacy clone (env, name) now: "moved", "deferred:<why>",
    "conflict:<why>", or "absent" when there is no legacy clone of that
    name. Never raises."""
    root = canvases.canvases_root(env)
    state = _state_for(root)
    if getattr(_IN_PASS, "active", False):
        return "deferred:a layout pass is already running on this thread"
    with state.lock:
        _IN_PASS.active = True
        try:
            if name != CLONES_SCOPE:
                blocked = _blocked_by_user_clone(root)
                if blocked and _legacy_rel(root, name) is not None:
                    return f"deferred:{blocked}"
            report = _run(env, root, only=name)
        except Exception:  # noqa: BLE001 — as ensure_layout
            logger.warning("canvas layout: moving %s/%s failed", env, name, exc_info=True)
            return "deferred:the canvas folder could not be checked"
        finally:
            _IN_PASS.active = False
        # What this process remembered about the root is stale now.
        state.done = False
        state.checked_at = None
    if name in report.moved:
        return MOVED
    if name in report.deferred:
        return f"deferred:{report.deferred[name]}"
    if name in report.conflicts:
        return f"conflict:{report.conflicts[name]}"
    return ABSENT


def ensure_all() -> None:
    """Every environment, once: the server's startup pass. Never raises."""
    from fused_render.fusedcli import WORKBENCH_ENV_CHOICES

    for env in WORKBENCH_ENV_CHOICES:
        ensure_layout(env)
