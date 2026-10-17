"""`GET /api/system/dashboard`: one snapshot of everything that can make the app
feel slow, and the verdict over it (sysmon/verdict.py).

Each section is built by its own function and isolated: one that raises becomes
`{"error": "..."}` for that section and the rest still answer. The slow or
platform-specific reads (swap trail, `sysctl`, `pmset`) are cached 30 s; the
24 h baseline and the directory sizes are cached 5 min.

`build()` blocks (subprocesses, file reads, dir walks), so the async handler
goes through `build_async()`, which runs it on a worker thread. The two reads
that are bound to the event loop and cheap (the anyio thread-limiter and the
index lane semaphores) are taken first, on the loop thread.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import statistics
import sys
import threading
import time
import types
from collections import deque

from fused_render import health, netlog
from fused_render.sysmon import sampler as sysmon_sampler
from fused_render.sysmon import verdict as sysmon_verdict

logger = logging.getLogger(__name__)

SWAP_WINDOW_S = 600.0
_MACHINE_TTL_S = 30.0
_machine_cache: dict[str, tuple[float, object]] = {}
_SLOW_TTL_S = 300.0
_slow_cache: dict[str, tuple[float, object]] = {}
LOG_TAIL_BYTES = 256 * 1024
TIMELINE_S = 600.0
CALLS_WINDOW_S = 600.0

# What only the event-loop thread may read: the thread-pool limiter and the
# index lane semaphores. Recorded on the loop by `record_loop_state` (the
# loop-lag ticker, twice a second, and `build_async`), each with a monotonic
# timestamp. `build()` runs off the loop and only ever reads these; a value
# older than LOOP_STATE_MAX_AGE_S (or never set) is unknown.
LOOP_STATE_MAX_AGE_S = 15.0
_loop_state = types.SimpleNamespace(pool=None, pool_t=None, lanes=None, lanes_t=None)


def record_loop_state() -> None:
    """Take the loop-bound reads. Call on the event-loop thread only."""
    from fused_render.server import serverstats
    now = time.monotonic()
    try:
        _loop_state.pool, _loop_state.pool_t = serverstats.pool_stats(), now
    except Exception:  # noqa: BLE001
        logger.debug("dashboard: pool stats failed", exc_info=True)
    try:
        from fused_render.server.routers import index
        _loop_state.lanes, _loop_state.lanes_t = index.lane_stats(), now
    except Exception:  # noqa: BLE001
        logger.debug("dashboard: lane stats failed", exc_info=True)


def _fresh(name: str):
    """The recorded `pool` / `lanes` value, or None when never set or stale."""
    value = getattr(_loop_state, name)
    stamp = getattr(_loop_state, name + "_t")
    if value is None or stamp is None or time.monotonic() - stamp > LOOP_STATE_MAX_AGE_S:
        return None
    return value


def swap_growth(rows: list[dict], now: float, window_s: float) -> int | None:
    """Bytes of swap gained across the trail rows in the last `window_s`."""
    pts = [(r["t"], r["swap_used"]) for r in rows
           if isinstance(r.get("swap_used"), (int, float)) and r["t"] >= now - window_s]
    if len(pts) < 2:
        return None
    return int(pts[-1][1] - pts[0][1])


def _cached(key: str, fn):
    now = time.monotonic()
    hit = _machine_cache.get(key)
    if hit is not None and now - hit[0] < _MACHINE_TTL_S:
        return hit[1]
    try:
        value = fn()
    except Exception:  # noqa: BLE001
        return None  # not cached: the next call retries
    if value is not None:
        _machine_cache[key] = (now, value)
    return value


def _abs_exe(name: str, *candidates: str) -> str:
    """An ABSOLUTE executable path: CPython only takes posix_spawn (no atfork
    handlers) when the path has a directory part, and a fork() in a process
    with libproj resident dies with SIGSEGV (see app_git.py)."""
    for path in candidates:
        if os.path.exists(path):
            return path
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(name)
    return os.path.abspath(found)


def _run(argv: list[str]) -> str | None:
    """The tool's stdout, or None when the tool is absent or cannot run."""
    exe = {"sysctl": ("/usr/sbin/sysctl",), "pmset": ("/usr/bin/pmset",)}.get(argv[0], ())
    try:
        return subprocess.run([_abs_exe(argv[0], *exe), *argv[1:]], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=2.0,
                              close_fds=False).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None


