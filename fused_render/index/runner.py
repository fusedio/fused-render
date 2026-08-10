"""The scan control plane: start a detached run, poll its event log, cancel
it, list recent runs, and record when a root was last scanned.

Nothing here blocks. A full home scan takes minutes, so the request that asks
for one gets a `run_id` back immediately and watches `events.jsonl` — the same
poll-friendly append-only log OpenIndex's page used, which drops in unchanged.

The worker is spawned as `python -m fused_render.index.worker`, not
`Popen([python, __file__])`: inside a py2app bundle there is no source file to
point at. Detached so the scan outlives the request — via DETACHED_PROCESS on
Windows, and via the worker's own `os.setsid()` on POSIX (see _detach_kwargs
for why it must NOT be `start_new_session=True`).

See specs/scan.md.
"""
import json
import os
import shutil
import subprocess
import sys
import time

from fused_render.index.config import IndexConfig
from fused_render.index.ignore import MountGuard, norm
from fused_render.shell import storage

WORKER_MODULE = "fused_render.index.worker"


def _mounts_dir() -> str:
    """Indirection so a test can point the mount guard somewhere harmless."""
    from fused_render.shell.mounts import mounts_dir
    return mounts_dir()


def _detach_kwargs() -> dict:
    """Popen kwargs that let the worker outlive the request and the page.

    On POSIX these MUST stay posix_spawn-compatible: no `start_new_session`,
    no `preexec_fn`, and `close_fds=False`. Any of those forces CPython onto
    fork()+exec, and a fork of a server process that has touched
    pyproj/rasterio runs PROJ's pthread_atfork handler and dies with SIGSEGV
    before Python starts — the startup scan works (nothing loaded yet) and
    every later on-demand scan dies with an empty worker.log. Same discipline
    as envinstall.py. The session detach (`os.setsid`) happens inside the
    worker itself, after exec, where it is safe."""
    if os.name == "nt":
        return {"creationflags": (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                                 | getattr(subprocess,
                                           "CREATE_NEW_PROCESS_GROUP", 0x200))}
    return {"close_fds": False}


def _run_spec(run_dir: str) -> dict:
    try:
        with open(os.path.join(run_dir, "spec.json")) as f:
            spec = json.load(f)
        return spec if isinstance(spec, dict) else {}
    except (OSError, ValueError):
        return {}


def _spec_ignore_sig(spec: dict):
    """The ignore fingerprint a live run is scanning under, or None.

    Recorded explicitly at spawn; derived from the config the spec carries for
    runs started before that field existed, so an in-flight legacy run is
    still joinable instead of being cancelled once on upgrade."""
    sig = spec.get("ignore_sig")
    if isinstance(sig, str):
        return sig
    conf = spec.get("config")
    if isinstance(conf, dict):
        try:
            return IndexConfig.from_dict(conf).rules.sig()
        except (TypeError, ValueError, KeyError):
            return None
    return None


def active_run(cfg: IndexConfig, root: str):
    """The live run scanning exactly `root`, or None.

    Liveness is the heartbeat _with_liveness applies, not the presence of a
    run directory: a killed worker leaves a `running` log behind forever, and
    reading that as live would wedge every future scan of the root.

    A run already told to stop does not count. Cancelling is asynchronous —
    the worker notices its flag within a couple hundred directories — so a
    dying run's log still reads `running`, and offering it as joinable would
    hand the caller a scan that is about to produce nothing."""
    now = time.time()
    for rid in _run_ids(cfg):
        rd = os.path.join(cfg.runs_dir, rid)
        folded = _folded(rd)
        if folded["root"] != root:
            continue
        if os.path.exists(os.path.join(rd, "cancel")):
            continue
        if _with_liveness(dict(folded), rd, now)["running"]:
            spec = _run_spec(rd)
            return {"run_id": rid, "root": root,
                    "ignore_sig": _spec_ignore_sig(spec),
                    "full": bool(spec.get("full"))}
    return None


