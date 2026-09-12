"""One task in progress per folder — who is holding it, and what is waiting.

Two `claude` processes editing one working tree is not parallelism, it is two
people typing into the same file. The rule the owner settled on (2026-09-12) is
one sentence: **one folder, one task in flight**; everything else that wants to
run in that folder queues. This module is the half of that rule which has no
opinions — it answers *which folder is this*, *who is holding it right now*, and
*in what order does the line move* — and it stores the one thing that cannot be
derived, a card decision made while the folder was busy.

**Nothing here is stored that can be derived.** `holders()` reads the agent's
run dirs, the live-session registry and the scheduler's claimed entries on every
call; there is no lease, no lock file, no heartbeat to go stale when a process
is killed -9. A holder that dies stops being a holder the moment its pid stops
answering, which is the property a stored lease can never have and the reason
this is a derivation and not a table.

**The folder — `queue_key`.** A task names a `project` (a cwd) or a `target` (a
page file); both resolve to the working tree they edit: the app folder
(`current_apps.app_dir_for`) if the path is inside one, else the nearest
ancestor holding a `.git` — a directory OR a file, so a worktree keys on itself
and not on the repo it was cut from — else the folder itself. Never `$HOME` and
never `/`: those are not a project, they are the machine, and gating them would
serialise every unrelated task on the box behind one another.

**Held answers** are the exception to "nothing stored". A user who answers a
permission card on a parked task while that folder is busy has made a decision
that the run cannot be given yet — the parked run would wake up and start
editing a tree another task owns. The decision is written here
(`held_answers.json`, beside the rest of the tasks state) and delivered by
`agent._write_decision` once the folder frees. It is the one fact in the whole
feature that exists nowhere else: nobody but the user knows what they clicked.

**Everything degrades to "nothing is held".** An unreadable run dir, a half
written `meta.json`, an agent module that will not load, a wedged mount, a
schedule store that is being rewritten — every one of them costs that folder its
news and never raises, because the caller on the other side is a chat send or a
scheduler tick, and refusing to run a task because a directory would not list is
a worse failure than running two.

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
from datetime import datetime, timezone

from fused_render import current_apps, session_liveness, tasks_store, tasks_watch
from fused_render._view_url_codec import canonical_fs_path
from fused_render.index.ignore import MountGuard

logger = logging.getLogger(__name__)

# The held-answers store, in the same directory (and under the same locking
# convention) as the rest of the tasks state — task_ids.json, read.json. Read
# through `tasks_store.STATE_DIR` on every call rather than captured at import,
# because that is the attribute tests redirect at a tmp dir.
#
# THE PATH IS SPELLED TWICE ON PURPOSE. `templates/claude/agent.py` reads this
# same file to mark a card as held for a frame that re-attaches, and it may not
# import fused_render — it is a TEMPLATE and a runPython target (SPEC PY-15) —
# so it re-derives `$FUSED_RENDER_HOME/claude-sessions/held_answers.json` from
# the environment exactly the way `tasks_store.STATE_DIR` does. The same
# deliberate duplication that file already makes for CLAUDE_DIR, and the two
# have to move together.
HELD_ANSWERS_FILE = "held_answers.json"

# The store's shape version. A file that does not carry THIS number reads as
# empty: a future layout is not something this code can half-understand, and
# guessing at it would deliver a decision built from fields it invented.
STORE_VERSION = 1

# How many run dirs (newest first) one scan reads. Nothing prunes the runs tree,
# so on a machine that has been chatting for weeks the tail is months of dead
# runs; a holder is by definition ALIVE, and a live run buried under 120 newer
# ones does not exist. The ONE bound over this tree: the tasks router's parked
# scan reads the same walk through `scan_runs`, so there is no second number to
# keep in step with this one.
RUN_SCAN_LIMIT = 120

# How long one walk of the runs tree is believed. The walk is the expensive half
# of everything here — `RUN_SCAN_LIMIT` directories, a `meta.json` and a
# `session` file each — and both readers of it run on the listing's path
# (`_build_task_rows` asks for the holders AND for the parked runs on every
# /api/tasks and every /api/tasks/changes). What is memoized is what the tree
# SAYS, plus the two answers only one reader each used to pay for and both now
# share — the folder key and the permission list (`run_key`,
# `run_permissions`). WHETHER A RUN IS ALIVE AND WHETHER ITS SESSION IS MID-TURN
# ARE STILL RE-READ PER CALL, so a holder that dies stops holding at once, which
# is the property this module exists to have. A card raised or answered inside
# the window is seen up to a second late — one second is under the 2 s the
# design promises between a turn ending and the next starting, and a caller that
# has just changed the tree itself says so (`invalidate_holders`). The memo is
# keyed on the directory listing as well, so a run dir appearing is never waited
# out.
SCAN_TTL = 1.0

# How long an admitted-but-not-yet-spawned send counts as the holder. The gap it
# covers is real: the server says "run", the client spawns, and the registry row
# appears once `claude` has started — which on a cold start (a big repo, a slow
# disk, the CLI's own first read) is seconds, not milliseconds. Five was a guess
# at a warm machine and left the folder deriving as free for the rest of the
# start, so a second send could be admitted into it; twenty is longer than any
# start we have seen and still far shorter than a turn, so a reservation that is
# never claimed (the client navigated away, the spawn failed) costs at most one
# scheduler tick of delay. A run dir that has appeared but not named its session
# holds the folder on its own account (`holders`, kind "starting"), so this
# covers only the window before even that exists.
RESERVATION_TTL = 20.0

# How old an anonymous reservation must be before a send that can name NOTHING
# may claim it back (`_anonymous_self`). The case it separates is the one
# nothing else can: two brand-new chats hitting Enter in one folder within the
# same breath both admit anonymously, and the second sees a reservation a
# fraction of a second old — while a real second message is answering a reply,
# and the admission, the spawn, the turn and the reading of it cannot have
# happened in under a couple of seconds. So anything younger than one
# admit->spawn round trip reads as "somebody else is starting right now" and
# queues. Seconds rather than milliseconds because the round trip includes a
# process start; two is longer than any of those and shorter than any reply.
ANONYMOUS_CLAIM_AFTER = 2.0

# How long a live run that has not named a session counts as STARTING (see
# `_starting`). Generous against a cold `claude` start and short against a
# machine's lifetime: the number's only job is to stop a run dir whose pid was
# recycled from holding its folder for ever.
STARTING_GRACE = 120.0

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

# key -> (session id, monotonic expiry, run id, monotonic moment it was taken).
# In memory and per process on purpose: it describes a spawn THIS server just
# authorised, and a reservation that outlived the process that made it would be
# a lease, which is the thing this module exists not to have.
#
# THE RUN ID IS THERE FOR THE CHAT THAT HAS NO SESSION YET. A new chat's first
# message is admitted with `session_id: ""` — there is no session until Claude
# Code mints one — so the reservation it takes names nobody, and the run it
# spawns is anonymous too until something polls it. The chat DOES know the run
# it started, and it sends that id on its next admission; recording it here is
# what lets the second message be recognised as the same conversation and
# RENAME the reservation rather than be refused by it.
#
# AND THE MOMENT IT WAS TAKEN, which is the only clock that separates a chat's
# own earlier message from another chat that pressed Enter in the same breath:
# both admissions are nameless, and the one thing that differs is that the
# reservation a real second message claims back has had a whole round trip to
# age in (`ANONYMOUS_CLAIM_AFTER`).
_reservations: dict[str, tuple[str, float, str, float]] = {}
_res_lock = threading.Lock()

_AGENT_MOD = None
_AGENT_MOD_TRIED = False
_AGENT_MOD_LOCK = threading.Lock()

_GUARD: MountGuard | None = None

# An unparsable `due` sorts last rather than raising: the store is a JSON file a
# human may edit, and one bad timestamp must cost that entry its place in the
# line, never the whole ordering.
_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


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
    With no agent module there are no run holders, which is the same answer this
    gave before the feature existed.
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
    no name, `holders()` calls it `starting`, and the very chat that spawned it
    queues behind itself for `STARTING_GRACE` — the bug Akshil reported on
    2026-09-12 (second message in a new chat answered `#1 in line · behind a
    run in this folder`, and the scheduler then started a SECOND `claude
    --resume` beside the chat's own idle host).

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

    Best-effort throughout: `holders()` promises never to raise, and a registry
    that cannot be read is simply a run that has not named itself yet."""
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
    for the holders and for the parked runs, and paying two walks of the same
    120 directories for one answer is the cost this exists to stop. A new run
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

    Asked only by `holders()`, which is only reached under the flag. `queue_key`
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

    TWO READERS, ONE LISTING. `holders()` asks whether a run is parked and the
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
    """Forget the memoized walk, so the next `holders()` re-reads the tree.

    For a caller that has just DONE something the tree is about to show and
    cannot wait `SCAN_TTL` to hear about — the scheduler after it claims and
    spawns an entry, which is the one moment a folder goes from free to busy
    with nothing on disk saying so yet. Not needed for a reservation (those live
    in memory, outside the walk) and not needed for a new run dir (that changes
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


# -------------------------------------------------------------- the reservation


def reserve(key: str, session_id: str, ttl: float = RESERVATION_TTL,
            run_id: str = "") -> None:
    """Count `session_id` as `key`'s holder for the next `ttl` seconds.

    Called by an admission that answered "run": between that answer and the
    registry row appearing there is nothing on disk saying the folder is taken,
    and a second send arriving in that window would be admitted into it. See
    `RESERVATION_TTL` for why the window is short and why an unclaimed
    reservation is harmless.

    `run_id` is the run the CALLER already knows about — a chat sending its
    second message names the run its first one started. A reservation that
    carries one can be claimed back by that run even when the session on it is
    still `""`, which is the whole of the new-chat case (see `_reservations`)."""
    if not key:
        return
    with _res_lock:
        _reservations[key] = (str(session_id or ""),
                              time.monotonic() + max(0.0, ttl),
                              str(run_id or ""), time.monotonic())


def reserved(key: str) -> str:
    """The session holding an unexpired reservation on `key`, or `""`. Expired
    entries are dropped as they are passed — nothing else prunes this."""
    if not key:
        return ""
    with _res_lock:
        found = _reservations.get(key)
        if found is None:
            return ""
        session_id, expiry = found[0], found[1]
        if expiry <= time.monotonic():
            _reservations.pop(key, None)
            return ""
        return session_id


def reserved_run(key: str) -> str:
    """The run id on `key`'s unexpired reservation, or `""`."""
    if not key:
        return ""
    with _res_lock:
        found = _reservations.get(key)
        if found is None or found[1] <= time.monotonic():
            return ""
        return found[2]


