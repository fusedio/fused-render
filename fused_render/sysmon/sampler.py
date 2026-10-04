"""The live sampler behind `GET /api/system/activity`.

A daemon thread samples once a second, but ONLY while someone has polled in the
last `IDLE_AFTER_S`: the first poll after an idle spell takes one sample inline
(host figures and process rows, CPU still null since CPU % needs two samples)
and starts the thread; the thread exits on its own once polls stop. With no
reader the cost is zero.

Each sample walks the process tree under this server (`os.getpid()`; on macOS
that is the app process itself) plus every live Claude run (detached, so
outside that tree), reads CPU time and memory per pid, and names each pid from
the registries (`labels.py`). A pid nobody claims inherits the kind of its
nearest claimed ancestor, so a tool a Claude run spawned reads as Claude.

WHOLE MACHINE, ON REQUEST (`?scope=all`, the shell's /monitor page): while
that scope has been polled in the last `IDLE_AFTER_S`, each sample also lists
every process on the machine (`backend.list_procs`, one sysctl on macOS),
reads CPU time and memory for each one the kernel lets this user read
(another user's pid keeps null figures, and its row stays), and keeps a
`RING_POINTS` ring of (cpuPct, memBytes) per pid for the page's per-process
graph. Once that scope goes unpolled the whole-machine state is dropped, so the
default scope costs exactly what it did before.

ONE THREAD. Exactly one sampling loop runs, however the scopes wake it: the
first poll after an idle spell starts it, and a first `scope=all` poll while
it already runs only takes one extra inline sample (so the rows appear at
once) — never a second loop. Two loops sharing one set of "previous" readings
measured deltas over a few milliseconds: the host read 0% (identical ticks)
and this server read ~100% (busy sampling inside the window).

CPU % IS A `WINDOW_S` (5 s) AVERAGE, the way Activity Monitor's default 5 s
refresh reads: every figure — a process's CPU, the host's, the totals — is
the delta between the newest reading and the oldest one still inside the
window (the last `_WINDOW_SAMPLES` samples), falling back to the longest
window available (at least `_MIN_WINDOW_S`) right after a start. Null until
there are two readings far enough apart. History and rings still take one
point a second; each point's value is that windowed figure. `host.windowS`
carries the window to the page.
"""
from __future__ import annotations

import collections
import logging
import os
import signal
import threading
import time

from fused_render import sysmon
from fused_render.sysmon import labels
from fused_render.sysmon.common import cpu_percent, host_cpu_percent, user_name

logger = logging.getLogger(__name__)

# The one place a process is signalled, so tests patch this, not `os.kill`.
_kill = os.kill

INTERVAL_S = 1.0
IDLE_AFTER_S = 15.0
HISTORY_S = 120
WINDOW_S = 5.0
_WINDOW_SAMPLES = 6        # newest + 5 one-second steps back
_MIN_WINDOW_S = 0.9        # below this a delta is noise, not a reading
RING_POINTS = 60
ARGS_CHARS = 200
_STAMP_TOLERANCE_S = 0.01  # start stamps travel as rounded epoch seconds
_ARGS_BYTES = 8192  # KERN_PROCARGS2 buffer per whole-machine pid; > ARGS_CHARS of argv


