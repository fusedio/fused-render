"""Name each process the sampler finds by joining fused-render's own registries.

Every registry is read defensively: an import that fails or a module whose
shape moved costs that registry's labels, never the sample. Each reader
returns `{pid: Owner}`; `Owner.ref` is what the stop endpoint needs to end the
process through its owner's own API instead of a bare signal.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Owner:
    kind: str       # app | claude | engine | model | terminal | install | run | other
    label: str
    ref: dict = field(default_factory=dict, compare=False, hash=False)


def _basename(path: str) -> str:
    return os.path.basename(str(path).rstrip("/\\")) or str(path)


def _short_model(model: str) -> str:
    return str(model).rstrip("/").rsplit("/", 1)[-1] or str(model)


def _engines(out: dict[int, Owner]) -> None:
    from fused_render.server import engine_host
    with engine_host._lock:
        children = list(engine_host._children.values())
    for c in children:
        pid = int(getattr(c, "pid", 0) or 0)
        if pid <= 0:
            continue
        engine_id = str(getattr(c, "engine_id", ""))
        folder = str(getattr(c, "folder", "") or "")
        if engine_id.startswith("bg_") and folder:
            label = f"App: {_basename(folder)}"
        elif folder:
            label = f"Engine: {_basename(folder)}"
        else:
            label = f"Engine: {engine_id}"
        out[pid] = Owner("engine", label, {"engine_id": engine_id})


def _models(out: dict[int, Owner]) -> None:
    from fused_render.ai import supervisor
    with supervisor._lock:
        workers = list(supervisor._workers.values())
    for w in workers:
        pid = int(getattr(w, "pid", 0) or 0)
        if pid <= 0:
            continue
        model = str(getattr(w, "model", ""))
        out[pid] = Owner("model", f"Model: {_short_model(model)}",
                         {"model": model, "capability": getattr(w, "capability", None)})


def _apple(out: dict[int, Owner]) -> None:
    from fused_render.ai.apple import host
    with host._lock:
        procs = list(host._text_children)
    for p in procs:
        if getattr(p, "pid", None) and p.poll() is None:
            out[int(p.pid)] = Owner("model", "Model: Apple on-device", {"apple": True})


def _terminals(out: dict[int, Owner]) -> None:
    from fused_render import pty_session
    for s in pty_session.REGISTRY.list():
        proc = getattr(s, "proc", None)
        if proc is not None and getattr(s, "alive", False):
            out[int(proc.pid)] = Owner("terminal", "Terminal", {"terminal": s.id})


def _installs(out: dict[int, Owner]) -> None:
    from fused_render import envinstall
    for pid in list(envinstall._SPAWNED):
        out[int(pid)] = Owner("install", "Installing env", {})


_READERS = (_engines, _models, _apple, _terminals, _installs)


def registry_owners() -> dict[int, Owner]:
    """pid -> Owner for every process a registry in this server tracks."""
    out: dict[int, Owner] = {}
    for reader in _READERS:
        try:
            reader(out)
        except Exception:  # noqa: BLE001 — one broken registry costs only its labels
            logger.debug("sysmon: %s failed", reader.__name__, exc_info=True)
    return out


# ---- Claude runs ------------------------------------------------------------
# A run's CLI is detached (setsid, reparented to init), so it is NOT under the
# server's process tree; the run dirs are how the sampler finds it. Both the
# session host and the CLI it spawns are reported, under the same label.

_CLAUDE_TTL_S = 5.0
_claude_cache: tuple[float, dict[int, Owner]] | None = None


def run_title(meta: dict) -> str:
    text = str(meta.get("title") or meta.get("message") or "").strip()
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = re.sub(r"\s+", " ", line)
    if len(line) > 48:
        line = line[:47].rstrip() + "…"
    return line


def _is_claude_argv(args: list[str] | None) -> bool:
    """A Claude CLI or the session host that runs one, by its command line."""
    if not args:
        return False
    if any("session_host" in a for a in args[:4]):
        return True
    return any(_basename(a) == "claude" or _basename(a).startswith("claude-")
               for a in args[:3])


def _is_run_process(backend, pid: int, run_dir: str) -> bool:
    """Whether `pid` (read from `run_dir/pid`) is still that run's process and
    not a recycled pid: its argv must be a Claude CLI or session host, and it
    must have started no later than the pid file was written (a process that
    reuses the pid starts after the original exited, so after the write)."""
    if not _is_claude_argv(backend.proc_args(pid)):
        return False
    try:
        written = os.path.getmtime(os.path.join(run_dir, "pid"))
        started = backend.start_time(pid)
    except (OSError, AttributeError):
        return True
    return started is None or started <= written + 2.0


def _claude_runs(backend) -> dict[int, Owner]:
    from fused_render import project_queue
    agent = project_queue.agent_module()
    if agent is None:
        return {}
    out: dict[int, Owner] = {}
    for run in project_queue.scan_runs(agent):
        pid_s = project_queue.run_pid(run["run_dir"])
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        if backend.is_zombie(pid) or backend.proc_sample(pid) is None:
            continue
        if not _is_run_process(backend, pid, run["run_dir"]):
            continue  # the pid file outlived its process and the pid was reused
        title = run_title(run.get("meta") or {})
        owner = Owner("claude", f"Claude: {title}" if title else "Claude",
                      {"run_id": run["run_id"]})
        out[pid] = owner
        host = backend.parent_pid(pid)
        if host and host > 1:
            args = backend.proc_args(host) or []
            if any("session_host" in a for a in args):
                out[host] = owner
    return out


def claude_roots(backend, now: float | None = None) -> dict[int, Owner]:
    """pid -> Owner for every live Claude run (CLI + its session host),
    cached for a few seconds: it reads run dirs off disk."""
    global _claude_cache
    now = time.monotonic() if now is None else now
    if _claude_cache is not None and now - _claude_cache[0] < _CLAUDE_TTL_S:
        return _claude_cache[1]
    try:
        found = _claude_runs(backend)
    except Exception:  # noqa: BLE001
        logger.debug("sysmon: claude run scan failed", exc_info=True)
        found = {}
    _claude_cache = (now, found)
    return found


def _claude_session_name(pid: int) -> str:
    try:
        from fused_render import tasks_watch
        sid = tasks_watch.session_for_pid(pid)
        row = tasks_watch.registry_row(sid) if sid else None
        return str((row or {}).get("name") or "")
    except Exception:  # noqa: BLE001
        return ""


# ---- by command line ----------------------------------------------------------

def _is_python(exe: str) -> bool:
    return exe.lower().startswith("python")


def command_name(args: list[str] | None) -> str:
    """A short human name for a command line: the script or module for a
    Python process, the executable's basename otherwise."""
    if not args:
        return "process"
    exe = _basename(args[0])
    if not _is_python(exe):
        return exe
    rest = args[1:]
    for i, a in enumerate(rest):
        if a == "-m" and i + 1 < len(rest):
            module = rest[i + 1]
            if module.startswith("fused_render."):
                # fused_render.index.worker -> "fused-render index worker"
                tail = module[len("fused_render."):].replace(".", " ").replace("_", " ")
                return f"fused-render {tail}"
            return module
        if a == "-c":
            return "python -c"
        if not a.startswith("-"):
            return _basename(a)
    return exe


def classify(pid: int, args: list[str] | None) -> Owner | None:
    """An Owner for a process recognisable from its command line alone."""
    if not args:
        return None
    exe = _basename(args[0])
    if exe == "claude":
        name = _claude_session_name(pid)
        return Owner("claude", f"Claude: {name}" if name else "Claude", {})
    if _is_python(exe) and any(_basename(a) == "_child.py" for a in args[1:3]):
        return Owner("run", "Run: Python", {})
    return None