def reserved_sessions() -> set:
    """Every session holding an unexpired reservation, anywhere.

    The listing's half of "instant running" (`tasks._running_now`). A send the
    server has just admitted is a turn that has started as far as the person who
    pressed Enter is concerned, and for the seconds before `claude` registers a
    session there is nothing on disk to say so — the row read `done` for 3.4 s
    after a send, which is the sidebar telling the user their message went
    nowhere. The reservation is the one record of that instant, it is already
    taken on the admission path, and it expires on its own
    (`RESERVATION_TTL`), so a spawn that never happened costs at most one row
    reading `in_progress` for twenty seconds — the same overshoot the folder
    gate already accepts, and the opposite of the failure it replaces.

    A reservation with no session names nobody and is left out: an empty id
    would match every task with no session on the machine at once.
    """
    return {session_id for session_id, _run in _live_reservations().values()
            if session_id}


def reserve_if_free(key: str, session_id: str, run_id: str = "",
                    now: float | None = None) -> bool:
    """Take `key`'s reservation for `session_id` if the folder is free — one
    decision, not a look followed by a write. True when the send may run.

    THE RACE THIS CLOSES IS REAL AND SMALL, and it is the one the reservation
    was invented for in the first place. Two chat sends into one free folder
    arriving on two request threads both ask `is_free`, both hear yes, and both
    reserve — the second write simply overwriting the first — and two `claude`
    processes start in one working tree, which is the single thing the feature
    exists to prevent. `is_free` + `reserve` cannot be made atomic by the
    caller; this can, so the caller stops trying.

    ONE LOCK, ONE READ. `holders()` is read once, OUTSIDE the lock — it is
    filesystem state, which no lock of ours makes atomic, and holding a mutex
    across 120 directory reads would serialise every send on the machine. What
    the lock covers is the reservation table: read, decided and written without
    a gap, which is where the two threads actually collide. The reserved holder
    the map may carry is re-decided from the table under the lock rather than
    trusted, because that is the half that can have moved since.

    A folder with no key is always free and nothing is stored for it. A folder
    this very session already holds answers True and refreshes the reservation:
    it is the inbox-absorb case, and the send is about to keep the run busy.

    **AND A FOLDER HELD BY THIS CHAT'S OWN RUN ANSWERS TRUE TOO, session or no
    session** (`run_id`). A new chat's first message is admitted with no session
    id at all, so the reservation it leaves names nobody and the run it spawns
    has no name either — and the second message, arriving with the session
    Claude Code has meanwhile minted, used to be told it was `#1 in line` behind
    its own process. The run id is the identity both ends can agree on before a
    session exists: it matches the holder's run, or the run recorded on the
    reservation, and either way this is one conversation and not two. The
    reservation is then REWRITTEN with whatever the caller now knows — that is
    how an anonymous reservation gets its session, and why a chat can claim back
    a reservation it made before it had a name.

    **AND A SEND THAT CAN NAME ITS SESSION CLAIMS BACK A NAMELESS RESERVATION
    ITS OWN DEAD RUN LEFT** (`_claims_back`): the Stop-then-send sequence, where
    the first message reserved anonymously, the user killed the host before the
    run ever became a holder, and the second message arrived with a session and
    no run to match it with.
    """
    if not key:
        return True
    sid = str(session_id or "")
    run = str(run_id or "")
    derived = holders(now)
    with _res_lock:
        _prune_reservations()
        found = _reservations.get(key)
        reserved_by = found[0] if found is not None else None
        reserved_run_id = found[2] if found is not None else ""
        # Read HERE, under the lock, and passed down: `_anonymous_self` must not
        # take this lock from inside a caller that already holds it.
        age = 0.0 if found is None else max(0.0, time.monotonic() - found[3])
        holder = derived.get(key)
        if holder is not None and holder["kind"] == "reserved":
            holder = None  # stale by construction; the table below is the truth
        if holder is None and reserved_by is not None:
            holder = {"session_id": reserved_by, "task_key": reserved_by,
                      "run_id": reserved_run_id, "kind": "reserved"}
        elif (holder is not None and reserved_by is not None
                and holder["kind"] == "starting" and not holder["session_id"]):
            # The reservation names the run that has not named itself — the two
            # describe one spawn from opposite ends. `_name_starting` does the
            # same for the map `holders` hands out.
            holder = dict(holder, session_id=reserved_by, task_key=reserved_by)
        if (holder is not None and not _self_held(holder, sid, run)
                and not _anonymous_self(key, holder, sid, run, now,
                                        reservation_age=age)
                and not _claims_back(key, holder, sid, now)):
            return False
        # The moment it was taken SURVIVES the rewrite. What a chat refreshing
        # its own reservation holds is one claim, not a new one, and restamping
        # it would make the next nameless message of that same conversation read
        # as a stranger arriving in the same breath.
        _reservations[key] = (sid, time.monotonic() + RESERVATION_TTL,
                              run or reserved_run_id,
                              found[3] if found is not None else time.monotonic())
        return True