def _to_int(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


def _pressure() -> str | None:
    if sys.platform != "darwin":
        return None

    def read():
        level = _to_int(_run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]) or "")
        return {1: "normal", 2: "warn", 4: "critical"}.get(level)
    return _cached("pressure", read)


def _power() -> dict | None:
    """Thermal speed limit and Low Power Mode (macOS), else None."""
    if sys.platform != "darwin":
        return None

    def read():
        limit = None
        for line in (_run(["pmset", "-g", "therm"]) or "").splitlines():
            if "CPU_Speed_Limit" in line:
                limit = _to_int(line.split("=")[-1])
        low = None
        for line in (_run(["pmset", "-g"]) or "").splitlines():
            if line.strip().startswith("lowpowermode"):
                low = line.split()[-1] == "1"
        return {"thermal_limit": limit, "low_power": low}
    return _cached("power", read)


def _sampler_payload() -> dict:
    return sysmon_sampler.get_sampler().poll("fused")


def _net() -> dict:
    log = netlog.get()
    return {"summary": log.summary(), "groups": log.groups()[:5]}


def _server() -> dict:
    from fused_render.server import serverstats
    lag = serverstats.loop_lag()
    pool = _fresh("pool")
    return {"pool": pool,
            "loop_lag": {"current_ms": lag.current(), "max_60s_ms": lag.max_over(60)},
            "sockets": netlog.get().socket_total(),
            "engines": serverstats.engine_workers()}


def _memory(payload: dict | None = None) -> dict:
    payload = payload if payload is not None else _sampler_payload()
    host = payload.get("host") or {}
    now = time.time()
    rows = health.read_trail(1800)
    procs = [p for p in payload.get("procs", []) if p.get("memBytes")]
    big = max(procs, key=lambda p: p["memBytes"], default=None)
    return {"used": host.get("memUsed"), "total": host.get("memTotal"),
            "swap_used": next((r["swap_used"] for r in reversed(rows)
                               if r.get("swap_used") is not None), None),
            "swap_growth_10m_bytes": swap_growth(rows, now, SWAP_WINDOW_S),
            "swap_growth_30m_bytes": swap_growth(rows, now, 1800),
            "pressure": _pressure(),
            "biggest": ({"pid": big["pid"], "label": big["label"], "mem": big["memBytes"]}
                        if big else None)}


def _cpu(payload: dict | None = None) -> dict:
    payload = payload if payload is not None else _sampler_payload()
    host = payload.get("host") or {}
    power = _power() or {}
    return {"cpu_pct": host.get("cpuPct"), "load": host.get("load"), "ncpu": host.get("ncpu"),
            "thermal_limit": power.get("thermal_limit"), "low_power": power.get("low_power")}


def _claude(payload: dict | None = None) -> dict:
    payload = payload if payload is not None else _sampler_payload()
    return payload.get("claude") or {"total": 0, "fused": 0, "other": 0,
                                                 "mem_bytes": 0, "cpu_pct": 0.0, "top": []}


def _index() -> dict:
    from fused_render.index import runner
    from fused_render.index.config import load_config
    lanes = _fresh("lanes")
    out = dict(lanes) if lanes is not None else {"unavailable": True}
    cfg = load_config()
    now = time.time()
    running = []
    for rid in runner._run_ids(cfg):
        rd = os.path.join(cfg.runs_dir, rid)
        st = runner._with_liveness(dict(runner._folded(rd)), rd, now)
        if st.get("running"):
            running.append({"run": rid, "root": st.get("root"), "phase": st.get("phase"),
                            "dirs": st.get("dirs"), "files": st.get("files")})
    out["scans"] = {"running": len(running), "list": running[:10],
                    "limit": runner.MAX_CONCURRENT_RUNS}
    return out


