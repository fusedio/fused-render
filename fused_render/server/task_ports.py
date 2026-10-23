"""The ports a task's session is listening on (Workspace scope, unit 4).

Asked for by the Workspace view only, for the rows it is showing that have a
live session — never part of the list payload, where one lsof per row on every
rebuild would be too slow and a port opening (which never touches the
transcript) would not ring the change feed anyway.

A task's processes are its live run's pid (the CLI, which leads its own process
group, written to `<run_dir>/pid` by the session hosts) and every descendant
(`sysmon.backend.descendants`). One `lsof -p a,b,c -iTCP -sTCP:LISTEN` per task
(`sysmon.detail.lsof_ports`). Answers are cached ~`TTL_S` per task so a Workspace
polling every few seconds costs one lsof per task per window. Platforms with no
sysmon backend (Windows) answer nothing: ports are simply not shown there.
Blocking: call from a worker thread."""
from __future__ import annotations

import logging
import os
import threading
import time

from fused_render import sysmon
from fused_render.sysmon import detail

logger = logging.getLogger(__name__)

TTL_S = 10.0
# First port of the IANA dynamic range. Port-0 binds (test workers, daemons,
# anything that asks the kernel for a port) land here on macOS and Linux, and a
# user never opens one of those, so they are not reported.
EPHEMERAL_FROM = 49152
_CACHE: dict[str, tuple[float, list[int]]] = {}
_LOCK = threading.Lock()


def reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _user_ports(ports) -> list[int]:
    """`ports` without the dynamic range and without repeats (one listener
    shows up once per address family), ascending."""
    return sorted({int(p) for p in ports if 0 < int(p) < EPHEMERAL_FROM})


def _run_pid(run_dir: str) -> int:
    try:
        with open(os.path.join(run_dir, "pid"), encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return 0


def _tree(pid: int) -> list[int]:
    return [pid, *sysmon.backend.descendants(pid)]


def _live_pid(session_id: str, agent, runs) -> int:
    """The pid of `session_id`'s newest live run, 0 when it has none (`runs` is
    newest first, `project_queue.scan_runs`)."""
    for run in runs or []:
        if session_id in (run.get("sessions") or []):
            run_dir = str(run.get("run_dir") or "")
            try:
                if run_dir and agent._alive(run_dir):
                    return _run_pid(run_dir)
            except Exception:  # noqa: BLE001 — a probe that fails is "not live"
                continue
    return 0


def ports_for(ids, agent, runs) -> dict[str, list[int]]:
    """`{task key: [port, ...]}` for the keys with a live run that is listening
    on something. A key with no live run, no listener, or no way to look is
    simply absent."""
    if sysmon.backend is None or agent is None:
        return {}
    out: dict[str, list[int]] = {}
    now = time.monotonic()
    for key in dict.fromkeys(str(i) for i in ids or []):
        with _LOCK:
            hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < TTL_S:
            ports = hit[1]
        else:
            ports = []
            try:
                pid = _live_pid(key, agent, runs)
                if pid:
                    ports = _user_ports(detail.lsof_ports(_tree(pid)))
            except Exception:  # noqa: BLE001 — ports are a garnish
                logger.debug("task ports: %s failed", key, exc_info=True)
            with _LOCK:
                _CACHE[key] = (now, ports)
        if ports:
            out[key] = list(ports)
    return out