def _self_held(holder: dict, session_id: str, run_id: str) -> bool:
    """Is this holder the very chat that is asking — by session, or by run?

    The one rule `is_free` and `reserve_if_free` share, and the run half of it
    is what a conversation with no session id yet has to identify itself with.
    A holder's `run_id` is the run in flight: a `run` or `starting` holder is a
    run dir and carries its own, and an anonymous `starting` or a `reserved`
    holder carries whatever the admission that authorised it knew
    (`_name_starting`). An equal run id is the same process, whatever either
    side calls the session.

    THE HOLDER'S OWN RUN ID AND NOTHING ELSE. Matching against the reservation
    table instead would let a chat that reserved a folder claim it back after a
    DIFFERENT run took it — the reservation is a claim on the folder, and once
    something real is holding it, the real thing is the only one that counts.
    """
    sid = str(session_id or "")
    if sid and sid in (holder.get("session_id"), holder.get("task_key")):
        return True
    run = str(run_id or "")
    return bool(run) and run == str(holder.get("run_id") or "")


def _anonymous_self(key: str, holder: dict, session_id: str, run_id: str,
                    now: float | None = None,
                    reservation_age: float | None = None) -> bool:
    """Is a request that can name NOTHING the chat that already owns this
    folder? Narrow on purpose, and every clause is one of the walls.

    THE WINDOW THIS CLOSES (Akshil's browser QA, 2026-09-12). A brand-new chat
    sends "hello", gets its reply, and the second message is admitted while the
    first turn is still tearing down — the process alive, the registry row a
    second or two behind. The client had not yet learned the session or the run
    (the frontend now sends both; this is the server's half of the same fix), so
    the admission named nobody, and the anonymous reservation the FIRST message
    left behind was the thing standing in its way: a claim on the folder made by
    this very chat, refusing this very chat because neither end had a name yet.

    TWO NAMELESS ENDS AND THEN THREE CONDITIONS, ALL OF THEM (bugbot HIGH,
    2026-09-12). "An unnamed holder plus any idle run in the folder" is true of
    almost every new conversation in a folder with history, so a second
    anonymous admission would steal the first chat's reservation and two
    processes would start in one working tree — the single thing this module
    exists to prevent.

    The two ends first: the REQUEST names nothing (a send carrying a session or
    a run has a real identity and is answered by `_self_held`; this is only for
    the one case that cannot be), and the HOLDER names nothing either.

    Then, of that nameless holder:

    1. **It is a `reserved` holder and nothing else.** A `run`, a `starting` or
       a `sending` holder is a process already editing that tree or one the tick
       has just claimed it for, and no amount of namelessness makes a send into
       it safe. Only a reservation — a claim, not a process — can be claimed
       back, and only by the conversation that made it.
    2. **The newest run keyed on this folder is QUIET and RECENT** — not
       running, and written to within `STARTING_GRACE` of now
       (`_recent_idle_run_in`). A run dir is the proof that a chat has already
       had a turn here, which is the only way a nameless send can be a SECOND
       message rather than a first; RECENT is what stops a folder's tail of dead
       run dirs from making that proof out of a conversation from last week. Two
       brand-new chats racing into a folder with history fail this clause, and
       the second queues.
    3. **The reservation has aged past one admit->spawn round trip**
       (`ANONYMOUS_CLAIM_AFTER`). Two anonymous admissions fired back-to-back
       both see a reservation a fraction of a second old; a real second message
       is answering a reply that had to be spawned, written and read first. This
       is the clause that tells apart the two cases the other two cannot.

    `reservation_age` is the seconds since that reservation was taken, passed in
    by a caller that holds `_res_lock` because this may not take it from under
    them; None means "read it now", which is what the lockless callers do.

    Nothing here is a lease and nothing is stored: the reservation still expires
    on its own, and the moment either end learns a name the ordinary
    `_self_held` rule takes over.
    """
    if session_id or run_id:
        return False
    if str(holder.get("kind") or "") != "reserved":
        return False
    if str(holder.get("session_id") or "") or str(holder.get("run_id") or ""):
        return False
    age = _reservation_age(key) if reservation_age is None else reservation_age
    if age < ANONYMOUS_CLAIM_AFTER:
        return False
    return _recent_idle_run_in(key, now)


