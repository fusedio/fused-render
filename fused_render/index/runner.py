"""The scan control plane: start a detached run, poll its event log, cancel
it, list recent runs, and record when a root was last scanned.

Nothing here blocks. A full home scan takes minutes, so the request that asks
for one gets a `run_id` back immediately and watches `events.jsonl` — the same
poll-friendly append-only log OpenIndex's page used, which drops in unchanged.

The worker is spawned as `python -m fused_render.index.worker`, not
`Popen([python, __file__])`: inside a py2app bundle there is no source file to
point at. Detached (`start_new_session` / DETACHED_PROCESS) like every other
long-running child in this app, so the scan outlives the request.

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
    `start_new_session` is POSIX-only — Windows accepts it and silently
    ignores it, so detaching there needs creation flags instead."""
    if os.name == "nt":
        return {"creationflags": (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                                 | getattr(subprocess,
                                           "CREATE_NEW_PROCESS_GROUP", 0x200))}
    return {"start_new_session": True}


def start(cfg: IndexConfig, root: str, full: bool = False) -> dict:
    """Spawn a detached scan of `root`; returns `{run_id, root}` at once."""
    root = norm(os.path.abspath(os.path.expanduser((root or "~").strip())))
    if not os.path.isdir(root):
        raise ValueError(f"not a directory: {root}")
    if MountGuard(mounts_dir=_mounts_dir()).blocks_root(root):
        raise ValueError(
            f"{root} is mount-backed; indexing remote mounts is not supported "
            "(a kernel crawl of an rclone mount can wedge it)")
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    run_dir = os.path.join(cfg.runs_dir, run_id)
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "spec.json"), "w") as f:
        json.dump({"root": root, "full": bool(full), "started": time.time(),
                   "config": cfg.to_dict(), "mounts_dir": _mounts_dir()}, f)
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


def status(cfg: IndexConfig, run_id: str, since: int = 0) -> dict:
    run_dir = _run_dir(cfg, run_id)
    events, new, cursor = read_events(run_dir, int(since))
    return {"state": derive_state(events), "events": new, "cursor": cursor}


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


def list_runs(cfg: IndexConfig, limit: int = 20) -> dict:
    out = []
    for rid in _run_ids(cfg)[:limit]:
        rd = os.path.join(cfg.runs_dir, rid)
        try:
            with open(os.path.join(rd, "spec.json")) as f:
                spec = json.load(f)
        except (OSError, ValueError):
            spec = {}
        events, _, _ = read_events(rd)
        out.append({"run_id": rid, "root": spec.get("root"),
                    **derive_state(events)})
    return {"runs": out}


# How long an unfinished run directory is left alone before it counts as
# abandoned rather than live. A worker emits at least one event per half
# second while it walks, so anything untouched for a day died without closing
# its log (killed, machine slept off, power) and its shards are dead weight.
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
        if derive_state(events)["running"] and not _looks_abandoned(rd, now):
            continue
        shutil.rmtree(rd, ignore_errors=True)
        removed += 1
    return removed


def _looks_abandoned(run_dir: str, now: float) -> bool:
    try:
        newest = max(os.stat(os.path.join(run_dir, n)).st_mtime
                     for n in os.listdir(run_dir) or ["."])
    except (OSError, ValueError):
        return True  # unreadable: nothing left to protect
    return (now - newest) > STALE_RUN_S


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
