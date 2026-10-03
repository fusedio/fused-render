"""Best-effort Linux backend over /proc. Memory is RSS (Linux has no cheap
phys_footprint analogue); everything else mirrors macos.py."""
from __future__ import annotations

import os

from fused_render.sysmon.common import ProcIdent, ProcSample, walk_descendants

_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _stat_fields(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the `(comm)` field, so index 0 is state."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return None
    _, _, rest = raw.rpartition(")")
    return rest.split()


def proc_sample(pid: int) -> ProcSample | None:
    f = _stat_fields(pid)
    if not f or len(f) < 22:
        return None
    try:
        cpu_ns = int((int(f[11]) + int(f[12])) * 1e9 / _TCK)
        rss = int(f[21]) * _PAGE
        start = int(f[19])
    except ValueError:
        return None
    return ProcSample(int(pid), cpu_ns, rss, rss, start)


def parent_pid(pid: int) -> int | None:
    f = _stat_fields(pid)
    try:
        return int(f[1]) if f else None
    except (ValueError, IndexError):
        return None


_BOOT: float | None = None


def _boot_time() -> float | None:
    global _BOOT
    if _BOOT is None:
        try:
            with open("/proc/stat", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("btime"):
                        _BOOT = float(line.split()[1])
                        break
        except (OSError, ValueError, IndexError):
            return None
    return _BOOT


def start_time(pid: int) -> float | None:
    f = _stat_fields(pid)
    boot = _boot_time()
    try:
        return boot + int(f[19]) / _TCK if f and boot is not None else None
    except (ValueError, IndexError):
        return None


def is_zombie(pid: int) -> bool:
    f = _stat_fields(pid)
    return not f or f[0] in ("Z", "X")


def descendants(root: int) -> dict[int, int]:
    children: dict[int, list[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return {}
    for name in names:
        if not name.isdigit():
            continue
        ppid = parent_pid(int(name))
        if ppid is not None:
            children.setdefault(ppid, []).append(int(name))
    return walk_descendants(root, lambda p: children.get(p, []))


def proc_ident(pid: int) -> ProcIdent | None:
    """One process's identity from /proc/<pid>/stat; readable for any user's."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        uid = os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None
    head, _, rest = raw.rpartition(")")
    name = head.partition("(")[2]
    f = rest.split()
    boot = _boot_time() or 0.0
    try:
        return ProcIdent(int(pid), int(f[1]), uid, boot + int(f[19]) / _TCK, name,
                         f[0] in ("Z", "X"))
    except (ValueError, IndexError):
        return None


def list_procs() -> list[ProcIdent]:
    """Every process on the machine: one ProcIdent per /proc/<pid>."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    out = []
    for name in names:
        if name.isdigit():
            ident = proc_ident(int(name))
            if ident is not None:
                out.append(ident)
    return out


def list_pids() -> list[int]:
    try:
        return [int(n) for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return []


def proc_args(pid: int, limit: int | None = None) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read(limit or -1)
    except OSError:
        return None
    return [p.decode("utf-8", "replace") for p in raw.rstrip(b"\0").split(b"\0") if p] or None


def host_memory() -> dict | None:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                parts = value.split()
                if parts:
                    info[key] = int(parts[0]) * 1024
    except (OSError, ValueError):
        return None
    total = info.get("MemTotal")
    if not total:
        return None
    used = total - info.get("MemAvailable", info.get("MemFree", 0))
    return {"total": total, "used": used, "app": used, "wired": None, "compressed": None}


def host_cpu_ticks() -> tuple[int, int, int] | None:
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            first = fh.readline().split()
        nums = [int(x) for x in first[1:9]]
    except (OSError, ValueError):
        return None
    while len(nums) < 8:
        nums.append(0)
    user, nice, system, idle, iowait, irq, softirq, steal = nums
    return user + nice, system + irq + softirq, sum(nums)