def start(cfg: IndexConfig, root: str, full: bool = False) -> dict:
    """Spawn a detached scan of `root`; returns `{run_id, root}` at once.

    A root already being scanned JOINS that run instead of starting a second.
    Overlapping scans of one root duplicate the entire walk, race each
    other's reuse cache, and — since each worker stamps the applied-ignore sig
    from its own spec — let a pre-edit run finish last and stamp the OLD rules
    over the post-edit run's, leaving the root stale indefinitely. The store
    lock serializes the two compactions; none of that is what it protects."""
    root = norm(os.path.abspath(os.path.expanduser((root or "~").strip())))
    # The guard runs BEFORE any kernel syscall on the caller's path: it is
    # pure string work against the mount records, while os.path.isdir on a
    # path under a wedged NFS mount blocks the request thread indefinitely
    # (this repo's documented mount-wedge class).
    if MountGuard(mounts_dir=_mounts_dir()).blocks_root(root):
        raise ValueError(
            f"{root} is mount-backed; indexing remote mounts is not supported "
            "(a kernel crawl of an rclone mount can wedge it)")
    if not os.path.isdir(root):
        raise ValueError(f"not a directory: {root}")
    sig = cfg.rules.sig()
    live = active_run(cfg, root)
    if live is not None:
        # Join only a run that can actually answer THIS request. A live scan
        # carries the ignore list it was spawned with and stamps that as
        # applied, so joining one started under different rules is how a
        # skip-rules save silently loses its reconciling rescan: the old
        # worker finishes, stamps the OLD fingerprint, and the root reads as
        # reconciled while the store still holds the folders just excluded —
        # with the UI having reported a rebuild. `full` is one-way: a full
        # rebuild already covers an incremental request, not the reverse.
        if live["ignore_sig"] == sig and (live["full"] or not full):
            return {"run_id": live["run_id"], "root": root,
                    "already_running": True}
        # Otherwise supersede it. Cancelling is safe to do bluntly: a
        # cancelled worker returns before it compacts or stamps anything
        # (index/scan.py), so its output was going to be discarded regardless
        # and only the walk done so far is lost.
        cancel(cfg, live["run_id"])
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    run_dir = os.path.join(cfg.runs_dir, run_id)
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "spec.json"), "w") as f:
        # `ignore_sig` is redundant with `config` and recorded anyway: the join
        # check above reads it on every start, and deriving it from the config
        # means compiling the rules just to answer "same rules?".
        json.dump({"root": root, "full": bool(full), "started": time.time(),
                   "ignore_sig": sig, "config": cfg.to_dict(),
                   "mounts_dir": _mounts_dir()}, f)
    with open(os.path.join(run_dir, "worker.log"), "w") as logf:
        subprocess.Popen(
            [sys.executable, "-m", WORKER_MODULE, run_dir],
            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            **_detach_kwargs(),
        )
    # Recorded at START, not at completion: the debounce this feeds exists to
    # stop a restart loop from queueing scan after scan, and a scan that is
    # still running should suppress the next one just as firmly as one that
    # finished.
    _record_scan(cfg, root)
    return {"run_id": run_id, "root": root}


def _run_dir(cfg: IndexConfig, run_id: str) -> str:
    # A run id is a path segment; refuse anything that could escape the runs
    # dir rather than trusting the caller's string.
    if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
        raise ValueError(f"no such run: {run_id}")
    d = os.path.join(cfg.runs_dir, run_id)
    if not os.path.isdir(d):
        raise ValueError(f"no such run: {run_id}")
    return d


def read_events(run_dir: str, since: int = 0):
    path = os.path.join(run_dir, "events.jsonl")
    events = []
    if os.path.exists(path):
        with open(path) as f:
            for i, line in enumerate(f):
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue  # half-written last line
                ev["_i"] = i
                events.append(ev)
    new = [e for e in events if e["_i"] >= since]
    cursor = (events[-1]["_i"] + 1) if events else 0
    return events, new, cursor


def derive_state(events) -> dict:
    """Fold the whole log into the flat state a client renders. Folding is
    idempotent, which is what makes resume-after-reload work."""
    st = {"running": True, "phase": "starting", "dirs": 0, "files": 0,
          "reused": 0, "current": "", "summary": None, "cancelled": False,
          "error": None}
    for e in events:
        t = e.get("type")
        if t == "progress":
            st.update(dirs=e.get("dirs", 0), files=e.get("files", 0),
                      reused=e.get("reused", 0), current=e.get("current", ""))
        elif t == "phase":
            st["phase"] = e.get("msg", "")
        elif t == "run_end":
            st["running"] = False
            st["summary"] = e.get("summary")
            st["cancelled"] = e.get("msg") == "cancelled"
            st["error"] = e.get("error")
    return st


def _with_liveness(state: dict, run_dir: str, now: float) -> dict:
    """Cross-check a `running` log against the run directory's mtimes.

    The log alone cannot distinguish "still walking" from "the worker died
    without a run_end" (killed, OOM, spawn crash): both just stop appending.
    A live worker touches its run dir at least every half second, so an
    unfinished run untouched for ABANDONED_RUN_S is dead — report it as such,
    or the status endpoint says `scanning` (and the UI says "indexing…", with
    the scan buttons disabled) until the dir is eventually pruned."""
    if state["running"] and _looks_abandoned(run_dir, now, ABANDONED_RUN_S):
        state["running"] = False
        state["error"] = state["error"] or (
            "the scan worker died without finishing (no activity for "
            f"{ABANDONED_RUN_S}s)")
    return state


