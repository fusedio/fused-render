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
3. A LINK to the clone's new place is made under a dot-name, the move is
   written to the journal (`.sync/.layout-journal.json`), the folder is
   renamed and the link renamed into its old path, and
   everything that names the clone by absolute path follows it (`_carry`):
   the desk row, bookmarks, recents, schedule targets and the pin
   (`app_state_move.rewrite_stores`), and the Claude transcripts bucketed by
   working directory (`claude_session_move.relocate`). Then the journal
   entry is dropped.
4. The seeded files are refreshed and the folder is marked live when its
   collection id is known (`canvases._mark_live`); with an unknown id or an
   external CLI it keeps no marker, which reads as a live clone all the same.

A CLAUDE SESSION AT WORK IN THE CLONE does not hold the move up (D1479).
Holding it up locked the user out of the very canvas they were working in,
with its sync stopped, for as long as the session lasted. The session keeps
its old absolute paths, so the link (`<env>/<name>` -> `user/<name>`) is what
it reaches the moved folder through: every read and write it makes lands in
the clone the sync watches. Its transcript cannot move while it runs; the
journal keeps that carry owed and a later pass finishes it. The link is
removed by the first pass that finds no journal entry for it and no session
under the old path (`_sweep_links`). Where a link cannot be made (a volume
without symlinks), and for the reserved names below, whose old path becomes
a scope folder, a session still defers the move, as before. The link is made
BEFORE the folder moves, so a link that cannot be made defers instead of
stranding the session; a crash between the two renames is finished by the
journal (`link: true` recreates it). While a link stands, its canvas cannot
be deleted or renamed, nor another canvas renamed to its name: the link
would dangle under the running session, or point into another canvas.

`.sync/` does not move: the merge base, trash, tombstones and lock are keyed
by canvas name under `<env>/.sync/`, as before.

A CLONE NAMED `user` takes two renames, because its target is inside the
folder it occupies: `<env>/user` -> `<env>/.legacy-user` -> `<env>/user/user`.
Each is journaled and carried on its own, and the second waits until the
first one's carry is complete: once `<env>/user` is the clones folder, a
rewrite of that prefix would sweep every other clone's rows along. It goes
first, and while it cannot move nothing else does (`LayoutReport.blocked`):
every other clone's new home is inside its folder. Clones named `team` or
`public` move in one step like any other (their target is not inside them),
ahead of the rest so the scope folders are free.

A legacy clone that is a SYMLINK is not moved: renaming the link one level
deeper would leave a relative one dangling. It is reported as a conflict.
The link a move leaves behind (`_is_forward_link`: exactly `user/<name>`,
relative) is not a legacy clone at all.

CRASH SAFETY. Every step is one `os.rename`. A journal entry is replayed only
while its source path does not exist (or is the link the move left there)
and its destination does, which is the one state that says "the rename
happened, the carry may not have": the carry is a prefix rewrite,
idempotent, and cannot touch anything else while nothing but that link
lives at the source. An entry whose source is back (never renamed, or
something new is there) is dropped unreplayed. Everything else is found again
by structure, `.legacy-*` folders included.

DEFERRED clones stay at the old path, invisible to the new layout, and are
tried again on a later call (at most every `RETRY_S`, or at once with
`force=True`, which the clone route passes).

Serialised in the process by a lock per canvas root, and across processes by
the same flock the writer lock uses, on `.sync/.layout.lock`.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time

from fused_render import canvas_marker, canvas_sync_state, canvases

logger = logging.getLogger(__name__)

#: Canvas names that are also scope folders of the new layout.
RESERVED_NAMES = canvas_marker.SCOPES
#: Where clones live under an environment's root.
CLONES_SCOPE = "user"
#: A clone named `user` between its two renames. Dot-prefixed: no clone
#: scan, no watcher and no `canvas mirror` run looks at it.
LEGACY_PREFIX = ".legacy-"
# Both dot-prefixed, so neither can be a canvas's file: `.sync/` holds
# `<canvas name>.lock` and `<canvas name>.json` per clone, and a canvas may
# be named `layout`. No canvas name starts with a dot (`canvases._NAME_RE`).
JOURNAL_NAME = ".layout-journal.json"
LOCK_NAME = ".layout.lock"
_JOURNAL_VERSION = 1
#: How soon a call may look again while something is still deferred, in
#: conflict, or owed a replay. The callers include a status poll.
RETRY_S = 15.0
#: How long a moved clone's carry is retried before it is given up: a
#: transcript that is still held then (a session left open for hours, a
#: store that keeps failing) stays in its old bucket, and the environment —
#: and its mirror — stop waiting for it.
CARRY_GIVE_UP_S = 3 * 3600.0

