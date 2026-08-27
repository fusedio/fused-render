"""GPU/VRAM detection beyond RAM (SPEC AI-18, D518).

`fit.py` judges a footprint against system RAM only — correct for CPU and
Apple-Silicon unified-memory loads, silently wrong on a discrete CUDA/ROCm
box, where the pool that actually holds the weights is VRAM, a completely
different (usually much smaller) number than `machine_ram_gb()`. This module
is the missing half: per-device VRAM, a device name, multi-GPU aggregation,
and a name -> memory-bandwidth lookup table a future speed-estimate feature
needs.

**Detection is a subprocess probe, and subprocess probes do not belong on the
verdict path.** `fit._wired_limit_mb`'s own docstring states the rule this
module has to keep: a cold `nvidia-smi`/`rocm-smi`/PowerShell spawn is
50-500ms, and `verdict()` runs once per catalog ROW on a route the picker
polls. So the split here is the same one `_wired_limit_mb` drew for the wired
ceiling, at a coarser grain: `detect_hardware()` is the slow probe, `fit.py`
never calls it — see `test_ai_hw_detect.py::
test_fit_module_only_reads_the_cache_never_the_probe`, which greps `fit.py`'s
own source to keep that true. `refresh_hardware()` runs the probe and writes
the result to `~/.fused-render/ai_hardware.json`; `cached_hardware()` is a
plain `storage.read_json` and nothing else — the only thing `fit.py` and
`benchmark.py` are meant to call, once a later phase wires either of them
up to read it.

**Three traps, each hit by the study this module is modelled on and each with
its own test:**

* **Windows `Win32_VideoController.AdapterRAM` is a 32-bit `uint32`.** A card
  with 4GB or more reports the field capped at (or wrapped past) that width —
  a 16GB RTX 4080 comes back as ~4GB. The FIX is not a bigger read of the same
  field: the display driver's own registry key
  (`HKLM\\...\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\<NNNN>\\
  HardwareInformation.qwMemorySize`, Microsoft's documented Display class
  GUID) carries the true value as a 64-bit `REG_QWORD`, so a capped WMI
  reading is re-resolved against that registry value rather than trusted.
  (`Win32_PhysicalMemory` — total SYSTEM ram via SMBIOS — is a different
  number entirely and is used below only for the unified-memory override,
  never as a stand-in for VRAM; an earlier draft of this project's own spec
  conflated the two and this module deliberately does not.)
* **Unified-memory APUs report a tiny carveout, not the real pool.** AMD's
  Ryzen AI Max ("Strix Halo") and NVIDIA's Grace/DGX Spark parts show up as a
  GPU with ~0.5-1GB of "VRAM" — the BIOS-assigned aperture, not the shared
  pool the driver can actually allocate from, which is the whole of system
  RAM. Detected by name (`_apply_unified_override`) and overridden to
  `ram_gb`, exactly like Apple Silicon already is.
* **A cold spawn must never be mistaken for "no GPU".** Every probe here
  degrades to `None` on ANY failure (missing binary, non-zero exit, a
  timeout, malformed output) rather than raising — `detect_hardware` composes
  them with `or`, so one vendor's absence falls through to the next rather
  than aborting the whole probe.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

from fused_render.shell import storage

#: A cold vendor-tool spawn is slow (see module docstring) — this caps how
#: long ANY one of them is allowed to hang before it is treated as absent,
#: so a wedged driver stalls the background refresh for seconds, not forever.
_PROBE_TIMEOUT_S = 3.0

#: `Win32_VideoController.AdapterRAM` is a `uint32`. Anything at or above
#: 4 GiB minus a little slack reads as "this is the capped value, not the
#: card's real size" — a handful of real sub-4GB cards report numbers close
#: to but under the cap and must NOT be treated as capped, which is why this
#: is a threshold near the top of the range rather than an exact-equality
#: check against `2**32 - 1`.
_ADAPTER_RAM_CAP_BYTES = 4 * 1024 ** 3 - 32 * 1024 * 1024  # ~3.97 GiB

VERSION = 1


@dataclass
class GpuDevice:
    """One GPU as this module reports it — the device name a bandwidth
    lookup keys on, its VRAM in decimal GB (matching `fit.GB_BYTES`'s unit),
    and whether that VRAM figure IS system RAM (Apple Silicon, or an
    overridden unified-memory APU)."""
    name: str
    vram_gb: float
    unified_memory: bool = False


@dataclass
class HardwareInfo:
    """One probe's (or one cache read's) answer. `total_vram_gb` is the sum
    across every device (multi-GPU aggregation: sum of vram x count) — a
    model sharded across identical cards fits against the SUM, not any one
    card's share, which is the pool `device_map="auto"`-style loading
    actually draws from. `bandwidth_gb_s` is the primary device's figure from
    the lookup table, or None when the name is not in it — a future
    speed-estimate feature falls back to a per-backend constant in that case,
    not to a guess here."""
    gpus: list
    total_vram_gb: float
    bandwidth_gb_s: float | None
    detected_at: float


def _run(args: list[str], timeout: float = _PROBE_TIMEOUT_S) -> str | None:
    """One subprocess call's stdout, or None on ANY failure — missing
    binary, non-zero exit, a hung process past `timeout`, or a decode
    error. The one seam every vendor probe below goes through, and the one
    every test monkeypatches instead of touching a real subprocess."""
    try:
        # `encoding`/`errors` pinned explicitly, never left to `text=True`'s
        # default `locale.getpreferredencoding(False)` — a GUI-launched
        # fused-render process inherits no LANG/LC_ALL, which resolves that to
        # ASCII, so the first non-ASCII byte a vendor tool prints (a curly
        # quote in an OEM device name, a Windows codepage that is not UTF-8)
        # would raise `UnicodeDecodeError` and turn "no GPU detected" into a
        # crashed background refresh. `errors="replace"` over a *correct*
        # per-platform codepage (`cp1252`, PowerShell's OEM page, ...)
        # because every field this module reads back out of `_run`'s output —
        # a GPU name, a byte count — is matched by `in`/parsed as a number;
        # a mis-decoded byte in the middle of a name that isn't in the
        # bandwidth table already read as "unknown" before this fix, and
        # `errors="replace"` keeps that the same failure mode (a table miss)
        # instead of a new one (a crash) — see `tests/
        # test_subprocess_encoding.py`, the repo-wide invariant this pins,
        # and `app_git.py`'s identical `subprocess.run(..., encoding="utf-8",
        # errors="replace")`, the house answer this matches rather than
        # picking a fresh one.
        result = subprocess.run(args, capture_output=True, timeout=timeout,
                                text=True, encoding="utf-8", errors="replace",
                                check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


# ------------------------------------------------------------------ NVIDIA


def _parse_nvidia_smi(csv: str) -> list[GpuDevice] | None:
    """`nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,
    nounits` output, one `name, mebibytes` row per card, into `GpuDevice`s —
    or None when there is nothing to parse (no GPUs, or the command failed
    upstream and handed this an empty string).

    **`memory.total` is MEBIBYTES (2**20 bytes), and `GpuDevice.vram_gb` is
    decimal GB** (its own docstring; matches `fit.GB_BYTES`, which
    `_select_pool` multiplies `total_vram_gb` by). `mib / 1024` would answer
    in binary GiB while claiming decimal GB — a real 24564 MiB (24GB) RTX
    4090 would report 23.99 instead of ~25.77, a silent ~7.4% under-report
    that survives all the way to a fit verdict. Caught by code review
    (`test_every_parser_reports_the_same_decimal_gb_for_the_same_real_card`
    pins it going forward, driving all three parsers on the same card so
    they cannot drift apart again)."""
    gpus = []
    for line in csv.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            mib = float(parts[1])
        except ValueError:
            continue
        gpus.append(GpuDevice(name=parts[0], vram_gb=mib * 1024 * 1024 / 1e9))
    return gpus or None


def _nvidia_gpus() -> list[GpuDevice] | None:
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total",
               "--format=csv,noheader,nounits"])
    if out is None:
        return None
    return _parse_nvidia_smi(out)


# ------------------------------------------------------------------- AMD


def _parse_rocm_smi(payload: str) -> list[GpuDevice] | None:
    """`rocm-smi --showproductname --showmeminfo vram --json` output —
    one object per card, keyed by an opaque `cardN` id — into `GpuDevice`s,
    or None on malformed JSON or an empty result."""
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    gpus = []
    for card in data.values():
        if not isinstance(card, dict):
            continue
        name = card.get("Card series") or card.get("Card model")
        total = card.get("VRAM Total Memory (B)")
        if not isinstance(name, str) or total is None:
            continue
        try:
            vram_gb = float(total) / 1e9
        except (TypeError, ValueError):
            continue
        gpus.append(GpuDevice(name=name, vram_gb=vram_gb))
    return gpus or None


def _amd_gpus() -> list[GpuDevice] | None:
    out = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"])
    if out is None:
        return None
    return _parse_rocm_smi(out)


# --------------------------------------------------------------- Windows


def _adapter_ram_is_capped(byte_value: int) -> bool:
    """Is `byte_value` (WMI `AdapterRAM`) close enough to the 32-bit ceiling
    that it is evidence of the cap, not a real reading — see module
    docstring."""
    return byte_value >= _ADAPTER_RAM_CAP_BYTES


def _parse_windows_adapter_ram(text: str) -> list[tuple[str, int]]:
    """`name|bytes` lines (one PowerShell `Win32_VideoController` row each)
    into `(name, bytes)` pairs. Malformed lines are skipped, not fatal — a
    row a future Windows build adds a new field to must not take down every
    OTHER row's reading."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        name, _, raw = line.rpartition("|")
        try:
            rows.append((name.strip(), int(raw.strip())))
        except ValueError:
            continue
    return rows


