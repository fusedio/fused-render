"""`GET /api/system/dashboard`: one snapshot of everything that can make the app
feel slow, and the verdict over it (sysmon/verdict.py).

Each section is built by its own function and isolated: one that raises becomes
`{"error": "..."}` for that section and the rest still answer. The slow or
platform-specific reads (swap trail, `sysctl`, `pmset`) are cached 30 s.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time

from fused_render import health, netlog
from fused_render.sysmon import sampler as sysmon_sampler
from fused_render.sysmon import verdict as sysmon_verdict

logger = logging.getLogger(__name__)

SWAP_WINDOW_S = 600.0
_MACHINE_TTL_S = 30.0
_machine_cache: dict[str, tuple[float, object]] = {}


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
        value = None
    _machine_cache[key] = (now, value)
    return value


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=2.0,
                          close_fds=True).stdout


def _pressure() -> str | None:
    if sys.platform != "darwin":
        return None

    def read():
        level = int(_run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip())
        return {1: "normal", 2: "warn", 4: "critical"}.get(level)
    return _cached("pressure", read)


def _power() -> dict | None:
    """Thermal speed limit and Low Power Mode (macOS), else None."""
    if sys.platform != "darwin":
        return None

    def read():
        limit = None
        for line in _run(["pmset", "-g", "therm"]).splitlines():
            if "CPU_Speed_Limit" in line:
                limit = int(line.split("=")[-1].strip())
        low = None
        for line in _run(["pmset", "-g"]).splitlines():
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
    return {"pool": serverstats.pool_stats(),
            "loop_lag": {"current_ms": lag.current(), "max_60s_ms": lag.max_over(60)},
            "sockets": netlog.get().socket_total(),
            "engines": serverstats.engine_workers()}


def _memory() -> dict:
    host = (_sampler_payload().get("host")) or {}
    now = time.time()
    rows = health.read_trail(1800)
    procs = [p for p in _sampler_payload().get("procs", []) if p.get("memBytes")]
    big = max(procs, key=lambda p: p["memBytes"], default=None)
    return {"used": host.get("memUsed"), "total": host.get("memTotal"),
            "swap_used": next((r["swap_used"] for r in reversed(rows)
                               if r.get("swap_used") is not None), None),
            "swap_growth_10m_bytes": swap_growth(rows, now, SWAP_WINDOW_S),
            "swap_growth_30m_bytes": swap_growth(rows, now, 1800),
            "pressure": _pressure(),
            "biggest": ({"pid": big["pid"], "label": big["label"], "mem": big["memBytes"]}
                        if big else None)}


def _cpu() -> dict:
    host = (_sampler_payload().get("host")) or {}
    power = _power() or {}
    return {"cpu_pct": host.get("cpuPct"), "load": host.get("load"), "ncpu": host.get("ncpu"),
            "thermal_limit": power.get("thermal_limit"), "low_power": power.get("low_power")}


def _claude() -> dict:
    return _sampler_payload().get("claude") or {"total": 0, "fused": 0, "other": 0,
                                                 "mem_bytes": 0, "cpu_pct": 0.0, "top": []}


def _index() -> dict:
    from fused_render.server.routers import index
    return index.lane_stats()


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


def _disk() -> dict:
    from fused_render.logs import log_dir
    path = log_dir()
    os.makedirs(path, exist_ok=True)
    return {"free_bytes": shutil.disk_usage(path).free}


def _errors() -> dict:
    outages = health.read_outages(20)
    last = max((o.get("t_down") or 0 for o in outages), default=0)
    return {"outages": outages,
            "last_outage_age_s": round(time.time() - last, 1) if last else None}


SECTIONS = {"net": _net, "server": _server, "memory": _memory, "cpu": _cpu,
            "claude": _claude, "index": _index, "jobs": _jobs, "disk": _disk,
            "errors": _errors}


def build() -> dict:
    out: dict = {"t": time.time()}
    for name, fn in SECTIONS.items():
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001 — one failing source never fails the page
            logger.debug("dashboard: %s failed", name, exc_info=True)
            out[name] = {"error": f"{type(e).__name__}: {e}"}
    out["verdict"] = sysmon_verdict.verdict(out)
    return out


_compact: tuple[float, dict] | None = None


def compact_verdict(ttl: float = 2.0) -> dict:
    """`{level, headline}` for the chip, from a dashboard at most `ttl` s old."""
    global _compact
    now = time.monotonic()
    if _compact is None or now - _compact[0] >= ttl:
        v = build()["verdict"]
        _compact = (now, {"level": v["level"], "headline": v["headline"]})
    return _compact[1]