MOVED = "moved"
ABSENT = "absent"
#: `_migrate`'s deferral while this process's mirror run writes the scope
#: folders: the one a forced pass under `canvas_mirror.hold` can clear.
MIRROR_BUSY = "the reference canvases are being updated"
#: `LayoutReport.refusal` for a canvas whose old path still forwards to it.
FORWARDING = ("its move from the old folder is still finishing: a Claude session "
              "started there may still be running")
#: Prefix of a link made ready before its clone moves (`_stage_link`).
STAGED_LINK_PREFIX = ".layout-link-"


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
    #: Old paths still forwarding to a moved clone: a session there has not
    #: ended yet (`_sweep_links` removes each once nothing uses it).
    forwarding: list[str] = dataclasses.field(default_factory=list)

    def pending(self) -> bool:
        return bool(self.deferred or self.conflicts or self.blocked
                    or self.carry_owed or self.error or self.forwarding)

    def refusal(self, name: str, *, creating: bool = False,
                has_clone: bool = False, retiring: bool = False) -> str | None:
        """Why canvas `name` must not be used at its new path right now, or
        None. Its own clone is still waiting at the old path (`deferred`);
        or the pass itself failed (`error`) and this canvas still has a
        folder there — fail closed for it. `creating` — the caller would
        make or fill a folder under `user/` — is also refused while the
        environment is `blocked`, and while this canvas is in `conflicts`:
        its clone sits at the old path with something else at the new one,
        and a pull into THAT would strand the old folder for good — unless
        `has_clone`: what is at the new path is a live clone in its own
        right, and using it changes nothing for the old folder. `retiring` —
        the caller deletes or renames the canvas — refuses a conflict and a
        blocked environment whatever is at the new path: the clone left at
        the old path would outlive the canvas (or keep its old name), and a
        later pass would move it back into `user/`. It also refuses a canvas
        whose old path still forwards to it (`forwarding`)."""
        if self.error:
            # Fail closed, but only where a clone could actually be waiting:
            # this canvas still has a folder at the old path, or a clone
            # named `user` still sits where a new folder would go. One
            # failing pass must not lock the user out of every canvas.
            root = canvases.canvases_root(self.env)
            if _legacy_rel(root, name) is not None:
                return self.error
            if (creating or retiring) and _blocked_by_user_clone(root):
                return self.error
            if retiring and _is_forward_link(root, name):
                # The pass could not say whether a session still uses the
                # link: fail closed for the canvas it forwards to.
                return self.error
            return None
        why = self.deferred.get(name)
        if why:
            return why
        if retiring and name in self.forwarding:
            # Deleted or renamed now, the link would dangle under the
            # session still using it — or, once another canvas takes
            # `user/<name>`, point that session into it.
            return FORWARDING
        if retiring:
            return self.blocked or self.conflicts.get(name)
        if creating:
            return self.blocked or (None if has_clone else self.conflicts.get(name))
        return None


@dataclasses.dataclass
class _RootState:
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    done: bool = False
    checked_at: float | None = None
    report: LayoutReport | None = None


_STATE_LOCK = threading.Lock()
_STATE: dict[str, _RootState] = {}
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


# -- what is on disk -----------------------------------------------------------


def _has_toml(folder: str) -> bool:
    return os.path.isfile(os.path.join(folder, "canvas.toml"))


