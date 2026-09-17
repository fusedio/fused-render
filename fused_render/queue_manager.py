"""The project queue's dispatcher — one folder, one owner, one spawn site.

See design.md ("Queue manager — PR 2"). The signatures here are the contract the
doors, the listing and the event transport build against.

Vocabulary: a *folder* is `project_queue.queue_key(target)`; a *task key* is
the Tasks page key (a session id, or ``pending:<entry id>``). The index is a
rebuildable pointer table persisted in STATE_DIR as ``queue_index.json``.

**Nothing here polls and nothing here reads a registry.** State moves only on the
events below, and the three facts the manager cannot derive — how to start a
turn, how to replay a held card decision, whether a task is running or blocked —
are injected callables, and neither of the slow two is ever called with the
queue lock held (`_txn`). That is what makes the dispatcher testable without a
process, and what keeps `reconcile()` honest: the index is a cache of pointers
whose truth lives in the scheduler store and the status sync.

2026-09-17.
"""
from __future__ import annotations

import contextlib
import copy
import logging
import os
import threading
import time
from typing import Callable

from fused_render import tasks_store

logger = logging.getLogger(__name__)

INDEX_FILE = "queue_index.json"

# The store the derived-holder layer kept its parked card decisions in, migrated
# into the index the first time a manager loads and then renamed out of the way
# (`QueueManager._migrate_legacy`). Named here rather than imported from
# `project_queue` because T5's deletion takes the store with it, and a migration
# that stops working the moment its source module is cleaned up is a migration
# that never runs on the machine that needed it.
LEGACY_ANSWERS_FILE = "held_answers.json"
MIGRATED_SUFFIX = ".migrated"

# THE ONE CLOCK IN THE QUEUE (2026-09-17). Everything else here moves on an event
# sent by a process we own. A brand-new chat's FIRST admission is the single case
# where there is no such process yet: Claude Code has not been spawned, so there
# is no run id, no session and nobody to send `started` — and an admission that
# filed nobody left the folder reading free, which let a second nameless send
# race straight into it. So the admission files a PLACEHOLDER owner named
# `admit:<token>`, and because nothing will ever free a placeholder on its own,
# it expires: an `admit:` owner older than `PLACEHOLDER_TTL` with no run filed is
# dropped by `claim` and by `reconcile`. `is_free` never counts one at all — see
# its docstring: the placeholder settles a race between two ADMISSIONS and is not
# a live turn.
PLACEHOLDER_PREFIX = "admit:"
PLACEHOLDER_TTL = 30.0

# THE REGISTRY LAGS A SPAWN. `reconcile` asks the status sync whether an owner is
# still alive, and a turn whose spawn has not come back yet answers "no" through
# every channel there is — no run dir to look at, no mark, no registry row — so
# an owner this young WITH NO RUN FILED is never popped for being dead. One that
# has a run id is asked about properly (its run dir has a pid in it) and needs no
# grace at all.
SPAWN_GRACE = 10.0


def _is_placeholder(task_key) -> bool:
    return isinstance(task_key, str) and task_key.startswith(PLACEHOLDER_PREFIX)


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
        return ({"task": raw, "entry_id": "", "promoted": False,
                 "run_id": "", "session_id": ""} if raw else None)
    if not isinstance(raw, dict):
        return None
    key = _text(raw.get("task"))
    if not key:
        return None
    return {"task": key, "entry_id": _text(raw.get("entry_id")),
            "promoted": bool(raw.get("promoted")),
            # THE RUN AND THE SESSION RIDE ALONG, and they matter on a BLOCKED
            # item: a parked task is a live turn waiting on a human, so when its
            # answer arrives into a folder nobody owns it takes the folder back
            # (`card_answered`) — and the owner that is written then has to name
            # the same run the card was raised against, not just the label the
            # page draws.
            "run_id": _text(raw.get("run_id")),
            "session_id": _text(raw.get("session_id"))}


