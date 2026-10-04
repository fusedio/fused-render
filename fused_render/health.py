"""Server identity, the outage record, and the resource trail (SPEC §50).

Three small things the diagnostics bundle joins on:

* **Boot identity.** `boot_id()` is minted once per process. `/api/health`
  returns it, the app log's boot line carries it, `server.json` carries it.
  A client that sees the id change across a "down" gap knows the server
  restarted; one that sees it unchanged knows the server was slow or the
  probe never arrived (D1).
* **Outage record.** `record_outage()` appends one JSON line per outage the
  shell observed (D2) — or per server-side event (`record_event`, e.g. the
  uvicorn thread dying) — to `<log home>/outages.jsonl`.
* **Resource trail.** `ResourceTrail` appends one line every `INTERVAL_S`
  to `<log home>/resources.jsonl`: this process's RSS, child RSS by kind,
  host memory, swap, load. Bounded to roughly a day (`MAX_LINES`). It uses
  the sysmon backend's process walk but runs regardless of the Monitor pref,
  which only governs the UI (D8).
"""
from __future__ import annotations

import collections
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time

from fused_render.logs import log_dir

logger = logging.getLogger(__name__)

_BOOT_ID = secrets.token_hex(6)
_STARTED_AT = time.time()


def boot_id() -> str:
    return _BOOT_ID


def started_at() -> float:
    return _STARTED_AT


def snapshot() -> dict:
    """The `/api/health` body. Dependency-free on purpose: no locks, no disk."""
    from fused_render import __version__

    now = time.time()
    return {
        "boot_id": _BOOT_ID,
        "pid": os.getpid(),
        "started_at": _STARTED_AT,
        "uptime_s": round(now - _STARTED_AT, 3),
        "version": __version__,
        "now": now,
    }


# ---- outages ---------------------------------------------------------------

_OUTAGE_LOCK = threading.Lock()
OUTAGE_FIELDS = (
    "t_down", "t_up", "strikes", "kinds", "boot_id_before", "boot_id_after",
    "visible", "page", "latencies_ms", "recovered",
)
MAX_OUTAGE_BYTES = 2_000_000


def outages_path() -> str:
    return os.path.join(log_dir(), "outages.jsonl")


def _append_jsonl(path: str, row: dict, lock: threading.Lock, max_bytes: int) -> None:
    line = json.dumps(row, separators=(",", ":"), default=str) + "\n"
    with lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > max_bytes:
                _truncate_half(path)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def _truncate_half(path: str) -> None:
    """Keep the newer half of a jsonl file. Called rarely, under the lock."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        cut = data.find(b"\n", len(data) // 2)
        if cut < 0:
            return
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data[cut + 1:])
        os.replace(tmp, path)
    except OSError:
        pass


def record_outage(record: dict, *, source: str = "client") -> dict:
    """Append one outage the client observed. Unknown keys are dropped, the
    server's own clock and boot id are stamped on, so a record from a tab that
    was open across a restart still says which server received it."""
    row = {k: record.get(k) for k in OUTAGE_FIELDS if k in record}
    row["kind"] = "outage"
    row["source"] = source
    row["received_at"] = time.time()
    row["boot_id_now"] = _BOOT_ID
    row["pid_now"] = os.getpid()
    _append_jsonl(outages_path(), row, _OUTAGE_LOCK, MAX_OUTAGE_BYTES)
    logger.warning(
        "outage reported by %s: down %s → up %s, %s strike(s) %s, boot %s → %s",
        source, row.get("t_down"), row.get("t_up"), row.get("strikes"),
        row.get("kinds"), row.get("boot_id_before"), row.get("boot_id_after"))
    return row


def record_event(kind: str, **fields) -> dict:
    """Append a server-side event (`server-thread-died`, `quit`, …)."""
    row = {"kind": kind, "source": "server", "received_at": time.time(),
           "boot_id_now": _BOOT_ID, "pid_now": os.getpid(), **fields}
    try:
        _append_jsonl(outages_path(), row, _OUTAGE_LOCK, MAX_OUTAGE_BYTES)
    except OSError:
        logger.debug("could not record event %s", kind, exc_info=True)
    return row


# ---- resource trail --------------------------------------------------------

INTERVAL_S = 20.0
MAX_LINES = 4400  # ~24 h at 20 s
_RING_CHECK_EVERY = 50


def resources_path() -> str:
    return os.path.join(log_dir(), "resources.jsonl")


def _load_average() -> list[float] | None:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def _swap_used_bytes() -> int | None:
    """macOS: `sysctl vm.swapusage` (one short subprocess, every 20 s).
    Linux: /proc/meminfo. Elsewhere None."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True,
                                 text=True, timeout=2, check=False).stdout
            # "total = 2048.00M  used = 1034.50M  free = 1013.50M  (encrypted)"
            for part in out.split("  "):
                part = part.strip()
                if part.startswith("used ="):
                    val = part.split("=", 1)[1].strip()
                    num, unit = float(val[:-1]), val[-1]
                    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}.get(unit, 1)
                    return int(num * mult)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return None
    if sys.platform.startswith("linux"):
        try:
            total = free = None
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("SwapTotal:"):
                        total = int(line.split()[1]) * 1024
                    elif line.startswith("SwapFree:"):
                        free = int(line.split()[1]) * 1024
            if total is not None and free is not None:
                return total - free
        except (OSError, ValueError):
            return None
    return None