def _windows_registry_vram() -> dict[str, int]:
    """`{driver description: qwMemorySize bytes}` read from the display
    driver's own registry class — the correction for a capped `AdapterRAM`
    reading (see module docstring). A real implementation shells out to
    PowerShell (`Get-ItemProperty` over
    `HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\
    {4d36e968-e325-11ce-bfc1-08002be10318}\\*`); split out as its own
    function so `_windows_gpus` can be tested without one, and so a future
    real implementation lands in exactly one place."""
    out = _run(["powershell", "-NoProfile", "-Command",
               "Get-ItemProperty -Path "
               "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\"
               "{4d36e968-e325-11ce-bfc1-08002be10318}\\*' "
               "-ErrorAction SilentlyContinue | ForEach-Object { "
               "$d = $_.DriverDesc; $m = $_.'HardwareInformation.qwMemorySize'; "
               "if ($d -and $m) { $d + '|' + $m } }"])
    if out is None:
        return {}
    result = {}
    for name, value in _parse_windows_adapter_ram(out):
        result[name] = value
    return result


def _windows_gpus() -> list[GpuDevice] | None:
    # No explicit `sys.platform` guard, unlike `_apple_gpu`: off Windows,
    # `_run` already returns None for free (no `powershell` binary, or one
    # that answers nothing useful) — see `test_ai_hw_detect.py`'s Windows
    # tests, which drive this on macOS/Linux CI by monkeypatching `_run`
    # rather than needing a Windows runner to exercise the parsing at all.
    out = _run(["powershell", "-NoProfile", "-Command",
               "Get-CimInstance Win32_VideoController | "
               "Select-Object Name,AdapterRAM | ForEach-Object { "
               "$_.Name + '|' + $_.AdapterRAM }"])
    if out is None:
        return None
    rows = _parse_windows_adapter_ram(out)
    if not rows:
        return None
    registry = None
    gpus = []
    for name, byte_value in rows:
        if _adapter_ram_is_capped(byte_value):
            if registry is None:
                registry = _windows_registry_vram()
            corrected = registry.get(name)
            if isinstance(corrected, int) and corrected > byte_value:
                byte_value = corrected
        # Decimal GB (`GpuDevice.vram_gb`'s own unit — see `_parse_nvidia_
        # smi`'s docstring for the full reasoning), NOT `/1024**3`: both
        # `AdapterRAM` and the registry's `qwMemorySize` are raw bytes, and
        # binary-GiB math here was the other half of the same under-report
        # `_parse_nvidia_smi` had.
        gpus.append(GpuDevice(name=name, vram_gb=byte_value / 1e9))
    return gpus or None


