"""Programmatic triggers for the workflow canvas: a workflow that runs when
something HAPPENS, rather than when somebody clicks Run (SPEC §46.1).

§46's canvas has one entry point, and it is a person: the click on Run is the
whole approval model (WC-4a), and it is what authorizes the `--allowed-tools`
list the detached session gets. A trigger deletes that click. So this module is
not "schedule.py for workflows" — the loop is the small part. The part that
matters is **what replaces the click**, and the answer is here in three pieces:

1. **Arming.** A person ARMS a workflow once, and what they are shown at that
   moment is THE EXACT TOOL LIST the future runs will be allowed to call. That
   list is the approval, so it is stored — as a fingerprint — beside the
   permission it grants.
2. **A fingerprint check before every unattended run.** The document is a file
   the user edits. Adding a `send_mail` node to an armed workflow must not
   silently buy `send_mail` the authorization a person gave to
   `list_accounts` + `search_mail`. Every run recompiles the document, and a
   tool set that does not match what was armed **refuses, disarms, and says
   why**. Re-arming is a person looking at the new list.
3. **Two automatic brakes**, because "nobody is watching" is the premise.
   A **rate cap** (runs per hour) bounds a trigger that turns out to fire far
   more often than its author expected, and **disarm-on-repeated-error** stops a
   workflow that has failed N times in a row from failing a thousand more.

**Why this is not an entry in schedule.py.** `schedule.py` already owns
unattended Claude work and this module deliberately reuses everything of it that
transfers: `cron.py` and `recur.py` answer "when is the next one", the
in-process-firing argument (its module docstring: `child_environment`, macOS
TCC) applies here unchanged, and the coalescing rule — a backlog collapses to ONE
run, never a replay — is the rule below too. What does not transfer is the ENTRY.
A `schedule.py` entry is *send this prompt to this target*: it resumes a session,
its concurrency unit is a transcript, and its permission story is
`_SCHEDULED_PERMISSION_MODE = "auto"` — the CLI's own classifier deciding what an
unattended turn may do. A workflow run is none of those. It spawns a fresh
headless session with **no permission mode at all** and an explicit
`--allowed-tools` list, which is exactly why arming can be a meaningful approval:
the authorization is a finite, showable set of tool names rather than a policy.
Modelling it as a schedule entry would have meant a second `target` kind, a
second permission story, a second concurrency unit and a second history shape
inside a module whose docstring is already about one thing. So: shared *timing*
libraries, shared *reasoning*, separate store and separate loop — stated here
rather than left to be discovered.

**The runner is the template's own `run.py`, invoked exactly as the panel does.**
Core does not re-implement the compile or the spawn: it calls
`executor.run_python(<workflow template>/run.py, ...)`, the same file over the
same seam that `fused.runPython("./run.py", …)` reaches. The template still
imports nothing from `fused_render` (SPEC PY-15 / D166) — the dependency points
this way only, which is why the firing loop lives in this file and not beside
`run.py`.

**One run at a time per workflow.** Further events queue, bounded; past the bound
the OLDEST is dropped and counted, so a wedged workflow degrades into "you missed
N events" rather than into unbounded memory. A workflow never runs concurrently
with itself, whoever started the runs.

No import of anything under `fused_render.server` at module level — the router
imports this module; keep it acyclic.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from fused_render import cron, jobs, recur
from fused_render.shell import storage

logger = logging.getLogger(__name__)

# The store. Branch-aware via storage.home_dir() like every other durable store
# here, so a dev checkout never arms the baseline install's workflows.
_STORE_NAME = "workflow_triggers.json"

# How often the loop looks. Deliberately the same cadence as `schedule.py`'s:
# a schedule trigger is a minute-granularity promise, and the file sweep is a
# directory listing, so this is chosen for "fires close enough" rather than for
# latency. It is also why the file trigger is a SWEEP and not an OS watcher —
# see `_sweep`.
POLL_INTERVAL_S = 30

# The two trigger kinds. A closed set: each one is a different question asked of
# the tick, and a third would be a third `_due_*` function, not a config value.
KIND_SCHEDULE = "schedule"
KIND_FILE = "file"
KINDS = (KIND_SCHEDULE, KIND_FILE)

# Where a run came from, recorded on every run and carried into the history so a
# person reading it can tell an unattended run from one they started themselves.
SOURCE_MANUAL = "manual"

# How many pending events one workflow may hold.
#
# A BOUND WITH A COUNTER, not an unbounded list and not a silent drop. The
# failure this is for is a file trigger pointed at a folder that turns out to
# receive a thousand files an hour, or a cron line finer than the runs it starts:
# the queue then grows for as long as the app is open. Past the bound the OLDEST
# event goes — the newest file is the one still worth acting on — and `dropped`
# counts what went, so the surface says "42 events dropped" instead of quietly
# being wrong about what happened.
QUEUE_MAX = 20

# The rate cap's default, per workflow, per rolling hour. A workflow run is a
# whole Claude session with real tools behind it; twelve an hour is roughly "one
# every five minutes forever" and is a ceiling, not a target. Configurable per
# workflow at arm time.
DEFAULT_RUNS_PER_HOUR = 12
_RATE_WINDOW_S = 3600

# Consecutive failed runs before the workflow disarms itself. CONSECUTIVE, and
# reset by any success: a workflow that fails once a day because a mail server
# blipped is not the failure this is for. The failure this is for is a workflow
# that is now broken — a tool that always errors, an app folder that moved — and
# will go on being broken every time it fires until somebody looks.
DEFAULT_ERROR_LIMIT = 3

# How recently a file may have been written and still be skipped by the sweep.
#
# A file being COPIED IN is visible to `scandir` long before it is complete, and
# firing on it hands the run a half-written file. One tick's grace costs at most
# one tick of latency and removes the whole class. It is not a guarantee (nothing
# short of the writer telling us is), which is why it is small and not sold as
# one.
SETTLE_S = 2.0

# Bounds on one sweep of one watched folder, per tick. The sweep runs on the tick
# thread, which also fires everything else.
SWEEP_MAX_FILES = 5000
SWEEP_MAX_DEPTH = 8

# How many processed-file markers one workflow keeps. This is THE idempotency
# record — it is what makes "the same file never triggers twice" survive a
# restart — so it is durable, and therefore has to be bounded. Pruned oldest
# first (dicts keep insertion order), which is the right end: the oldest marker
# is the file least likely to be rewritten.
SEEN_MAX = 20000

# Runs kept in a workflow's history. The run dirs themselves are `run.py`'s
# business and outlive nothing; this is the index into them.
RUNS_KEPT = 50

# How long a `current` run may sit unfinished before the tick abandons it. Past
# this the workflow is unblocked rather than wedged forever — the process is gone
# and its run dir may have been swept out of the temp root with it. Abandoning is
# NOT a failure for the error counter's purposes: "we lost track of it" is not
# evidence the workflow is broken.
_RUN_MAX_AGE_S = 6 * 3600

# Timeout for one call into the template's `run.py`. `start` spawns a detached
# process and returns; `plan` and `poll` are file reads. None of them is slow,
# and a hang here would hang the tick thread.
_RUNNER_TIMEOUT_S = 120

# How long a claim whose SPAWN OUTCOME IS UNKNOWN is held before it is released.
#
# `run.py::_start` spawns with `start_new_session`, so the `claude` process
# outlives its parent. If the executor call is killed after that `Popen`
# succeeded — a timeout, a crash — the run is alive and the answer never came
# back. Treating that as "the run failed" cleared the claim and the next tick
# started a SECOND session alongside the first, which is exactly what WC-13
# forbids. So core names the run before it asks for it (`_new_run_id`), and a
# start whose outcome is unknown keeps its claim for at least one further tick,
# during which `poll` gets to answer for the id we chose. Past this, with the
# run dir still absent, the spawn provably never happened and the claim goes.
_SPAWN_GRACE_S = 2 * POLL_INTERVAL_S

# Bounded event ring, exactly the shape `schedule.py` established: a running
# narration for the shell to toast, never the record. The store is the record.
_EVENTS_MAX = 100
EVENT_RAN = "ran"
EVENT_FAILED = "failed"
EVENT_DISARMED = "disarmed"
EVENT_REFUSED = "refused"
EVENT_KINDS = (EVENT_RAN, EVENT_FAILED, EVENT_DISARMED, EVENT_REFUSED)

# `sys:` marks a job this process owns, which is what lets the manager's ✕ be a
# real cancel (jobs.OWNER_SERVER). One id per WORKFLOW — keyed off the document
# path and NOT off the fingerprint, which is a fact about the TOOL SET: two
# workflows built over the same two tools would otherwise share one row, so the
# second's report would overwrite the first's, the manager's ✕ would cancel
# whichever happened to be displayed, and one document's completion would be
# attributed to the other. Hashed, because a path is neither bounded nor in the
# id charset.
_JOB_PREFIX = "sys:workflow:"

# Names a watched folder produces that are never the arrival anybody meant.
# Editor swap files, half-finished downloads, lock files. Dotfiles are skipped
# wholesale below, which covers `.DS_Store`, `.#foo` and `.foo.swp` in one rule.
_NOISE_SUFFIXES = (".swp", ".swx", ".swo", ".tmp", ".temp", ".part", ".partial",
                   ".crdownload", ".download", ".lock", ".bak", "~")
_NOISE_PREFIXES = ("~$",)

_events: list[dict] = []
_event_seq = 0
_delivered = 0
_events_lock = threading.Lock()

# Serialises the read-modify-write of the store. `storage.write_json` is atomic
# per write, but the store is read-modify-written from the loop thread and from
# request threads, and last-write-wins across those would lose a disarm.
_lock = threading.RLock()

_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


# ---------------------------------------------------------------- primitives


def store_path() -> str:
    return os.path.join(storage.home_dir(), _STORE_NAME)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def _refuse(reason: str, message: str) -> dict:
    return {"ok": False, "reason": reason, "message": message}


def _ok(**extra) -> dict:
    out = {"ok": True}
    out.update(extra)
    return out


def _job_id(wf: dict) -> str:
    """The job-registry id for a workflow: `sys:workflow:<hash of its path>`."""
    key = key_for(wf.get("path") or "")
    return _JOB_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def key_for(path: str) -> str:
    """The store key for a workflow document: its resolved, case-normalized path.

    RESOLVED, so `~/w/x.workflow.json` and a symlink to it are one workflow and
    not two — arming through one name and firing through the other would be two
    approvals for one file, which is exactly the confusion this feature must not
    have.
    """
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except OSError:
        return os.path.normcase(os.path.abspath(str(path)))


def authorization_of(compiled: dict) -> dict:
    """`{tools, servers}` out of a `plan` (or `start`) payload.

    Both halves, because a tool NAME is only half a tool.
    `run.py::_server_names` assigns `mail`, `mail-2`, … over the graph's app
    folders IN NODE ORDER, so two folders whose basenames collide
    (`~/showcase/mail` and `~/work/mail`) swap names when the nodes are
    reordered — and the tool-name set is then byte-identical across a document
    that now reaches the other account. The server map is what pins which
    folder is behind each name.
    """
    return {
        "tools": sorted({str(t) for t in (compiled.get("tools") or [])}),
        "servers": {str(k): str(v)
                    for k, v in (compiled.get("servers") or {}).items()},
    }


def fingerprint(tools, servers=None) -> str:
    """The fingerprint of an authorization.

    Over the SORTED, DEDUPED tool names AND the server map, so it is a fact
    about what a run may reach rather than about the order a compile happened to
    produce: a reordered graph is not a new approval, but a graph whose `mail`
    server now points at a different folder IS one. `\\0`-joined, with the two
    sections split by a marker, so no two different authorizations can splice
    into one string.

    `tools` may be a whole authorization dict (`{tools, servers}`), which is how
    every caller inside this module passes it; the two-argument form is what the
    tests and a caller holding only names use.
    """
    if isinstance(tools, dict) and servers is None:
        tools, servers = tools.get("tools"), tools.get("servers")
    names = sorted({str(t) for t in (tools or [])})
    pairs = sorted("%s=%s" % (k, v) for k, v in (servers or {}).items())
    blob = "\0".join(names) + "\0\x01servers\x01\0" + "\0".join(pairs)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return "sha256:" + digest[:32]


# -------------------------------------------------------------------- events


def _emit(kind: str, wf: dict, detail: str = "") -> None:
    global _event_seq
    with _events_lock:
        _event_seq += 1
        _events.append({
            "id": _event_seq,
            "kind": kind if kind in EVENT_KINDS else EVENT_FAILED,
            "path": wf.get("path", ""),
            "name": wf.get("name", "") or os.path.basename(wf.get("path", "")),
            "detail": str(detail)[:400],
            "at": _iso(_now()),
        })
        del _events[:-_EVENTS_MAX]


def event_log() -> list[dict]:
    with _events_lock:
        return list(_events)


def undelivered_events() -> list[dict]:
    with _events_lock:
        return [e for e in _events if e["id"] > _delivered]


def ack_events(event_id: int) -> int:
    global _delivered
    with _events_lock:
        try:
            _delivered = max(_delivered, int(event_id))
        except (TypeError, ValueError):
            pass
        return _delivered


# --------------------------------------------------------------- the store


def _row_sig(wf: dict) -> str:
    """A cheap, order-stable digest of one workflow row.

    Used to decide whether a tick needs to write at all. Hashing one row is far
    less work than re-serialising every row and atomically replacing the file,
    which is what an unconditional write costs — and `seen` alone can hold
    SEEN_MAX markers per workflow.
    """
    try:
        blob = json.dumps(wf, sort_keys=True, default=str)
    except (TypeError, ValueError):
        # Unserialisable means "assume changed": the write below is what would
        # have raised anyway, and it will say so properly.
        return ""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read() -> dict:
    data = storage.read_json(store_path())
    if not isinstance(data, dict):
        return {}
    flows = data.get("workflows")
    return flows if isinstance(flows, dict) else {}


def _write(flows: dict) -> None:
    storage.write_json(store_path(), {"version": 1, "workflows": flows})


def reset() -> None:
    """Drop the in-memory narration. Tests only; the store is a file."""
    global _event_seq, _delivered
    with _events_lock:
        _events.clear()
        _event_seq = 0
        _delivered = 0


# -------------------------------------------------------------- the runner


def _runner_path() -> str:
    """Absolute path to the workflow template's `run.py`, or `""`.

    Resolved through the SAME name resolution the server uses for
    `fused.runPython("./run.py")`, so a user override at
    `~/.fused-render/templates/workflow/` is honoured here exactly as it is in
    the panel — a machine where the panel runs one runner and the trigger loop
    runs another would be a machine where arming approves the wrong tools.

    Imported inside the function on purpose: this module must not import
    anything under `fused_render.server` at module level (the router imports
    this module).
    """
    from fused_render.server import templates as _templates

    resolved, _err = _templates._resolve_name("workflow")
    if not resolved:
        return ""
    runner = os.path.join(os.path.dirname(resolved), "run.py")
    return runner if os.path.isfile(runner) else ""


def _template_run(params: dict) -> dict:
    """Call the workflow template's `run.py` and return ITS payload.

    The executor's envelope (`ok`/`error`/`stdout`) is unwrapped here, because
    every caller below wants `run.py`'s own refusal vocabulary — and an executor
    failure is translated INTO that vocabulary rather than leaked as a
    traceback, so there is one shape to handle.

    This is the seam the tests drive; nothing else in this module spawns.
    """
    from fused_render.executor import run_python

    runner = _runner_path()
    if not runner:
        return _refuse(
            "no_runner",
            "The workflow template is not installed on this machine, so a "
            "workflow cannot be run from here.")
    try:
        out = run_python(runner, dict(params), timeout=_RUNNER_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — the contract here is a payload
        return _refuse("runner_failed", "%s: %s" % (type(exc).__name__, exc))
    if not out.get("ok"):
        err = out.get("error") or {}
        return _refuse(
            "runner_failed",
            "%s: %s" % (err.get("type", "Error"), err.get("message", "")))
    result = out.get("result")
    if not isinstance(result, dict):
        return _refuse("runner_failed",
                       "the workflow runner returned no result payload.")
    return result


def plan(path: str) -> dict:
    """What arming a document would authorize: `{ok, name, tools, steps, …}`."""
    return _template_run({"action": "plan", "path": str(path)})


# --------------------------------------------------------- trigger validation


def _clean_triggers(raw) -> tuple[list, str]:
    """`(triggers, error)` — the submitted trigger list, validated.

    Validated HERE and not at the router, because the store is what the loop
    reads and a trigger the loop cannot evaluate is a trigger that silently does
    nothing. A cron line is PARSED (not merely stored) for the same reason
    `schedule.create` parses one: the moment to tell somebody their expression is
    wrong is while they are looking at it.
    """
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return [], "triggers must be a list"
    if not raw:
        return [], "arming a workflow with no triggers would arm it for nothing"
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], "trigger %d is not an object" % (i + 1)
        kind = str(item.get("kind") or "")
        if kind not in KINDS:
            return [], ("trigger %d has kind %r; the kinds are %s"
                        % (i + 1, kind, " and ".join(KINDS)))
        # Annotated, because the literal below infers `dict[str, str]` and the
        # branches then assign a rule dict and a bool into it. No runtime
        # consequence — this is a type error only — but a wrong annotation on
        # the structure the loop reads is a bad thing to leave lying around.
        clean: dict[str, object] = {
            "id": str(item.get("id") or "t%d" % (i + 1))[:64], "kind": kind,
            "label": str(item.get("label") or "")[:200]}
        if kind == KIND_SCHEDULE:
            expr = str(item.get("cron") or "").strip()
            rule = item.get("rule")
            if expr:
                try:
                    cron.parse(expr)
                except ValueError as exc:
                    return [], "trigger %d: %s" % (i + 1, exc)
                clean["cron"] = expr
            elif isinstance(rule, dict):
                try:
                    checked = recur.validate_rule(rule)
                except ValueError as exc:
                    return [], "trigger %d: %s" % (i + 1, exc)
                clean["rule"] = checked
                # `count` is THE STORE'S TO ENFORCE — `recur`'s own docstring
                # says so, and `schedule.py` enforces it against its `made`
                # tally. So this one does too, in `_evaluate`, and the count is
                # surfaced here so a reader of the store can see the bound
                # without re-deriving it. Left unenforced, a rule reading
                # "3 times" fired daily forever, which is the worst kind of
                # wrong for an unattended feature: it looks configured.
                if checked.get("count"):
                    clean["count"] = int(checked["count"])
                anchor = str(item.get("anchor") or "")
                clean["anchor"] = anchor
            else:
                return [], ("trigger %d is a schedule with neither a `cron` line "
                            "nor a `rule`" % (i + 1))
        else:
            folder = str(item.get("folder") or "")
            if not folder or not os.path.isdir(folder):
                return [], ("trigger %d watches %r, which is not a folder"
                            % (i + 1, folder))
            clean["folder"] = os.path.abspath(folder)
            clean["match"] = str(item.get("match") or "*")[:200]
            clean["recursive"] = bool(item.get("recursive"))
        out.append(clean)
    ids = [t["id"] for t in out]
    if len(set(ids)) != len(ids):
        return [], "two triggers share an id"
    return out, ""


# ----------------------------------------------------------- schedule timing


def _local(when: datetime) -> datetime:
    """A UTC instant as the naive local wall clock `cron`/`recur` work in."""
    return when.astimezone().replace(tzinfo=None)


def _from_local(when: datetime) -> datetime:
    """A naive local wall-clock time back as a UTC instant."""
    if when.tzinfo is not None:
        return when.astimezone(timezone.utc)
    return when.astimezone().astimezone(timezone.utc)


def _next_due(trigger: dict, after: datetime) -> datetime | None:
    """The next firing instant (UTC) of a schedule trigger strictly after `after`.

    All the arithmetic is `cron.py`'s and `recur.py`'s — this is the adapter, and
    the zone handling is theirs too: both work in NAIVE LOCAL time because "daily
    at 9am" is a promise about the reader's wall clock, and the instant is
    attached at this edge exactly as `schedule.py` does it.
    """
    local_after = _local(after)
    expr = trigger.get("cron")
    if expr:
        try:
            return _from_local(cron.parse(str(expr)).next_after(local_after))
        except ValueError:
            return None
    rule = trigger.get("rule")
    if isinstance(rule, dict):
        anchor = trigger.get("anchor") or ""
        try:
            base = datetime.fromisoformat(str(anchor)) if anchor else local_after
        except ValueError:
            base = local_after
        if base.tzinfo is not None:
            base = _local(base)
        try:
            nxt = recur.next_occurrence(rule, base, local_after)
        except (ValueError, TypeError):
            return None
        return _from_local(nxt) if nxt else None
    return None


# ------------------------------------------------------------- the file sweep


def _noisy(name: str) -> bool:
    """Whether a filename is the kind a watched folder produces by accident.

    Dotfiles wholesale, because that is `.DS_Store`, `.#foo` (emacs), `.foo.swp`
    (vim) and `.goutputstream-xxxx` (gio) in one rule and none of them is ever
    the arrival somebody meant. Then the half-written suffixes, which are the
    same idea for names that are not dotted.
    """
    lower = name.lower()
    if name.startswith("."):
        return True
    if any(lower.endswith(s) for s in _NOISE_SUFFIXES):
        return True
    return any(name.startswith(p) for p in _NOISE_PREFIXES)


def _ignore_rules():
    """The index's compiled ignore list, or `None` if it cannot be built.

    The INDEX's list, deliberately: a watched folder is a folder on this machine
    and the user has already said, once, which folders on this machine are noise.
    `None` (an unreadable config) means the name rules below still apply — a
    missing ignore list must not stop the sweep, only widen it.
    """
    try:
        from fused_render.index.config import load_config

        return load_config().rules
    except Exception:  # noqa: BLE001 — best-effort; the name rules stand
        logger.debug("workflow trigger sweep: no index ignore rules", exc_info=True)
        return None


def _sweep(trigger: dict, seen: dict, now_ts: float) -> list[dict]:
    """New or changed files under a watched folder, as trigger payloads.

    **A SWEEP, not an OS watcher**, and that is a decision rather than a
    shortcut. `index/fsevents.py` is macOS-only and best-effort by construction
    (it returns `None` off darwin and on any doubt) because its job is to make a
    rescan cheaper, and a missed journal entry there costs a little work. Here a
    missed entry costs a run that never happened, unattended, with nobody to
    notice. A directory listing on the tick cadence is the same answer on every
    platform, and it is cheap: this is a drop folder, not a home directory.

    **Idempotency is the marker, and the marker is durable.** A file is
    identified by `(mtime_ns, size)`; a path whose marker equals the stored one
    is not an arrival. The map lives in the store, so a server restart does not
    re-fire a folder full of files — which is the failure that would make this
    feature unusable rather than merely annoying.

    `SETTLE_S` keeps a file that is still being written out of this tick's
    answer, and the ignore rules keep `.git` internals and editor swap files out
    of every tick's.
    """
    folder = str(trigger.get("folder") or "")
    if not os.path.isdir(folder):
        return []
    pattern = str(trigger.get("match") or "*")
    recursive = bool(trigger.get("recursive"))
    rules = _ignore_rules()
    from fused_render.index import ignore as _ignore

    found: list[dict] = []
    budget = SWEEP_MAX_FILES
    stack = [(folder, 0)]
    while stack and budget > 0:
        current, depth = stack.pop()
        try:
            # SORTED, so the sweep is deterministic. `scandir` yields in
            # filesystem order, which meant that when arrivals outnumbered the
            # budget, WHICH ones a tick processed was a coin toss — and a test
            # for the budget could pass or fail on the same code. Sorting costs
            # nothing on a drop folder and makes "the first N by name" a
            # statable rule; the rest are picked up on the next tick.
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if budget <= 0:
                break
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if not recursive or depth + 1 > SWEEP_MAX_DEPTH:
                    continue
                norm = _ignore.norm(entry.path)
                if entry.name in _ignore.SKIP_DIRS or entry.name.startswith("."):
                    continue
                if _ignore.is_leaf_dir(norm):
                    continue
                if rules is not None and _ignore.ignored_for_index(
                        rules, norm, tree=False):
                    continue
                stack.append((entry.path, depth + 1))
                continue
            if _noisy(entry.name):
                continue
            if not fnmatch.fnmatch(entry.name, pattern):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not st.st_size:
                # ZERO BYTES IS NEVER AN ARRIVAL, however old the file is.
                #
                # This was a settle-window test, which made it dead code — the
                # window test below already covered every case it did — while
                # leaving the case it was written for wide open: a writer that
                # creates a file and then stalls (a slow copy, a crashed
                # exporter, a bare `touch`) leaves zero bytes behind, and two
                # seconds later a full unattended run started on an empty file.
                # Size only, and stated as a rule rather than as a delay: a
                # workflow triggered by a file wants the file, and an empty one
                # is a placeholder for a file that has not arrived yet. It fires
                # the moment there are bytes in it, because that is a changed
                # marker like any other.
                continue
            if now_ts - st.st_mtime < SETTLE_S:
                continue
            path = os.path.abspath(entry.path)
            marker = "%d:%d" % (st.st_mtime_ns, st.st_size)
            if seen.get(path) == marker:
                continue
            # DECREMENTED FOR AN ARRIVAL, NOT FOR A LOOK. Spending the budget
            # above this check meant a folder holding SWEEP_MAX_FILES already
            # processed files exhausted it on known names every tick — and a new
            # file landing behind them was not delayed, it was never swept at
            # all. The cap exists to bound the work an arrival costs.
            budget -= 1
            seen[path] = marker
            found.append({
                "path": path,
                "name": entry.name,
                "dir": os.path.dirname(path),
                "ext": os.path.splitext(entry.name)[1].lstrip(".").lower(),
                "size": st.st_size,
                "mtime": _iso(datetime.fromtimestamp(st.st_mtime, timezone.utc)),
            })
    # Prune oldest-first. Dicts keep insertion order, and the oldest marker is
    # the file least likely to be rewritten — the right end to lose.
    excess = len(seen) - SEEN_MAX
    if excess > 0:
        for path in list(seen)[:excess]:
            del seen[path]
    return found


def _seed_seen(triggers: list, seen: dict, now_ts: float) -> None:
    """Record every file already in every watched folder, WITHOUT firing.

    Arming must not be an event. A folder with four hundred files in it is four
    hundred runs' worth of queue-and-drop the moment somebody arms a workflow
    against it, and none of those files ARRIVED — they were already there. So the
    first sweep's results are thrown away and only its markers are kept.
    """
    for trigger in triggers:
        if trigger.get("kind") == KIND_FILE:
            _sweep(trigger, seen, now_ts)


# ---------------------------------------------------------------- projection


def _blank(path: str) -> dict:
    return {
        "path": os.path.abspath(path),
        "name": "",
        "armed": False,
        "armed_at": "",
        "tools": [],
        # The whole approval as compared data, beside the fingerprint that
        # summarises it: `{tools, servers}`. This is what travels to `run.py`
        # on every unattended start, and the fingerprint is what the surfaces
        # show.
        "authorization": {"tools": [], "servers": {}},
        "fingerprint": "",
        # Bumped by every arm and every disarm. A claim records the generation
        # it was made under, so work decided before a revocation cannot land
        # after it — see `_settle`.
        "generation": 0,
        "needs_rearm": False,
        "needs_rearm_reason": "",
        "triggers": [],
        "max_runs_per_hour": DEFAULT_RUNS_PER_HOUR,
        "error_limit": DEFAULT_ERROR_LIMIT,
        "consecutive_errors": 0,
        "model": "",
        "queue": [],
        "dropped": 0,
        "current": None,
        "runs": [],
        "seen": {},
        "next_due": {},
        # Occurrences fired per schedule trigger, for a rule carrying `count`.
        "made": {},
        "rate_window": [],
    }


def get(path: str) -> dict | None:
    """One workflow's stored state, or `None`. A COPY — callers may not mutate."""
    with _lock:
        wf = _read().get(key_for(path))
    return json.loads(json.dumps(wf)) if wf else None


