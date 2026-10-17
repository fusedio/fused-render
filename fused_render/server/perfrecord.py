"""Record 60 s: capture what the Monitor would show over a minute, for a bug
report. While recording the "watched" flag and the sampler stay on, the
dashboard is snapshotted every 2 s, and at the end one JSON bundle (snapshots,
the window's request records, group and socket rollups, the process list at
both ends) is written to `<log home>/perf-recordings/<ts>.json`. The last 10
are kept. Request paths carry keys only, never query values (netlog.full_path).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from fused_render import netlog

logger = logging.getLogger(__name__)

DURATION_S = 60.0
INTERVAL_S = 2.0
KEEP = 10


def recordings_dir() -> str:
    from fused_render.logs import log_dir
    return os.path.join(log_dir(), "perf-recordings")


def _snapshot() -> dict:
    from fused_render.server import dashboard
    return dashboard.build()


def _procs() -> list[dict]:
    from fused_render.server.routers.system import redact_args
    from fused_render.sysmon import sampler
    rows = sampler.get_sampler().poll("fused").get("procs", [])
    return [{**r, "args": redact_args(r["args"])} if "args" in r else dict(r) for r in rows]


class Recorder:
    def __init__(self, duration: float = DURATION_S, interval: float = INTERVAL_S):
        self.duration = duration
        self.interval = interval
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._deadline = 0.0

    def start(self) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {**self.status(), "already": True}
            self._stop.clear()
            self._deadline = time.monotonic() + self.duration
            netlog.get().force_watch(self.duration + 5)
            self._thread = threading.Thread(target=self._run, name="perf-record", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    def _recording(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        rec = self._recording()
        return {"recording": rec,
                "seconds_left": round(max(0.0, self._deadline - time.monotonic()), 1) if rec else 0,
                "recordings": self.list()}

    def list(self) -> list[dict]:
        d = recordings_dir()
        try:
            names = sorted((n for n in os.listdir(d) if n.endswith(".json")), reverse=True)
        except OSError:
            return []
        out = []
        for n in names:
            try:
                out.append({"name": n, "bytes": os.path.getsize(os.path.join(d, n))})
            except OSError:
                pass
        return out

    def _run(self) -> None:
        began = time.time()
        snapshots: list[dict] = []
        procs_start = self._safe(_procs, [])
        while not self._stop.is_set():
            snapshots.append(self._safe(_snapshot, {"error": "snapshot failed"}))
            if time.monotonic() + self.interval >= self._deadline:
                break
            self._stop.wait(self.interval)
        log = netlog.get()
        bundle = {
            "started": began, "ended": time.time(), "snapshots": snapshots,
            "requests": self._safe(lambda: log.requests(include_self=True)["rows"], []),
            "groups": self._safe(lambda: log.groups(), []),
            "sockets": self._safe(lambda: log.sockets(), []),
            "procs_start": procs_start,
            "procs_end": self._safe(_procs, []),
        }
        try:
            self._write(bundle, began)
        except OSError:
            logger.warning("perf recording could not be written", exc_info=True)

    @staticmethod
    def _safe(fn, default):
        try:
            return fn()
        except Exception:  # noqa: BLE001 — a failed source costs its field only
            logger.debug("perf record: %s failed", getattr(fn, "__name__", fn), exc_info=True)
            return default

    def _write(self, bundle: dict, began: float) -> None:
        d = recordings_dir()
        os.makedirs(d, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(began))
        path = os.path.join(d, f"{stamp}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(bundle, f, default=str)
        os.replace(tmp, path)
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
        for old in names[:-KEEP]:
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass


_RECORDER = Recorder()