class Sampler:
    def __init__(self, backend=None, *, root_pid: int | None = None,
                 owners=labels.registry_owners, roots=None,
                 clock=time.monotonic, wall=time.time,
                 interval: float = INTERVAL_S, idle_after: float = IDLE_AFTER_S,
                 threaded: bool = True):
        self.backend = backend if backend is not None else sysmon.backend
        self.root_pid = root_pid
        self._owners = owners
        self._roots = roots if roots is not None else (lambda: labels.claude_roots(self.backend))
        self._clock = clock
        self._wall = wall
        self.interval = interval
        self.idle_after = idle_after
        self._threaded = threaded
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_poll = float("-inf")
        self._all_last_poll = float("-inf")
        self._sample_lock = threading.Lock()
        self._reset()

    @property
    def supported(self) -> bool:
        return self.backend is not None

    def _reset(self) -> None:
        # (monotonic t, {pid: ProcSample}, host ticks), oldest first: the
        # readings the 5 s CPU window is measured against.
        self._window: collections.deque = collections.deque(maxlen=_WINDOW_SAMPLES)
        self._rows: list[dict] = []
        self._refs: dict[int, tuple[str, dict, int]] = {}
        self._host: dict = {}
        # Trimmed by TIME in payload(); the cap only bounds memory.
        self._history: collections.deque = collections.deque(maxlen=HISTORY_S * 2)
        self._args: dict[tuple[int, int], list[str] | None] = {}
        self._totals: dict = {"cpuPct": None, "memBytes": None}
        self._reset_all()

    def _reset_all(self) -> None:
        """Forget the whole-machine state (prior samples, rows, rings)."""
        self._all_active = False
        self._all_rows: list[dict] = []
        self._all_stamps: dict[int, float] = {}
        self._all_args: dict[tuple[int, float], list[str] | None] = {}
        self._rings: dict[int, tuple[float, collections.deque]] = {}

    # ---- polling lifecycle --------------------------------------------------

    def poll(self, scope: str = "fused") -> dict:
        """Note a reader, make sure sampling runs, return the current payload.
        `scope="all"` also wakes (and keeps awake) the whole-machine sample."""
        if not self.supported:
            return {"supported": False, "host": None, "procs": [], "history": [],
                    "totals": {"cpuPct": None, "memBytes": None}}
        whole = scope == "all"
        with self._lock:
            now = self._clock()
            self._last_poll = now
            cold = not self._running
            if cold:
                self._running = True
                self._window.clear()
            all_cold = whole and not self._all_active
            if whole:
                self._all_last_poll = now
                self._all_active = True
        if cold:
            self._safe_sample()
            # `cold` means the previous loop has already decided to exit (it is
            # the only thing that clears `_running`), so this is the one loop.
            if self._threaded:
                thread = threading.Thread(target=self._loop, name="sysmon-sampler",
                                          daemon=True)
                try:
                    thread.start()
                    self._thread = thread
                except RuntimeError:
                    # No thread to be had (interpreter shutting down, thread
                    # limit): stay cold so the next poll tries again.
                    logger.debug("sysmon: sampler thread failed to start", exc_info=True)
                    with self._lock:
                        self._running = False
        elif all_cold:
            # The loop is already running: fill the whole-machine rows now
            # without a history point, so the 1 s history stays 1 s apart.
            self._safe_sample(record=False)
        return self.payload(scope)

    def tick(self) -> bool:
        """One step of the sampling loop: sample, or stop if nobody has polled
        within `idle_after`. Returns whether the loop should go on."""
        # `_sample_lock` too: an inline sample (a first scope=all poll on
        # another thread) reads the whole-machine state `_reset_all` replaces.
        # Same order sample() takes them in (sample lock, then state lock).
        with self._sample_lock, self._lock:
            now = self._clock()
            if self._all_active and now - self._all_last_poll >= self.idle_after:
                self._reset_all()
            if now - self._last_poll >= self.idle_after:
                self._running = False
                return False
        self._safe_sample()
        return True

    def _loop(self) -> None:
        while True:
            self._wake.wait(self.interval)
            if not self.tick():
                return

    @property
    def running(self) -> bool:
        return self._running

    # ---- one sample ---------------------------------------------------------

    def _safe_sample(self, record: bool = True) -> None:
        try:
            with self._sample_lock:
                self.sample(record=record)
        except Exception:  # noqa: BLE001 — a failed sample keeps the last one
            logger.debug("sysmon sample failed", exc_info=True)

    def _cmd(self, pid: int, start: int) -> list[str] | None:
        key = (pid, start)
        if key not in self._args:
            self._args[key] = self.backend.proc_args(pid)
        return self._args[key]

    def _windowed_cpu(self, s, t: float) -> float | None:
        """`s`'s CPU % over the oldest reading of the same process still
        inside the window, or None without one far enough back."""
        for wt, readings, _ticks in self._window:
            if t - wt > WINDOW_S + 0.5:
                continue
            if t - wt < _MIN_WINDOW_S:
                return None
            prev = readings.get(s.pid)
            if prev is not None and prev.start == s.start:
                return round(cpu_percent(prev, s, t - wt), 1)
        return None

    def _windowed_host(self, ticks, t: float) -> dict | None:
        if not ticks:
            return None
        for wt, _readings, prev in self._window:
            if t - wt > WINDOW_S + 0.5 or not prev:
                continue
            if t - wt < _MIN_WINDOW_S:
                return None
            return host_cpu_percent(prev, ticks)
        return None

    def sample(self, record: bool = True) -> None:
        """Take one reading. `record=False` (an extra inline sample between two
        loop ticks) refreshes the rows but adds no history or ring point."""
        b = self.backend
        t = self._clock()
        now = self._wall()
        root = self.root_pid or os.getpid()

        tree: dict[int, int] = {root: 0}
        try:
            tree.update(b.descendants(root))
        except Exception:  # noqa: BLE001
            logger.debug("sysmon: tree walk failed", exc_info=True)
        extra = dict(self._roots() or {})
        for pid in list(extra):
            if pid in tree:
                continue
            tree[pid] = b.parent_pid(pid) or 0
            try:
                for child, parent in b.descendants(pid).items():
                    tree.setdefault(child, parent)
            except Exception:  # noqa: BLE001
                pass
        owners = dict(self._owners() or {})
        owners.update(extra)

        samples = {}
        for pid in tree:
            if pid != root and b.is_zombie(pid):
                continue
            s = b.proc_sample(pid)
            if s is not None:
                samples[pid] = s

        resolved: dict[int, labels.Owner] = {}

        def resolve(pid: int, depth: int = 0) -> labels.Owner:
            if pid in resolved:
                return resolved[pid]
            if pid == root:
                owner = labels.Owner("app", "fused-render", {})
            elif pid in owners:
                owner = owners[pid]
            else:
                s = samples.get(pid)
                args = self._cmd(pid, s.start if s else 0)
                owner = labels.classify(pid, args)
                if owner is None:
                    name = labels.command_name(args)
                    parent = tree.get(pid, 0)
                    up = resolve(parent, depth + 1) if parent in tree and depth < 64 else None
                    if up is not None and up.kind not in ("app", "other"):
                        owner = labels.Owner(up.kind, f"{up.label} › {name}", {})
                    else:
                        owner = labels.Owner("other", name, {})
            resolved[pid] = owner
            return owner

        rows, refs = [], {}
        for pid, s in samples.items():
            owner = resolve(pid)
            cpu = self._windowed_cpu(s, t)
            try:
                started = b.start_time(pid)
            except Exception:  # noqa: BLE001
                started = None
            rows.append({"pid": pid, "ppid": tree.get(pid) or None, "kind": owner.kind,
                         "label": owner.label, "cpuPct": cpu, "memBytes": s.footprint,
                         "startedAt": started})
            refs[pid] = (owner.kind, owner.ref, s.start)
        rows.sort(key=lambda r: (-(r["cpuPct"] or 0.0), -(r["memBytes"] or 0)))

        mem = b.host_memory() or {}
        ticks = b.host_cpu_ticks()
        hcpu = self._windowed_host(ticks, t)
        host = {
            "windowS": WINDOW_S,
            "cpuPct": round(hcpu["user"] + hcpu["system"], 1) if hcpu else None,
            "cpuUser": round(hcpu["user"], 1) if hcpu else None,
            "cpuSystem": round(hcpu["system"], 1) if hcpu else None,
            "ncpu": os.cpu_count() or 1,
            "load": [round(x, 2) for x in os.getloadavg()] if hasattr(os, "getloadavg") else None,
            "memTotal": mem.get("total"),
            "memUsed": mem.get("used"),
            "memApp": mem.get("app"),
            "memWired": mem.get("wired"),
            "memCompressed": mem.get("compressed"),
        }
        cpus = [r["cpuPct"] for r in rows if r["cpuPct"] is not None]
        totals = {"cpuPct": round(sum(cpus), 1) if cpus else None,
                  "memBytes": sum(r["memBytes"] or 0 for r in rows)}

        whole = self._sample_all(t, now, samples, rows, record) if self._all_active else None

        with self._lock:
            readings = samples
            if whole is not None and self._all_active:
                (readings, self._all_rows, self._all_stamps, self._rings) = whole
            if record:
                # An inline extra sample stays out of the 6-slot window, so the
                # window keeps spanning ~5 s of one-second steps.
                self._window.append((t, readings, ticks))
            self._rows = rows
            self._refs = refs
            self._host = host
            self._totals = totals
            if len(self._args) > 512:
                live = {(p, s.start) for p, s in samples.items()}
                self._args = {k: v for k, v in self._args.items() if k in live}
            if record and (host["cpuPct"] is not None or totals["cpuPct"] is not None):
                self._history.append({"t": round(now, 3), "cpuPct": host["cpuPct"],
                                      "memUsed": host["memUsed"],
                                      "appCpuPct": totals["cpuPct"],
                                      "appMemBytes": totals["memBytes"]})

    def _all_cmd(self, pid: int, start: float) -> list[str] | None:
        key = (pid, start)
        if key not in self._all_args:
            self._all_args[key] = self.backend.proc_args(pid, _ARGS_BYTES)
        return self._all_args[key]

    def _sample_all(self, t: float, now: float, fused_samples: dict,
                    fused_rows: list[dict], record: bool = True):
        """One whole-machine pass: a row per process, windowed CPU %, and each
        pid's ring extended (when `record`). Returns the new (readings, rows,
        start stamps, rings) for `sample` to publish."""
        b = self.backend
        fused = {r["pid"]: r for r in fused_rows}
        prev_rings = self._rings
        samples: dict[int, object] = {}
        rows: list[dict] = []
        stamps: dict[int, float] = {}
        rings: dict[int, tuple[float, collections.deque]] = {}
        for ident in b.list_procs():
            pid = ident.pid
            if ident.zombie and pid not in fused:
                continue
            stamps[pid] = ident.start
            s = fused_samples.get(pid) or b.proc_sample(pid)
            cpu = mem = None
            if s is not None:
                samples[pid] = s
                mem = s.footprint
                cpu = fused[pid]["cpuPct"] if pid in fused else self._windowed_cpu(s, t)
            args = self._all_cmd(pid, ident.start) if pid > 0 else None
            command = labels._basename(args[0]) if args else ident.name
            f = fused.get(pid)
            rows.append({
                "pid": pid, "ppid": ident.ppid or None, "user": user_name(ident.uid),
                "command": command,
                "args": (" ".join(args) if args else ident.name)[:ARGS_CHARS],
                "cpuPct": cpu, "memBytes": mem, "startedAt": round(ident.start, 3),
                "fused": f is not None,
                "kind": f["kind"] if f else "system",
                "label": f["label"] if f else command,
            })
            if s is not None:
                old = prev_rings.get(pid)
                ring = old[1] if old is not None and old[0] == ident.start else \
                    collections.deque(maxlen=RING_POINTS)
                if record:
                    ring.append({"t": round(now, 3), "cpuPct": cpu, "memBytes": mem})
                rings[pid] = (ident.start, ring)
        rows.sort(key=lambda r: (-(r["cpuPct"] if r["cpuPct"] is not None else -1.0),
                                 -(r["memBytes"] or 0)))
        if len(self._all_args) > 4 * max(len(stamps), 256):
            live = set(stamps.items())
            self._all_args = {k: v for k, v in self._all_args.items() if k in live}
        return samples, rows, stamps, rings

    # ---- reading ------------------------------------------------------------

    def payload(self, scope: str = "fused") -> dict:
        with self._lock:
            cutoff = self._wall() - HISTORY_S
            procs = self._all_rows if scope == "all" and self._all_active else self._rows
            return {
                "supported": True,
                "host": dict(self._host) or None,
                "procs": [dict(r) for r in procs],
                "history": [dict(h) for h in self._history if h["t"] >= cutoff],
                "totals": dict(self._totals),
            }

    def lookup(self, pid: int) -> tuple[str, dict, int] | None:
        with self._lock:
            return self._refs.get(int(pid))

    def stamp(self, pid: int) -> float | None:
        """`pid`'s start stamp in the last whole-machine sample, or None."""
        with self._lock:
            return self._all_stamps.get(int(pid))

    def proc_history(self, pids) -> dict[str, list[dict]]:
        """The last `RING_POINTS` (t, cpuPct, memBytes) readings per pid; an
        empty list for a pid with none (gone, unreadable, or scope=all idle)."""
        with self._lock:
            return {str(int(p)): [dict(x) for x in self._rings[int(p)][1]]
                    if int(p) in self._rings else [] for p in pids}