def list_workflows() -> list[dict]:
    """Every workflow this machine has ever armed, armed ones first.

    Disarmed ones are KEPT rather than deleted, and that is the point of the
    listing: `needs_rearm` with the reason on it is how somebody finds out that
    adding a node to their workflow stopped it running, and a row that vanished
    would have told them nothing.
    """
    with _lock:
        flows = _read()
    out = [dict(wf) for wf in flows.values()]
    out.sort(key=lambda wf: (not wf.get("armed"), wf.get("path", "")))
    return out


# ------------------------------------------------------------------- arming


def arm(path: str, *, tools=None, triggers=(), max_runs_per_hour=None,
        error_limit=None, model: str = "") -> dict:
    """Arm a workflow: record that a person approved THIS tool set for it.

    `tools` is not advisory and it is not optional. It is the list the caller
    showed the human, and this function re-compiles the document and **refuses
    if the two differ** — so an arm dialog that was rendered from a stale read of
    the file cannot approve a tool set nobody saw. That check is the entire
    reason the parameter exists; without it "the UI shows the tool list" is a
    convention rather than a guarantee.
    """
    target = os.path.abspath(str(path))
    if not os.path.isfile(target):
        return _refuse("not_a_file", "%r is not a file." % (target,))
    clean, error = _clean_triggers(triggers)
    if error:
        return _refuse("bad_trigger", error)

    compiled = plan(target)
    if not compiled.get("ok"):
        return compiled
    authorization = authorization_of(compiled)
    actual = authorization["tools"]
    if tools is None:
        return _refuse(
            "no_tool_list",
            "Arming a workflow means approving the tools it may call, so the "
            "list you were shown has to be sent back with the approval.")
    submitted = sorted({str(t) for t in tools})
    if submitted != actual:
        return _refuse(
            "tools_changed",
            "This workflow's tools changed while you were looking at it. It now "
            "calls %s. Nothing was armed — read the new list and arm again."
            % (", ".join(actual) or "no tools"))

    # A trigger input no trigger can fill is refused HERE, with the key named,
    # rather than three failed unattended runs from now with nothing named.
    problem = _unsatisfiable(compiled, clean)
    if problem:
        return _refuse("unsatisfiable_input", problem)

    now = _now()
    key = key_for(target)
    with _lock:
        flows = _read()
        wf = flows.get(key) or _blank(target)
        wf["path"] = target
        wf["name"] = str(compiled.get("name") or "") or os.path.basename(target)
        wf["armed"] = True
        wf["armed_at"] = _iso(now)
        wf["tools"] = actual
        wf["authorization"] = authorization
        wf["fingerprint"] = fingerprint(authorization)
        wf["generation"] = int(wf.get("generation") or 0) + 1
        wf["needs_rearm"] = False
        wf["needs_rearm_reason"] = ""
        wf["triggers"] = clean
        wf["consecutive_errors"] = 0
        wf["model"] = str(model or "")[:80]
        wf["queue"] = []
        wf["dropped"] = 0
        wf["max_runs_per_hour"] = _positive(
            max_runs_per_hour, DEFAULT_RUNS_PER_HOUR)
        wf["error_limit"] = _positive(error_limit, DEFAULT_ERROR_LIMIT)
        wf.setdefault("seen", {})
        wf.setdefault("runs", [])
        wf.setdefault("rate_window", [])
        # A fresh approval restarts a bounded recurrence: "three times" said
        # again means three more, not zero.
        wf["made"] = {}
        # Seeding is inside the lock and BEFORE the first due time is computed,
        # so no tick can see an armed file trigger with an empty marker map.
        _seed_seen(clean, wf["seen"], time.time())
        wf["next_due"] = {
            t["id"]: _iso(due)
            for t in clean if t["kind"] == KIND_SCHEDULE
            for due in [_next_due(t, now)] if due is not None
        }
        flows[key] = wf
        _write(flows)
    return _ok(workflow=dict(wf))