def _claims_back(key: str, holder: dict, session_id: str,
                 now: float | None = None) -> bool:
    """May a send that CAN name its session take back a reservation that names
    nobody?

    THE STOP-THEN-SEND SEQUENCE (Akshil, 2026-09-12). A new chat's first
    message is admitted with no session and no run, so the reservation it
    leaves is anonymous. The user presses Stop before that run ever became a
    holder and the host is killed. The second message now DOES carry a session
    — the client read it off the URL — but still no run id, because the client
    never had one. `_self_held` needs a name on the holder's side and there is
    none; `_anonymous_self` needs the REQUEST to be nameless and this one is
    not. So the chat queued behind its own dead reservation, and the row read
    "next in this folder" about a folder nothing was in.

    The gate is the same wall `_anonymous_self` uses, read one clause tighter:

    1. **A `reserved` holder naming nobody, and nothing else.** A `run`, a
       `starting` or a `sending` holder is a process in that tree or one the
       tick has claimed it for, and no session id makes a second send into it
       safe. A reservation that HAS a session is answered by `_self_held` or
       not at all.
    2. **The newest run keyed on this folder is quiet and recent** — not
       running (`_live_session`, which believes the registry and therefore a
       killed process), and written to within `STARTING_GRACE`. The run dir is
       the proof that this conversation has already been in this folder;
       recency is what stops last week's corpse from proving it.
    3. **…and that run is this session's, or nobody's.** A quiet run naming a
       DIFFERENT session is somebody else's conversation, and the anonymous
       reservation standing in this folder is far more likely to be theirs. A
       run that named no session at all is the killed one from the sequence
       above — it never lived long enough to publish one — and that is the case
       this exists for.

    Nothing is stored and nothing is leased: the caller rewrites the
    reservation with the session it now knows, which is what stops the same
    question being asked twice."""
    if not session_id:
        return False
    if str(holder.get("kind") or "") != "reserved":
        return False
    if str(holder.get("session_id") or "") or str(holder.get("run_id") or ""):
        return False
    run = _newest_run_in(key)
    if run is None:
        return False
    now = time.time() if now is None else now
    if _live_session(run["sessions"], now):
        return False
    if not _starting(run["run_dir"], now):
        return False
    names = {s for s in run["sessions"] if s}
    return not names or session_id in names


def _newest_run_in(key: str, agent=None) -> dict | None:
    """The most recent run dir filed under `key`'s folder, or None.

    Newest and nothing older, because everything behind it is a previous
    conversation — `scan_runs` is newest-first, which is what makes this one
    pass with an early exit rather than a sort."""
    agent = agent or agent_module()
    if agent is None:
        return None
    for run in scan_runs(agent):
        if run_key(run) == key:
            return run
    return None


def _recent_idle_run_in(key: str, now: float | None = None) -> bool:
    """Does `key`'s folder hold a run of its own that is QUIET AND RECENT — a
    turn of this conversation's that ended moments ago, rather than a
    conversation from last week?

    The newest run filed under this folder decides and nothing older is looked
    at: everything behind it is a previous conversation. Two questions are asked
    of it, and the second is what bugbot's HIGH was about — a folder chatted in
    for weeks always has SOME idle run in it, so "idle" on its own proved
    nothing and let a stranger's nameless admission claim a reservation it had
    never made. The run dir's mtime is the clock, the same one `_starting`
    bounds a spawn by: a directory nothing has written to in minutes is not the
    other half of the message being admitted right now.

    Best-effort like the rest of the module: no agent module, no runs tree or no
    run in this folder all answer False, which is the answer that keeps the gate
    closed."""
    run = _newest_run_in(key)
    if run is None:
        return False
    now = time.time() if now is None else now
    if _live_session(run["sessions"], now):
        return False
    return _starting(run["run_dir"], now)


def _reservation_age(key: str) -> float:
    """Seconds since `key`'s unexpired reservation was taken, or 0.0 when there
    is none — which reads as brand new and therefore claims nothing."""
    if not key:
        return 0.0
    with _res_lock:
        found = _reservations.get(key)
        if found is None or found[1] <= time.monotonic():
            return 0.0
        return max(0.0, time.monotonic() - found[3])


def _prune_reservations() -> None:
    """Drop every expired reservation. Callers hold `_res_lock`."""
    now = time.monotonic()
    for key in [k for k, found in _reservations.items() if found[1] <= now]:
        _reservations.pop(key, None)


def _live_reservations() -> dict[str, tuple[str, str]]:
    """`{key: (session id, run id)}` for every unexpired reservation."""
    with _res_lock:
        _prune_reservations()
        return {k: (found[0], found[2]) for k, found in _reservations.items()}


# ------------------------------------------------------------------ the holders


