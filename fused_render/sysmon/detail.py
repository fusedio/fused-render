"""Everything the Monitor's detail drawer shows about one process. Each field
is read on its own and falls back to None / [] so a process that exits or
refuses a read costs that field, never the answer. Blocking (lsof, procfs):
call from a worker thread."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess

from fused_render.sysmon import labels

logger = logging.getLogger(__name__)

_PARENT_DEPTH = 12
_LSOF_TIMEOUT_S = 2.0
_PORT = re.compile(r":(\d+)\s+\(LISTEN\)")


def lsof_ports(pids: list[int]) -> list[int]:
    """TCP ports any of `pids` listens on, in ONE lsof call (macOS ships it,
    many Linux boxes too). [] for no pids, no lsof, or a call that failed."""
    ids = sorted({int(p) for p in pids if int(p) > 0})
    if not ids:
        return []
    # Absolute exe + close_fds=False + no cwd: posix_spawn, never a fork() (a
    # fork with libproj resident dies with SIGSEGV; see app_git.py).
    exe = next((p for p in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.path.exists(p)),
               None) or shutil.which("lsof")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [os.path.abspath(exe), "-nP", "-a", "-p", ",".join(map(str, ids)),
             "-iTCP", "-sTCP:LISTEN"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_LSOF_TIMEOUT_S, close_fds=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({int(m.group(1)) for m in _PORT.finditer(proc.stdout or "")})


def _lsof_ports(pid: int) -> list[int]:
    """TCP ports `pid` listens on."""
    return lsof_ports([pid])


def _venv_of(args: list[str] | None) -> str | None:
    """The venv an interpreter belongs to (`<venv>/bin/python`), or None."""
    if not args:
        return None
    exe = os.path.normpath(args[0])
    parts = exe.split(os.sep)
    if len(parts) >= 3 and parts[-2] == "bin" and (
            parts[-3] in (".venv", "venv") or labels._in_shared_venvs(exe)):
        return os.path.dirname(os.path.dirname(exe))
    return None


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        logger.debug("sysmon detail: %s failed", getattr(fn, "__name__", fn), exc_info=True)
        return default


def process_detail(sampler, pid: int, redact) -> dict | None:
    """The detail for `pid`, or None when no such process exists."""
    b = sampler.backend
    if b is None:
        return None
    ident = _safe(lambda: b.proc_ident(pid))
    if ident is None or getattr(ident, "zombie", False):
        return None
    args = _safe(lambda: b.proc_args(pid, 8192))
    rows = {r["pid"]: r for r in sampler.payload("all")["procs"]}

    found = sampler.lookup(pid)
    if found is not None:
        kind, ref, _start = found
        label = (rows.get(pid) or {}).get("label") or labels.command_name(args)
        owner = {"kind": kind, "label": label, "ref": ref}
    else:
        guess = _safe(lambda: labels.classify(pid, args))
        kind, label = (guess.kind, guess.label) if guess else ("other", labels.command_name(args))
        owner = {"kind": kind, "label": (rows.get(pid) or {}).get("label") or label, "ref": {}}

    parents, cur, seen = [], ident.ppid, {pid}
    while cur and cur > 0 and cur not in seen and len(parents) < _PARENT_DEPTH:
        seen.add(cur)
        pargs = _safe(lambda c=cur: b.proc_args(c, 8192))
        parents.append({"pid": cur, "label": (rows.get(cur) or {}).get("label")
                        or labels.command_name(pargs)})
        pi = _safe(lambda c=cur: b.proc_ident(c))
        cur = pi.ppid if pi else None

    cwd_reader = getattr(b, "cwd", None)
    return {
        "pid": int(pid),
        "argv": redact(args) if args else [],
        "cwd": _safe(lambda: cwd_reader(pid)) if cwd_reader else None,
        "parents": parents,
        "owner": owner,
        "venv": _venv_of(args),
        "started_at": ident.start,
        "ports": _safe(lambda: _lsof_ports(pid), []),
    }
