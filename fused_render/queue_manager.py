"""The project queue's dispatcher — one folder, one owner, one spawn site.

See design.md ("Queue manager — PR 2"). The signatures here are the contract the
doors, the listing and the event transport build against.

Vocabulary: a *folder* is `project_queue.queue_key(target)`; a *task key* is
the Tasks page key (a session id, or ``pending:<entry id>``). The index is a
rebuildable pointer table persisted in STATE_DIR as ``queue_index.json``.

**Nothing here polls and nothing here reads a registry.** State moves only on the
events below, and the three facts the manager cannot derive — how to start a
turn, how to replay a held card decision, whether a task is running or blocked —
are injected callables. That is what makes the dispatcher testable without a
process, and what keeps `reconcile()` honest: the index is a cache of pointers
whose truth lives in the scheduler store and the status sync.

2026-09-17.
"""
from __future__ import annotations

import contextlib
import copy
import logging
import threading
import time
from typing import Callable

from fused_render import tasks_store

logger = logging.getLogger(__name__)

INDEX_FILE = "queue_index.json"


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _item(raw) -> dict | None:
    """One line/blocked entry, normalised, or None if it names no task.

    A bare string loads as well as the object we write: `promoted` (the ⤒ rule)
    has to survive a restart so the file holds objects, but an index written by
    a version that did not — or edited by a human — must cost that item its
    promotion, never the whole folder's line."""
    if isinstance(raw, str):
        return {"task": raw, "entry_id": "", "promoted": False} if raw else None
    if not isinstance(raw, dict):
        return None
    key = _text(raw.get("task"))
    if not key:
        return None
    return {"task": key, "entry_id": _text(raw.get("entry_id")),
            "promoted": bool(raw.get("promoted"))}