def _owner_rec(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    key = _text(raw.get("task"))
    if not key:
        return None
    # RUN FIRST, and the order of these fields says so. The owner of a folder is
    # a PROCESS: `run_id` is the identity every event matches on, `session_id`
    # the conversation it belongs to, and `task` only the label the Tasks page
    # draws — which is what lets a chat that has not minted a session yet own a
    # folder at all.
    return {"run_id": _text(raw.get("run_id")),
            "session_id": _text(raw.get("session_id")),
            "task": key, "entry_id": _text(raw.get("entry_id")),
            "since": _number(raw.get("since")),
            "starting": bool(raw.get("starting"))}


def _answer_rec(raw) -> dict | None:
    """A stored card decision. `raw["raw"]` is the `_decide` arguments verbatim,
    for the same reason `project_queue.hold_answer` keeps them: the scope, mode
    and answer checks read the LIVE run, so they are applied at delivery time and
    never precomputed here."""
    if not isinstance(raw, dict) or not isinstance(raw.get("raw"), dict):
        return None
    return {"run_id": _text(raw.get("run_id")), "request_id": _text(raw.get("request_id")),
            "raw": copy.deepcopy(raw["raw"]), "at": _number(raw.get("at"))}


def _answer_list(raw) -> list[dict]:
    """The decisions filed under one task, oldest first.

    A LIST AND NOT ONE RECORD. One run can raise several cards, and a store
    keyed by the task alone let the second answer overwrite the first — a
    decision the user gave that nothing would ever deliver. An index written by
    the version that kept a single record per task loads as a list of one."""
    rows = raw if isinstance(raw, list) else [raw]
    out = []
    for row in rows:
        record = _answer_rec(row)
        if record is not None:
            out.append(record)
    out.sort(key=lambda r: r["at"])
    return out


def _empty_folder() -> dict:
    return {"owner": None, "line": [], "blocked": []}


def _nowhere() -> dict:
    """The answer for a task that stands in no line — position 0, no folder."""
    return {"key": "", "position": 0, "ahead_key": ""}


def _legacy_records() -> list[dict]:
    """The old `held_answers.json` rows: `{queue_key, session_id, run_id,
    request_id, payload: {raw}, at}`.

    `project_queue.read_legacy_held_answers()` is the reader T5 leaves behind and
    the one to prefer — it knows that store's own shape. A build where the name
    is gone (or the import fails) reads the file directly, because the whole
    point of the migration is that it must still run on the upgrade where the
    old module has already been cleaned up."""
    try:
        from fused_render import project_queue

        reader = getattr(project_queue, "read_legacy_held_answers", None)
        if callable(reader):
            return [row for row in (reader() or []) if isinstance(row, dict)]
    except Exception:
        logger.debug("queue: project_queue could not hand over the legacy answers",
                     exc_info=True)
    raw = tasks_store.load_state(LEGACY_ANSWERS_FILE)
    rows = raw.get("answers") if isinstance(raw, dict) else None
    return [row for row in (rows if isinstance(rows, list) else []) if isinstance(row, dict)]


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
        `deliver(answer)` replays a held card decision; `pending_due()` lists
        ``(folder, task_key, entry_id)`` for every pending, due scheduler entry
        (what `reconcile` rebuilds the lines from).

        `running(record)` / `blocked(record)` are the status sync, and they are
        asked with a RECORD — ``{"task", "run_id", "session_id"}`` — not a key.
        A conversation answers to three names and half the index knows only one
        of them: a `pending:` owner has no session under its label, a new chat
        has no session at all, and a status read that could only ask about the
        label answered "dead" for a live turn."""
        self._spawn = spawn
        self._deliver = deliver
        self._running = running
        self._blocked = blocked
        self._pending_due = pending_due
        self._notify = notify or (lambda keys: None)
        self._clock = clock or time.time
        self._lock = threading.RLock()
        # What the last transaction DECIDED and `_flush` has still to do, with
        # the lock released: `(kind, folder, item)`. See `_txn`.
        self._starting: list[tuple[str, str, dict]] = []
        self._state = self._load()
        self._migrate_legacy()
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
        answers: dict[str, list] = {}
        stored = raw.get("answers")
        for key, value in (stored if isinstance(stored, dict) else {}).items():
            records = _answer_list(value) if isinstance(key, str) and key else []
            if records:
                answers[key] = records
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

    def _migrate_legacy(self) -> None:
        """Fold the derived-holder layer's `held_answers.json` into the index,
        once, then move the file aside.

        A parked card decision was the one piece of queue state the old layer
        kept outside the scheduler's store, so it is the one piece a rebuild
        cannot re-derive: the user pressed Allow, the folder was busy, and the
        answer has been waiting ever since. Dropping it on upgrade would leave
        a live run parked on a card nobody will ever answer again.

        **The task goes back into the line at the HEAD, promoted.** It is not a
        message waiting for its turn — it is a turn already running, parked, and
        owed the decision it was promised, which is exactly what
        `card_answered` does for an answer taken today. Oldest `at` first, the
        order the old deliverer used (`project_queue.held_answers`).

        NO PUMP AND NO NOTIFY: construction is inert on purpose (see `__init__`),
        so the first `reconcile()` is what actually hands these folders out.

        Best-effort at every step. A state dir that will not rename keeps the
        file, and the next load re-runs an idempotent migration (`setdefault`
        on the answer, the standing line checked before an insert)."""
        path = os.path.join(tasks_store.STATE_DIR, LEGACY_ANSWERS_FILE)
        if not os.path.exists(path):
            return
        try:
            records = _legacy_records()
        except Exception:
            logger.exception("queue: could not read %s; leaving it alone",
                             LEGACY_ANSWERS_FILE)
            return
        known: set[str] = set()
        for rec in self._state["folders"].values():
            if rec["owner"] is not None:
                known.add(rec["owner"]["task"])
            known.update(i["task"] for i in rec["line"] + rec["blocked"])
        heads: dict[str, int] = {}
        changed = False
        for record in sorted(records, key=lambda r: _number(r.get("at"))):
            session_id = _text(record.get("session_id"))
            if not session_id:
                continue
            payload = record.get("payload")
            raw = payload.get("raw") if isinstance(payload, dict) else None
            run_id = _text(record.get("run_id"))
            # A DECISION FOR A RUN THAT IS GONE IS NOT A DECISION. The old store
            # outlived the runs it was about, so an upgrade on a machine that has
            # been off for a week would re-own a folder for a process that died
            # days ago and replay a verdict into an empty run dir. The status
            # sync is asked; an unreadable one KEEPS the record, because dropping
            # a live decision is the expensive direction of that guess.
            if not self._alive({"task": session_id, "run_id": run_id,
                                "session_id": session_id}):
                continue
            if not self._add_answer(session_id, {
                    "run_id": run_id,
                    "request_id": _text(record.get("request_id")),
                    "raw": copy.deepcopy(raw) if isinstance(raw, dict) else {},
                    "at": _number(record.get("at"))}):
                continue
            changed = True
            folder = _text(record.get("queue_key"))
            if not folder or session_id in known:
                continue
            slot = heads.get(folder, 0)
            self._folder(folder)["line"].insert(
                slot, {"task": session_id, "entry_id": "", "promoted": True})
            heads[folder] = slot + 1
            known.add(session_id)
        if changed:
            self._save()
        try:
            os.replace(path, path + MIGRATED_SUFFIX)
        except OSError:
            logger.debug("queue: could not rename %s aside", path, exc_info=True)

    @contextlib.contextmanager
    def _txn(self):
        """One event: the lock, the task keys it touched, then persist — and the
        SLOW HALF strictly outside the lock.

        Starting a turn and replaying a card decision are injected callables
        that talk to the disk and to another process. Holding the queue lock
        across one of them made every event that arrived in that window — a card
        going up in another folder, a turn ending in a third — wait on a spawn
        it had nothing to do with. So `_pump` only DECIDES under the lock: it
        pops the head of the line and files it as a `starting` owner with one
        `since` stamp. `_flush` then does the work with the lock released and
        re-takes it for microseconds to patch the result in.

        Handlers use the `_`-prefixed internals inside, so a pump nested in an
        enqueue is still one write and one notify."""
        with self._lock:
            keys: set[str] = set()
            yield keys
            self._save()
        self._flush(keys)

    def _flush(self, keys: set) -> None:
        """Run what the transaction decided, then ring the poll once.

        A LOOP AND NOT ONE PASS: a started item can turn out to name no work any
        more, which frees the folder for the next one — and that is another job.

        A placeholder key never reaches `notify`: `admit:<token>` names no row on
        the Tasks page, and waking every poller about it would be a ring about
        nothing (`PLACEHOLDER_PREFIX`)."""
        while True:
            with self._lock:
                jobs, self._starting = self._starting, []
            if not jobs:
                break
            for kind, folder, item in jobs:
                self._start_one(kind, folder, item, keys)
        live = {key for key in keys if key and not _is_placeholder(key)}
        if live:
            self._notify(live)

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

    def _own(self, rec: dict, item: dict, run_id: str = "",
             session_id: str = "") -> dict:
        """File `item` as this folder's owner, run first (see `_owner_rec`).

        The item's own `run_id`/`session_id` are the fallback, which is what
        makes re-owning a BLOCKED task honest: the run that raised the card is
        the run that gets the folder back."""
        rec["owner"] = {"run_id": _text(run_id) or _text(item.get("run_id")),
                        "session_id": (_text(session_id)
                                       or _text(item.get("session_id"))),
                        "task": item["task"],
                        "entry_id": item.get("entry_id", ""),
                        "since": float(self._clock()), "starting": False}
        return rec["owner"]

    def _release(self, rec: dict, folder: str, keys: set) -> None:
        owner = rec["owner"]
        if owner is not None:
            keys.add(owner["task"])
        rec["owner"] = None
        self._pump(folder, keys)

    def _resign(self, rec: dict, item: dict, promoted: bool = False) -> None:
        """Give the head of the line its place back and free the folder — what a
        spawn that could not go and a delivery that raised both end in."""
        rec["owner"] = None
        if promoted:
            item["promoted"] = True
        rec["line"].insert(0, item)

    def _still_starting(self, rec: dict, item: dict) -> dict | None:
        """The owner record this job filed, IF IT IS STILL THERE. Events keep
        arriving while a spawn is in flight (that is the whole point of letting
        go of the lock), so a job that comes back to a folder somebody else now
        owns has nothing to patch."""
        owner = rec["owner"]
        if owner is None or not owner.get("starting"):
            return None
        return owner if owner["task"] == item["task"] else None

    # -- identity ---------------------------------------------------------
    #
    # A conversation answers to three names and any of them can be the only one
    # it has: the run it is, the session it is running, and the task key the
    # Tasks page files it under. A new chat has only the first for its first
    # turn; a `pending:` message has only the last until it spawns.

    @staticmethod
    def _names(record) -> set:
        return {_text((record or {}).get(field))
                for field in ("run_id", "session_id", "task")} - {""}

    def _owner_matches(self, owner: dict, task_key: str = "", run_id: str = "",
                       session_id: str = "") -> bool:
        """Is this owner the conversation the caller means? Run first, then the
        session, then the label."""
        names = self._names(owner)
        return any(value and value in names
                   for value in (_text(run_id), _text(session_id),
                                 _text(task_key)))

    def _event_matches(self, owner: dict, task_key: str, run_id: str) -> bool:
        """The STRICTER rule, for an event that ends a turn.

        When both sides name a run, the runs decide and nothing else does: a
        `result` row or an exit belonging to an OLD run of this conversation must
        not release the owner that resumed it seconds ago (the host retries, and
        a session outlives many runs). Only where one side has no run id to offer
        does this fall back to the session and the label."""
        mine = _text(owner.get("run_id"))
        theirs = _text(run_id)
        if mine and theirs:
            return mine == theirs
        key = _text(task_key)
        return bool(key) and key in {_text(owner.get("task")),
                                     _text(owner.get("session_id"))}

    @staticmethod
    def _as_record(item: dict) -> dict:
        """A line/blocked item as the status sync wants it — the same three
        names an owner carries."""
        return {"task": item["task"], "run_id": _text(item.get("run_id")),
                "session_id": _text(item.get("session_id"))}

    def _alive(self, record: dict) -> bool:
        """The status sync, asked with the WHOLE record so it can answer to
        whichever name this task actually has. A callable that raises keeps the
        item: dropping a live task is the expensive direction of that guess."""
        try:
            return bool(self._running(record)) or bool(self._blocked(record))
        except Exception:
            logger.exception("queue: the status sync failed for %r", record)
            return True

    def _stale_placeholder(self, owner, now: float | None = None) -> bool:
        """An `admit:` owner nothing ever came back for (`PLACEHOLDER_TTL`). A
        placeholder that has a run filed against it is not stale — it is a turn."""
        if owner is None or not _is_placeholder(_text(owner.get("task"))):
            return False
        if _text(owner.get("run_id")):
            return False
        moment = float(self._clock()) if now is None else now
        return moment - _number(owner.get("since")) > PLACEHOLDER_TTL

    @staticmethod
    def _ahead_label(owner) -> str:
        """What a queued row PRINTS as the thing in front of it, or "" when the
        owner has no name a page can show.

        Two owners have none: an `admit:` placeholder, and a chat filed under its
        own run id because it has not minted a session yet. Neither names a row
        on the Tasks page — the client says "behind a run in this folder" for an
        empty one, which is true, where printing the raw id would put a uuid in
        the chip and link it to nothing."""
        if owner is None:
            return ""
        task = _text(owner.get("task"))
        if not task or _is_placeholder(task):
            return ""
        if task == _text(owner.get("run_id")) and not _text(owner.get("session_id")):
            return ""
        return task

    # -- the answer store -------------------------------------------------

    def _answers_of(self, task_key: str) -> list:
        rows = self._state["answers"].get(task_key)
        return rows if isinstance(rows, list) else []

    def _add_answer(self, task_key: str, record: dict) -> bool:
        """Store one card decision, oldest first.

        FIRST WRITER WINS ON `(run_id, request_id)` — False when that very
        question already has a verdict waiting. A double-click, or one card
        answered in two tabs, is two calls about one question; several DIFFERENT
        cards of one run are several answers, and they all keep their place."""
        rows = self._state["answers"].setdefault(task_key, [])
        pair = (record["run_id"], record["request_id"])
        if any((row["run_id"], row["request_id"]) == pair for row in rows):
            return False
        rows.append(record)
        rows.sort(key=lambda row: row["at"])
        return True

    def _drop_answer(self, task_key: str, record: dict) -> None:
        rows = self._answers_of(task_key)
        pair = (record.get("run_id"), record.get("request_id"))
        for i, row in enumerate(rows):
            if (row["run_id"], row["request_id"]) == pair:
                rows.pop(i)
                break
        else:
            if rows:
                rows.pop(0)
        if not rows:
            self._state["answers"].pop(task_key, None)

    def _standing(self) -> set:
        """Every task the index still points at, anywhere."""
        out: set = set()
        for rec in self._state["folders"].values():
            if rec["owner"] is not None:
                out.add(rec["owner"]["task"])
            out.update(i["task"] for i in rec["line"] + rec["blocked"])
        return out

    def _forget_answers(self, task_key: str) -> None:
        """Drop a task's held decisions once it stands nowhere and owns nothing."""
        if task_key in self._standing():
            return
        self._state["answers"].pop(task_key, None)

    # -- the pump ---------------------------------------------------------

    def _pump(self, folder: str, keys: set) -> None:
        """Hand the folder to the head of its line — THE DECISION half, under the
        lock. `_flush` does the spawning.

        A stored answer beats a spawn: that task is already running and parked on
        a card, so what it needs is the decision it was promised, not a second
        turn. Which of the two this is, is decided here (the answer store is the
        index and the index is locked); the doing is a job, and every way it can
        end is in `_start_one`: `spawn` answering None means the item names no
        work any more and the next one gets the folder, `SpawnBusy` means not yet
        and the item keeps the head of the line."""
        rec = self._folder(folder)
        if rec["owner"] is not None or not rec["line"]:
            return
        item = rec["line"].pop(0)
        key = item["task"]
        keys.add(key)
        answers = self._answers_of(key)
        self._own(rec, item, _text(answers[0]["run_id"]) if answers else "")
        rec["owner"]["starting"] = True
        self._starting.append(("deliver" if answers else "spawn", folder, item))

    def _start_one(self, kind: str, folder: str, item: dict, keys: set) -> None:
        """One queued job, with the lock RELEASED for the injected call."""
        if kind == "deliver":
            self._deliver_answers(folder, item, keys)
            return
        key = item["task"]
        failure = None
        started = None
        try:
            started = self._spawn(folder, key)
        except Exception as exc:  # noqa: BLE001 — every outcome is handled below
            failure = exc
        with self._lock:
            rec = self._folder(folder)
            owner = self._still_starting(rec, item)
            if owner is None:
                # The world moved on while we were outside the lock — the task
                # was cancelled, or its turn ended before we got back. Nothing
                # to patch, and nothing to undo: whoever moved it owns the
                # folder's state now.
                logger.debug("queue: %s in %s was reassigned mid-spawn", key,
                             folder)
                self._save()
                return
            if failure is not None:
                busy = _busy_class()
                if busy and isinstance(failure, busy):
                    self._resign(rec, item)
                else:
                    logger.error("queue: spawning %s in %s failed", key, folder,
                                 exc_info=failure)
                    rec["owner"] = None
                    self._pump(folder, keys)
            elif not isinstance(started, dict):
                rec["owner"] = None
                self._pump(folder, keys)
            else:
                # THE RUN THE SPAWN ACTUALLY MADE, re-filed onto the owner: from
                # here the folder is held by a process with a name, and every
                # event about it matches on that name first.
                owner["run_id"] = _text(started.get("run_id"))
                owner["session_id"] = _text(started.get("session_id"))
                owner["starting"] = False
            self._save()

    def _deliver_answers(self, folder: str, item: dict, keys: set) -> None:
        """Replay every decision this task is owed, oldest first.

        AN ANSWER IS DROPPED ONLY ONCE ITS DELIVERY HAS RETURNED. A `deliver`
        that raised used to take the decision with it — the user pressed Allow,
        the folder freed, the replay failed and the verdict was gone. Now the
        task goes back to the head of its line, promoted, the folder is freed and
        the answers that are left stay: the next event tries again, and a
        re-delivery of one already applied is a no-op on disk (`_write_decision`
        is its own first-writer-wins latch)."""
        key = item["task"]
        while True:
            with self._lock:
                answers = self._answers_of(key)
                if not answers:
                    rec = self._folder(folder)
                    owner = self._still_starting(rec, item)
                    if owner is not None:
                        owner["starting"] = False
                    self._save()
                    return
                answer = copy.deepcopy(answers[0])
            try:
                self._deliver(answer)
            except Exception:
                logger.exception("queue: delivering the held answer for %s failed",
                                 key)
                with self._lock:
                    rec = self._folder(folder)
                    if self._still_starting(rec, item) is not None:
                        self._resign(rec, item, promoted=True)
                    self._save()
                return
            with self._lock:
                self._drop_answer(key, answer)
                self._save()

    def _place(self, task_key: str) -> dict:
        """``{"key", "position", "ahead_key"}`` for one task.

        `key` IS THE FOLDER, not the task — the same field `positions()` answers
        and the same one the listing's row reads (`_queue_row`): a queued row
        says which folder it is waiting on, and the task's own key is the dict
        key it is filed under."""
        for folder, rec in self._state["folders"].items():
            for i, item in enumerate(rec["line"]):
                if item["task"] == task_key:
                    ahead = (rec["line"][i - 1]["task"] if i
                             else self._ahead_label(rec["owner"]))
                    return {"key": folder, "position": i + 1, "ahead_key": ahead}
        return _nowhere()

    # ------------------------------------------------------------ events
    def enqueue(self, folder: str, task_key: str, entry_id: str = "") -> dict:
        """Append to the folder's line, then pump. Idempotent: a key that already
        owns, stands in a line or sits blocked keeps the place it has."""
        if not folder or not task_key:
            return _nowhere()
        with self._txn() as keys:
            where, _rec = self._locate(task_key)
            if where is None:
                self._folder(folder)["line"].append(
                    {"task": task_key, "entry_id": _text(entry_id),
                     "promoted": False, "run_id": "", "session_id": ""})
                keys.add(task_key)
                self._pump(folder, keys)
            return self._place(task_key)

    def skip(self, task_key: str) -> dict:
        """Move to index 0 — right behind the owner — and mark it promoted so the
        listing shows the ⤒. Newest press wins, so a later skip pushes an earlier
        one to 2. A blocked task comes back into the line; the owner is already
        ahead of everybody and is a no-op.

        ``{"key", "position", "ahead_key", "started"}``. `started` is the answer
        to the one case a position cannot describe: the folder was FREE, so the
        pump handed it straight to this task and it now stands in no line at all.
        Position 0 then means "running", not "not queued", and the endpoint has
        to be able to tell those apart."""
        with self._txn() as keys:
            folder, rec = self._locate(task_key)
            if rec is None:
                return {**_nowhere(), "started": False}
            owner = rec["owner"]
            if owner is not None and owner["task"] == task_key:
                return {**_nowhere(), "started": False}
            item = self._take(rec, task_key)
            if item is None:
                return {**_nowhere(), "started": False}
            item["promoted"] = True
            rec["line"].insert(0, item)
            keys.add(task_key)
            self._pump(folder, keys)
            owner = rec["owner"]
            started = owner is not None and owner["task"] == task_key
            return {**self._place(task_key), "started": bool(started)}

    def remove(self, task_key: str) -> None:
        """Cancel or delete THE TASK: gone from every line, every blocked list and
        the answer store. If it owned a folder, that is also the end of its turn.

        `forget_entry` is the verb for one MESSAGE of a task that may still be
        running."""
        with self._txn() as keys:
            if self._state["answers"].pop(task_key, None) is not None:
                keys.add(task_key)
            for folder, rec in list(self._state["folders"].items()):
                if self._take(rec, task_key) is not None:
                    keys.add(task_key)
                owner = rec["owner"]
                if owner is not None and owner["task"] == task_key:
                    self._release(rec, folder, keys)

    def forget_entry(self, entry_id: str) -> None:
        """ONE MESSAGE IS GONE — not the task it belongs to.

        Cancelling a queued message went through `remove`, which also releases
        the folder when that task happens to own it: a user who cancelled the
        second thing they had typed took the turn that was running away from
        themselves. This drops the item that names this entry from every line and
        blocked list and TOUCHES NO OWNER."""
        if not entry_id:
            return
        with self._txn() as keys:
            orphans: set = set()
            for folder, rec in list(self._state["folders"].items()):
                for name in ("line", "blocked"):
                    kept = []
                    for item in rec[name]:
                        if item.get("entry_id") == entry_id:
                            keys.add(item["task"])
                            orphans.add(item["task"])
                        else:
                            kept.append(item)
                    rec[name] = kept
                self._pump(folder, keys)
            for task_key in orphans:
                self._forget_answers(task_key)

    def claim(self, folder: str, task_key: str, run_id: str = "",
              session_id: str = "") -> bool:
        """TAKE THE FOLDER, OR LEARN THAT SOMEBODY ELSE HAS IT — one decision,
        under one lock.

        True means it is yours from here (or already was); False means somebody
        else owns it and the caller must queue. The admission asked `is_free` and
        then called `started`, which is two acquisitions of this lock with a gap
        in between — and two sends into one free folder arriving on two request
        threads both heard "free" and both spawned. `is_free` and `started` stay
        for the readers and for the one real spawn site; a door that is about to
        ACT on the answer uses this.

        A PLACEHOLDER GIVES WAY TO A NAME. An `admit:` owner is a chat that has
        not been spawned yet (`PLACEHOLDER_PREFIX`); a claim that carries a real
        run or session is either that same chat finally naming itself or another
        one arriving in the window, and there is no way here to tell those apart.
        Refusing would re-open the bug this whole identity chase is about — a
        chat told it is queued behind itself — which is worse than the seconds of
        overlap it would close, so the name wins and the placeholder is replaced.
        Two NAMELESS claims are the case the placeholder exists for, and the
        second of those is refused."""
        if not folder or not task_key:
            return False
        with self._txn() as keys:
            rec = self._folder(folder)
            owner = rec["owner"]
            if owner is not None and self._stale_placeholder(owner):
                keys.add(owner["task"])
                owner = rec["owner"] = None
            if owner is not None:
                if self._owner_matches(owner, task_key, run_id, session_id):
                    # THE SAME CONVERSATION, under a better name than it had.
                    if run_id and not owner["run_id"]:
                        owner["run_id"] = _text(run_id)
                    if session_id and not owner["session_id"]:
                        owner["session_id"] = _text(session_id)
                    return True
                if not (_is_placeholder(owner["task"])
                        and not _is_placeholder(task_key)):
                    return False
                keys.add(owner["task"])
            for other in self._state["folders"].values():
                self._take(other, task_key)
            self._own(rec, {"task": task_key, "entry_id": ""}, _text(run_id),
                      _text(session_id))
            keys.add(task_key)
            return True

    def started(self, folder: str, task_key: str, run_id: str = "",
                session_id: str = "") -> None:
        """An external spawn (chat admit with `run: true`, run-now) declaring
        ownership.

        The caller claimed the folder at admission, so this trusts it and
        overwrites. A manager that argued here would leave a live turn with no
        owner, which is the one state the index exists to prevent — and it is
        what makes this the event that RETIRES a placeholder: the admission filed
        `admit:<token>` because there was no process yet, and this is that
        process, arriving with both names.

        **A NAMELESS SEND IS FILED UNDER ITS RUN.** A brand-new chat's first send
        has no session — Claude Code mints one somewhere inside the spawn — so
        there was nothing to call the task and `started` filed nobody, which left
        the folder reading free and let a second nameless send into it while the
        first was still starting. The run id is a name: it exists the moment
        `_start` returns, the page carries it on every message afterwards, and
        `is_free` answers to it. When the session does arrive it simply re-files
        under the better name."""
        task_key = task_key or _text(run_id)
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
        to the next task. Only the OWNER can raise a card here — matched run
        first, because the permission server knows the run it is serving and the
        owner may be filed under a name the card never heard of."""
        with self._txn() as keys:
            for folder, rec in list(self._state["folders"].items()):
                owner = rec["owner"]
                if owner is None or not self._event_matches(owner, task_key,
                                                            run_id):
                    continue
                # FILED UNDER THE NAME THE ANSWER WILL COME BACK WITH. The decide
                # door knows this run by its session; the owner may be filed
                # under a run id or a `pending:` key, and a blocked item nobody
                # can look up is a parked task nothing will ever unpark.
                label = _text(task_key) or owner["task"]
                rec["blocked"].append(
                    {"task": label, "entry_id": owner.get("entry_id", ""),
                     "promoted": False,
                     "run_id": _text(owner.get("run_id")) or _text(run_id),
                     "session_id": _text(owner.get("session_id"))})
                self._release(rec, folder, keys)
                return

    def card_answered(self, task_key: str, run_id: str, request_id: str,
                      raw: dict) -> dict:
        """Returns ``{"held": bool, "position": int}``.

        Not held means the caller delivers the decision itself, immediately — the
        endpoint is a human pressing Allow and it must not wait on a pump. Held
        means somebody else has the folder: the answer is stored and the task
        goes to index 0 promoted, so it is the very next thing the folder does.

        **AN ANSWER INTO A FOLDER NOBODY OWNS TAKES IT BACK.** The task is parked
        in that folder's blocked list — it is a live turn, and the decision about
        to be delivered un-parks it — so leaving the folder unowned would let the
        next tick start a second turn in the tree this one is about to resume.
        The owner written names the run the card was raised against.

        FIRST WRITER WINS ON `(run_id, request_id)`, and only on that pair: one
        run can raise several cards, and the second card's answer must not
        replace the first card's. A double-click, or one card answered in two
        tabs, is two calls about one question and the second is a no-op. The same
        latch `_write_decision` applies on disk, applied here because here the
        decision has not reached the disk yet."""
        with self._txn() as keys:
            folder, rec = self._locate(task_key)
            if rec is None:
                return {"held": False, "position": 0}
            owner = rec["owner"]
            if owner is None:
                self._reown_blocked(rec, task_key, run_id, keys)
                return {"held": False, "position": 0}
            if self._owner_matches(owner, task_key, run_id):
                return {"held": False, "position": 0}
            fresh = self._add_answer(task_key, {
                "run_id": _text(run_id), "request_id": _text(request_id),
                "raw": copy.deepcopy(raw) if isinstance(raw, dict) else {},
                "at": float(self._clock())})
            if not fresh:
                return {"held": True,
                        "position": max(1, self._place(task_key)["position"])}
            item = (self._take(rec, task_key)
                    or {"task": task_key, "entry_id": "", "run_id": _text(run_id),
                        "session_id": ""})
            item["promoted"] = True
            rec["line"].insert(0, item)
            keys.add(task_key)
            self._pump(folder, keys)
            return {"held": True,
                    "position": max(1, self._place(task_key)["position"])}

    def card_cleared(self, task_key: str, run_id: str = "",
                     request_id: str = "") -> None:
        """This card was answered by SOMEBODY ELSE — a terminal, a file written
        by hand, another route into the same run.

        The task is unparked either way: it takes its folder back when nothing
        else has it (the same rule as an answer arriving into a free folder), and
        otherwise it goes to the head of the line, because a turn that was
        running and is no longer waiting on a human is the next thing that should
        run.

        A VERDICT WE ARE HOLDING IS NOT DROPPED. A task that has one is already
        in the line rather than blocked (`card_answered` moves it), so this is a
        no-op for it — and that is the wanted answer: the held decision is what
        makes the pump hand the folder BACK to that live run instead of trying to
        spawn a turn for a session that is already running. Replaying a decision
        into a card somebody else answered is a no-op on disk
        (`_write_decision`'s latch); dropping it would cost the folder.

        Idempotent: a task that is not parked here is left exactly as it is.
        `request_id` names the question for the caller's sake; nothing here reads
        it."""
        with self._txn() as keys:
            folder, rec = self._locate(task_key)
            if rec is None:
                return
            if not any(i["task"] == task_key for i in rec["blocked"]):
                return
            if rec["owner"] is None:
                self._reown_blocked(rec, task_key, run_id, keys)
                return
            item = self._take(rec, task_key)
            if item is None:
                return
            item["promoted"] = True
            rec["line"].insert(0, item)
            keys.add(task_key)

    def _reown_blocked(self, rec: dict, task_key: str, run_id: str,
                       keys: set) -> bool:
        """A parked task takes its own folder back. False when it is not parked
        here, which is an unknown task and no state change at all."""
        item = next((i for i in rec["blocked"] if i["task"] == task_key), None)
        if item is None:
            return False
        self._take(rec, task_key)
        self._own(rec, item, _text(item.get("run_id")) or _text(run_id))
        keys.add(task_key)
        return True

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
                if self._event_matches(owner, task_key, run_id):
                    self._release(rec, folder, keys)
                    return

    # ------------------------------------------------------------- reads
    def owner(self, folder: str) -> dict | None:
        with self._lock:
            rec = self._state["folders"].get(folder)
            owner = rec["owner"] if rec else None
            return copy.deepcopy(owner) if owner else None

    def is_free(self, folder: str, task_key: str = "") -> bool:
        """Free, or owned by `task_key` itself.

        THREE NAMES FOR ONE CONVERSATION and any of them is enough: the owner's
        task key, the session it is running, and the run it is. A chat that has
        not minted a session yet is filed under its run (`started`), and its
        very next message names that run — so matching on the task key alone
        would tell a conversation it is standing behind itself.

        **A PLACEHOLDER OWNER IS NOT A RUN.** `admit:<token>` is what an
        admission leaves for a chat that has no name yet, and its whole job is to
        settle a race between two ADMISSIONS (`claim`). It is not a live turn, so
        it never refuses a send at the run gate (`routers/run._folder_busy`) —
        which would be this record refusing the very start it was taken for."""
        owner = self.owner(folder)
        if owner is None:
            return True
        if _is_placeholder(_text(owner.get("task"))):
            return True
        if not task_key:
            return False
        return self._owner_matches(owner, task_key)

    def positions(self) -> dict[str, dict]:
        """``{task_key: {"key", "position", "ahead_key", "priority"}}`` — the
        shape `_queue_lines` returned, for every task standing in a line.

        `key` IS THE FOLDER THIS TASK IS WAITING ON (`queue_key`), which is what
        the field meant before the manager existed and what the listing reads it
        as (`_queue_row` → `_row`). The task's own key is the dict key.

        Blocked tasks hold nothing and stand in no line, so they are not here.
        `priority` is the ⤒: true only at the head, and only when it got there by
        a skip or an answer, because that is the only case where the order the
        user sees is not the order they created. `ahead_key` is "" for an owner
        with no name a page can draw (`_ahead_label`)."""
        with self._lock:
            out: dict[str, dict] = {}
            for folder, rec in self._state["folders"].items():
                ahead = self._ahead_label(rec["owner"])
                for i, item in enumerate(rec["line"]):
                    if i:
                        ahead = rec["line"][i - 1]["task"]
                    out[item["task"]] = {"key": folder, "position": i + 1,
                                         "ahead_key": ahead,
                                         "priority": i == 0 and bool(item["promoted"])}
            return out

    def place(self, task_key: str) -> dict:
        """``{"key", "position", "ahead_key"}`` for one task; position 0 = not
        queued, and `key` is the FOLDER (see `_place`)."""
        with self._lock:
            return self._place(task_key)

    def held_answer(self, task_key: str) -> dict | None:
        """The OLDEST decision this task is owed, or None. One question of the
        several a run may have raised — `held_answers` is the whole list."""
        with self._lock:
            rows = self._answers_of(task_key)
            return copy.deepcopy(rows[0]) if rows else None

    def held_answers(self, task_key: str) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._answers_of(task_key))

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
        is the expensive direction of that guess.

        **ASKED WITH THE WHOLE RECORD, NOT THE LABEL.** An owner filed under
        `pending:<entry id>` — which is every scheduled message the pump started
        — has no session and no registry row under that name, so asking the
        status sync about the label alone answered "dead" and the index dropped a
        LIVE owner on the next tick. The record carries the run and the session
        the spawn returned, and the callables answer to whichever of the three
        they can (`_alive`).

        **AND NEVER AGAINST A SPAWN THAT HAS NOT LANDED YET** (`SPAWN_GRACE`): an
        owner whose spawn has not come back with a run id has NOTHING the status
        sync can be asked about — no run dir, no registry row, no mark — so every
        channel says "not running" about a process that is starting. Once there
        is a run id the run dir itself answers (the tie-breaker `_queue_running`
        ends on), and a grace would only delay the truth. A placeholder owner is
        outside both rules: it is not a process at all, so it answers to nothing
        but its own expiry (`PLACEHOLDER_TTL`)."""
        with self._txn() as keys:
            try:
                due = list(self._pending_due() or [])
            except Exception:
                logger.exception("queue: pending_due failed; keeping the index as loaded")
                due = []
            known: set[str] = self._standing()
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
                    {"task": task_key, "entry_id": _text(entry_id),
                     "promoted": False, "run_id": "", "session_id": ""})
                known.add(task_key)
                keys.add(task_key)

            now = float(self._clock())
            for rec in self._state["folders"].values():
                for name in ("line", "blocked"):
                    kept, dropped = [], []
                    for item in rec[name]:
                        alive = (item["task"] in due_keys
                                 or self._alive(self._as_record(item)))
                        (kept if alive else dropped).append(item)
                    keys.update(i["task"] for i in dropped)
                    rec[name] = kept
                owner = rec["owner"]
                if owner is None:
                    continue
                if _is_placeholder(owner["task"]):
                    if self._stale_placeholder(owner, now):
                        keys.add(owner["task"])
                        rec["owner"] = None
                    continue
                if (not _text(owner.get("run_id"))
                        and now - _number(owner.get("since")) < SPAWN_GRACE):
                    continue
                if not self._alive(owner):
                    keys.add(owner["task"])
                    rec["owner"] = None
            self._prune_answers(keys)
            for folder in list(self._state["folders"]):
                self._pump(folder, keys)

    def _prune_answers(self, keys: set) -> None:
        """A decision whose task the index no longer points at anywhere, and
        whose conversation the status sync cannot find, is a verdict nothing will
        ever deliver. It would otherwise sit in the file for ever and re-own a
        folder the day that key happened to come round again."""
        standing = self._standing()
        for task_key in list(self._state["answers"]):
            if task_key in standing:
                continue
            if self._alive({"task": task_key, "run_id": "",
                            "session_id": task_key}):
                continue
            self._state["answers"].pop(task_key, None)
            keys.add(task_key)

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