def _live_session(sessions, now: float) -> str:
    """The first of a run's session ids that something is actually running, or
    `""`.

    The registry is asked first and is BELIEVED when it has an opinion — it
    knows the pid, so a `claude` that was killed is not live however fresh its
    transcript looks. `None` back from it means no process ever registered this
    session here (an older CLI, or a run whose row has not been written yet),
    and only then does the transcript tail get to decide.
    """
    for session_id in sorted(sessions):
        if not session_id:
            continue
        verdict = tasks_watch.live_from_registry(session_id)
        if verdict is not None:
            if verdict[0]:
                return session_id
            continue
        try:
            if session_liveness.session_running(session_id, now):
                return session_id
        except Exception:  # noqa: BLE001 — an unreadable transcript is not live
            continue
    return ""


def _sending_entries() -> list[dict]:
    """Scheduler entries the tick has CLAIMED but not yet spawned.

    A `sending` entry is a folder about to be busy: the claim happened, the
    process has not started, and for that instant nothing in the runs tree says
    so. Counting it is what stops one tick pass from dispatching two entries
    into one folder. Imported inside the function because `schedule.py` reads
    this module back — a module-level import either way closes the cycle."""
    try:
        from fused_render import schedule

        return [e for e in schedule.list_entries()
                if isinstance(e, dict) and e.get("state") == schedule.SENDING]
    except Exception:  # noqa: BLE001 — an unreadable schedule holds nothing
        logger.debug("could not read the schedule for sending entries",
                     exc_info=True)
        return []


def holders(now: float | None = None) -> dict[str, dict]:
    """`{queue_key: {"session_id", "run_id", "task_key", "kind"}}` — the one
    thing in flight in each busy folder, derived from scratch.

    Four kinds, in precedence order, and the order is the point:

    * ``"run"`` — a run dir that is ALIVE, whose session the registry (or, with
      no registry opinion, the transcript tail) says is mid-turn, and that is
      NOT parked on an unanswered card. All three conditions are load-bearing: a
      dead process holds nothing however recent its transcript, an idle session
      host holds nothing though its pid answers, and a run waiting on a
      permission card holds nothing because it may wait for hours.
    * ``"starting"`` — a run dir that is ALIVE and not parked but has not named
      a session yet. **A live process in that folder is exactly what the gate
      protects, id or not.** Cold-starting `claude` in a big tree takes seconds
      to write its session, and calling the folder free for those seconds is
      calling it free while a process is already editing it. It is named by
      whatever reservation covers the same folder, so the conversation that just
      spawned it is still free to send into its own run (the inbox-absorb case);
      with no reservation it is held by nobody, which no session can match and
      everything therefore queues behind.
    * ``"sending"`` — a scheduler entry the tick has claimed and not yet
      spawned (see `_sending_entries`). It fills an EMPTY slot only: a claim is
      a folder about to be busy and a live process is one that already is, so
      it never replaces a `run` or a `starting`.
    * ``"reserved"`` — an admitted chat send whose process has not appeared at
      all yet (see `reserve`). Folded in LAST and only where nothing real was
      found, so a stale reservation can never mask the truth.

    Never raises. Every read inside is best-effort and an unreadable run dir is
    skipped, because the callers are a chat send and a scheduler tick: the cost
    of an exception is a request that fails, and the cost of a missed holder is
    at worst a second task in a folder — which is what happened every day before
    this existed.
    """
    out = _derived_holders(now)
    for key, (session_id, run_id) in _live_reservations().items():
        if key in out:
            continue
        out[key] = {"session_id": session_id, "run_id": run_id,
                    "task_key": session_id, "kind": "reserved"}
    _name_starting(out)
    return out


def _derived_holders(now: float | None = None) -> dict[str, dict]:
    """`holders()` without the reservations — everything derived from disk.

    Split out so `reserve_if_free` can do the filesystem half outside its lock
    and the reservation half inside it, which is what makes admission one
    decision rather than a look followed by a write."""
    now = time.time() if now is None else now
    out: dict[str, dict] = {}
    agent = agent_module()
    if agent is not None:
        for run in scan_runs(agent):
            key = run_key(run)
            if not key or key in out:
                continue
            if not run_alive(agent, run["run_dir"]):
                continue
            if run_waiting(agent, run):
                continue
            session_id = _live_session(run["sessions"], now)
            if session_id:
                out[key] = {"session_id": session_id, "run_id": run["run_id"],
                            "task_key": session_id, "kind": "run"}
            elif not run["sessions"] and _starting(run["run_dir"], now):
                # STARTING: alive, not parked, and it has not said who it is.
                # Not the same as a run whose session is known and idle (that
                # one is above and holds nothing) — this one has announced
                # nothing at all, and the only honest reading of a live process
                # in a working tree is that the tree is taken.
                out[key] = {"session_id": "", "run_id": run["run_id"],
                            "task_key": "", "kind": "starting"}
    for entry in _sending_entries():
        key = queue_key(str(entry.get("target") or ""))
        # NEVER DOWNGRADE A LIVE PROCESS (round-3 review, 2026-09-12). A claim
        # is a folder about to be busy; a `run` or `starting` holder is one
        # that already is, and `sending` used to overwrite `starting` — so the
        # instant a spawned run appeared in a folder a claim also named, the
        # map stopped saying a process was in there. `sending` fills an empty
        # slot and nothing else.
        if not key or key in out:
            continue
        session_id = str(entry.get("claude_session_id")
                         or entry.get("session_id") or "")
        # A scheduled message with no session yet is a task all the same, keyed
        # the way the Tasks page keys it (§5) — `pending:<entry id>`.
        task_key = session_id or tasks_store.pending_key(str(entry.get("id") or ""))
        out[key] = {"session_id": session_id, "run_id": "",
                    "task_key": task_key, "kind": "sending"}
    return out


def _starting(run_dir: str, now: float) -> bool:
    """Is this unnamed run still plausibly STARTING — young enough that its
    silence is a spawn in progress rather than a corpse?

    The bound is what keeps the starting rule from being a lease. `_alive` is a
    pid probe, and a pid on a busy machine is eventually recycled: without a
    clock, one abandoned run dir whose pid was reissued to something unrelated
    would hold its folder for ever, and nothing could ever free it — precisely
    the failure mode this module was built not to have. `STARTING_GRACE` is
    minutes where a cold `claude` start is seconds, so the rule covers every
    real start and bounds every wrong one.

    The run dir's own mtime is the clock: it is written at spawn and touched
    again by every file the run adds, so a directory nothing has written to in
    minutes is not a process that is still getting going."""
    try:
        return (now - os.stat(run_dir).st_mtime) <= STARTING_GRACE
    except OSError:
        return False