# ---------------------------------------------------------------- Apple


def _apple_gpu(ram_gb: float | None) -> list[GpuDevice] | None:
    """Apple Silicon's single "GPU" — unified memory, so its pool IS system
    RAM, not a separate figure to probe for. None off Darwin/Intel Macs and
    when `ram_gb` is unknown, since a unified-memory device with no known
    pool size is not a usable reading."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return None
    if not ram_gb:
        return None
    name = (_run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "").strip()
    return [GpuDevice(name=name or "Apple Silicon", vram_gb=ram_gb, unified_memory=True)]


# --------------------------------------------------------- unified overrides


#: Substrings (matched case-insensitively) that name an AMD unified-memory
#: APU on the CPU side — Strix Halo ships as "AMD Ryzen AI Max+ 395" etc.,
#: never as a discrete GPU name, so this checks the CPU, not the GPU device.
_AMD_UNIFIED_CPU_MARKERS = ("ryzen ai max", "strix halo")

#: Substrings naming an NVIDIA unified-memory SoC — these DO show up as the
#: GPU device's own name (`NVIDIA GB10`, the DGX Spark part), unlike the AMD
#: case above.
_NVIDIA_UNIFIED_GPU_MARKERS = ("grace", "gb10", "gb20", "dgx spark")


def _apply_unified_override(device: GpuDevice, *, cpu_name: str, ram_gb: float) -> None:
    """Mutates `device` in place to `vram_gb = ram_gb, unified_memory = True`
    when it is a known unified-memory part — a tiny BIOS-reported carveout
    (AMD) or an absent/placeholder VRAM reading (NVIDIA Grace-class) is not
    the real pool a load can use, and that real pool is `ram_gb`. A no-op for
    a discrete GPU."""
    cpu_lower = cpu_name.lower()
    name_lower = device.name.lower()
    is_amd_apu = any(marker in cpu_lower for marker in _AMD_UNIFIED_CPU_MARKERS)
    is_nvidia_unified = any(marker in name_lower for marker in _NVIDIA_UNIFIED_GPU_MARKERS)
    if is_amd_apu or is_nvidia_unified:
        device.vram_gb = ram_gb
        device.unified_memory = True


#: `/proc/cpuinfo`'s own path — a seam so a test can point this at a fixture
#: file rather than the real `/proc`, which does not exist off Linux at all.
_PROC_CPUINFO_PATH = "/proc/cpuinfo"


def _linux_cpu_name() -> str:
    """The CPU's marketing name, read from `/proc/cpuinfo`'s `model name`
    field — the standard Linux mechanism (`lscpu` and a bare `cat /proc/
    cpuinfo` read the identical field), used here because `platform.
    processor()` is documented as frequently EMPTY on Linux (the stdlib's
    own caveat), which silently broke two things at once (Bugbot review):
    `_apply_unified_override`'s AMD-unified-APU detection (keyed off the
    CPU name, never the GPU's own — Strix Halo's iGPU reports as an
    ordinary-looking "AMD Radeon 8060S Graphics") never fired on Linux, and
    neither did the bandwidth table's "ryzen ai max"/"strix halo" rows,
    matched against the identical CPU-name string.

    Empty string on ANY failure — missing file (this is called
    unconditionally when `sys.platform` starts with `"linux"`, and a
    container or an exotic kernel might still lack `/proc`), a permission
    error, or a `model name` field this format has never had — the same
    "no signal" answer the Darwin/Windows CPU-name reads already give in
    their own failure cases, so `detect_hardware`'s override/bandwidth
    logic needs no extra branch for it.
    """
    try:
        with open(_PROC_CPUINFO_PATH, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("model name"):
                    _key, _sep, value = line.partition(":")
                    return value.strip()
    except OSError:
        pass
    return ""


def _total_vram_gb(gpus: list[GpuDevice]) -> float:
    """The sum across every device — multi-GPU aggregation, sum of vram x
    count. Identical GPUs are not deduplicated: two real 24GB cards really do
    sum to a 48GB pool for a sharded load."""
    return sum(g.vram_gb for g in gpus)


# ---------------------------------------------------------- bandwidth table


#: Device name substring -> memory bandwidth in GB/s, for a future speed
#: estimate (`tok/s ~= bandwidth / model_size * 0.55`) that will use it when a
#: device's own bandwidth is known, falling back to a per-backend constant
#: otherwise.
#: Figures are manufacturer-published memory-bandwidth specs (not measured
#: on any of our own hardware), rounded to a representative value per SKU
#: family — a small model-to-model spread within a family (e.g. a binned
#: M3 Max) is not worth a separate table row for an ESTIMATE this coarse.
#:
#: **Ordered most-specific match first.** Matching is "does this substring
#: appear in the device name", so `"Apple M1"` would also match `"Apple M1
#: Max"` if checked first — every multi-word Apple/RTX variant is listed
#: ahead of its bare chip name for exactly that reason (see
#: `test_bandwidth_lookup_prefers_the_more_specific_match`).
_BANDWIDTH_TABLE: list[tuple[str, float]] = [
    # Apple Silicon (LPDDR5/5X unified memory), most-specific first.
    ("m1 ultra", 800.0), ("m1 max", 400.0), ("m1 pro", 200.0), ("m1", 68.25),
    ("m2 ultra", 800.0), ("m2 max", 400.0), ("m2 pro", 200.0), ("m2", 100.0),
    ("m3 ultra", 819.0), ("m3 max", 400.0), ("m3 pro", 150.0), ("m3", 100.0),
    ("m4 max", 546.0), ("m4 pro", 273.0), ("m4", 120.0),
    # M5 (base only; Apple's own published spec at introduction). Pro/Max/
    # Ultra are deliberately absent rather than guessed at — an unlisted name
    # falls back to a per-backend constant, which is the honest answer
    # until Apple publishes those figures.
    ("m5", 153.0),
    # NVIDIA data-center (HBM).
    ("h100", 3350.0), ("a100 80gb", 1935.0), ("a100", 1555.0),
    # NVIDIA RTX 50 series (GDDR7).
    ("rtx 5090", 1792.0), ("rtx 5080", 960.0), ("rtx 5070 ti", 896.0),
    ("rtx 5070", 672.0),
    # NVIDIA RTX 40 series (GDDR6X/6).
    ("rtx 4090", 1008.0), ("rtx 4080", 717.0), ("rtx 4070 ti", 672.0),
    ("rtx 4070", 504.0), ("rtx 4060 ti", 288.0), ("rtx 4060", 272.0),
    # NVIDIA RTX 30 series (GDDR6X/6).
    ("rtx 3090", 936.0), ("rtx 3080", 760.0), ("rtx 3070", 448.0),
    ("rtx 3060", 360.0),
    # AMD RDNA (GDDR6).
    ("rx 7900 xtx", 960.0), ("rx 7900", 800.0), ("rx 6800", 512.0),
    # AMD CDNA (HBM, datacenter).
    ("mi300x", 5300.0), ("mi250", 3277.0),
    # Unified-memory APUs / SoCs (LPDDR5X).
    ("ryzen ai max", 256.0), ("strix halo", 256.0),
    ("gb10", 273.0), ("gb20", 273.0), ("grace", 546.0),
]


def _bandwidth_for(device_name: str) -> float | None:
    """The memory-bandwidth table entry for `device_name`, or None when it
    names nothing this table knows — a future speed-estimate feature falls
    back to a per-backend constant in that case rather than guessing a
    bandwidth.

    Matched on WORD boundaries, not a bare substring: a plain `in` check
    made `"m3"` match inside `"NVIDIA H100 80GB HBM3"` (the tail of `HBM3`),
    silently reporting an Apple M3's bandwidth for a data-center NVIDIA
    card — caught by
    `test_bandwidth_lookup_covers_named_families[...H100...]`. `\\b` around
    a key ending in a digit needs the digit to be followed by a non-word
    character or the string end, which `re.escape` plus `\\b` already gives
    for every key in the table above.
    """
    name_lower = device_name.lower()
    for key, bandwidth in _BANDWIDTH_TABLE:
        if re.search(r"\b" + re.escape(key) + r"\b", name_lower):
            return bandwidth
    return None


# --------------------------------------------------------------- the probe


def detect_hardware(ram_gb: float | None = None) -> HardwareInfo:
    """The slow probe — subprocess spawns, never called on the verdict path
    (see module docstring). Composes every vendor probe with `or` so one
    absent tool falls through to the next: NVIDIA, then AMD, then Windows
    WMI, then Apple Silicon's unified pool. `ram_gb` — pass
    `fit.machine_ram_gb()` — feeds both the Apple-Silicon reading and the
    unified-APU override; without it those two degrade to "no GPU" and "no
    override" respectively rather than guessing a pool size.

    Never raises: every probe underneath already degrades failures to None,
    and an empty result here is a legitimate answer (a CPU-only machine),
    not a caller-visible error.
    """
    gpus = _nvidia_gpus() or _amd_gpus() or _windows_gpus() or _apple_gpu(ram_gb) or []
    if sys.platform == "darwin":
        cpu_name = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or ""
    elif sys.platform.startswith("linux"):
        # `platform.processor()` is commonly empty here — see
        # `_linux_cpu_name`'s own docstring for why this reads `/proc/
        # cpuinfo` directly instead (Bugbot review).
        cpu_name = _linux_cpu_name()
    else:
        cpu_name = platform.processor() or ""
    if ram_gb:
        for device in gpus:
            _apply_unified_override(device, cpu_name=cpu_name, ram_gb=ram_gb)
    bandwidth = _bandwidth_for(gpus[0].name) if gpus else None
    if bandwidth is None and gpus and gpus[0].unified_memory:
        # A unified APU's OWN device name (an ordinary-looking iGPU string,
        # e.g. "AMD Radeon 8060S Graphics") names nothing in the bandwidth
        # table — the table's "ryzen ai max"/"strix halo" rows are matched
        # against the CPU's marketing name instead, the identical signal
        # `_apply_unified_override` already used to detect the device as
        # unified in the first place (Bugbot review).
        bandwidth = _bandwidth_for(cpu_name)
    return HardwareInfo(gpus=gpus, total_vram_gb=_total_vram_gb(gpus),
                        bandwidth_gb_s=bandwidth, detected_at=time.time())


# ------------------------------------------------------------------ the cache


def _path() -> str:
    return os.path.join(storage.home_dir(), "ai_hardware.json")


def _to_json(info: HardwareInfo) -> dict:
    return {
        "version": VERSION,
        "detectedAt": info.detected_at,
        "totalVramGb": info.total_vram_gb,
        "bandwidthGbS": info.bandwidth_gb_s,
        "gpus": [{"name": g.name, "vramGb": g.vram_gb,
                  "unifiedMemory": g.unified_memory} for g in info.gpus],
    }


def _from_json(data: dict) -> HardwareInfo | None:
    gpus_raw = data.get("gpus")
    if not isinstance(gpus_raw, list):
        return None
    gpus = []
    for row in gpus_raw:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        vram = row.get("vramGb")
        if not isinstance(vram, (int, float)) or isinstance(vram, bool):
            continue
        gpus.append(GpuDevice(name=row["name"], vram_gb=float(vram),
                              unified_memory=bool(row.get("unifiedMemory"))))
    total = data.get("totalVramGb")
    if not isinstance(total, (int, float)) or isinstance(total, bool):
        total = _total_vram_gb(gpus)
    bandwidth = data.get("bandwidthGbS")
    if not isinstance(bandwidth, (int, float)) or isinstance(bandwidth, bool):
        bandwidth = None
    detected_at = data.get("detectedAt")
    if not isinstance(detected_at, (int, float)) or isinstance(detected_at, bool):
        detected_at = 0.0
    return HardwareInfo(gpus=gpus, total_vram_gb=float(total),
                        bandwidth_gb_s=bandwidth, detected_at=float(detected_at))


def cached_hardware() -> HardwareInfo | None:
    """The last `refresh_hardware()`'s result, straight off disk — a plain
    `storage.read_json` and nothing else, so this is cheap enough to call on
    every verdict/estimate. None before anything has ever been detected, or
    when the file is corrupt/unreadable — the same "no measurement yet"
    contract `footprints.read` and `bench_store.read` already give their own
    callers.

    **This is the ONLY function in this module `fit.py` and `benchmark.py`
    may call.** `detect_hardware`/`refresh_hardware` spawn subprocesses; see
    the module docstring.
    """
    data = storage.read_json(_path())
    if not isinstance(data, dict):
        return None
    return _from_json(data)


def refresh_hardware(ram_gb: float | None = None) -> HardwareInfo:
    """Runs the slow probe and writes its result to the cache — the only
    function that touches the network^Wsubprocess layer AND persists. Meant
    to be called off the verdict path: a background refresh cadence, or an
    explicit "detect my hardware" action; nothing in `fit.py` calls this."""
    info = detect_hardware(ram_gb=ram_gb)
    storage.write_json(_path(), _to_json(info))
    return info