def _jobs() -> dict:
    from fused_render import jobs
    now = time.time()
    rows = jobs.list_jobs(now=now)
    stalled = [j for j in rows if jobs.is_stalled(j, now)]
    running = [j for j in rows if j.state == jobs.RUNNING]
    return {"running": len(running), "stalled": len(stalled),
            "queued": sum(1 for j in rows if getattr(j, "state", "") == "queued"),
            "stalled_list": [{"id": getattr(j, "id", None), "title": getattr(j, "title", None)}
                             for j in stalled][:10]}


def _slow(key: str, fn):
    """`fn()` cached `_SLOW_TTL_S`: for reads too costly to repeat every 2 s."""
    now = time.monotonic()
    hit = _slow_cache.get(key)
    if hit is not None and now - hit[0] < _SLOW_TTL_S:
        return hit[1]
    value = fn()
    _slow_cache[key] = (now, value)
    return value


def dir_size(path: str) -> int:
    """Total bytes under `path` (0 when missing). Walks the tree: callers cache."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _disk_sizes() -> dict:
    from fused_render import calls
    from fused_render.index.config import load_config
    from fused_render.logs import log_dir

    def walk():
        return {"log_home": dir_size(log_dir()), "call_store": dir_size(calls.store_dir()),
                "index_db": dir_size(load_config().dir)}
    return _slow("disk_sizes", walk)


def _disk() -> dict:
    from fused_render.logs import log_dir
    path = log_dir()
    os.makedirs(path, exist_ok=True)
    return {"free_bytes": shutil.disk_usage(path).free, **_disk_sizes()}


def server_log_errors(path: str, limit: int = 20) -> list[str]:
    """The last `limit` ERROR-level lines of the log at `path`, reading only its
    last `LOG_TAIL_BYTES`. Traceback continuation lines are not ERROR lines and
    are left out; the level is the `%(levelname)s` field after the timestamp."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - LOG_TAIL_BYTES))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > LOG_TAIL_BYTES:
        lines = lines[1:]  # the first line is cut mid-way
    errs = [ln for ln in lines if " ERROR [" in ln]
    return errs[-limit:]


def boot_changes(rows: list[dict]) -> list[dict]:
    """The trail rows where `boot_id` differs from the row before: restarts."""
    out, prev = [], None
    for r in rows:
        bid = r.get("boot_id")
        if prev is not None and bid is not None and bid != prev:
            out.append({"t": r["t"], "boot_id": bid})
        if bid is not None:
            prev = bid
    return out


def _crashes() -> list[dict]:
    from fused_render import crashlog
    d = crashlog.crash_dir()
    out = []
    try:
        for name in os.listdir(d):
            try:
                st = os.stat(os.path.join(d, name))
            except OSError:
                continue
            out.append({"name": name, "bytes": st.st_size, "t": st.st_mtime})
    except OSError:
        return []
    out.sort(key=lambda c: -c["t"])
    return out[:10]


def _errors() -> dict:
    from fused_render.logs import log_path
    outages = health.read_outages(20)
    last = max((o.get("t_down") or 0 for o in outages), default=0)
    restarts = boot_changes(health.read_trail(86400))
    return {"outages": outages,
            "last_outage_age_s": round(time.time() - last, 1) if last else None,
            "server_log_errors": server_log_errors(log_path()),
            "crashes": _crashes(),
            "restarts_24h": len(restarts), "restarts": restarts[-10:]}


