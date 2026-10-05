"""macOS process and host metrics through libproc and the mach host API (ctypes,
stdlib only; psutil is deliberately not a core dependency, see ai/fit.py).

The numbers match Activity Monitor:

* per-process Memory is `phys_footprint` from `proc_pid_rusage`;
* per-process CPU % is the user+system CPU-time delta over the wall delta
  (100 = one core);
* host Memory Used is app (internal - purgeable) + wired + compressed, from
  `host_statistics64`;
* host CPU % comes from HOST_CPU_LOAD_INFO tick deltas.

Only the current user's processes can be read: the kernel refuses
`proc_pid_rusage` for anyone else's, and every reader here returns None then.

The process tree is walked with `proc_listchildpids` and each process's argv
is read with the KERN_PROCARGS2 sysctl, so a sample spawns no subprocess.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct

from fused_render.sysmon.common import ProcIdent, ProcSample, walk_descendants

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


class _RusageV0(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "user_time system_time pkg_idle_wkups interrupt_wkups pageins wired_size "
        "resident_size phys_footprint proc_start_abstime proc_exit_abstime").split()]


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_tb = _Timebase()
_libc.mach_timebase_info(ctypes.byref(_tb))
_ABS_TO_NS = _tb.numer / _tb.denom


def proc_sample(pid: int) -> ProcSample | None:
    ru = _RusageV0()
    if _libc.proc_pid_rusage(int(pid), 0, ctypes.byref(ru)) != 0:
        return None
    return ProcSample(int(pid), int((ru.user_time + ru.system_time) * _ABS_TO_NS),
                      int(ru.phys_footprint), int(ru.resident_size),
                      int(ru.proc_start_abstime))


# ---- process tree ----------------------------------------------------------

_PROC_PIDTBSDINFO = 3
_BSDINFO_SIZE = 136  # sizeof(struct proc_bsdinfo)
_SZOMB = 5


def _bsdinfo(pid: int) -> tuple[int, int, float] | None:
    """(status, ppid, start epoch seconds) for `pid`, or None if unreadable."""
    buf = ctypes.create_string_buffer(_BSDINFO_SIZE)
    n = _libc.proc_pidinfo(int(pid), _PROC_PIDTBSDINFO, ctypes.c_uint64(0),
                           buf, _BSDINFO_SIZE)
    if n != _BSDINFO_SIZE:
        return None
    _flags, status, _xstatus, _pid, ppid = struct.unpack_from("<5I", buf.raw)
    sec, usec = struct.unpack_from("<QQ", buf.raw, 120)
    return status, ppid, sec + usec / 1e6


def parent_pid(pid: int) -> int | None:
    info = _bsdinfo(pid)
    return info[1] if info else None


def start_time(pid: int) -> float | None:
    info = _bsdinfo(pid)
    return info[2] if info else None


def is_zombie(pid: int) -> bool:
    info = _bsdinfo(pid)
    return info is None or info[0] == _SZOMB


def child_pids(pid: int) -> list[int]:
    size = 256
    while True:
        arr = (ctypes.c_int * size)()
        n = _libc.proc_listchildpids(int(pid), arr, ctypes.sizeof(arr))
        if n < 0:
            return []
        if n < size:
            return [p for p in arr[:n] if p > 0]
        size *= 4


# ---- every process on the machine -------------------------------------------
# `proc_pidinfo(PROC_PIDTBSDINFO)` is refused for another user's pid, but the
# KERN_PROC sysctl `ps` itself reads is not: one call returns a `kinfo_proc`
# per process, root's and other users' included, which is what the whole-
# machine Monitor page lists. Offsets are into `struct kinfo_proc` (648 bytes
# on both x86_64 and arm64): extern_proc's p_starttime, p_stat, p_pid and
# p_comm, then eproc's e_ucred.cr_uid and e_ppid.

_KINFO_SIZE = 648
_KP_STAT, _KP_PID, _KP_COMM, _KP_UID, _KP_PPID = 36, 40, 243, 420, 560


def _kinfo_ident(raw: bytes, off: int) -> ProcIdent:
    sec, usec = struct.unpack_from("<qi", raw, off)
    comm = raw[off + _KP_COMM: off + _KP_COMM + 17].split(b"\0", 1)[0]
    return ProcIdent(struct.unpack_from("<i", raw, off + _KP_PID)[0],
                     struct.unpack_from("<i", raw, off + _KP_PPID)[0],
                     struct.unpack_from("<I", raw, off + _KP_UID)[0],
                     sec + usec / 1e6, comm.decode("utf-8", "replace"),
                     raw[off + _KP_STAT] == _SZOMB)


def _kinfo(mib_tail: tuple[int, ...]) -> bytes:
    mib = (ctypes.c_int * (2 + len(mib_tail)))(1, 14, *mib_tail)  # CTL_KERN, KERN_PROC
    for _ in range(4):
        size = ctypes.c_size_t(0)
        if _libc.sysctl(mib, len(mib), None, ctypes.byref(size), None, 0) != 0:
            return b""
        size.value += 32 * _KINFO_SIZE  # room for processes born in between
        buf = ctypes.create_string_buffer(size.value)
        if _libc.sysctl(mib, len(mib), buf, ctypes.byref(size), None, 0) == 0:
            return ctypes.string_at(buf, size.value)
        if ctypes.get_errno() != 12:  # ENOMEM: grew again, retry
            return b""
    return b""


def list_procs() -> list[ProcIdent]:
    """Every process on the machine (KERN_PROC_ALL), any user's."""
    raw = _kinfo((0, 0))
    return [_kinfo_ident(raw, off) for off in range(0, len(raw) - _KINFO_SIZE + 1, _KINFO_SIZE)]