def _owner_rec(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    key = _text(raw.get("task"))
    if not key:
        return None
    return {"task": key, "entry_id": _text(raw.get("entry_id")),
            "since": _number(raw.get("since")), "run_id": _text(raw.get("run_id")),
            "session_id": _text(raw.get("session_id"))}


def _answer_rec(raw) -> dict | None:
    """A stored card decision. `raw["raw"]` is the `_decide` arguments verbatim,
    for the same reason `project_queue.hold_answer` keeps them: the scope, mode
    and answer checks read the LIVE run, so they are applied at delivery time and
    never precomputed here."""
    if not isinstance(raw, dict) or not isinstance(raw.get("raw"), dict):
        return None
    return {"run_id": _text(raw.get("run_id")), "request_id": _text(raw.get("request_id")),
            "raw": copy.deepcopy(raw["raw"]), "at": _number(raw.get("at"))}


def _empty_folder() -> dict:
    return {"owner": None, "line": [], "blocked": []}


_BUSY: tuple = ()
_BUSY_TRIED = False


def _busy_class() -> tuple:
    """`schedule.SpawnBusy` — "not yet", as opposed to `spawn` answering None,
    which is "there is nothing here to start".

    Imported on use, and only once a spawn has actually raised: the scheduler
    imports this module back, and the name is the one thing the manager needs
    from it. A build where the class is missing falls through to the drop path,
    which is the behaviour this had before the gate existed."""
    global _BUSY, _BUSY_TRIED
    if not _BUSY_TRIED:
        _BUSY_TRIED = True
        try:
            from fused_render.schedule import SpawnBusy

            _BUSY = (SpawnBusy,)
        except Exception:
            logger.debug("no schedule.SpawnBusy; a busy spawn will drop", exc_info=True)
    return _BUSY


class QueueManager:
    """All state moves through the event methods below, under one lock, and
    each one persists the index. Spawning, answer delivery and status reads are
    injected so the manager itself owns no process and reads no registry."""

    def __init__(self, *,
                 spawn: Callable[[str, str], dict | None],
                 deliver: Callable[[dict], None],
                 running: Callable[[str], bool],
                 blocked: Callable[[str], bool],
                 pending_due: Callable[[], list[tuple[str, str, str]]],
                 notify: Callable[[set[str]], None] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        """`spawn(folder, task_key)` starts the turn for a queued task and
        returns ``{"run_id", "session_id"}`` (or None if nothing to start);
        `deliver(answer)` replays a held card decision; `running(task_key)` /
        `blocked(task_key)` are the status sync; `pending_due()` lists
        ``(folder, task_key, entry_id)`` for every pending, due scheduler entry
        (what `reconcile` rebuilds the lines from)."""
        self._spawn = spawn
        self._deliver = deliver
        self._running = running
        self._blocked = blocked
        self._pending_due = pending_due
        self._notify = notify or (lambda keys: None)
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._state = self._load()
        # NOT RECONCILED HERE, and that absence is load-bearing. `reconcile`
        # pumps, and pumping SPAWNS — so a manager built by whatever happened to
        # ask for it first would start turns as a side effect of a cancel, a
        # listing or a gate check (T4's handoff, 2026-09-17: a GET claimed a
        # pending entry and rekeyed the row it was drawing). The scheduler's
        # tick calls `reconcile()` on every pass, which is "on load" for a
        # process that has just started and "on demand" for every one after.

    # -------------------------------------------------------------- the file

    def _load(self) -> dict:
        raw = tasks_store.load_state(INDEX_FILE)
        folders: dict[str, dict] = {}
        source = raw.get("folders")
        for key, value in (source if isinstance(source, dict) else {}).items():
            if not isinstance(key, str) or not key or not isinstance(value, dict):
                continue
            rec = _empty_folder()
            rec["owner"] = _owner_rec(value.get("owner"))
            for name in ("line", "blocked"):
                entries = value.get(name)
                for entry in entries if isinstance(entries, list) else []:
                    item = _item(entry)
                    if item is not None:
                        rec[name].append(item)
            folders[key] = rec
        answers: dict[str, dict] = {}
        stored = raw.get("answers")
        for key, value in (stored if isinstance(stored, dict) else {}).items():
            record = _answer_rec(value) if isinstance(key, str) and key else None
            if record is not None:
                answers[key] = record
        return {"folders": folders, "answers": answers}

    def _save(self) -> None:
        """Whole-index write through `tasks_store._update` — the same sibling
        `.lock` every other store in that directory uses, held for the whole
        read-modify-write. An unwritable state dir costs the write and nothing
        else: the in-memory index is still right and `reconcile` rebuilds it."""
        snapshot = copy.deepcopy(self._state)

        def mutate(data: dict):
            data.clear()
            data.update(snapshot)
            return None, True

        try:
            tasks_store._update(INDEX_FILE, mutate)
        except OSError:
            logger.debug("could not write %s", INDEX_FILE, exc_info=True)

    @contextlib.contextmanager
    def _txn(self):
        """One event: the lock, the task keys it touched, then persist and notify
        exactly once. Handlers use the `_`-prefixed internals inside, so a pump
        nested in an enqueue is still one write and one notify."""
        with self._lock:
            keys: set[str] = set()
            yield keys
            self._save()
            if keys:
                self._notify(set(keys))

    # ------------------------------------------------------------- internals

    def _folder(self, folder: str) -> dict:
        rec = self._state["folders"].get(folder)
        if rec is None:
            rec = _empty_folder()
            self._state["folders"][folder] = rec
        return rec

    def _locate(self, task_key: str) -> tuple[str, dict] | tuple[None, None]:
        """The folder a task stands in — as owner, in the line, or blocked."""
        for name, rec in self._state["folders"].items():
            owner = rec["owner"]
            if owner is not None and owner["task"] == task_key:
                return name, rec
            if any(i["task"] == task_key for i in rec["line"] + rec["blocked"]):
                return name, rec
        return None, None

    def _take(self, rec: dict, task_key: str) -> dict | None:
        """Pull a task out of a folder's line and blocked list, returning it."""
        found = None
        for name in ("line", "blocked"):
            keep = []
            for item in rec[name]:
                if item["task"] == task_key and found is None:
                    found = item
                else:
                    keep.append(item)
            rec[name] = keep
        return found

    def _own(self, rec: dict, item: dict, run_id: str, session_id: str) -> None:
        rec["owner"] = {"task": item["task"], "entry_id": item.get("entry_id", ""),
                        "since": float(self._clock()), "run_id": run_id,
                        "session_id": session_id}

    def _release(self, rec: dict, folder: str, keys: set) -> None:
        owner = rec["owner"]
        if owner is not None:
            keys.add(owner["task"])
        rec["owner"] = None
        self._pump(folder, keys)

    def _pump(self, folder: str, keys: set) -> None:
        """Hand the folder to the head of its line, and keep going while the head
        turns out to be nothing to start.

        A stored answer beats a spawn: that task is already running and parked on
        a card, so what it needs is the decision it was promised, not a second
        turn. `spawn` answering None means the item names no work any more (its
        entry was cancelled, its session is gone) — it is dropped and the next
        one gets the folder, rather than the folder idling behind a ghost.
        `SpawnBusy` is the opposite answer: not yet. The item keeps the head of
        the line with no owner set and the pump stops, so the next event tries
        it again instead of starting whatever is behind it."""
        rec = self._folder(folder)
        while rec["owner"] is None and rec["line"]:
            item = rec["line"].pop(0)
            key = item["task"]
            keys.add(key)
            answer = self._state["answers"].pop(key, None)
            if answer is not None:
                self._own(rec, item, answer.get("run_id", ""), "")
                try:
                    self._deliver(answer)
                except Exception:
                    logger.exception("queue: delivering the held answer for %s failed", key)
                continue
            try:
                started = self._spawn(folder, key)
            except Exception as exc:
                busy = _busy_class()
                if busy and isinstance(exc, busy):
                    rec["line"].insert(0, item)
                    return
                logger.exception("queue: spawning %s in %s failed", key, folder)
                started = None
            if not isinstance(started, dict):
                continue
            self._own(rec, item, _text(started.get("run_id")),
                      _text(started.get("session_id")))

    def _place(self, task_key: str) -> dict:
        for rec in self._state["folders"].values():
            for i, item in enumerate(rec["line"]):
                if item["task"] == task_key:
                    owner = rec["owner"]
                    ahead = rec["line"][i - 1]["task"] if i else (owner["task"] if owner else "")
                    return {"position": i + 1, "ahead_key": ahead}
        return {"position": 0, "ahead_key": ""}

    # ------------------------------------------------------------ events
    def enqueue(self, folder: str, task_key: str, entry_id: str = "") -> dict:
        """Append to the folder's line, then pump. Idempotent: a key that already
        owns, stands in a line or sits blocked keeps the place it has."""
        if not folder or not task_key:
            return {"position": 0, "ahead_key": ""}
        with self._txn() as keys:
            where, _rec = self._locate(task_key)
            if where is None:
                self._folder(folder)["line"].append(
                    {"task": task_key, "entry_id": _text(entry_id), "promoted": False})
                keys.add(task_key)
                self._pump(folder, keys)
            return self._place(task_key)

    def skip(self, task_key: str) -> dict:
        """Move to index 0 — right behind the owner — and mark it promoted so the
        listing shows the ⤒. Newest press wins, so a later skip pushes an earlier
        one to 2. A blocked task comes back into the line; the owner is already
        ahead of everybody and is a no-op."""
        with self._txn() as keys:
            folder, rec = self._locate(task_key)
            if rec is None:
                return {"position": 0, "ahead_key": ""}
            owner = rec["owner"]
            if owner is not None and owner["task"] == task_key:
                return {"position": 0, "ahead_key": ""}
            item = self._take(rec, task_key)
            if item is None:
                return {"position": 0, "ahead_key": ""}
            item["promoted"] = True
            rec["line"].insert(0, item)
            keys.add(task_key)
            self._pump(folder, keys)
            return self._place(task_key)

    def remove(self, task_key: str) -> None:
        """Cancel or delete: gone from every line, every blocked list and the
        answer store. If it owned a folder, that is also the end of its turn."""
        with self._txn() as keys:
            if self._state["answers"].pop(task_key, None) is not None:
                keys.add(task_key)
            for folder, rec in list(self._state["folders"].items()):
                if self._take(rec, task_key) is not None:
                    keys.add(task_key)
                owner = rec["owner"]
                if owner is not None and owner["task"] == task_key:
                    self._release(rec, folder, keys)

    def started(self, folder: str, task_key: str, run_id: str = "",
                session_id: str = "") -> None:
        """An external spawn (chat admit with `run: true`) declaring ownership.

        The caller checked `is_free` at admission, so this trusts it and
        overwrites. A manager that argued here would leave a live turn with no
        owner, which is the one state the index exists to prevent."""
        if not folder or not task_key:
            return
        with self._txn() as keys:
            for other in self._state["folders"].values():
                self._take(other, task_key)
            rec = self._folder(folder)
            previous = rec["owner"]
            if previous is not None and previous["task"] != task_key:
                keys.add(previous["task"])
            self._own(rec, {"task": task_key, "entry_id": ""},
                      _text(run_id), _text(session_id))
            keys.add(task_key)

    def card_raised(self, task_key: str, run_id: str = "") -> None:
        """A permission card went up: the owner is waiting on a human, so it
        holds nothing. It moves to the parallel blocked list and the folder goes
        to the next task. Only the owner can raise a card here."""
        with self._txn() as keys:
            for folder, rec in list(self._state["folders"].items()):
                owner = rec["owner"]
                if owner is None or owner["task"] != task_key:
                    continue
                rec["blocked"].append({"task": task_key,
                                       "entry_id": owner.get("entry_id", ""),
                                       "promoted": False})
                self._release(rec, folder, keys)
                return

    def card_answered(self, task_key: str, run_id: str, request_id: str,
                      raw: dict) -> dict:
        """Returns ``{"held": bool, "position": int}``.

        Not held means the caller delivers the decision itself, immediately — the
        endpoint is a human pressing Allow and it must not wait on a pump. Held
        means somebody else has the folder: the answer is stored and the task
        goes to index 0 promoted, so it is the very next thing the folder does.

        FIRST WRITER WINS ON `(run_id, request_id)`. A double-click, or one card
        answered in two tabs, is two calls about one question — and the second
        must not replace a verdict that is already waiting to be delivered. The
        same latch `_write_decision` applies on disk, applied here because here
        the decision has not reached the disk yet."""
        with self._txn() as keys:
            folder, rec = self._locate(task_key)
            if rec is None or rec["owner"] is None or rec["owner"]["task"] == task_key:
                return {"held": False, "position": 0}
            standing = self._state["answers"].get(task_key)
            if (standing is not None
                    and standing.get("run_id") == _text(run_id)
                    and standing.get("request_id") == _text(request_id)):
                return {"held": True,
                        "position": max(1, self._place(task_key)["position"])}
            self._state["answers"][task_key] = {
                "run_id": _text(run_id), "request_id": _text(request_id),
                "raw": copy.deepcopy(raw) if isinstance(raw, dict) else {},
                "at": float(self._clock())}
            item = self._take(rec, task_key) or {"task": task_key, "entry_id": ""}
            item["promoted"] = True
            rec["line"].insert(0, item)
            keys.add(task_key)
            self._pump(folder, keys)
            return {"held": True, "position": max(1, self._place(task_key)["position"])}

    def turn_ended(self, task_key: str, run_id: str = "") -> None:
        """The session host saw a `result` row: the owner's turn is over; pump."""
        self._finish(task_key, run_id)

    def exited(self, task_key: str, run_id: str = "", code: int | None = None) -> None:
        """The child process is gone. Same effect as `turn_ended`, and for one
        turn the two arrive in either order — which is why both are no-ops
        against an owner that has already moved on."""
        self._finish(task_key, run_id)

    def _finish(self, task_key: str, run_id: str = "") -> None:
        with self._txn() as keys:
            for folder, rec in list(self._state["folders"].items()):
                owner = rec["owner"]
                if owner is None:
                    continue
                if owner["task"] == task_key or (run_id and owner["run_id"] == run_id):
                    self._release(rec, folder, keys)
                    return

    # ------------------------------------------------------------- reads
    def owner(self, folder: str) -> dict | None:
        with self._lock:
            rec = self._state["folders"].get(folder)
            owner = rec["owner"] if rec else None
            return copy.deepcopy(owner) if owner else None

    def is_free(self, folder: str, task_key: str = "") -> bool:
        """Free, or owned by `task_key` itself."""
        owner = self.owner(folder)
        return owner is None or (bool(task_key) and owner["task"] == task_key)

    def positions(self) -> dict[str, dict]:
        """``{task_key: {"key", "position", "ahead_key", "priority"}}`` — the
        shape `_queue_lines` returned, for every task standing in a line.

        Blocked tasks hold nothing and stand in no line, so they are not here.
        `priority` is the ⤒: true only at the head, and only when it got there by
        a skip or an answer, because that is the only case where the order the
        user sees is not the order they created."""
        with self._lock:
            out: dict[str, dict] = {}
            for rec in self._state["folders"].values():
                owner = rec["owner"]
                for i, item in enumerate(rec["line"]):
                    ahead = rec["line"][i - 1]["task"] if i else (owner["task"] if owner else "")
                    out[item["task"]] = {"key": item["task"], "position": i + 1,
                                         "ahead_key": ahead,
                                         "priority": i == 0 and bool(item["promoted"])}
            return out

    def place(self, task_key: str) -> dict:
        """``{"position", "ahead_key"}`` for one task; position 0 = not queued."""
        with self._lock:
            return self._place(task_key)

    def held_answer(self, task_key: str) -> dict | None:
        with self._lock:
            answer = self._state["answers"].get(task_key)
            return copy.deepcopy(answer) if answer else None

    # -------------------------------------------------------- maintenance
    def pump(self, folder: str) -> None:
        with self._txn() as keys:
            self._pump(folder, keys)

    def reconcile(self) -> None:
        """Rebuild the pointer table against the two things that are actually
        true — the scheduler store and the status sync — then pump every folder.

        Run on load and on demand. A crash mid-turn, an entry cancelled from
        another window, a session that died without a `result` row: each shows up
        here as an index that disagrees with the world, and the world wins. A
        status callable that raises keeps the item, because dropping a live task
        is the expensive direction of that guess."""
        with self._txn() as keys:
            try:
                due = list(self._pending_due() or [])
            except Exception:
                logger.exception("queue: pending_due failed; keeping the index as loaded")
                due = []
            known: set[str] = set()
            for rec in self._state["folders"].values():
                if rec["owner"] is not None:
                    known.add(rec["owner"]["task"])
                known.update(i["task"] for i in rec["line"] + rec["blocked"])
            due_keys: set[str] = set()
            for row in due:
                try:
                    folder, task_key, entry_id = row
                except (TypeError, ValueError):
                    continue
                if not folder or not task_key:
                    continue
                due_keys.add(task_key)
                if task_key in known:
                    continue
                self._folder(folder)["line"].append(
                    {"task": task_key, "entry_id": _text(entry_id), "promoted": False})
                known.add(task_key)
                keys.add(task_key)

            def live(key: str) -> bool:
                try:
                    return bool(self._running(key)) or bool(self._blocked(key))
                except Exception:
                    logger.exception("queue: the status sync failed for %s", key)
                    return True

            for rec in self._state["folders"].values():
                for name in ("line", "blocked"):
                    kept, dropped = [], []
                    for item in rec[name]:
                        (kept if item["task"] in due_keys or live(item["task"]) else dropped).append(item)
                    keys.update(i["task"] for i in dropped)
                    rec[name] = kept
                owner = rec["owner"]
                if owner is not None and not live(owner["task"]):
                    keys.add(owner["task"])
                    rec["owner"] = None
            for folder in list(self._state["folders"]):
                self._pump(folder, keys)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)


_manager: QueueManager | None = None
_factory: Callable[[], QueueManager] | None = None
_get_lock = threading.Lock()


def set_factory(fn: Callable[[], QueueManager] | None) -> None:
    """Register how to build the process-wide manager.

    The real spawn/deliver/status callables live with the doors, and this module
    must not import them: the scheduler, the routers and the permission server
    all reach the manager, so an import edge from here to any of them is a
    cycle. The hook inverts it."""
    global _factory
    _factory = fn


def peek() -> QueueManager | None:
    """The manager IF ONE HAS ALREADY BEEN BUILT, and never a build of its own.

    FOR READERS, and `get()` is for everything else. Building the manager is not
    a neutral act: it loads the index off disk and the first `reconcile()` after
    that hands each free folder to the head of its line, which SPAWNS. That is
    exactly right when a door or the scheduler asks — somebody is starting work
    — and exactly wrong on the listing's road, where a GET that happened to be
    the first caller in the process claimed a pending entry and rekeyed the row
    it was being asked to draw (T4's handoff, 2026-09-17).

    So the listing reads through this and treats None as "nothing is queued",
    which is the truthful answer for a process where nothing has queued anything
    yet. A door, the tick and the event transport build it; the page draws what
    they built."""
    with _get_lock:
        return _manager


def get() -> QueueManager:
    """The process-wide manager, built lazily from the registered factory.

    A BUILD MAY SPAWN (see `peek`). Callers on a read-only road want `peek`."""
    global _manager
    with _get_lock:
        if _manager is None:
            if _factory is None:
                raise RuntimeError(
                    "queue_manager.get(): no factory registered. Call "
                    "queue_manager.set_factory(...) during wiring, or "
                    "queue_manager.reset_for_tests(manager) in a test.")
            _manager = _factory()
        return _manager


def reset_for_tests(manager: QueueManager | None = None) -> None:
    global _manager
    _manager = manager
