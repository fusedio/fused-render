"""Will this model FIT on this machine? — fused_render/ai/fit.py (SPEC AI-16,
AI-16b, AI-16c, D495).

`ai_runtime._fit_verdict` used to be handed `size_gb` and asked a memory
question, which conflates two quantities that coincide only for a
single-checkpoint text model: `ltx-video` runs `DistilledPipeline
(low_memory=True)`, which FREES the transformer and the Gemma text encoder
between stages — its peak is one STAGE, its download (AI-11a) is every byte
of TWO repos — so a 28.5GB constant answered "Likely too big for this
machine" on a machine where it demonstrably renders. A cached, uncurated repo
was worse: its `size_gb` comes from bytes on DISK including every revision
the cache holds, a figure that drifts further from memory the longer the
cache lives.

**The verdict is computed here, not by the router, which is a view.** Over
the best FOOTPRINT available, on a precedence ladder that degrades to
today's behaviour rather than replacing it — a model nobody has run yet is
judged exactly as it was before this module existed:

    measured   footprints.py, keyed <capability>/<model_id> — this model has
               RUN here and this is what it cost
    declared   an optional `resident_gb` on a curated catalog entry — the
               curator (or the runner's own docstring) knows the envelope
    download   `size_gb`, exactly as today — nothing better is known

`None` when even `size_gb` is missing, unchanged: AI-11a's rule that an
unknown size is a dash and never a guess governs the verdict too.
`resident_gb` is optional and additive, the shape AI-11i/AI-11j already
established for `recommended`/`acceptsImage` — a curator MAY answer, and
absence falls through rather than meaning anything.

`verdict()` returns `{verdict, basis, footprintBytes}` or `None` — never a
bare string, so the page can word a MEASURED verdict as a fact rather than a
guess (AI-16c) instead of hedging every answer the same way.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import functools
import os
import struct
import sys

from fused_render.ai import footprints

#: `catalog.py`'s unit for `size_gb`, and the same unit a curator writes
#: `resident_gb` in — decimal GB, matching `modelSize.ts`'s own
#: `CATALOG_GB_BYTES`. Mixing this with a binary (1024-based) reading would be
#: the same ~7% drift that module's own header explains.
GB_BYTES = 1e9

#: OS + browser + this server (SPEC AI-16b). Subtracted from total RAM before
#: any fraction is taken, so the budget SCALES: on a 16GB machine this leaves
#: a correct 8GB for everything else; on a 64GB machine the old 50%-of-total
#: rule left 32GB unusable for no stated reason, which is the defect headroom
#: thresholds exist to fix.
RESERVE_BYTES = 8e9

#: Of the USABLE budget (total RAM minus the reserve), the fraction under
#: which a footprint is "easy". Chosen so a 16GB machine keeps roughly the
#: boundaries it had under the old 25%/50%-of-total rule (5.5/9.2GB against
#: today's 4.3/8.6GB) and only large machines change.
EASY_FRACTION = 0.6

#: Apple's own documented meaning of `iogpu.wired_limit_mb == 0`: no explicit
#: limit has been set, so the kernel enforces its DEFAULT ceiling, which is
#: roughly this fraction of total RAM.
_DEFAULT_WIRED_FRACTION = 0.75


@functools.lru_cache(maxsize=1)
def machine_ram_gb() -> float | None:
    """Total physical memory in decimal GB, or None where it cannot be read.

    Moved here from `ai_runtime.py` unchanged (AI-16's own text: "`ram` stays
    `_machine_ram_gb`'s stdlib reading — decimal GB, cached forever") — this
    module is where the headroom arithmetic that consumes it now lives, and a
    router importing a private name out of another router would be the wrong
    direction of dependency. Stdlib only — psutil lives in the runner venvs,
    not the server's own environment (AI-2's rule). `sysconf` covers macOS and
    Linux; Windows answers through `GlobalMemoryStatusEx`. Cached forever: the
    machine's RAM does not change under a running server, and this is read
    per catalog request.
    """
    try:
        if hasattr(os, "sysconf") and os.sysconf_names.get("SC_PHYS_PAGES"):
            return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9
    except (ValueError, OSError):
        pass
    try:  # pragma: no cover - the Windows branch
        class _MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys / 1e9
    except Exception:  # noqa: BLE001 - absent windll off Windows, and none of it is fatal
        pass
    return None


def _wired_limit_mb() -> int | None:
    """`iogpu.wired_limit_mb`, read via `ctypes.sysctlbyname` — None off
    Darwin, or if the read fails for any reason (SPEC AI-16b).

    **A `ctypes` libc call, not a subprocess.** AI-6 refuses `nvidia-smi`/
    `rocminfo` on this same per-catalog-request path because a cold spawn is
    50-500ms; measured directly on this machine (2026-08-26, Apple Silicon,
    `ctypes.CDLL(...).sysctlbyname`), one read-the-size-then-read-the-value
    round trip is ~2µs — a `sysctl` subprocess was never actually necessary
    here, which is worth writing down since the spec that asked for this probe
    expected one.

    `0` is a REAL answer, Apple's own documented meaning of "no explicit
    limit — the kernel enforces its default, roughly 75% of RAM" — not
    "unset". Only a failed read (wrong platform, the sysctl name does not
    exist, a `ctypes`-level error) is `None`, and `None` must cost the GATE
    below, never the verdict — an unreadable wired limit is not evidence a
    model does not fit.
    """
    if sys.platform != "darwin":
        return None
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        name = b"iogpu.wired_limit_mb"
        size = ctypes.c_size_t(0)
        if libc.sysctlbyname(name, None, ctypes.byref(size), None, 0) != 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctlbyname(name, buf, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value == 4:
            return struct.unpack("<i", buf.raw)[0]
        if size.value == 8:
            return struct.unpack("<q", buf.raw)[0]
        return None  # an unexpected width — answer honestly with "unknown"
    except Exception:  # noqa: BLE001 - a probe must never break the catalog route
        return None


def _wired_limit_bytes(ram_gb: float) -> float | None:
    """The Apple-Silicon hard ceiling in bytes, or None where it does not
    apply (off Darwin) or cannot be read. See `_wired_limit_mb`."""
    limit_mb = _wired_limit_mb()
    if limit_mb is None:
        return None
    if limit_mb <= 0:
        return ram_gb * GB_BYTES * _DEFAULT_WIRED_FRACTION
    return limit_mb * 1024 * 1024


def footprint_bytes(capability: str, model_id: str, size_gb: float | None = None,
                    resident_gb: float | None = None) -> tuple[float | None, str | None]:
    """The best footprint available for `<capability>/<model_id>`, and which
    rung of the ladder it came from — `(bytes, basis)`, or `(None, None)`.

    Precedence: measured (this machine, this run) > declared (a curator's
    `resident_gb`) > download (`size_gb`, today's behaviour). The first rung
    that answers wins outright — this is NOT an average or a "prefer the
    largest", because a measured number is strictly better evidence than a
    guess about the same model, whichever guess is bigger.
    """
    measured = footprints.read(capability, model_id)
    if measured is not None:
        return measured, "measured"
    if isinstance(resident_gb, (int, float)) and not isinstance(resident_gb, bool) and resident_gb > 0:
        return resident_gb * GB_BYTES, "declared"
    if isinstance(size_gb, (int, float)) and not isinstance(size_gb, bool) and size_gb > 0:
        return size_gb * GB_BYTES, "download"
    return None, None


def verdict(capability: str, model_id: str, size_gb: float | None = None,
           resident_gb: float | None = None) -> dict | None:
    """`{verdict, basis, footprintBytes}`, or `None` when nothing is known —
    SPEC AI-16, AI-16c.

    `verdict` is "easy" | "tight" | "no", judged over the footprint's
    HEADROOM against this machine's RAM (AI-16b) rather than a fraction of
    the total. On Apple Silicon a footprint past the wired-memory ceiling is
    "no" regardless of the headroom arithmetic — MLX cannot allocate past it
    no matter how much of the reserve is unused.

    A MEASURED "no" is reachable and is not a contradiction: the footprint
    store only ever holds models that ran, but the budget here is what is
    left after the reserve, so a model measured above it ran while nothing
    else was competing for memory. `basis` is what lets a reader (AI-16c)
    tell that apart from a guess.
    """
    footprint, basis = footprint_bytes(capability, model_id, size_gb, resident_gb)
    if footprint is None:
        return None
    ram_gb = machine_ram_gb()
    if ram_gb is None or ram_gb <= 0:
        return None
    ram_bytes = ram_gb * GB_BYTES

    wired_limit = _wired_limit_bytes(ram_gb)
    if wired_limit is not None and footprint > wired_limit:
        return {"verdict": "no", "basis": basis, "footprintBytes": footprint}

    usable = max(0.0, ram_bytes - RESERVE_BYTES)
    if footprint <= EASY_FRACTION * usable:
        result = "easy"
    elif footprint <= usable:
        result = "tight"
    else:
        result = "no"
    return {"verdict": result, "basis": basis, "footprintBytes": footprint}