def _name_starting(out: dict[str, dict]) -> None:
    """Give every anonymous "starting" holder the session that reserved its
    folder, where one did.

    The reservation and the starting run describe the same spawn from the two
    ends — the server authorised it, the run dir appeared — so the session on
    the reservation is the session of the run. Without this the conversation
    that just started a turn would queue behind its own process the moment the
    run dir beat the session file to disk."""
    anonymous = [key for key, holder in out.items()
                 if holder["kind"] == "starting" and not holder["session_id"]]
    if not anonymous:
        return
    reservations = _live_reservations()
    for key in anonymous:
        session_id, run_id = reservations.get(key, ("", ""))
        if session_id:
            out[key] = dict(out[key], session_id=session_id,
                            task_key=session_id)
        elif run_id and not out[key]["run_id"]:
            # A reservation with no session but a run id still says which run
            # this folder was authorised for — keep it on the holder so the
            # chat that named it can still recognise its own process.
            out[key] = dict(out[key], run_id=run_id)


def holder_expires_in(key: str, holder: dict | None,
                      now: float | None = None) -> float:
    """Seconds until this holder lapses ON ITS OWN CLOCK — 0.0 for one that
    ends on an event instead, or whose clock cannot be read.

    Two of the four kinds are grace periods and nothing rings when they run
    out: a `reserved` folder is an admitted send whose process has not appeared
    (the reservation's own TTL), and a `starting` one is a live process that has
    not named its session yet (`STARTING_GRACE` from the run dir's mtime, the
    same clock `_starting` decides on). A `run` holder ends when its turn ends,
    which `schedule._turn_ended` and the watcher both ring, and a `sending` one
    ends inside the pass that claimed it — both answer 0.0, meaning "do not set
    a timer for me".

    A DELTA AND NOT A DEADLINE, because the two clocks are not the same clock:
    the reservation table is `time.monotonic` and the run dir is wall time.
    Seconds-from-now is the only unit the caller can compare them in, and it is
    what `schedule._rearm` wants anyway.
    """
    kind = str((holder or {}).get("kind") or "")
    if kind == "reserved":
        with _res_lock:
            found = _reservations.get(str(key or ""))
        if found is None:
            return 0.0
        return max(0.0, found[1] - time.monotonic())
    if kind == "starting":
        agent = agent_module()
        run_id = str((holder or {}).get("run_id") or "")
        root = str(getattr(agent, "RUNS", "") or "") if agent is not None else ""
        if not (root and run_id):
            return 0.0
        try:
            mtime = os.stat(os.path.join(root, run_id)).st_mtime
        except OSError:
            return 0.0
        return max(0.0, mtime + STARTING_GRACE
                   - (time.time() if now is None else now))
    return 0.0


def holder_for(key: str, now: float | None = None) -> dict | None:
    """The one thing in flight in `key`'s folder, or None when it is free."""
    if not key:
        return None
    return holders(now).get(key)


def is_free(key: str, session_id: str, run_id: str = "",
            now: float | None = None) -> bool:
    """May this chat run in `key`'s folder right now?

    True when the folder is free AND when this very chat is the thing holding
    it — a second message into a conversation that is already running is the
    inbox-absorb case the chat has always had, and gating it would be this
    feature refusing a send that touches nothing new.

    **THE CHAT IS ITS SESSION OR ITS RUN, whichever it can name.** A brand-new
    chat has no session until Claude Code mints one, and the run it started is
    anonymous in the same window; asking only about the session made that chat
    queue behind its own first message (Akshil, 2026-09-12). `run_id` — the run
    the caller knows it started — matches the holder's own run whatever either
    side calls the session. A chat that can name NEITHER is the folder's own
    only where nothing but an anonymous reservation stands in it and a run of
    that folder's own has just gone quiet AND the reservation is old enough to
    be a round trip rather than a race (`_anonymous_self`); short of that it
    waits, which is what keeps two brand-new tasks out of one folder. A chat
    that can name its SESSION but not its run claims back a nameless
    reservation on the same wall (`_claims_back`) — the Stop-then-send case.
    """
    if not key:
        return True
    holder = holder_for(key, now)
    if holder is None:
        return True
    sid, run = str(session_id or ""), str(run_id or "")
    return (_self_held(holder, sid, run)
            or _anonymous_self(key, holder, sid, run, now,
                               reservation_age=_reservation_age(key))
            or _claims_back(key, holder, sid, now))


# -------------------------------------------------------------------- the order


def order_key(entry: dict, by_id: dict | None = None) -> tuple:
    """`(not priority, -priority_at, due, id)` — how a folder's line is
    ordered, everywhere.

    `priority` is what Run next sets, and it inverts because False sorts first:
    a promoted entry jumps the queue.

    **RUN NEXT IS "PLAY NEXT", NOT "MOVE TO THE BACK OF THE PROMOTED PILE"
    (Akshil, 2026-09-12).** Ordering the promoted entries by `due` made the
    FIRST thing promoted stay first: click B, then C, then D and the line ran
    B, C, D — the opposite of what the button says. A playlist's "play next"
    puts the newest choice immediately after what is playing and pushes the
    earlier choices down, so the second element is the moment the button was
    pressed (`schedule.set_priority` stamps `priority_at`), negated, which
    sorts the NEWEST promotion first: D, C, B. Pressing it again on B restamps
    B and B is first again — the same gesture, the same meaning, every time.

    An entry promoted before this field existed carries no stamp and reads
    0.0, which sorts it behind every stamped one and, among its own kind,
    back to the `due` order it already had.

    Ties fall to the older `due` — read through
    `_entry_due`, so a message the user pressed Run now on sorts by the moment
    they asked rather than by a due time that may be days away — and finally to
    the entry id, which is
    itself due-time-ordered — a total order with no coin flips, which is what
    lets a position number shown in the UI still be true on the next poll.

    **A FOLLOWER NEVER SORTS BEFORE ITS LEADER.** Given `by_id` (the store keyed
    by entry id), an entry carrying `follow_of` that would otherwise sort at or
    before the message it was typed behind takes the leader's key with a tail
    on it instead — so it lands immediately after the leader, and several
    followers of one leader keep their own order among themselves. Two messages
    typed into one chat are one conversation, and a conversation whose second
    turn could be sent first is not one.

    A follower already LATER than its leader keeps its own key, so the ordinary
    case (each message typed after the last, due when it was typed) is the
    ordinary sort. Without `by_id` no leader is looked up at all — the callers
    that rank one task's own entries against each other pass nothing, because a
    leader and its followers share a task and therefore share a slot.
    """
    own = _own_key(entry)
    if not by_id or not str(entry.get("follow_of") or ""):
        return own
    from fused_render import schedule

    # The TOPMOST leader, so a follower of a follower ranks against the head of
    # the chain rather than against a key that has already been rewritten —
    # which keeps this a plain comparison instead of a recursion.
    leader = schedule.leader_of(entry, by_id)
    if leader is None:
        return own
    lead = _own_key(leader)
    # `(*lead, 1, id)` is greater than `lead` and less than anything that sorts
    # after it — tuples of different lengths compare on their common prefix, and
    # the entry ids in it are unique, so the two can never tie.
    return own if own > lead else (*lead, 1, own[-1])