def _legacy_rel(root: str, name: str) -> str | None:
    """Where legacy clone `name` sits right now, relative to `root`: its old
    place, its `.legacy-` halfway house (reserved names), or None. The link a
    move left at the old place is not a clone there."""
    if not canvases._NAME_RE.match(name):
        return None
    if _is_forward_link(root, name):
        return None
    if _has_toml(os.path.join(root, name)):
        return name
    if name == CLONES_SCOPE and _has_toml(os.path.join(root, LEGACY_PREFIX + name)):
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
        if entry.startswith(LEGACY_PREFIX) and name != CLONES_SCOPE:
            continue
        if _legacy_rel(root, name) is not None:
            found.add(name)
    return _order(found)


def legacy_clones(env: str) -> list[str]:
    """The names of `env`'s clones still at the pre-D1476 path, in the order
    they would be moved."""
    return _legacy_names(canvases.canvases_root(env))


def is_legacy(env: str, name: str) -> bool:
    """Whether canvas (env, name) still has a clone at the pre-D1476 path."""
    return _legacy_rel(canvases.canvases_root(env), name) is not None


# -- the link a move leaves at the old path ------------------------------------


def _link_text(name: str) -> str:
    """What the link at `<root>/<name>` holds: relative, so it survives the
    workspace itself moving."""
    return os.path.join(CLONES_SCOPE, name)


def _is_forward_link(root: str, name: str) -> bool:
    """`<root>/<name>` is the link a move of clone `name` left behind, and
    nothing else: a symlink holding exactly `user/<name>`. A link of the
    user's own, pointing anywhere else, is not ours (and stays a conflict)."""
    path = os.path.join(root, name)
    try:
        return os.path.islink(path) and os.readlink(path) == _link_text(name)
    except OSError:
        return False


def _forward_links(root: str) -> list[str]:
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    return [e for e in entries if canvases._NAME_RE.match(e) and _is_forward_link(root, e)]


def _stage_link(root: str, name: str) -> str | None:
    """Make the link clone `name` will leave at its old path, ready under a
    dot-name beside it, BEFORE the folder moves: a link that cannot be made
    must defer a move a session depends on, not strand the session after
    it. The path of the staged link, or None (a volume without symlinks)."""
    staged = os.path.join(root, STAGED_LINK_PREFIX + name)
    try:
        if os.path.islink(staged):
            os.unlink(staged)   # a crashed pass's; the layout lock is ours
        os.symlink(_link_text(name), staged, target_is_directory=True)
        return staged
    except (OSError, NotImplementedError):
        logger.info("canvas layout: cannot make a link in %s", root, exc_info=True)
        return None


def _drop_staged_links(root: str) -> None:
    """Remove links a crashed pass staged and never placed (dot-names, seen
    by nothing else). Under the layout lock: no pass is staging one now."""
    try:
        entries = os.listdir(root)
    except OSError:
        return
    for entry in entries:
        path = os.path.join(root, entry)
        if entry.startswith(STAGED_LINK_PREFIX) and os.path.islink(path):
            try:
                os.unlink(path)
            except OSError:
                pass


def _leave_link(root: str, name: str) -> None:
    """Put the forwarding link at the old path of clone `name` directly (a
    crash lost the staged one). Never raises: the move is done either way."""
    try:
        os.symlink(_link_text(name), os.path.join(root, name), target_is_directory=True)
    except (OSError, NotImplementedError):
        logger.warning("canvas layout: could not leave a link at %s to the moved clone",
                       os.path.join(root, name), exc_info=True)


def _sweep_links(root: str) -> list[str]:
    """Remove every forwarding link nothing needs any more: no journal entry
    still carries from it, and no Claude session is under it. Returns the
    links that stay."""
    owed = {m["src"] for m in _journal_read(root)}
    kept = []
    for name in _forward_links(root):
        if name in owed or _held(root, name):
            kept.append(name)
            continue
        try:
            os.unlink(os.path.join(root, name))
            logger.info("canvas layout: removed the link left at %s",
                        os.path.join(root, name))
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("canvas layout: could not remove the link at %s",
                           os.path.join(root, name), exc_info=True)
            kept.append(name)
    return kept