def calls_rollup(records: list[dict]) -> dict:
    """Per .py target: run_ms (the engine's own time) against overhead
    (`server_ms - run_ms`: queueing and serialisation), p50/p95 of each, and
    the five targets with the most p95 overhead. Superseded/aborted calls and
    records with no run_ms are not counted (calls._durations)."""
    from fused_render import calls
    acc: dict[str, dict] = {}
    for rec in records:
        server = calls._durations(rec)
        run = rec.get("run_ms")
        if server is None or not isinstance(run, (int, float)):
            continue
        name = rec.get("entrypoint") or rec.get("route") or "(unknown)"
        row = acc.setdefault(name, {"run": [], "over": []})
        row["run"].append(float(run))
        row["over"].append(max(0.0, server - float(run)))
    targets = []
    for name, row in acc.items():
        targets.append({"entrypoint": name, "count": len(row["run"]),
                        "run_p50_ms": calls._percentile(row["run"], 50),
                        "run_p95_ms": calls._percentile(row["run"], 95),
                        "overhead_p50_ms": calls._percentile(row["over"], 50),
                        "overhead_p95_ms": calls._percentile(row["over"], 95)})
    targets.sort(key=lambda t: -(t["overhead_p95_ms"] or 0))
    return {"window_s": CALLS_WINDOW_S, "targets": targets[:50], "top_overhead": targets[:5]}


def _calls() -> dict:
    from fused_render import calls
    since = time.time() - CALLS_WINDOW_S
    recs = [r for r in calls._iter_records(calls.store_files(), since=since)
            if (calls._epoch(r.get("occurred_at")) or 0) >= since]
    return calls_rollup(recs)


def _ai() -> dict:
    from fused_render.ai import supervisor
    from fused_render.server import ai_metrics
    snap = ai_metrics.snapshot(10)
    now = time.monotonic()
    with supervisor._lock:
        workers = list(supervisor._workers.values())
    return {"metrics": {k: snap.get(k) for k in
                        ("window", "totals", "tiers", "failure_types", "models", "last_completion_at")},
            "workers": [{"model": w.model, "capability": w.capability, "state": w.state,
                         "pid": w.pid, "in_flight": w.in_flight,
                         "rss": w.os_footprint_bytes or w.resident_bytes,
                         "idle_s": round(max(0.0, now - w.last_activity), 1)}
                        for w in workers]}


def _installs() -> dict:
    from fused_render import envinstall
    from fused_render.shell.storage import home_dir
    root = os.path.join(home_dir(), "cache", "_env_install")
    try:
        keys = [k for k in os.listdir(root) if envinstall.valid_key(k)]
    except OSError:
        keys = []
    now = time.time()
    running, failed = [], []
    for key in keys:
        prog = envinstall.progress(key)
        if not prog:
            continue
        if not prog.get("done"):
            try:
                began = os.path.getmtime(envinstall._claim_path(key))
            except OSError:
                began = None
            running.append({"key": key, "stage": prog.get("stage"), "pct": prog.get("pct"),
                            "detail": prog.get("detail"),
                            "elapsed_s": round(now - began, 1) if began else None})
        elif prog.get("error"):
            try:
                at = os.path.getmtime(envinstall._progress_path(key))
            except OSError:
                continue
            if now - at < 86400:
                failed.append({"key": key, "t": at, "error": str(prog["error"])[:300]})
    failed.sort(key=lambda f: -f["t"])
    return {"in_flight": running, "failures": failed[:5]}


def baseline_from_rows(rows: list[dict]) -> dict:
    def med(key):
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        return statistics.median(vals) if vals else None
    return {"load": med("load"), "swap_used": med("swap_used"), "rss": med("rss"),
            "samples": len(rows)}


def _baseline() -> dict:
    """24 h medians of load, swap and fused-render RSS from resources.jsonl."""
    return _slow("baseline", lambda: baseline_from_rows(health.read_trail(86400)))


class Timeline:
    """A ring of 1 Hz points, trimmed to `retain_s` behind the newest."""

    def __init__(self, retain_s: float = TIMELINE_S):
        self.retain_s = retain_s
        self._lock = threading.Lock()
        self._points: deque = deque(maxlen=int(retain_s) + 60)

    def add(self, point: dict) -> None:
        with self._lock:
            self._points.append(point)
            while self._points and point["t"] - self._points[0]["t"] > self.retain_s:
                self._points.popleft()

    def points(self, since: float | None = None) -> list[dict]:
        with self._lock:
            return [p for p in self._points if since is None or p["t"] >= since]