class ResourceTrail:
    """Daemon thread appending one resource sample per interval."""

    def __init__(self, *, interval: float = INTERVAL_S, path: str | None = None,
                 backend=None):
        self.interval = interval
        self._path = path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._count = 0
        if backend is None:
            try:
                from fused_render import sysmon
                backend = sysmon.backend
            except Exception:  # noqa: BLE001
                backend = None
        self.backend = backend

    @property
    def path(self) -> str:
        return self._path or resources_path()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="fused-resource-trail",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # First sample right away so a bundle built a minute after boot has one.
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception:  # noqa: BLE001 — the trail must never take the server down
                logger.debug("resource sample failed", exc_info=True)
            self._stop.wait(self.interval)

    def sample(self) -> dict:
        """One sample as a dict (also what tests read)."""
        row: dict = {"t": time.time(), "boot_id": _BOOT_ID, "pid": os.getpid()}
        row["load"] = _load_average()
        row["swap_used"] = _swap_used_bytes()
        try:
            import resource  # noqa: PLC0415 — stdlib, POSIX only
            ru = resource.getrusage(resource.RUSAGE_SELF)
            row["max_rss"] = ru.ru_maxrss if sys.platform != "darwin" else ru.ru_maxrss
        except Exception:  # noqa: BLE001
            pass
        try:
            row["threads"] = threading.active_count()
        except Exception:  # noqa: BLE001
            pass
        backend = self.backend
        if backend is None:
            return row
        try:
            host = backend.host_memory()
        except Exception:  # noqa: BLE001
            host = None
        if host:
            row["host_total"] = host.get("total")
            row["host_used"] = host.get("used")
        try:
            me = backend.proc_sample(os.getpid())
            if me is not None:
                row["rss"] = me.rss
                row["footprint"] = me.footprint
        except Exception:  # noqa: BLE001
            pass
        try:
            kinds = self._children_by_kind(backend)
            row["children"] = kinds
        except Exception:  # noqa: BLE001
            logger.debug("child walk failed", exc_info=True)
        return row

    def _children_by_kind(self, backend) -> dict:
        from fused_render.sysmon import labels

        tree = backend.descendants(os.getpid())  # {pid: parent}
        try:
            owners = labels.registry_owners()
        except Exception:  # noqa: BLE001
            owners = {}
        out: dict[str, dict] = collections.defaultdict(lambda: {"n": 0, "rss": 0})
        pids = [p for p in tree if p != os.getpid()]
        for pid in pids:
            try:
                s = backend.proc_sample(pid)
            except Exception:  # noqa: BLE001
                s = None
            if s is None:
                continue
            kind = "other"
            cur = pid
            # Inherit the nearest claimed ancestor's kind, like the sampler.
            for _ in range(8):
                owner = owners.get(cur)
                if owner is not None:
                    kind = owner.kind
                    break
                cur = tree.get(cur)
                if cur is None or cur == os.getpid():
                    break
            bucket = out[kind]
            bucket["n"] += 1
            bucket["rss"] += s.rss
        return dict(out)

    def sample_once(self) -> dict:
        row = self.sample()
        path = self.path
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
            self._count += 1
            if self._count % _RING_CHECK_EVERY == 0:
                self._trim(path)
        return row

    @staticmethod
    def _trim(path: str) -> None:
        try:
            with open(path, "rb") as f:
                lines = f.readlines()
            if len(lines) <= MAX_LINES:
                return
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.writelines(lines[-MAX_LINES:])
            os.replace(tmp, path)
        except OSError:
            pass


_TRAIL: ResourceTrail | None = None
_TRAIL_LOCK = threading.Lock()


def start_resource_trail() -> ResourceTrail:
    """Start the process-wide trail once; return it."""
    global _TRAIL
    with _TRAIL_LOCK:
        if _TRAIL is None:
            _TRAIL = ResourceTrail()
            _TRAIL.start()
        return _TRAIL


def stop_resource_trail() -> None:
    with _TRAIL_LOCK:
        if _TRAIL is not None:
            _TRAIL.stop()