def status(cfg: IndexConfig, run_id: str, since: int = 0) -> dict:
    run_dir = _run_dir(cfg, run_id)
    events, new, cursor = read_events(run_dir, int(since))
    state = _with_liveness(derive_state(events), run_dir, time.time())
    return {"state": state, "events": new, "cursor": cursor}


def cancel(cfg: IndexConfig, run_id: str) -> dict:
    run_dir = _run_dir(cfg, run_id)
    open(os.path.join(run_dir, "cancel"), "w").close()
    return {"cancelled": run_id}


def _run_ids(cfg: IndexConfig):
    try:
        return sorted((r for r in os.listdir(cfg.runs_dir)
                       if os.path.isdir(os.path.join(cfg.runs_dir, r))),
                      reverse=True)
    except OSError:
        return []


# Folded state per run dir, keyed on the event log's (size, mtime). The
# status panel polls every 1.5s and each poll used to re-fold EVERY run's log
# from line 0: quadratic in the live run's own length, plus ~19 finished logs
# that can never change again. Liveness is deliberately NOT cached — it is a
# function of `now`, so it is re-applied to a copy on every call.
_STATE_CACHE: dict = {}


def _folded(rd: str) -> dict:
    """`{root, **state}` for one run dir, re-folding only when its log moves."""
    try:
        st = os.stat(os.path.join(rd, "events.jsonl"))
        key = (st.st_size, st.st_mtime_ns)
    except OSError:
        key = None  # no log yet: cheap to re-read, and it is about to appear
    hit = _STATE_CACHE.get(rd)
    if hit is not None and hit[0] == key and key is not None:
        return hit[1]
    try:
        with open(os.path.join(rd, "spec.json")) as f:
            spec = json.load(f)
    except (OSError, ValueError):
        spec = {}
    events, _, _ = read_events(rd)
    folded = {"root": spec.get("root"), **derive_state(events)}
    _STATE_CACHE[rd] = (key, folded)
    return folded


def list_runs(cfg: IndexConfig, limit: int = 20) -> dict:
    out = []
    now = time.time()
    seen = set()
    for rid in _run_ids(cfg)[:limit]:
        rd = os.path.join(cfg.runs_dir, rid)
        seen.add(rd)
        folded = _folded(rd)
        out.append({"run_id": rid, **_with_liveness(dict(folded), rd, now)})
    # Pruned run dirs must not accumulate here for the life of the process.
    for gone in [k for k in _STATE_CACHE if k not in seen]:
        del _STATE_CACHE[gone]
    return {"runs": out}


# Two thresholds for an unfinished run directory, because the two mistakes
# cost differently. A worker touches its run dir at least every half second
# while it walks, but a big DuckDB compaction can go quiet for a while — so
# REPORTING a run as dead waits a few minutes (wrongly saying "indexing…"
# for minutes after a crash is annoying; wrongly saying a live scan died is
# just a stale label until the next event lands). DELETING its directory
# waits a day: pruning a live run's shards out from under it loses the scan.
ABANDONED_RUN_S = 5 * 60
STALE_RUN_S = 24 * 3600


def prune_runs(cfg: IndexConfig, keep: int = 20) -> int:
    """Delete all but the `keep` newest run directories; returns how many
    went. OpenIndex left these in the system temp dir forever — here they sit
    under the index dir, holding the parquet shards of every abandoned run, so
    something has to reclaim them. A run whose log has no terminal event is
    kept while it still looks live (recently written), so pruning can never
    pull the shards out from under a scan in flight."""
    removed = 0
    now = time.time()
    for rid in _run_ids(cfg)[keep:]:
        rd = os.path.join(cfg.runs_dir, rid)
        events, _, _ = read_events(rd)
        if derive_state(events)["running"] and not _looks_abandoned(rd, now, STALE_RUN_S):
            continue
        shutil.rmtree(rd, ignore_errors=True)
        removed += 1
    return removed


def _looks_abandoned(run_dir: str, now: float, threshold_s: float) -> bool:
    try:
        newest = max(os.stat(os.path.join(run_dir, n)).st_mtime
                     for n in os.listdir(run_dir) or ["."])
    except (OSError, ValueError):
        return True  # unreadable: nothing left to protect
    return (now - newest) > threshold_s


# ---------------------------------------------------------- scan bookkeeping

def _scans(cfg: IndexConfig) -> dict:
    raw = storage.read_json(cfg.scans_json)
    return raw if isinstance(raw, dict) else {}


def _record_scan(cfg: IndexConfig, root: str) -> None:
    scans = _scans(cfg)
    scans[root] = time.time()
    storage.write_json(cfg.scans_json, scans)


def last_scan(cfg: IndexConfig, root: str):
    """Epoch seconds of the last scan STARTED for exactly this root, or None."""
    v = _scans(cfg).get(norm(os.path.abspath(os.path.expanduser(root))))
    return v if isinstance(v, (int, float)) else None