_TIMELINE = Timeline()


def timeline_point() -> dict:
    """One point for the ring: whatever each source can say right now (a source
    that fails reads None). Runs on the sampler thread, so the thread-pool
    figure is the last one `build_async` took on the loop."""
    from fused_render.server import serverstats

    def safe(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None

    summary = safe(lambda: netlog.get().summary()) or {}
    clients = summary.get("in_flight") or []
    host = safe(lambda: sysmon_sampler.get_sampler().payload("fused").get("host")) or {}
    rows = safe(lambda: health.read_trail(1800)) or []
    return {"t": time.time(), "rps": summary.get("rps_10s"),
            "in_flight": max((c.get("in_flight") or 0 for c in clients), default=0),
            "pool": (_fresh("pool") or {}).get("borrowed"),
            "lag_ms": safe(lambda: serverstats.loop_lag().current()),
            "cpu": host.get("cpuPct"),
            "swap": next((r["swap_used"] for r in reversed(rows)
                          if r.get("swap_used") is not None), None)}


def _tick() -> None:
    _TIMELINE.add(timeline_point())


def ensure_timeline_hook() -> None:
    """Have the sampler's 1 Hz tick feed the ring (it only ticks while watched)."""
    hooks = sysmon_sampler.get_sampler().tick_hooks
    if _tick not in hooks:
        hooks.append(_tick)


def _timeline() -> dict:
    ensure_timeline_hook()
    since = time.time() - TIMELINE_S
    events = [{"t": o["t_down"], "kind": "outage"} for o in health.read_outages(20)
              if (o.get("t_down") or 0) >= since]
    events += [{"t": b["t"], "kind": "boot"} for b in boot_changes(health.read_trail(1800))
               if b["t"] >= since]
    return {"points": _TIMELINE.points(since), "events": sorted(events, key=lambda e: e["t"])}


SECTIONS = {"net": _net, "server": _server, "memory": _memory, "cpu": _cpu,
            "claude": _claude, "index": _index, "jobs": _jobs, "disk": _disk,
            "errors": _errors, "calls": _calls, "ai": _ai, "installs": _installs,
            "baseline": _baseline, "timeline": _timeline}


_PAYLOAD_SECTIONS = {"memory", "cpu", "claude"}


async def build_async() -> dict:
    """`build()` for an async handler: the loop-bound reads first, on this
    thread, then everything that blocks on a worker thread."""
    import anyio.to_thread
    record_loop_state()
    return await anyio.to_thread.run_sync(build)


def build() -> dict:
    out: dict = {"t": time.time()}
    try:
        payload = _sampler_payload()  # one poll per build, shared by the three readers
    except Exception:  # noqa: BLE001 — each reader then reports its own error
        payload = None
    for name, fn in SECTIONS.items():
        try:
            out[name] = fn(payload) if name in _PAYLOAD_SECTIONS and payload is not None else fn()
        except Exception as e:  # noqa: BLE001 — one failing source never fails the page
            logger.debug("dashboard: %s failed", name, exc_info=True)
            out[name] = {"error": f"{type(e).__name__}: {e}"}
    out["verdict"] = sysmon_verdict.verdict(out)
    return out


_compact: tuple[float, dict] | None = None
_compact_lock = threading.Lock()
COMPACT_TTL_S = 10.0


def compact_verdict(ttl: float = COMPACT_TTL_S) -> dict:
    """`{level, headline}` for the chip, from a dashboard at most `ttl` s old.
    Single-flight: concurrent pollers wait on one build and share it, so a
    closed chip polling every second costs at most one build per `ttl`."""
    global _compact
    with _compact_lock:
        now = time.monotonic()
        if _compact is None or now - _compact[0] >= ttl:
            v = build()["verdict"]
            _compact = (time.monotonic(), {"level": v["level"], "headline": v["headline"]})
        return _compact[1]
