"""Which folder is this task editing, and what is that folder's runs tree doing?

Two `claude` processes editing one working tree is not parallelism, it is two
people typing into the same file. The rule the owner settled on (2026-09-12) is
one sentence: **one folder, one task in flight**; everything else that wants to
run in that folder queues. WHO OWNS A FOLDER AND WHAT ORDER THE LINE MOVES IN
ARE NOT HERE — they are `fused_render/queue_manager.py`, one event-driven index
with a single spawn site (PR 2, 2026-09-17). What is left here is the pair of
questions that index asks the disk, and neither of them is an opinion:

**The folder — `queue_key`.** A task names a `project` (a cwd) or a `target` (a
page file); both resolve to the working tree they edit: the app folder
(`current_apps.app_dir_for`) if the path is inside one, else the nearest
ancestor holding a `.git` — a directory OR a file, so a worktree keys on itself
and not on the repo it was cut from — else the folder itself. Never `$HOME` and
never `/`: those are not a project, they are the machine, and gating them would
serialise every unrelated task on the box behind one another. Memoized per path
(`KEY_CACHE_MAX`), because a listing asks it once per row.

**The runs tree — `scan_runs` and its readers.** One bounded, memoized walk of
the agent's run dirs (`RUN_SCAN_LIMIT`, `SCAN_TTL`) with the answers both of its
callers need already on each record: the session ids a run answers to
(`run_sessions`), its folder (`run_key`), its permission cards
(`run_permissions`), whether its process is alive (`run_alive`) and whether it
is parked on a card nobody has answered (`run_waiting`). The tasks router's
parked-run column and the queue manager's per-task `blocked` check are the two
readers, and they share the walk rather than each paying for one.

**Everything degrades to "nothing to report".** An unreadable run dir, a half
written `meta.json`, an agent module that will not load, a wedged mount — every
one of them costs that run its news and never raises, because the caller on the
other side is a chat send or a scheduler tick, and refusing to run a task
because a directory would not list is a worse failure than running two.

`read_legacy_held_answers` is the one backward-looking thing left: the old
`held_answers.json` store, read once so the manager can take over what a
previous version parked in it. Nothing writes it any more.

The whole behaviour sits behind `project_queue_enabled` (default off; see
`shell/prefs.py`). `enabled()` is read fresh per call so a toggle applies to the
very next send with no restart — the same no-restart discipline as the engine
preference.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from fused_render import current_apps, tasks_store, tasks_watch
from fused_render._view_url_codec import canonical_fs_path
from fused_render.index.ignore import MountGuard

logger = logging.getLogger(__name__)

# THE OLD held-answers store, beside the rest of the tasks state — read once,
# by `read_legacy_held_answers`, and never written again. Held card decisions
# live in the queue manager's index now (`queue_manager.card_answered`); this
# name survives only so a machine upgrading from the store version can have
# what it parked there moved across. Read through `tasks_store.STATE_DIR` on
# every call rather than captured at import, because that is the attribute
# tests redirect at a tmp dir.
HELD_ANSWERS_FILE = "held_answers.json"

# The old store's shape version. A file that does not carry THIS number reads as
# empty: a layout this code does not know is not something it can half-
# understand, and guessing at it would migrate a decision built from fields it
# invented.
STORE_VERSION = 1

# How many run dirs (newest first) one scan reads. Nothing prunes the runs tree,
# so on a machine that has been chatting for weeks the tail is months of dead
# runs; every reader of this walk wants a LIVE run, and a live run buried under
# 120 newer ones does not exist. The ONE bound over this tree: the tasks
# router's parked scan reads the same walk through `scan_runs`, so there is no
# second number to keep in step with this one.
RUN_SCAN_LIMIT = 120

# How long one walk of the runs tree is believed. The walk is the expensive half
# of everything here — `RUN_SCAN_LIMIT` directories, a `meta.json` and a
# `session` file each — and both readers of it run on the listing's path (the
# parked-run scan and the manager's per-task `blocked` check ask for the same
# walk on every /api/tasks and every /api/tasks/changes). What is memoized is
# what the tree SAYS, plus the two answers only one reader each used to pay for
# and both now share — the folder key and the permission list (`run_key`,
# `run_permissions`). WHETHER A RUN IS ALIVE IS STILL RE-READ PER CALL, so a
# dead run stops reading as parked at once. A card raised or answered inside the
# window is seen up to a second late, and a caller that has just changed the
# tree itself says so (`invalidate_holders`). The memo is keyed on the directory
# listing as well, so a run dir appearing is never waited out.
SCAN_TTL = 1.0

# How many folder keys are remembered before the table is thrown away whole.
# `queue_key` is memoized per PATH and the paths come from task rows, so the
# table is bounded by "how many distinct projects has this machine ever chatted
# in" — a few dozen in life and unbounded only in principle. Cleared rather than
# evicted one by one, the same guard `tasks_store`'s transcript-head cache makes
# over the same shape of table: the whole point of the memo is that it is cheap
# to refill, so a clear costs one `.git` walk per live project and nothing else.
KEY_CACHE_MAX = 20000

_KEY_CACHE: dict[str, str] = {}

# (runs dir, limit, directory listing) -> (expiry, runs). One slot: there is one
# runs tree. See `SCAN_TTL`.
_scan_memo: tuple | None = None
_scan_lock = threading.Lock()

_AGENT_MOD = None
_AGENT_MOD_TRIED = False
_AGENT_MOD_LOCK = threading.Lock()

# The one `MountGuard` behind `queue_key` — see `_guard`.
_GUARD: MountGuard | None = None


# ------------------------------------------------------------------- the flag


def enabled() -> bool:
    """Whether the project queue governs anything at all (default off).

    Read FRESH on every call — `read_prefs` is a small JSON read and the switch
    has to apply to the very next send, not the next restart. Imported inside
    the function because `shell/prefs.py` pulls in FastAPI and this module is
    read from the scheduler, which has no business growing a web framework in
    its import graph.
    """
    from fused_render.shell import prefs

    return prefs.project_queue_enabled()


# ------------------------------------------------------------------ the folder


def _guard() -> MountGuard:
    """One `MountGuard`, built once. Its roots are resolved at construction (two
    realpaths), and `queue_key` is on the chat send path — building a fresh one
    per key would pay that per task row."""
    global _GUARD
    if _GUARD is None:
        _GUARD = MountGuard()
    return _GUARD


def _home() -> str:
    return canonical_fs_path(os.path.abspath(os.path.expanduser("~"))).rstrip("/")


def _is_root(path: str) -> bool:
    """Is this a filesystem root — `/`, or `C:/` on Windows. `dirname` of a root
    is the root itself, which is the one test that spells the same on both."""
    native = path if os.sep == "/" else path.replace("/", os.sep)
    return os.path.dirname(native) == native


def queue_key(project: str) -> str:
    """The working tree a task edits, canonical — the key everything queues on.

    `project` is either a folder (a task row's cwd) or a page file (a scheduled
    entry's `target`); both answer with the FOLDER, so two tasks on two files in
    one directory queue against each other rather than running side by side.

    Three sources, in order:

    1. **The app folder** — `current_apps.app_dir_for`, so two tasks on two
       subfolders of one app share a key. This is the same rule the sidebar's
       Current apps section already uses to decide what is one app.
    2. **The nearest ancestor holding `.git`**, a directory *or a file*. The
       file case is what makes a git worktree its own key: `git worktree add`
       writes a `.git` FILE pointing back at the main repo, and a worktree is
       precisely the thing the user set up so two agents could run at once.
    3. **The folder itself**, for a project that is neither.

    `$HOME` and `/` are never keys and answer `""`. They are not a project, they
    are the machine: keying on them would put every unrelated task on the box in
    one line behind one another. `""` is also what an empty or relative input
    gets, and `""` is treated as "no folder" everywhere below — never held,
    always free.

    **Guarded before any syscall.** A project under a wedged network mount
    answers with its own path string and nothing is stat'd — `MountGuard.blocks`
    is pure string work against roots resolved once (see `_guard`). A chat send
    must not block for thirty seconds on a dead NFS server to find out which
    folder it is in.

    **Memoized per path**, because the answer is a property of the layout on
    disk and the callers ask it per task row per poll. That does mean a `.git`
    created after the first ask is not seen until `reset_cache()` — acceptable
    for a folder identity, and the reason tests that move `$HOME` or plant a
    repo must reset.
    """
    if not isinstance(project, str) or not project or not os.path.isabs(project):
        return ""
    cached = _KEY_CACHE.get(project)
    if cached is not None:
        return cached
    key = _resolve_key(project)
    if len(_KEY_CACHE) >= KEY_CACHE_MAX:
        _KEY_CACHE.clear()
    _KEY_CACHE[project] = key
    return key


def _resolve_key(project: str) -> str:
    path = canonical_fs_path(os.path.abspath(project)).rstrip("/") or "/"
    if _refused(path):
        return ""
    if _guard().blocks(path):
        # A wedged mount: the path string IS the answer, and no syscall is made
        # to improve on it — not even the isdir that would tell a file target
        # from a folder, so two pages under one dead mount key separately. That
        # is the price of not hanging, and both still key stably, which is all
        # the gate needs from a folder nobody can read anyway.
        return path
    path = _as_folder(path)
    if _refused(path):
        return ""
    folder = current_apps.app_dir_for(path)
    if folder:
        return "" if _refused(folder) else folder
    repo = _repo_root(path)
    if repo:
        return repo
    return path


def _as_folder(path: str) -> str:
    """A target as the working directory it means — `agent._workdir`'s rule,
    restated here because this module is read from the scheduler and agent.py
    is a template outside the import graph.

    A directory IS the working directory; anything else is a file target and its
    parent is. Note what that does to a path that no longer exists: it reads as
    a file and answers with the parent, which is the same answer agent.py gives
    and the right trade — a target that is gone is a task already broken, while
    a *file* mistaken for a folder would stop two entries on two pages of one
    app from queueing against each other, which is the case this rule exists
    for.
    """
    try:
        if os.path.isdir(path):
            return path
    except OSError:
        return path
    parent = path.rsplit("/", 1)[0]
    return parent or "/"


def _refused(path: str) -> bool:
    """Paths that may never be a key: nothing, `/`, and the user's home."""
    return not path or _is_root(path) or path == _home()


def _repo_root(path: str) -> str:
    """The nearest ancestor of `path` (itself included) holding a `.git` entry
    of either kind, stopping short of `$HOME` and the filesystem root — neither
    of which may be a key, so neither is worth looking inside."""
    home = _home()
    folder = path
    while folder and folder != home and not _is_root(folder):
        try:
            if os.path.exists(os.path.join(folder, ".git")):
                return folder
        except OSError:
            return ""
        parent = folder.rsplit("/", 1)[0]
        if not parent or parent == folder:
            break
        folder = parent
    return ""


# ------------------------------------------------------------- the agent's runs


def agent_module():
    """The claude template's agent.py, loaded once, or None if it will not load.

    Loaded through `claude_spawn.load_agent()` and never imported at module
    import time: agent.py is a TEMPLATE, outside the package's import graph by
    design (SPEC PY-15), and `load_agent` execs the whole file. The FAILURE is
    cached with the success for the same reason the tasks router caches it — a
    module that will not load now will not load on the next tick either, and
    retrying it once a second would turn one broken import into a busy loop.
    With no agent module the runs tree cannot be read at all, which is the same
    answer this gave before the feature existed.
    """
    global _AGENT_MOD, _AGENT_MOD_TRIED
    with _AGENT_MOD_LOCK:
        if not _AGENT_MOD_TRIED:
            _AGENT_MOD_TRIED = True
            try:
                from fused_render import claude_spawn

                _AGENT_MOD = claude_spawn.load_agent()
            except Exception:  # noqa: BLE001 — no agent module is an answer
                logger.warning("could not load the claude agent module; the "
                               "project queue cannot tell which folders are "
                               "busy", exc_info=True)
                _AGENT_MOD = None
        return _AGENT_MOD


def run_sessions(agent, run_dir: str, meta: dict) -> set:
    """Every session id this run answers to.

    BOTH SPELLINGS, for the reason `agent._live_run` spells out: a run knows the
    session it RESUMED (`resumed_from` in meta.json) and the one the CLI minted
    for it (the `session` file, or the head of out.jsonl before the first poll
    has written one), and either can be the id a task row carries. Matching on
    one of them is how a live run goes unnoticed for exactly the sessions that
    were forked or freshly started — which is most scheduled runs.

    **AND THE PID, WHICH IS THE THIRD SPELLING AND THE FASTEST ONE.** Both
    spellings above come from the run dir, and a brand-new chat's run dir names
    NEITHER for as long as nothing polls it: `resumed_from` is empty because
    there was nothing to resume, and the `session` file is written by the first
    poll that sees the CLI's id. Until then the folder has a live process with
    no name, so nothing can tell that run from a stranger's and the very chat
    that spawned it queues behind itself — the bug Akshil reported on 2026-09-12
    (second message in a new chat answered `#1 in line · behind a run in this
    folder`, and the scheduler then started a SECOND `claude --resume` beside
    the chat's own idle host).

    The CLI knows its session from the instant it comes up and writes it where
    the live registry can see it (`~/.claude/sessions/<pid>.json`), and the run
    dir has carried the CLI's pid since the session host spawned it. So the pid
    is the bridge: `tasks_watch.session_for_pid` costs a lookup in a map the
    watcher already rebuilds every second, and a run stops being anonymous
    seconds after the CLI registers rather than minutes later when something
    finally polls it. Asked LAST and only when the run dir itself is silent —
    the cheap local reads answer for every run that has ever been polled.
    """
    out = {str(meta.get("resumed_from") or "")}
    own = ""
    try:
        with open(os.path.join(run_dir, "session"), encoding="utf-8") as fh:
            own = fh.read().strip()
    except OSError:
        pass
    if not own:
        try:
            own = agent._session_from_out(run_dir)
        except Exception:  # noqa: BLE001 — a head we cannot read is not an id
            own = ""
    if not own:
        # …and the id `_start` MINTED (#1177): `--session-id` is chosen before
        # the spawn and written to meta.json, so it is the one of these that
        # exists from the run's first instant. Read off `meta`, which this
        # function already has open.
        own = str(meta.get("session_id") or "")
    if not own:
        # The live registry names a run by its pid within seconds of the CLI
        # coming up, long before the run dir's own `session` file is written.
        # Flag-agnostic (Akshil, 2026-09-16): knowing which session a run is
        # is liveness bookkeeping, not the queue rule — the map is one the
        # watcher's 1 s tick already rebuilds, so the lookup costs a dict read.
        own = session_from_pid(run_dir)
    out.add(own)
    out.discard("")
    return out


def run_pid(run_dir: str) -> str:
    """The pid in `run_dir/pid`, or `""` — the CLI's own, once the session host
    has overwritten the transient host pid `_start` leaves there."""
    try:
        with open(os.path.join(run_dir, "pid"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def session_from_pid(run_dir: str) -> str:
    """The session this run's process is registered under, via the live
    registry — `""` when the run has no pid, or nothing has registered it.

    Best-effort throughout: nothing on this walk may raise, and a registry that
    cannot be read is simply a run that has not named itself yet."""
    try:
        return tasks_watch.session_for_pid(run_pid(run_dir))
    except Exception:  # noqa: BLE001 — an unreadable registry names nobody
        logger.debug("could not read the live registry for %s", run_dir,
                     exc_info=True)
        return ""


def scan_runs(agent=None, limit: int | None = None) -> list[dict]:
    """The newest `limit` run dirs, each as
    ``{run_id, run_dir, meta, sessions}`` — newest first, with two LAZY slots
    (`key`, `permissions`) filled in on first ask by `run_key` and
    `run_permissions`.

    The shared bounded pass over `agent.RUNS`. Two readers need exactly this
    walk and neither can afford an unbounded one: this module, asking which
    folders are busy, and the tasks router, asking which runs are parked on a
    card. Newest-first is load-bearing for both — run ids lead with a timestamp,
    so the first answer for a session is its most recent run, and a resumed
    conversation with several runs in the tree is described by the one on screen.

    The pid is deliberately NOT touched here: it is the expensive half and the
    two callers want it at opposite ends of their own pass (this one throws out
    dead runs first, the parked scan throws out runs nobody is waiting on
    first). `run_alive` is that half — and keeping it OUT is what makes this
    safe to memoize, because the one thing a memo could go stale about that
    MATTERS in under a second is asked fresh by the caller.

    NEITHER IS THE FOLDER KEY, AND NEITHER IS THE PERMISSION DIRECTORY. Both are
    asked by one reader each per listing and both cost real work — `queue_key`
    walks for a `.git`, `_permissions` opens the perm dir and every request in
    it — so they are computed on demand and remembered ON THE RECORD, which
    lives exactly as long as the memo does (`run_key`, `run_permissions`). The
    folder key in particular is asked ONLY under the flag: with the project
    queue off the listing still wants the parked runs, and paying a registry
    read and a `.git` walk per run dir to answer a question nobody is asking is
    the flag-off cost this lazy slot exists to remove.

    MEMOIZED FOR `SCAN_TTL`, keyed on the listing itself. One /api/tasks asks
    for the parked runs and, per queued task, whether that task is blocked, and
    paying two walks of the same 120 directories for one answer is the cost
    this exists to stop. A new run
    dir changes the listing and misses the memo at once; everything else the
    tree can say about a run changes slowly enough for a second.

    Best-effort throughout: a run dir whose `meta.json` will not read is left
    out, never raised over.
    """
    agent = agent or agent_module()
    if agent is None:
        return []
    # Read off the module attribute rather than bound as a default, so the cap
    # is one number a caller (or a test) can move in one place.
    limit = RUN_SCAN_LIMIT if limit is None else limit
    try:
        names = sorted(os.listdir(agent.RUNS), reverse=True)[:limit]
    except OSError:
        return []  # no runs tree yet: nothing has ever chatted on this machine
    slot = (str(agent.RUNS), limit, tuple(names))
    global _scan_memo
    with _scan_lock:
        memo = _scan_memo
    if memo is not None and memo[0] == slot and memo[1] > time.monotonic():
        return memo[2]
    runs = _read_runs(agent, names)
    with _scan_lock:
        _scan_memo = (slot, time.monotonic() + SCAN_TTL, runs)
    return runs


def _read_runs(agent, names: list[str]) -> list[dict]:
    """The walk `scan_runs` memoizes: one `meta.json` and one session per run."""
    out: list[dict] = []
    for name in names:
        run_dir = os.path.join(agent.RUNS, name)
        try:
            with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        out.append({
            "run_id": name,
            "run_dir": run_dir,
            "meta": meta,
            "sessions": run_sessions(agent, run_dir, meta),
        })
    return out


def run_key(run: dict) -> str:
    """The folder this run is editing — `queue_key` of its target, resolved
    ONCE per scanned record and remembered on it.

    Asked only under the flag. `queue_key`
    is memoized per path too, but reaching it at all costs the `meta.json`
    lookup and, for a path seen once, a MountGuard check and a walk for `.git`;
    doing that per run dir on the listing's path with the queue turned OFF was
    work for an answer nobody read (round-2 review, 2026-09-12)."""
    key = run.get("key")
    if key is None:
        key = queue_key(str(run.get("meta", {}).get("file") or ""))
        run["key"] = key
    return key


def run_permissions(agent, run: dict) -> list:
    """Every permission request this run has raised — `agent._permissions`,
    resolved ONCE per scanned record and remembered on it.

    TWO READERS, ONE LISTING. `run_waiting` asks whether a run is parked and the
    tasks router's parked scan asks what it is parked ON, both over the same
    runs, both on every /api/tasks and every /api/tasks/changes — and
    `_permissions` is not a cheap question: it lists the perm directory, opens
    every request in it, reads every decision beside it and (until the same
    review) opened the held-answers store as well. Paying that twice for one
    answer is the cost this slot removes, and it is safe for exactly the reason
    the scan memo is: the record lives `SCAN_TTL`, which is under the two
    seconds the design promises between a turn ending and the next starting.

    Best-effort: a perm directory that will not read is no cards, never a raise.
    """
    perms = run.get("permissions")
    if perms is None:
        try:
            perms = agent._permissions(run["run_dir"])
        except Exception:  # noqa: BLE001 — an unreadable perm dir is no cards
            perms = []
        run["permissions"] = perms
    return perms


def invalidate_holders() -> None:
    """Forget the memoized walk, so the next `scan_runs` re-reads the tree.

    For a caller that has just DONE something the tree is about to show and
    cannot wait `SCAN_TTL` to hear about — the scheduler after it claims and
    spawns an entry, which is the one moment a folder gains a process with
    nothing on disk saying so yet. Not needed for a new run dir (that changes
    the listing the memo is keyed on); this is the belt for the case where a
    run dir is REUSED."""
    global _scan_memo
    with _scan_lock:
        _scan_memo = None


def run_alive(agent, run_dir: str) -> bool:
    """Is this run's process still going — `agent._alive`, and False for
    anything that will not answer."""
    try:
        return bool(agent._alive(run_dir))
    except Exception:  # noqa: BLE001 — a probe we cannot run is not a live run
        return False


def run_waiting(agent, run: dict) -> bool:
    """Is this run PARKED — blocked on a permission or question card nobody has
    answered. Takes a SCANNED RECORD, not a path, so the permission list it
    reads is the one the parked scan is about to read too (`run_permissions`).

    A parked run does not hold its folder (design, "Blocked/parked tasks do not
    hold the folder"), and that is the whole reason this is asked: the run is in
    flight in the one way that never ends on its own, and letting it hold the
    tree would mean a folder stays locked until a human comes back from lunch.
    Only a request with no `decision` counts — `_permissions` returns answered
    cards too, so a run whose cards were all allowed minutes ago is simply
    working.
    """
    return any(not p.get("decision") for p in run_permissions(agent, run))




# ------------------------------------------------------------- the held answers


def bad_id(value) -> bool:
    """Is this run id or request id unsafe to join onto a directory we own?

    `agent._bad_id`'s rule, spelled a second time for the same reason the
    held-answers PATH is (see `HELD_ANSWERS_FILE`): agent.py is a template
    outside the package's import graph, so the server cannot import the one it
    has. Empty, a leading dot, or a `/`, a backslash or a `:` — the last two
    because on Windows a backslash separates exactly like `/`, and a drive
    prefix ("d:x") makes `os.path.join` drop our directory altogether.

    Held HERE and not only at the endpoint because these two ids become a path
    twice over: the run id joins `agent.RUNS` and the request id is joined with
    `.res.json` under the perm directory when a held answer is finally
    delivered (`queue_manager.deliver`). A store that accepted `../../x` would
    be a path traversal written by one request and walked by a scheduler tick
    minutes later, where nothing is left to say where it came from (round-2
    review, 2026-09-12).
    """
    value = value if isinstance(value, str) else ""
    return not value or value.startswith(".") or any(c in value for c in "/\\:")


def read_legacy_held_answers() -> list[dict]:
    """The records still sitting in the OLD `held_answers.json`, oldest first.

    ALL THAT IS LEFT OF THE HELD-ANSWERS STORE. Holding a card decision until
    its folder frees is the queue manager's job now — it files the answer in
    the same index that owns the line (`queue_manager.card_answered`) — and two
    stores for one fact is how a decision gets delivered twice. This reads the
    file one last time so the manager can take what a previous version parked
    there; nothing writes it any more.

    Validated field by field and silently: a record that lost its `run_id`
    cannot be delivered to anything, and a file written by a version this code
    does not know reads as empty rather than as a guess. An unreadable store
    holds nothing, which is the same answer it has always given.
    """
    state = tasks_store.load_state(HELD_ANSWERS_FILE)
    if not isinstance(state, dict) or state.get("version") != STORE_VERSION:
        return []
    raw = state.get("answers")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("queue_key")
        run_id = item.get("run_id")
        request_id = item.get("request_id")
        session_id = item.get("session_id")
        payload = item.get("payload")
        at = item.get("at")
        if not (isinstance(key, str) and key):
            continue
        if not (isinstance(run_id, str) and run_id):
            continue
        if not (isinstance(request_id, str) and request_id):
            continue
        if not isinstance(session_id, str) or not isinstance(payload, dict):
            continue
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            continue
        out.append({"queue_key": key, "session_id": session_id,
                    "run_id": run_id, "request_id": request_id,
                    "payload": payload, "at": float(at)})
    out.sort(key=lambda a: a["at"])
    return out


# -------------------------------------------------------------------- for tests


def reset_cache() -> None:
    """Forget every memo: folder keys, the agent module, the mount guard and the
    walk of the runs tree. For tests, which move `$HOME`, plant repos and
    redirect the runs tree between cases — all facts this module is entitled to
    believe never change inside one process."""
    global _AGENT_MOD, _AGENT_MOD_TRIED, _GUARD
    _KEY_CACHE.clear()
    invalidate_holders()
    with _AGENT_MOD_LOCK:
        _AGENT_MOD = None
        _AGENT_MOD_TRIED = False
    _GUARD = None
