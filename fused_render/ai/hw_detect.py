"""GPU/VRAM detection beyond RAM (SPEC AI-18, D519).

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

**Four traps, each with its own test:**

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
* **A vendor tool is optional, and its output drifts.** "Degrades to None"
  only helps if something else can still answer, and for a long time nothing
  could: an AMD box read as GPU-less if `rocm-smi` was off PATH (ROCm installs
  to `/opt/rocm/bin` and exports nothing), and read as GPU-less AGAIN if it
  ran but title-cased its JSON keys the way ROCm 6 does. Both are now covered
  — the fallback path, a case-insensitive read — and behind them sits
  `_sysfs_gpus`, which needs no vendor package at all because the kernel
  already publishes VRAM under `/sys/class/drm`. The failure this closes is
  not cosmetic: `fit._select_pool` reads "no GPU" as `cpu-only`, so a single
  missing binary made every row in the model catalog claim the machine had no
  accelerator.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

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


def _card_field(card: dict, *names: str) -> Any:
    """One of `names` out of a `rocm-smi` card object, matched WITHOUT
    regard to key case.

    rocm-smi's JSON key casing is not stable across ROCm releases: the
    build this parser was first written against emitted `"Card series"`,
    while ROCm 6's emits `"Card Series"` / `"Card Model"` title-cased. An
    exact-key read of the older spelling silently skipped every card on a
    newer install — `gpus or None` then returned None, `detect_hardware`
    fell through to the next vendor, and a real discrete Radeon reported as
    no GPU at all, which `fit._select_pool` turns into `cpu-only` for every
    row in the catalog. Matching case-insensitively costs one dict rebuild
    per card and survives both spellings (and any future re-casing).
    """
    folded = {key.lower(): value for key, value in card.items() if isinstance(key, str)}
    for name in names:
        value = folded.get(name.lower())
        if value is not None:
            return value
    return None


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
        name = _card_field(card, "Card series", "Card model")
        total = _card_field(card, "VRAM Total Memory (B)")
        if not isinstance(name, str) or total is None:
            continue
        try:
            vram_gb = float(total) / 1e9
        except (TypeError, ValueError):
            continue
        gpus.append(GpuDevice(name=name, vram_gb=vram_gb))
    return gpus or None


#: Where a ROCm install puts `rocm-smi` when it is not on PATH. ROCm's own
#: packages (and Arch's `rocm-smi-lib`) install to `/opt/rocm/bin` and add
#: NOTHING to the default PATH — a desktop-launched fused-render inherits a
#: login PATH that has never sourced a ROCm profile script, so the bare-name
#: spawn below raises `FileNotFoundError` on a machine where the tool is
#: installed and working. Tried in order after the bare name, which stays
#: first so a user's own PATH (a newer ROCm, a versioned `/opt/rocm-6.2`)
#: still wins.
_ROCM_SMI_FALLBACK_PATHS = ("/opt/rocm/bin/rocm-smi",)


def _amd_gpus() -> list[GpuDevice] | None:
    args = ["--showproductname", "--showmeminfo", "vram", "--json"]
    for binary in ("rocm-smi", *_ROCM_SMI_FALLBACK_PATHS):
        out = _run([binary, *args])
        if out is not None:
            return _parse_rocm_smi(out)
    return None


# ------------------------------------------------------------ Linux DRM sysfs
#
# The vendor tools above are the RICHER readings and stay first, but each of
# them is a separate install a user may simply not have: `rocm-smi` ships in
# ROCm, a multi-GB SDK nobody installs to run a GGUF through llama.cpp's
# Vulkan backend. The kernel, meanwhile, has already published the one number
# that matters — `/sys/class/drm/cardN/device/mem_info_vram_total`, exported
# by `amdgpu` on every machine the driver is bound to, no userspace package
# required. Reading it is what makes a plain Linux desktop with a discrete
# Radeon stop reporting itself as GPU-less.

#: The kernel's DRM class directory, as its own constant so tests can point
#: the probe at a fixture tree instead of the real `/sys` (which does not
#: exist on macOS/Windows CI at all, and on Linux CI describes the runner's
#: hardware rather than the case under test).
_DRM_CLASS_DIR = "/sys/class/drm"

#: `/sys/class/drm` holds one entry per CARD (`card1`) plus one per connector
#: (`card1-DP-1`) and per render node (`renderD128`). Only the bare `cardN`
#: entries have a `device/` with the VRAM file under it, so the others are
#: filtered out by shape rather than by probing each one and failing.
_CARD_DIR_RE = re.compile(r"^card\d+$")


def _read_sysfs(path: str) -> str | None:
    """One sysfs file's contents, stripped, or None on any read failure —
    the same "degrade to None, never raise" contract `_run` holds for the
    subprocess probes. A `/sys` file can vanish between the listing and the
    read (a card unbinding), and some are root-only."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _parse_lspci_names(payload: str) -> dict[str, str]:
    """`lspci -mm -nn -D` output into `{pci slot: device name}`.

    `-mm` is the machine-readable form, which quotes each field so a device
    name containing the commas and brackets these names are full of
    (`Advanced Micro Devices, Inc. [AMD/ATI]`) survives splitting; `-D`
    prints the full domain-qualified slot (`0000:03:00.0`), which is the
    exact spelling sysfs's own `uevent` uses, so the two join without any
    reformatting. A line this cannot parse is skipped, never raised on:
    lspci's output format is a compatibility surface we do not control, and
    a missing NAME costs a bandwidth-table lookup, not the VRAM figure.
    """
    names: dict[str, str] = {}
    for line in payload.splitlines():
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        # slot, class, vendor, device, ... — anything shorter is not a
        # device line (a blank line, a warning lspci printed to stdout).
        if len(fields) < 4:
            continue
        names[fields[0]] = _clean_pci_device_name(fields[3])
    return names