def _own_key(entry: dict) -> tuple:
    """One entry's own place in the line, before any leader is consulted."""
    promoted = bool(entry.get("priority"))
    return (not promoted,
            # NEGATED, so the most recent Run next sorts first. 0.0 for
            # everything that was never promoted — a constant, which is why the
            # ordinary line still falls straight through to `due`.
            -_priority_at(entry) if promoted else 0.0,
            _entry_due(entry),
            str(entry.get("id") or ""))


def _priority_at(entry: dict) -> float:
    """When Run next was last pressed on this entry, as epoch seconds — 0.0 for
    one promoted before the stamp existed, or carrying an unreadable one."""
    try:
        return max(0.0, float(entry.get("priority_at") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _entry_due(entry: dict) -> datetime:
    """The due time the LINE reads — `due`, or the moment Run now was pressed on
    this entry if that came first (`schedule._queue_due`).

    Run now never rewrites `due` (it is the record of what was asked for), so
    without this an entry the user asked for now would sort by a time days away
    and stand at the back of a line it was skipped to the head of."""
    from fused_render import schedule

    return schedule._queue_due(entry, _due_dt(entry.get("due")))


def _due_dt(value) -> datetime:
    try:
        from fused_render import schedule

        return schedule.parse_due(value)
    except Exception:  # noqa: BLE001 — one bad stamp costs that entry its place
        return _FAR_FUTURE


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
    `.res.json` under the perm directory when the answer is finally delivered
    (`validate_held_answers`, `_deliver_held_answers`). A store that accepted
    `../../x` would be a path traversal written by one request and walked by a
    scheduler tick minutes later, where nothing is left to say where it came
    from (round-2 review, 2026-09-12).
    """
    value = value if isinstance(value, str) else ""
    return not value or value.startswith(".") or any(c in value for c in "/\\:")


def _store_path() -> str:
    return os.path.join(tasks_store.STATE_DIR, HELD_ANSWERS_FILE)


def _validated(raw) -> dict | None:
    """One stored answer, or None if it is not one.

    Strict field by field, and silent. This file is read on a scheduler tick and
    on a server restart; a record that lost its `run_id` cannot be delivered to
    anything and a record whose `payload` came back as a string cannot be
    written as a decision, so both are dropped rather than carried forward to
    fail somewhere with no context.
    """
    if not isinstance(raw, dict):
        return None
    key = raw.get("queue_key")
    run_id = raw.get("run_id")
    request_id = raw.get("request_id")
    session_id = raw.get("session_id")
    payload = raw.get("payload")
    at = raw.get("at")
    if not (isinstance(key, str) and key):
        return None
    if not (isinstance(run_id, str) and run_id):
        return None
    if not (isinstance(request_id, str) and request_id):
        return None
    if not isinstance(session_id, str):
        return None
    if not isinstance(payload, dict):
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return {"queue_key": key, "session_id": session_id, "run_id": run_id,
            "request_id": request_id, "payload": payload, "at": float(at)}


def _answers(state) -> list[dict]:
    """The store's records, validated, in stored (oldest first) order. A missing
    file, a corrupt one, or one written by a version this code does not know all
    read as no answers held."""
    if not isinstance(state, dict) or state.get("version") != STORE_VERSION:
        return []
    raw = state.get("answers")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        record = _validated(item)
        if record is not None:
            out.append(record)
    return out


def _mutate(mutate, default):
    """Read-modify-write the store under an exclusive lock, or give up quietly.

    `tasks_store._update` is the lock: a sibling `.lock` file held for the whole
    read-modify-write (not just the write), so two windows answering two cards
    at once cannot persist a snapshot taken before the other's change. Reused
    rather than re-implemented because the store lives in that module's
    directory and a second locking convention over one directory is two.

    An unwritable state dir costs the write and nothing else — the caller is a
    user clicking Allow, and an exception there would lose a decision it could
    at least have delivered had the folder been free."""
    try:
        return tasks_store._update(HELD_ANSWERS_FILE, mutate)
    except OSError:
        logger.debug("could not write %s", _store_path(), exc_info=True)
        return default


def _replace(state: dict, answers: list[dict]) -> None:
    state.clear()
    state.update({"version": STORE_VERSION, "answers": answers})


def held_answers() -> list[dict]:
    """Every held answer, oldest first — by `at`, which is the place in the
    line and not the position in the file: a record the deliverer put back is
    appended at the end carrying the `at` it was given."""
    return sorted(_answers(tasks_store.load_state(HELD_ANSWERS_FILE)),
                  key=lambda a: a["at"])


def hold_answer(queue_key: str, session_id: str, run_id: str, request_id: str,
                payload: dict, at: float | None = None) -> dict | None:
    """Park one card decision until `queue_key`'s folder frees, and return the
    record that is now held — or None for a `run_id`/`request_id` that is not a
    name (`bad_id`), which is stored for nobody and delivered to nothing.

    **`at` IS THE PLACE IN THE LINE, so a record put back keeps the one it
    had** (round-3 review, 2026-09-12). The delivery takes every answer held for
    a folder at once and gives the folder to the session that has waited
    longest, putting the rest back — and stamping those with `time.time()` sent
    them to the back of a line they were already at the front of, once per pass,
    for as long as the other conversation kept the folder. The deliverer passes
    the record's own `at` and the order stands. Left None by the endpoint that
    holds an answer for the first time: for that one, now IS the place.

    **`payload` IS `{"raw": {...the `_decide` arguments...}}`, not the decision
    dict that gets written to the run's perm directory** (decided 2026-09-12,
    changing the design doc's first sketch; the deliverer therefore calls
    `agent._decide(**payload["raw"])` and NOT `agent._write_decision`). Two
    reasons, and the second is the one that matters. The first: building the
    written payload here means a second copy of every rule in `_decide` — the
    narrow-never-widen scope downgrade, the mode switch, the keep-planning
    message, the answer validation against the parked request's own questions —
    and a second copy is a copy that drifts. The second: those rules read the
    LIVE run (`_alive`, the request file on disk), and a held answer is made now
    and applied later, so a scope or a mode computed against today's card and
    written minutes afterwards would be a grant nobody re-checked. Deferring the
    whole call defers every one of those decisions to the moment the run actually
    gets the answer.

    Nothing else here cares what shape `payload` is — `_validated` asks only that
    it be a dict, and `validate_held_answers` writes `expired` for a dead run
    without reading it at all — so this is a contract between the endpoint that
    holds and the tick that delivers, and it is written down in both.

    FIRST WRITER WINS on `(run_id, request_id)`, the same latch
    `agent._write_decision` applies on disk: a double-click, or a cancel landing
    on a card the user just allowed, must not queue two answers to one question.
    The second call is answered with the record already held, which is what the
    card is showing anyway.
    """
    if bad_id(run_id) or bad_id(request_id):
        logger.warning("refusing to hold an answer for %r/%r", run_id, request_id)
        return None
    record = {"queue_key": str(queue_key or ""),
              "session_id": str(session_id or ""),
              "run_id": str(run_id or ""),
              "request_id": str(request_id or ""),
              "payload": payload if isinstance(payload, dict) else {},
              "at": time.time() if at is None else float(at)}

    def mutate(state):
        answers = _answers(state)
        for existing in answers:
            if (existing["run_id"] == record["run_id"]
                    and existing["request_id"] == record["request_id"]):
                return existing, False
        answers.append(record)
        _replace(state, answers)
        return record, True

    return _mutate(mutate, record)


def pop_held_answers(queue_key: str) -> list[dict]:
    """Take every answer held for `queue_key`, oldest first by `at`, and remove
    them.

    One call, because delivery is the moment the folder frees and a read
    followed by a delete would let a second tick see the same answers and write
    the same decisions twice."""
    key = str(queue_key or "")
    if not key:
        return []

    def mutate(state):
        answers = _answers(state)
        taken = sorted((a for a in answers if a["queue_key"] == key),
                       key=lambda a: a["at"])
        if not taken:
            return [], False
        _replace(state, [a for a in answers if a["queue_key"] != key])
        return taken, True

    return _mutate(mutate, [])


def drop_held_answer(run_id: str, request_id: str) -> bool:
    """Forget one held answer. True when there was one to forget."""
    run_id, request_id = str(run_id or ""), str(request_id or "")
    if not run_id or not request_id:
        return False

    def mutate(state):
        answers = _answers(state)
        kept = [a for a in answers
                if not (a["run_id"] == run_id and a["request_id"] == request_id)]
        if len(kept) == len(answers):
            return False, False
        _replace(state, kept)
        return True, True

    return _mutate(mutate, False)


def validate_held_answers(agent) -> list[dict]:
    """Drop every held answer whose run has died, writing each one out as
    `expired`. Returns what was dropped.

    Called on startup, and cheap enough to call whenever the store is about to
    be trusted. A held answer outliving its run is the ordinary case after a
    restart or a crash: the decision was made for a process that no longer
    exists, and delivering it to the run dir's perm directory as `expired` is
    exactly what `agent._decide` already does when it is handed a dead run — so
    a page that re-attaches to the old run sees the same latched verdict it
    would have seen had the user answered a second too late.

    The decision write is best-effort and the drop is not conditional on it: the
    run is dead either way, and an answer that cannot be written out is still an
    answer nothing will ever deliver.
    """
    if agent is None:
        return []
    dead: list[dict] = []
    for answer in held_answers():
        run_dir = os.path.join(str(getattr(agent, "RUNS", "")), answer["run_id"])
        if not run_alive(agent, run_dir):
            dead.append(answer)
    if not dead:
        return []
    for answer in dead:
        run_dir = os.path.join(str(getattr(agent, "RUNS", "")), answer["run_id"])
        try:
            agent._write_decision(agent._perm_dir(run_dir), answer["request_id"],
                                  {"decision": "expired"})
        except Exception:  # noqa: BLE001 — a corpse we cannot mark is still one
            logger.debug("could not expire held answer %s/%s",
                         answer["run_id"], answer["request_id"], exc_info=True)
    gone = {(a["run_id"], a["request_id"]) for a in dead}

    def mutate(state):
        answers = _answers(state)
        kept = [a for a in answers if (a["run_id"], a["request_id"]) not in gone]
        if len(kept) == len(answers):
            return None, False
        _replace(state, kept)
        return None, True

    _mutate(mutate, None)
    return dead


# -------------------------------------------------------------------- for tests


def reset_cache() -> None:
    """Forget every memo: folder keys, the agent module, the mount guard, the
    walk of the runs tree, and the live reservations. For tests, which move
    `$HOME`, plant repos and redirect the runs tree between cases — all facts
    this module is entitled to believe never change inside one process."""
    global _AGENT_MOD, _AGENT_MOD_TRIED, _GUARD
    _KEY_CACHE.clear()
    invalidate_holders()
    with _res_lock:
        _reservations.clear()
    with _AGENT_MOD_LOCK:
        _AGENT_MOD = None
        _AGENT_MOD_TRIED = False
    _GUARD = None