def list_pids() -> list[int]:
    return [p.pid for p in list_procs()]


def proc_ident(pid: int) -> ProcIdent | None:
    """One process's identity (KERN_PROC_PID), any user's; None once gone."""
    raw = _kinfo((1, int(pid)))
    return _kinfo_ident(raw, 0) if len(raw) >= _KINFO_SIZE else None


_ARGMAX: int | None = None


def _argmax() -> int:
    global _ARGMAX
    if _ARGMAX is None:
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        mib = (ctypes.c_int * 2)(1, 8)  # CTL_KERN, KERN_ARGMAX
        if _libc.sysctl(mib, 2, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
            return 256 * 1024
        _ARGMAX = value.value or 256 * 1024
    return _ARGMAX


def proc_args(pid: int, limit: int | None = None) -> list[str] | None:
    """argv of `pid` (KERN_PROCARGS2), or None when the kernel refuses.
    `limit` caps the buffer (the kernel truncates to it): the whole-machine
    sample reads hundreds of argvs and only shows 200 characters of each, so
    it asks for a few KB rather than ARG_MAX (1 MB) apiece."""
    size = ctypes.c_size_t(limit or _argmax())
    buf = ctypes.create_string_buffer(size.value)
    mib = (ctypes.c_int * 3)(1, 49, int(pid))  # CTL_KERN, KERN_PROCARGS2
    if _libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None
    raw = ctypes.string_at(buf, size.value)
    if len(raw) < 4:
        return None
    argc = struct.unpack_from("i", raw)[0]
    _exe, _, rest = raw[4:].partition(b"\0")
    parts = rest.lstrip(b"\0").split(b"\0")[:argc]
    return [p.decode("utf-8", "replace") for p in parts]


# ---- host ------------------------------------------------------------------

_HOST = _libc.mach_host_self()
_HOST_VM_INFO64 = 4
_HOST_CPU_LOAD_INFO = 3
_PAGE = os.sysconf("SC_PAGE_SIZE")


class _VmStat64(ctypes.Structure):
    _fields_ = (
        [(n, ctypes.c_uint32) for n in "free active inactive wire".split()]
        + [(n, ctypes.c_uint64) for n in "zero_fill reactivations pageins pageouts faults cow lookups hits purges".split()]
        + [(n, ctypes.c_uint32) for n in "purgeable speculative".split()]
        + [(n, ctypes.c_uint64) for n in "decompressions compressions swapins swapouts".split()]
        + [(n, ctypes.c_uint32) for n in "compressor_page_count throttled external internal".split()]
        + [("total_uncompressed", ctypes.c_uint64)])


class _CpuLoad(ctypes.Structure):
    _fields_ = [("ticks", ctypes.c_uint32 * 4)]  # user, system, idle, nice


def _sysctl_bytes(name: bytes) -> bytes:
    size = ctypes.c_size_t(0)
    _libc.sysctlbyname(name, None, ctypes.byref(size), None, 0)
    buf = ctypes.create_string_buffer(size.value)
    _libc.sysctlbyname(name, buf, ctypes.byref(size), None, 0)
    return ctypes.string_at(buf, size.value)


_MEMSIZE: int | None = None


def host_memory() -> dict | None:
    global _MEMSIZE
    vm = _VmStat64()
    cnt = ctypes.c_uint32(ctypes.sizeof(vm) // 4)
    if _libc.host_statistics64(_HOST, _HOST_VM_INFO64, ctypes.byref(vm), ctypes.byref(cnt)) != 0:
        return None
    if not _MEMSIZE:  # 0 = the read failed; try again next time
        _MEMSIZE = int.from_bytes(_sysctl_bytes(b"hw.memsize"), "little") or None
    app = max(0, vm.internal - vm.purgeable) * _PAGE
    wired = vm.wire * _PAGE
    compressed = vm.compressor_page_count * _PAGE
    return {"total": _MEMSIZE, "used": app + wired + compressed, "app": app,
            "wired": wired, "compressed": compressed}


def host_cpu_ticks() -> tuple[int, int, int] | None:
    """(busy-user, busy-system, total) cumulative ticks."""
    cl = _CpuLoad()
    cnt = ctypes.c_uint32(4)
    if _libc.host_statistics(_HOST, _HOST_CPU_LOAD_INFO, ctypes.byref(cl), ctypes.byref(cnt)) != 0:
        return None
    user, system, idle, nice = (int(t) for t in cl.ticks)
    return user + nice, system, user + system + idle + nice


def descendants(root: int) -> dict[int, int]:
    return walk_descendants(root, child_pids)