class StopRefused(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _check_started(current: float | None, started_at) -> None:
    """The client names a process by (pid, startedAt); refuse if that pid now
    belongs to a different process (404 "process changed"). `current` is the
    pid's start stamp as the caller read it now."""
    try:
        claimed = float(started_at)
    except (TypeError, ValueError):
        raise StopRefused("startedAt is required") from None
    if current is None:
        raise StopRefused("process already exited", 404)
    if abs(current - claimed) > _STAMP_TOLERANCE_S:
        raise StopRefused("process changed", 404)


def stop(sampler: Sampler, pid: int, started_at=None) -> str:
    """End one of fused-render's processes through its owner's own API, or
    SIGTERM it when it has none. Returns how it was stopped. Raises
    StopRefused for the app itself, a pid the last sample did not report
    (someone else's process), a pid that has since been recycled, or one whose
    start stamp no longer matches the `started_at` the client saw."""
    found = sampler.lookup(pid)
    if found is None:
        raise StopRefused("not one of fused-render's processes", 404)
    kind, ref, start = found
    if kind == "app":
        raise StopRefused("refusing to stop fused-render itself")
    b = sampler.backend
    current = b.proc_sample(pid) if b is not None else None
    if current is None or current.start != start:
        raise StopRefused("process already exited", 404)
    _check_started(b.start_time(pid), started_at)

    if ref.get("engine_id"):
        from fused_render.server import engine_host
        engine_host.stop(ref["engine_id"])
        return "engine"
    if ref.get("model"):
        from fused_render.ai import supervisor
        supervisor.unload(model=ref["model"], capability=ref.get("capability"),
                          reason="Stopped from Monitor")
        return "model"
    if ref.get("apple"):
        from fused_render.ai.apple import host
        if host.cancel_pid(int(pid)):
            return "model"
    if ref.get("run_id"):
        try:
            from fused_render import project_queue
            agent = project_queue.agent_module()
            if agent is not None:
                agent._cancel(ref["run_id"], interrupt_first=False)
                return "claude"
        except Exception:  # noqa: BLE001 — fall through to a plain signal
            logger.debug("sysmon: agent cancel failed", exc_info=True)
    if ref.get("terminal"):
        from fused_render import pty_session
        session = pty_session.REGISTRY.get(ref["terminal"])
        if session is not None:
            session.kill()
            return "terminal"
    _kill(int(pid), signal.SIGTERM)
    return "signal"


def kill(sampler: Sampler, pid: int, force: bool = False, started_at=None) -> dict:
    """SIGTERM (SIGKILL with `force`) any process on the machine the last
    whole-machine sample listed. Refuses pid 0/1/negative and fused-render
    itself (400), and a pid that sample did not list or whose start stamp has
    changed since — a recycled pid — (404). A pid this user may not signal is
    an answer, not an error: `{"ok": False, "error": "permission denied"}`."""
    pid = int(pid)
    if pid <= 1:
        raise StopRefused(f"refusing to signal pid {pid}")
    if pid in {sampler.root_pid or os.getpid(), os.getpid()}:
        raise StopRefused("refusing to kill fused-render itself")
    stamp = sampler.stamp(pid)
    if stamp is None:
        raise StopRefused("not in the last sample", 404)
    b = sampler.backend
    current = b.proc_ident(pid) if b is not None else None
    if current is None or current.zombie or abs(current.start - stamp) > _STAMP_TOLERANCE_S:
        raise StopRefused("process already exited", 404)
    # From the KERN_PROC ident, not b.start_time(): that reads proc_pidinfo,
    # which macOS refuses for another user's pid.
    _check_started(current.start, started_at)
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        _kill(pid, sig)
    except PermissionError:
        return {"ok": False, "error": "permission denied"}
    except ProcessLookupError:
        raise StopRefused("process already exited", 404) from None
    return {"ok": True, "signal": sig.name}


_SAMPLER: Sampler | None = None
_SAMPLER_LOCK = threading.Lock()


def get_sampler() -> Sampler:
    global _SAMPLER
    with _SAMPLER_LOCK:
        if _SAMPLER is None:
            _SAMPLER = Sampler()
        return _SAMPLER