def forwarded_path(path: str) -> str | None:
    """`path` as it reads at the clone's new place when it reaches a moved
    clone through the link left at the old one (a session still at work
    there), else None. Paths only, plus one `readlink` per environment root
    the path is under."""
    from fused_render.fusedcli import WORKBENCH_ENV_CHOICES

    if not isinstance(path, str) or not os.path.isabs(path):
        return None
    cand = os.path.normpath(path)
    for env in WORKBENCH_ENV_CHOICES:
        for root in _spellings(canvases.canvases_root(env)):
            prefix = root.rstrip(os.sep) + os.sep
            if not cand.startswith(prefix):
                continue
            parts = cand[len(prefix):].split(os.sep)
            if parts[0] and _is_forward_link(root, parts[0]):
                return os.path.join(root, CLONES_SCOPE, *parts)
    return None


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
    out = []
    for m in moves:
        if not (isinstance(m, dict)
                and all(isinstance(m.get(k), str) and m[k] for k in ("name", "src", "dst"))):
            continue
        since = m.get("since")
        out.append({"name": m["name"], "src": m["src"], "dst": m["dst"],
                    # The move left (or was to leave) a link at `src`.
                    "link": m.get("link") is True,
                    # When the move was made (a journal from before this field
                    # counts from now: the wait is bounded either way).
                    "since": float(since) if isinstance(since, (int, float))
                    and not isinstance(since, bool) else time.time()})
    return out


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
    nothing is left behind (False: a transcript was held, or a store could
    not be rewritten; worth a retry).
    Idempotent, and never raises: the folder has already moved."""
    complete = True
    try:
        from fused_render import app_state_move, claude_session_move, tasks_watch

        for base in _spellings(root):
            old, new = os.path.join(base, src), os.path.join(base, dst)
            if not app_state_move.rewrite_stores(old, new):
                # A store failed (logged there); the rest were rewritten.
                complete = False
            carried = claude_session_move.relocate(old, new)
            if carried.get("pending"):
                complete = False
                logger.warning("canvas layout %s -> %s: sessions left behind %s",
                               old, new, carried["pending"])
        tasks_watch.notify_all()
    except Exception:  # noqa: BLE001 — the move itself is done
        # Not complete: the journal keeps the move and a later pass carries
        # it again (every step is idempotent).
        complete = False
        logger.warning("canvas layout: carrying stores/sessions %s -> %s failed",
                       src, dst, exc_info=True)
    return complete


def _move(root: str, name: str, src: str, dst: str, *,
          staged_link: str | None = None) -> bool:
    """One journaled rename + carry. True when the carry is complete (False:
    the journal keeps the move for a later pass). OSError when the rename did
    not happen (nothing changed, the journal entry is gone again).
    `staged_link` (`_stage_link`) is renamed into `src` once the folder has
    left it (`_sweep_links` removes it later)."""
    move = {"name": name, "src": src, "dst": dst, "since": time.time(),
            "link": staged_link is not None}
    _journal_add(root, move)
    try:
        os.rename(os.path.join(root, src), os.path.join(root, dst))
    except OSError:
        _journal_drop(root, move)
        raise
    if staged_link is not None:
        try:
            os.rename(staged_link, os.path.join(root, src))
        except OSError:
            # Something took the old path in the instant between the two
            # renames; `_replay` names it.
            logger.warning("canvas layout: could not place the link at %s",
                           os.path.join(root, src), exc_info=True)
    if _carry(root, src, dst):
        _journal_drop(root, move)
        return True
    return False


def _replay(root: str) -> bool:
    """Finish the carries a crash (or a held session) left owed. True when
    the journal is empty afterwards."""
    owed = False
    for move in _journal_read(root):
        src, dst = os.path.join(root, move["src"]), os.path.join(root, move["dst"])
        # Only this state replays (module docstring): renamed, and nothing
        # but the move's own link has taken the old path since.
        back = os.path.lexists(src) and not _is_forward_link(root, move["src"])
        if back and move["link"] and os.path.lexists(dst):
            # The move was to leave a link here and something else is here:
            # a session at the old path wrote into a fresh folder in the
            # instant between the folder's rename and the link's. Nothing
            # syncs that folder; say so, and leave it alone.
            logger.warning(
                "canvas layout: %s is not the link to the moved clone %s; files a "
                "Claude session wrote there are not synced — move them into the clone",
                src, dst)
        if back or not os.path.lexists(dst):
            _journal_drop(root, move)
            continue
        if move["link"] and not os.path.lexists(src):
            _leave_link(root, move["src"])   # a crash between the two renames
        if _carry(root, move["src"], move["dst"]):
            _journal_drop(root, move)
        elif time.time() - move["since"] > CARRY_GIVE_UP_S:
            # Bounded: what could not be carried in this long is left where
            # it is (a transcript keeps its old bucket; nothing is lost on
            # disk), and nothing waits on it any more.
            logger.warning(
                "canvas layout: giving up carrying the stores and sessions of %s -> %s "
                "after %.0f h; a session started under the old path keeps its history "
                "there (and its link, while it runs)", src, dst, CARRY_GIVE_UP_S / 3600)
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
        if canvas_marker.remove_mirror(target, scope_dir):
            return None
        return f"the reference copy at {target} could not be removed"
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
    if os.path.islink(source):
        # Renaming the LINK one level deeper would leave a relative one
        # pointing at nothing, and its real folder is not ours to move.
        return (f"conflict:{source} is a link to another folder; move the real "
                "folder there, or remove the link and open the canvas again")
    if staged and any(m["dst"] == rel for m in _journal_read(root)):
        # The first rename's carry is still owed (a transcript was held).
        # The second must wait for it: after it, `<env>/user` is the clones
        # folder and that prefix can never be rewritten again.
        return "deferred:sessions of this canvas are still running"
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
    from fused_render import canvas_mirror

    lock_path = canvases._lock_path(env, name)
    try:
        outcome, record = canvas_sync_state.claim_lock(lock_path)
    except OSError as exc:
        return f"deferred:could not take the canvas sync lock ({exc})"
    if outcome == "refused":
        return ("deferred:it is synced by another fused-render "
                f"(pid {record.get('pid')}, port {record.get('port')})")
    lock_since = (canvas_sync_state.read_lock(lock_path) or {}).get("since")
    # A session at work in the clone moves with it through the link left at
    # the old path (module docstring). Without one — a reserved name, whose
    # old path becomes a scope folder, or a volume without symlinks — its
    # paths would dangle, and it defers the move as it always did.
    staged_link = None if name in RESERVED_NAMES else _stage_link(root, name)
    try:
        if staged_link is None and _held(root, rel):
            return ("deferred:Claude is working in this canvas, or finished "
                    "moments ago")
        if canvas_mirror.child_running(env):
            # This process's `canvas mirror` run is writing the scope folders:
            # a reference copy removed or a clone renamed into `user/` under
            # its feet could be swapped or deleted by it. A clone, rename or
            # delete stops the run first (`canvas_mirror.hold`); anything else
            # waits. Asked AFTER every reason that outlasts the run, so this
            # deferral means "stopping the run would let it move"
            # (`worth_forcing`). No child can spawn meanwhile: it spawns under
            # the pass lock this pass holds.
            return f"deferred:{MIRROR_BUSY}"
        if name == CLONES_SCOPE and not staged:
            via = LEGACY_PREFIX + name
            if os.path.lexists(os.path.join(root, via)):
                return f"conflict:a folder already exists at {os.path.join(root, via)}"
            try:
                carried = _move(root, name, rel, via)
            except OSError as exc:
                return f"deferred:could not move the canvas folder ({exc})"
            if not carried:
                return "deferred:sessions of this canvas are still running"
            rel, source = via, os.path.join(root, via)
        try:
            os.makedirs(os.path.join(root, CLONES_SCOPE), exist_ok=True)
        except OSError as exc:
            return f"deferred:could not create {os.path.join(root, CLONES_SCOPE)} ({exc})"
        blocker = _free_target(root, target)
        if blocker is not None:
            return f"conflict:{blocker}"
        try:
            _move(root, name, rel, new_rel, staged_link=staged_link)
        except OSError as exc:
            return f"deferred:could not move the canvas folder ({exc})"
    finally:
        canvas_sync_state.release_lock(lock_path, lock_since)
        if staged_link is not None and os.path.islink(staged_link):
            try:
                os.unlink(staged_link)   # not placed: the move did not happen
            except OSError:
                pass
    # Seeded files first (the marker's `.fusedignore` line among them), then
    # the marker: both no-ops on the external-CLI path, and the marker also
    # when the collection id is unknown — sync start heals it later.
    # The folder that arrived IS a clone. An adoption flag left under its
    # name (an adoption whose folder has since gone) would make it read "not
    # cloned", and the next open would pull over it.
    canvases._unlink_quiet(canvas_marker.adopting_path(target))
    canvases._seed_clone_claude_files(target, name)
    canvases._mark_live(env, name)
    logger.info("canvas %s: clone moved to %s", canvases._sync_key(env, name), target)
    return MOVED


def _run(env: str, root: str) -> LayoutReport:
    """One pass over `root` under the cross-process layout lock."""
    report = LayoutReport(env=env)
    names = _legacy_names(root)
    if not names and not _journal_read(root) and not _forward_links(root):
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
        # A clone named `user` in the way holds the environment whoever is
        # running the pass: nothing may be made under `user/` meanwhile.
        report.blocked = _blocked_by_user_clone(root)
        # Every link standing at an old path may still carry a session: no
        # delete or rename of its canvas until a pass that holds the lock
        # has looked (`refusal(retiring=True)`).
        report.forwarding = _forward_links(root)
        return report
    lock_since = (canvas_sync_state.read_lock(lock_path) or {}).get("since")
    try:
        _drop_staged_links(root)
        if not _replay(root):
            # A carry is still owed (a held transcript): look again later.
            report.carry_owed = True
        # Re-read under the lock: another process may have just finished.
        for name in _legacy_names(root):
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
        # session at work in the clone) is owed a replay as much as one a
        # crash left: without this the pass reads as finished, the answer
        # is remembered, and the journal is never looked at again.
        if _journal_read(root):
            report.carry_owed = True
        report.forwarding = _sweep_links(root)
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
    """The standing form of `LayoutReport.blocked`, for a pass that could not
    look for itself (another process holds the layout lock)."""
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


def pass_lock(env: str) -> threading.Lock:
    """The lock a pass over `env`'s root runs under: held, no pass of this
    process is moving anything (the mirror runner spawns its child under it)."""
    return _state_for(canvases.canvases_root(env)).lock


def pending(env: str) -> dict:
    """What the last pass over `env` left unmoved, for the status routes:
    `{"deferred": {name: why}, "conflicts": {name: why}, "blocked": str,
    "error": str}` — all empty when nothing is. `conflicts` are the ones a
    user has to resolve: a clone still at `<env>/<name>` with something else
    at `<env>/user/<name>`. Reads memory; runs no pass."""
    report = _state_for(canvases.canvases_root(env)).report
    if report is None:
        return {"deferred": {}, "conflicts": {}, "blocked": "", "error": ""}
    return {"deferred": dict(report.deferred), "conflicts": dict(report.conflicts),
            "blocked": report.blocked, "error": report.error}


def worth_forcing(env: str, name: str) -> bool:
    """Whether a forced pass, with this process's mirror run stopped, could
    move canvas (env, name)'s clone from its old path now: it still has one
    there, and the last pass did not find it in conflict or deferred for a
    reason that outlasts the mirror run (another fused-render syncs it, a
    session holds it where no link can be left). sync/start asks, so a page
    retrying every few seconds does not stop and restart the mirror run each
    time for a clone that cannot move anyway."""
    if not is_legacy(env, name):
        return False
    report = _state_for(canvases.canvases_root(env)).report
    if report is None:
        return True
    if name in report.conflicts or report.blocked:
        return False
    why = report.deferred.get(name)
    return why is None or why == MIRROR_BUSY


def owed_scope_names(env: str) -> list[str]:
    """Scope-folder names (`team`, `public`, `user`) that a moved clone of
    that name still owes a carry FROM. While one is owed, nothing may create
    `<env>/<that name>`: the carry replays only while its source path does
    not exist, and would be dropped — the clone's chat history with it."""
    root = canvases.canvases_root(env)
    return [m["src"] for m in _journal_read(root) if m["src"] in RESERVED_NAMES]


def ensure_all() -> None:
    """Every environment, once: the server's startup pass. Never raises."""
    from fused_render.fusedcli import WORKBENCH_ENV_CHOICES

    for env in WORKBENCH_ENV_CHOICES:
        ensure_layout(env)