#: The `[1002]`-style hex id `-nn` appends to every vendor/device field.
_PCI_ID_SUFFIX_RE = re.compile(r"\s*\[[0-9a-fA-F]{4}\]$")

#: The marketing name inside a codename-plus-brackets device string —
#: `Navi 44 [Radeon RX 9060 XT]` -> `Radeon RX 9060 XT`.
_PCI_MARKETING_NAME_RE = re.compile(r"\[([^\[\]]+)\]\s*$")


def _clean_pci_device_name(raw: str) -> str:
    """An lspci device field reduced to the name a human (and
    `_BANDWIDTH_TABLE`) would recognise.

    lspci reports a discrete GPU by its SILICON codename with the retail
    name in brackets — `Navi 44 [Radeon RX 9060 XT]`, `Navi 21 [Radeon RX
    6800/6800 XT / 6900 XT]`. The bracketed half is the half that matches
    the bandwidth table (whose keys are retail names, `rx 7900 xtx`), so it
    is preferred when present; a field with no brackets (some cards report
    a plain name) is kept whole rather than dropped.
    """
    name = _PCI_ID_SUFFIX_RE.sub("", raw).strip()
    marketing = _PCI_MARKETING_NAME_RE.search(name)
    return marketing.group(1).strip() if marketing else name


def _uevent_field(uevent: str, key: str) -> str | None:
    for line in uevent.splitlines():
        name, _, value = line.partition("=")
        if name == key:
            return value.strip() or None
    return None


def _sysfs_gpus() -> list[GpuDevice] | None:
    """Every DRM card that publishes a non-zero VRAM total, or None when
    there are none (or no `/sys/class/drm` at all — every non-Linux
    platform, where this probe is simply a no-op the `or` chain falls
    through).

    A card whose `mem_info_vram_total` is absent or `0` is skipped rather
    than reported with a zero pool: an integrated `i915`/`xe` display device
    has no VRAM file, and `fit._select_pool` reads `total_vram_gb <= 0` as
    "no GPU" anyway — emitting a 0GB device would only make the aggregate
    across a real second card harder to reason about.
    """
    try:
        entries = sorted(os.listdir(_DRM_CLASS_DIR))
    except OSError:
        return None

    cards = [entry for entry in entries if _CARD_DIR_RE.match(entry)]
    if not cards:
        return None

    # ONE lspci spawn for the whole listing, and only when there is at least
    # one card to name — this probe's whole point is working on a machine
    # with no vendor tooling, so it must not become another per-card
    # subprocess storm on the way there.
    lspci = _run(["lspci", "-mm", "-nn", "-D"])
    names = _parse_lspci_names(lspci) if lspci else {}

    gpus = []
    for card in cards:
        device_dir = os.path.join(_DRM_CLASS_DIR, card, "device")
        total = _read_sysfs(os.path.join(device_dir, "mem_info_vram_total"))
        if total is None:
            continue
        try:
            vram_gb = float(total) / 1e9
        except ValueError:
            continue
        if vram_gb <= 0:
            continue
        uevent = _read_sysfs(os.path.join(device_dir, "uevent")) or ""
        slot = _uevent_field(uevent, "PCI_SLOT_NAME")
        name = names.get(slot or "")
        if not name:
            # No lspci and no name for this slot: the DRIVER is still a
            # truthful, if coarse, label ("amdgpu"), and it keeps the device
            # identifiable in the cache rather than blank. It matches nothing
            # in `_BANDWIDTH_TABLE`, which is the honest outcome — an
            # unnamed device gets the per-backend speed constant.
            name = _uevent_field(uevent, "DRIVER") or "GPU"
        gpus.append(GpuDevice(name=name, vram_gb=vram_gb))
    return gpus or None


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
    absent tool falls through to the next: NVIDIA, then AMD, then the Linux
    DRM sysfs reading (the tool-free fallback for a box with no ROCm
    installed — see `_sysfs_gpus`), then Windows WMI, then Apple Silicon's
    unified pool. `ram_gb` — pass
    `fit.machine_ram_gb()` — feeds both the Apple-Silicon reading and the
    unified-APU override; without it those two degrade to "no GPU" and "no
    override" respectively rather than guessing a pool size.

    Never raises: every probe underneath already degrades failures to None,
    and an empty result here is a legitimate answer (a CPU-only machine),
    not a caller-visible error.
    """
    gpus = (_nvidia_gpus() or _amd_gpus() or _sysfs_gpus() or _windows_gpus()
            or _apple_gpu(ram_gb) or [])
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