# The keys each kind of trigger puts in the payload it starts a run with. Kept
# beside the two `_evaluate` branches that build those payloads; a key added
# there and not here makes `arm` refuse something that would have worked, which
# is the safe direction to be stale in.
_PAYLOAD_KEYS = {
    KIND_SCHEDULE: frozenset({"trigger", "due", "kind"}),
    KIND_FILE: frozenset({"path", "name", "dir", "ext", "size", "mtime",
                          "trigger", "kind"}),
}


def _unsatisfiable(compiled: dict, triggers: list) -> str:
    """A sentence naming a `source: "trigger"` key no trigger can supply, or "".

    Checked AT ARM TIME, because the alternative is what the first version did:
    arming succeeded, every fire refused `missing_trigger_input` inside a
    detached run, and after three of those the workflow disarmed itself saying
    "3 runs in a row failed — fix what is failing", which names nothing and
    points at the wrong thing. The information needed to say it properly is all
    here — the compile lists the keys the document reads, and the trigger list
    says what will be supplied — so it is said here.

    A key must be supplied by EVERY trigger, not by one of them: a workflow
    armed on both a file watch and a cron line runs from either, and a key only
    the file watch carries is a run that refuses every time the cron fires.
    """
    wanted = compiled.get("triggerInputs") or []
    if not wanted:
        return ""
    for trigger in triggers:
        kind = str(trigger.get("kind") or "")
        keys = _PAYLOAD_KEYS.get(kind, frozenset())
        for item in wanted:
            key = str(item.get("key") or item.get("name") or "")
            if key and key not in keys:
                return (
                    "Step %r reads its %r argument from the run's input under "
                    "the key %r, and a %s trigger supplies only %s. Nothing was "
                    "armed — point that input at one of those keys, or give it "
                    "a fixed value."
                    % (item.get("label") or item.get("step"),
                       item.get("name"), key, kind,
                       ", ".join(sorted(keys))))
    return ""


