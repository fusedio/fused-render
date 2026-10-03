"""Shapes and arithmetic shared by every sysmon backend."""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

try:
    import pwd
except ImportError:  # Windows: no passwd database (and no sysmon backend)
    pwd = None


@dataclass
class ProcSample:
    pid: int
    cpu_ns: int          # cumulative user+system CPU time, ns
    footprint: int       # bytes; Activity Monitor's "Memory" on macOS
    rss: int             # bytes
    start: int           # backend-specific start stamp; tells a recycled pid apart


class ProcIdent(NamedTuple):
    """Who a process is, readable for EVERY pid on the machine (not only the
    current user's): the whole-machine Monitor page's row identity."""
    pid: int
    ppid: int
    uid: int
    start: float         # epoch seconds; with pid, tells a recycled pid apart
    name: str            # the kernel's short command name (p_comm / comm)
    zombie: bool


_USERS: dict[int, str] = {}


def user_name(uid: int | None) -> str | None:
    """Login name for `uid`, cached (the passwd lookup is the slow part)."""
    if uid is None:
        return None
    name = _USERS.get(uid)
    if name is None:
        try:
            name = pwd.getpwuid(uid).pw_name if pwd is not None else str(uid)
        except (KeyError, OSError):
            name = str(uid)
        _USERS[uid] = name
    return name


def cpu_percent(prev: ProcSample, cur: ProcSample, wall_s: float) -> float:
    """CPU % between two samples of one process; 100 = one core."""
    if wall_s <= 0:
        return 0.0
    return max(0.0, (cur.cpu_ns - prev.cpu_ns) / 1e9 / wall_s * 100.0)


def host_cpu_percent(prev: tuple, cur: tuple) -> dict:
    """Whole-machine CPU % from two `host_cpu_ticks()` readings, as a share of
    ALL cores (100 = every core busy), the way Activity Monitor's CPU Load
    graph reads."""
    user = cur[0] - prev[0]
    system = cur[1] - prev[1]
    total = (cur[2] - prev[2]) or 1
    return {"user": max(0.0, user / total * 100.0),
            "system": max(0.0, system / total * 100.0)}


def walk_descendants(root: int, children_of) -> dict[int, int]:
    """pid -> ppid for every descendant of `root` (root excluded)."""
    out: dict[int, int] = {}
    stack = [root]
    while stack:
        parent = stack.pop()
        for pid in children_of(parent):
            if pid in out or pid == root:
                continue
            out[pid] = parent
            stack.append(pid)
    return out