def _positive(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def disarm(path: str, reason: str = "", by: str = "user") -> dict:
    """Stop a workflow firing, NOW, and drop everything it has queued.

    Dropping the queue is the half that makes this a revocation rather than a
    pause. A disarm that left twenty events to run on re-arm would be a disarm
    that did not stop anything; a person who disarms has decided this workflow
    should not act, and the events that arrived before they decided are exactly
    what they meant.

    A run already in flight is a detached process; it is left to finish and
    recorded, because killing a session mid-tool-call is a worse outcome than one
    more run. The `current` marker stays so nothing new starts behind it.
    """
    key = key_for(path)
    with _lock:
        flows = _read()
        wf = flows.get(key)
        if wf is None:
            return _refuse("unknown_workflow",
                           "%r has never been armed." % (os.path.abspath(path),))
        # THE GENERATION BUMP IS WHAT MAKES THIS IMMEDIATE, and it is inside
        # `_disarm_in_place` so no path that stops a workflow can forget it.
        # Clearing the queue stops work that has not been decided yet; the bump
        # stops work that was decided before the click and has not landed yet —
        # a claim taken a moment ago whose spawn is still in flight (`_settle`).
        _disarm_in_place(wf)
        wf["dropped"] = 0
        if by != "user":
            wf["needs_rearm"] = True
            wf["needs_rearm_reason"] = str(reason)[:400]
        flows[key] = wf
        _write(flows)
    if by != "user":
        _emit(EVENT_DISARMED, wf, reason)
    return _ok(workflow=dict(wf))


def forget(path: str) -> dict:
    """Drop a workflow's row entirely — the approval, the history and the
    processed-file markers. Re-arming after this re-seeds from scratch."""
    key = key_for(path)
    with _lock:
        flows = _read()
        if key not in flows:
            return _refuse("unknown_workflow",
                           "%r has never been armed." % (os.path.abspath(path),))
        del flows[key]
        _write(flows)
    return _ok()


# ---------------------------------------------------------------- the queue


def _enqueue(wf: dict, payload: dict, source: str) -> None:
    """Append one event, dropping the oldest past the bound and counting it."""
    queue = wf.setdefault("queue", [])
    queue.append({"payload": payload, "source": source, "at": _iso(_now())})
    if len(queue) > QUEUE_MAX:
        dropped = len(queue) - QUEUE_MAX
        del queue[:dropped]
        wf["dropped"] = int(wf.get("dropped") or 0) + dropped


def enqueue(path: str, payload=None, source: str = SOURCE_MANUAL) -> dict:
    """Queue one run of an ARMED workflow, with a payload.

    Refused when the workflow is not armed. That is not a technicality: this is
    the path a trigger takes, and its authorization is the arming. A person who
    wants to run a workflow right now with an input of their own has the panel's
    Run button, which is its own approval (WC-4a) and does not come through here.
    """
    key = key_for(path)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return _refuse("bad_payload", "the input for a run must be a JSON object")
    with _lock:
        flows = _read()
        wf = flows.get(key)
        if wf is None or not wf.get("armed"):
            return _refuse(
                "not_armed",
                "This workflow is not armed, so nothing may start a run of it "
                "except somebody clicking Run.")
        _enqueue(wf, payload, str(source or SOURCE_MANUAL)[:80])
        flows[key] = wf
        _write(flows)
    return _ok(queued=len(wf["queue"]), dropped=wf.get("dropped") or 0)


# ------------------------------------------------------------------- the tick


def _rate_ok(wf: dict, now_ts: float) -> bool:
    window = [t for t in (wf.get("rate_window") or [])
              if isinstance(t, (int, float)) and now_ts - t < _RATE_WINDOW_S]
    wf["rate_window"] = window
    return len(window) < _positive(wf.get("max_runs_per_hour"),
                                   DEFAULT_RUNS_PER_HOUR)


def _record_finish(wf: dict, state: str, detail: str) -> None:
    """Close out `current`, move it into history, and count the outcome."""
    current = wf.get("current") or {}
    entry = dict(current)
    entry["state"] = state
    entry["detail"] = str(detail)[:400]
    entry["finished"] = _iso(_now())
    runs = wf.setdefault("runs", [])
    runs.append(entry)
    del runs[:-RUNS_KEPT]
    wf["current"] = None
    if state == "done":
        wf["consecutive_errors"] = 0
    elif state == "error":
        wf["consecutive_errors"] = int(wf.get("consecutive_errors") or 0) + 1
    # "lost" deliberately counts as neither: losing track of a run is not
    # evidence the workflow is broken, and disarming on it would punish a temp
    # dir being swept.
    try:
        jobs.upsert({"id": _job_id(wf),
                     "title": "Workflow: %s" % (wf.get("name") or "run"),
                     "kind": "task",
                     "state": {"done": "done", "error": "error",
                               "cancelled": "cancelled"}.get(state, "done"),
                     "message": entry["detail"]}, server=True)
    except Exception:  # noqa: BLE001 — reporting is best-effort, never the record
        logger.debug("workflow trigger job report failed", exc_info=True)


def _poll_outcome(current: dict) -> tuple[str, str] | None:
    """`(state, detail)` for a claim that has ended, or `None` while it runs.

    OUTSIDE THE STORE LOCK — this is the half of the poll that talks to the
    runner, and `_apply_poll` is the half that writes. The split is not
    cosmetic: `_lock` also serialises every HTTP request that touches the store,
    so holding it across a subprocess would make Arm and Disarm wait on a poll.
    """
    run_id = str(current.get("runId") or "")
    started = current.get("started_ts") or 0
    if not run_id:
        # Claimed but never named — only reachable for a row written by an
        # older build, since `_drain` now names every claim. Safe to release:
        # nothing that has no id can be found, and nothing was reported started.
        return "lost", "the run was claimed but never started"
    out = _template_run({"action": "poll", "runId": run_id})
    age = time.time() - started
    stale = age > _RUN_MAX_AGE_S
    if not out.get("ok"):
        # THE UNKNOWN-SPAWN CASE. `_settle` left this claim in place because the
        # start call never answered and `run.py` detaches before it does, so the
        # session may well be alive. `poll` cannot find it, which means one of
        # two things — the spawn never happened, or the run dir is not there yet
        # — and only time separates them. Past the grace, with still nothing to
        # poll, the spawn provably never happened.
        if current.get("spawn_unknown") and age > _SPAWN_GRACE_S:
            return "lost", ("the call that started this run never answered, and "
                            "no run by that name exists — it never started")
        if current.get("spawn_unknown"):
            return None
        return ("lost", out.get("message", "the run was lost")) if stale else None
    if not out.get("done"):
        return ("lost", "the run did not finish within 6 hours") if stale else None
    failed_nodes = [n for n in (out.get("nodes") or [])
                    if isinstance(n, dict) and n.get("status") == "error"]
    error = str(out.get("error") or "")
    if error or failed_nodes:
        return "error", (error or "step %r failed: %s"
                         % (failed_nodes[0].get("label"),
                            failed_nodes[0].get("error")))
    return "done", str(out.get("summary") or "")[:400]


def _apply_poll(wf: dict, outcome: tuple[str, str]) -> None:
    """Write a finished claim into the history. Under the lock."""
    state, detail = outcome
    source = (wf.get("current") or {}).get("source", "")
    _record_finish(wf, state, detail)
    if state == "error":
        _emit(EVENT_FAILED, wf, detail)
    elif state == "done":
        _emit(EVENT_RAN, wf, source)


def _evaluate(wf: dict, now: datetime) -> None:
    """Turn everything that has happened since the last tick into queue events.

    **Schedule triggers COALESCE.** A due time in the past produces ONE event,
    not one per missed slot, and the next due time is then computed from NOW.
    That is `schedule.py`'s rule (its docstring, point 2) arrived at from the
    other side and for the same reason: replaying a week of "every hour" into a
    workflow that sends mail is not what the words meant, and the next run is
    already coming.
    """
    due_map = wf.setdefault("next_due", {})
    made = wf.setdefault("made", {})
    for trigger in wf.get("triggers") or []:
        if trigger.get("kind") == KIND_SCHEDULE:
            tid = trigger.get("id")
            # A BOUNDED RECURRENCE STOPS. `recur` computes occurrences and says
            # in its own docstring that `count` is the store's to enforce;
            # `schedule.py` enforces it against its `made` tally and so does
            # this, against ours. Checked before the due time is even read, so
            # an exhausted trigger stops costing recurrence arithmetic too.
            cap = trigger.get("count")
            if cap and int(made.get(tid) or 0) >= int(cap):
                due_map.pop(tid, None)
                continue
            stamp = due_map.get(tid)
            if not stamp:
                nxt = _next_due(trigger, now)
                if nxt is not None:
                    due_map[tid] = _iso(nxt)
                continue
            try:
                due = datetime.fromisoformat(str(stamp))
            except ValueError:
                due_map.pop(tid, None)
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if due > now:
                continue
            _enqueue(wf, {"trigger": tid, "due": _iso(due),
                          "kind": KIND_SCHEDULE},
                     "%s:%s" % (KIND_SCHEDULE, tid))
            # Counted per FIRED OCCURRENCE, not per completed run: coalescing
            # already means one event stands for a backlog, and a rule that
            # said "three times" should not be extended by three runs that
            # happened to fail.
            made[tid] = int(made.get(tid) or 0) + 1
            if cap and int(made[tid]) >= int(cap):
                due_map.pop(tid, None)
                continue
            nxt = _next_due(trigger, now)
            if nxt is None:
                due_map.pop(tid, None)
            else:
                due_map[tid] = _iso(nxt)
        elif trigger.get("kind") == KIND_FILE:
            seen = wf.setdefault("seen", {})
            for payload in _sweep(trigger, seen, time.time()):
                _enqueue(wf, dict(payload, trigger=trigger.get("id"),
                                  kind=KIND_FILE),
                         "%s:%s" % (KIND_FILE, trigger.get("id")))


def _new_run_id() -> str:
    """A run id, chosen by CORE and handed to `run.py` to use.

    In `run.py::_run_dir`'s alphabet (`[0-9a-zA-Z-]+`) because that module
    validates it as a basename before building a path out of it. Named here
    rather than there so a start call whose answer never arrives still leaves
    something pollable — see `_SPAWN_GRACE_S`.
    """
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(4).hex()


def _drain(wf: dict, now: datetime) -> dict | None:
    """Start at most ONE run, or return None and leave the queue where it is.

    Returns the claim to spawn (the store is written by the caller before the
    spawn happens — claim-before-spawn, `schedule.py`'s order and its reasoning:
    a process that dies between the two must leave a workflow that does not
    start the same run again).
    """
    if wf.get("current") or not wf.get("queue"):
        return None
    if not wf.get("armed"):
        return None
    if not _rate_ok(wf, time.time()):
        # Left in the queue, not dropped: the cap is a bound on rate, not a
        # verdict on the event. The queue's own bound is what stops that growing
        # without limit, and it counts what it drops.
        return None
    event = wf["queue"].pop(0)
    wf["current"] = {
        # NAMED BEFORE IT IS ASKED FOR. The claim is pollable from this moment,
        # whatever becomes of the call that starts it.
        "runId": _new_run_id(),
        "source": event.get("source", ""),
        "payload": event.get("payload") or {},
        "queued_at": event.get("at", ""),
        "started": _iso(now), "started_ts": time.time(),
        # The approval this run is being started under, and the generation it
        # was decided in. Both travel with the claim so `_settle` can tell
        # whether the world changed underneath it.
        "approved": dict(wf.get("authorization") or {}),
        "gen": int(wf.get("generation") or 0),
        # Cleared by `_settle` once the spawn's outcome is known either way.
        "spawn_unknown": False,
    }
    wf.setdefault("rate_window", []).append(time.time())
    return dict(wf["current"])


def tick(now: datetime | None = None) -> list[dict]:
    """One pass over every workflow. Returns the runs actually started.

    The order inside one workflow is poll, evaluate, drain — and it is the order
    that makes "one run at a time" true rather than nearly true: a run that
    finished since the last tick has to be cleared BEFORE the drain looks, or the
    queue would wait a whole tick behind a run that is already over.

    Every spawn happens OUTSIDE the store lock and AFTER the claim has been
    written, so a crash mid-spawn leaves a claimed run (which the next tick
    resolves) and never an unclaimed one (which it would start twice).

    **The authorization travels WITH the start call.** It is not checked here
    and then acted on there: `run.py` compares the approved set against the very
    compile whose tool set becomes `--allowed-tools`, so there is no window in
    which a document save can slip between the check and the thing it guards.
    See `_settle` for what happens to the answer.
    """
    now = now or _now()
    started: list[dict] = []
    with _lock:
        keys = list(_read())
    for key in keys:
        try:
            claim = _prepare(key, now)
        except Exception:  # noqa: BLE001 — one bad workflow must not stop the rest
            logger.exception("workflow trigger tick failed for %s", key)
            continue
        if claim is None:
            continue
        result = _template_run({
            "action": "start",
            "path": claim["path"],
            "model": claim.get("model") or "",
            "payload": claim.get("payload") or {},
            # THE APPROVAL, handed to the process that will act on it.
            "approved": claim.get("approved") or {},
            # THE RUN'S NAME, chosen before the call so the claim stays
            # pollable even if this call never answers.
            "runId": claim.get("runId") or "",
        })
        if _settle(key, claim, result):
            started.append({"path": claim["path"],
                            "runId": result.get("runId") or claim.get("runId"),
                            "source": claim.get("source", ""),
                            "payload": claim.get("payload") or {}})
    return started


def _prepare(key: str, now: datetime) -> dict | None:
    """One workflow's whole tick up to the claim: poll, sweep, evaluate, claim.

    The one call into the runner (`poll`) happens with the store lock RELEASED,
    and only the read-modify-writes take it. `_lock` also serialises every HTTP
    request that touches the store, so holding it across a subprocess would make
    Arm and Disarm wait on a tick.

    There is deliberately NO compile here any more. The authorization check used
    to live in this function, as a `plan` subprocess whose answer was then acted
    on by a SECOND subprocess that compiled the document again — so the set that
    was checked and the set that became `--allowed-tools` were two different
    readings of a file, with a save able to land between them. The check moved
    into `run.py`, where there is only one reading. What is left here is the
    claim, which is what stops two ticks racing into the same run.

    Returns the claim to spawn, or None.
    """
    # (1) poll, outside the lock, against a snapshot of the claim.
    with _lock:
        wf = _read().get(key)
        current = (wf or {}).get("current")
    if wf is None:
        return None
    outcome = _poll_outcome(current) if isinstance(current, dict) else None

    # (2) apply, evaluate, claim — under the lock, re-reading rather than
    # writing back the snapshot above, so a disarm that landed during the poll
    # is not undone by it.
    with _lock:
        flows = _read()
        wf = flows.get(key)
        if wf is None:
            return None
        before = _row_sig(wf)
        if outcome is not None and isinstance(wf.get("current"), dict) \
                and str(wf["current"].get("runId") or "") == \
                str((current or {}).get("runId") or ""):
            _apply_poll(wf, outcome)
        limit = _positive(wf.get("error_limit"), DEFAULT_ERROR_LIMIT)
        disarmed_for_errors = False
        if wf.get("armed") and int(wf.get("consecutive_errors") or 0) >= limit:
            _disarm_in_place(wf)
            wf["needs_rearm"] = True
            wf["needs_rearm_reason"] = (
                "%d runs in a row failed, so this workflow disarmed itself. Fix "
                "what is failing and arm it again." % limit)
            disarmed_for_errors = True
        if wf.get("armed"):
            _evaluate(wf, now)
        claim = _drain(wf, now) if wf.get("armed") else None
        flows[key] = wf
        # WRITTEN ONLY WHEN SOMETHING CHANGED. A tick over an idle armed
        # workflow used to re-serialise and atomically replace the whole store
        # every 30 seconds forever — and `seen` holds up to SEEN_MAX markers per
        # workflow, so "the whole store" is megabytes for a couple of busy
        # watched folders.
        if _row_sig(wf) != before:
            _write(flows)
        payload = dict(claim) if claim else None
        if payload is not None:
            payload["path"] = wf["path"]
            payload["model"] = wf.get("model") or ""
    if disarmed_for_errors:
        _emit(EVENT_DISARMED, wf, wf["needs_rearm_reason"])
    return payload


def _disarm_in_place(wf: dict) -> None:
    """Disarm a row already held under the lock, bumping its generation.

    One definition, because every path that stops a workflow has to stop it the
    same way — and the generation bump is the half that is easy to leave out.
    """
    wf["armed"] = False
    wf["queue"] = []
    wf["next_due"] = {}
    wf["generation"] = int(wf.get("generation") or 0) + 1


def _release_claim(wf: dict) -> None:
    """Undo a claim that never became a run: the slot goes back too."""
    wf["current"] = None
    window = wf.get("rate_window") or []
    wf["rate_window"] = window[:-1]


def _settle(key: str, claim: dict, result: dict) -> bool:
    """Write the spawn's outcome onto the claim it belongs to.

    Returns whether a run is now genuinely in flight. Four answers, and the
    three unhappy ones are the point:

    * **`tools_changed`** — `run.py` refused because the document no longer
      matches what was approved. The claim is UNDONE (slot and all) rather than
      recorded as a failed run, because nothing ran, and the workflow disarms so
      the next tick does not ask a question whose answer will not have changed.
    * **the generation moved** — somebody disarmed (or re-armed) while this
      start was in flight, so the decision behind it has been revoked. The run
      IS spawned by now, so it is CANCELLED rather than wished away, and
      recorded as cancelled. WC-12d says nothing starts behind a disarm; this is
      what makes that true of the moment either side of the click.
    * **`runner_failed`** — the executor call died (a timeout, an exception) and
      the spawn's outcome is genuinely unknown, because `run.py` detaches the
      session before it answers. Held, with the id core chose, for
      `_SPAWN_GRACE_S`, during which `poll` can find the run if it exists. This
      is the one case where doing nothing is right.

    Every other refusal is a real answer from `run.py` (`bad_payload`,
    `missing_trigger_input`, `spawn_failed`, `no_claude_cli`): nothing is
    running, and it is recorded as a failed run so the error counter — which is
    the brake for a workflow that is simply broken — sees it.
    """
    reason = str(result.get("reason") or "")
    spawn_unknown = (not result.get("ok")) and reason == "runner_failed"
    with _lock:
        flows = _read()
        wf = flows.get(key)
        if wf is None:
            return False
        current = wf.get("current")
        if not isinstance(current, dict) or \
                str(current.get("runId") or "") != str(claim.get("runId") or ""):
            # The claim we started is not the claim on the row any more: a
            # restart, a forget, or a poll that already resolved it. Nothing of
            # ours to write.
            return False
        stale_generation = int(current.get("gen") or 0) != \
            int(wf.get("generation") or 0)

        if result.get("ok") and not stale_generation:
            current["spawn_unknown"] = False
            current["runId"] = str(result.get("runId") or claim.get("runId") or "")
            flows[key] = wf
            _write(flows)
            _report_running(wf, current)
            return True

        if stale_generation and (result.get("ok") or spawn_unknown):
            # REVOCATION WINS OVER UNCERTAINTY. `ok` means the session is
            # certainly away; `spawn_unknown` means it may be. Both get the
            # cancel, because the cost of cancelling a run that never existed is
            # a refusal nobody reads, and the cost of not cancelling one that
            # did is the unattended session a person just revoked.
            outcome = ("cancelled",
                       "the workflow was disarmed while this run was starting")
        elif spawn_unknown:
            current["spawn_unknown"] = True
            flows[key] = wf
            _write(flows)
            logger.warning(
                "workflow %s: the start call did not answer; holding run %s for "
                "one tick in case it spawned", key, current.get("runId"))
            return False
        elif reason == "tools_changed":
            _release_claim(wf)
            _disarm_in_place(wf)
            wf["needs_rearm"] = True
            wf["needs_rearm_reason"] = str(result.get("message") or "")[:400]
            flows[key] = wf
            _write(flows)
            _emit(EVENT_REFUSED, wf, wf["needs_rearm_reason"])
            return False
        else:
            outcome = ("error", str(result.get("message")
                                    or "the run did not start"))
        flows[key] = wf
        run_id = str(current.get("runId") or "")
        _record_finish(wf, outcome[0], outcome[1])
        _write(flows)
    if outcome[0] == "cancelled":
        # Outside the lock: this is a subprocess, and the row is already
        # consistent without it. Best-effort by nature — the session may have
        # finished on its own — but it is the difference between "we stopped it"
        # and "we hoped".
        _template_run({"action": "cancel", "runId": run_id})
    else:
        _emit(EVENT_FAILED, wf, outcome[1])
    return False


def _report_running(wf: dict, current: dict) -> None:
    try:
        jobs.upsert({"id": _job_id(wf),
                     "title": "Workflow: %s" % (wf.get("name") or "run"),
                     "kind": "task", "state": "running",
                     "message": "started by %s"
                                % (current.get("source") or "a trigger")},
                    server=True)
    except Exception:  # noqa: BLE001 — best-effort
        logger.debug("workflow trigger job report failed", exc_info=True)


# --------------------------------------------------------------- the loop


def _loop() -> None:
    while True:
        try:
            tick()
        except Exception:
            logger.exception("workflow trigger tick failed")
        time.sleep(POLL_INTERVAL_S)


def start() -> None:
    """Start the background loop. Idempotent.

    The FIRST tick is the catch-up pass: it fires a schedule trigger whose time
    went by while the app was closed (once — `_evaluate` coalesces) and it sweeps
    every watched folder for files that arrived meanwhile. It deliberately does
    not sleep first, for the same reason `schedule.start` does not.
    """
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, daemon=True,
                                   name="fused-workflow-triggers")
        _thread.start()
