"""What local inference this machine can do, and which folder does it (SPEC §40).

A **runner** is a folder holding a `pyproject.toml` and a `worker.py`. That is the
whole of it — the same shape `templates/zarr_aoi/` uses for its tile daemon, and
for the same reason: the model runs in its OWN interpreter, built from its own
declaration, in its own process.

Three things follow from that, and they are the reasons this is a folder rather
than an import:

* **fused-render's venv never grows torch or mlx.** They are multi-GB and
  platform-specific; a file explorer that could not start without them would be a
  worse file explorer. The runner's `pyproject.toml` is the only place they are
  named, and `envinstall` (PY-18) builds it on first use — the same detached
  `uv sync` with the same progress record and the same verbatim errors that every
  other declaring folder gets. No new install machinery exists for AI.
* **A wedged model cannot take the app down.** OOM, a CUDA fault, a Rust panic
  inside a loader — all of it happens in a process the supervisor can kill.
* **Adding a backend is adding a folder.** This was written when MLX was the only
  text runner and said "transformers for Windows tomorrow" — which turned out to
  be exactly one new row here and one new folder (D293), with nothing else in the
  app changed. The claim is kept because it was tested.

**Availability is checked, never assumed.** MLX runs on Apple Silicon and nowhere
else, so `available()` answers with a REASON rather than a bool — "needs Apple
Silicon (this is linux/x86_64)" is something a page can show, while a silently
missing capability is something a user files a bug about.

Resolution is by CAPABILITY, not by model: a caller asks for `text-generation`
and gets whichever runner serves it here. A model id never picks the runner,
because the same repo can be servable by two backends and the choice belongs to
the machine, not to the string.

**Several runners can share one capability, and the ORDER between them is the
whole mechanism.** Text generation prefers MLX on Apple Silicon and uses torch
on Windows and Linux, with torch also remaining a fallback on Apple Silicon when
MLX is unavailable; speech to text does the same thing with MLX Whisper over
CTranslate2, and since D319 carries a THIRD row — Parakeet, Apple Silicon only,
registered under MLX Whisper so that the default does not move. Every row is
registered, every one is asked whether it can run, and the first that says yes
wins. Nothing else in the app knows there is more than
one — but the CATALOG does, because what to suggest depends on which backend
will load it (`catalog.py`), and an MLX checkpoint on a Windows machine is a
download that cannot be used.

**A user can override that order, and the override is a REQUEST rather than an
instruction** (D302). `resolve()` reads a per-capability preference — "auto", or
a runner code — from `shell/prefs.py`, and a named runner wins only if it can
actually run here. An honoured preference is the whole story; an override naming
a runner this machine cannot run is IGNORED and the ordering above decides, with
the reason carried out in the `Resolution` so a page can say what happened. That
asymmetry is the point: prefs.json travels — it is a plain file in a home
directory people sync, copy between machines and restore from a backup — so a
preference set on a Mac must not arrive on a Windows box and take speech to text
away entirely. A preference that quietly does nothing is recoverable; a
capability that has silently vanished is a bug report.
"""

from __future__ import annotations

import glob
import os
import platform
from dataclasses import dataclass, field
from typing import Callable

# One capability per runner for now. The constants are the vocabulary the whole
# feature speaks — the API's `capability` parameter, the catalog's grouping, and
# the supervisor's one-resident-model-per-capability rule all key off these.
TEXT_GENERATION = "text-generation"
IMAGE_GENERATION = "text-to-image"
#: The Hub's own tag for it, like `IMAGE_GENERATION` is — so the constant, the
#: `pipeline_tag` on a Whisper repo and the capability a card asks to load are
#: one string rather than three that have to be kept in step.
SPEECH_TO_TEXT = "automatic-speech-recognition"

RUNNERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runners")


@dataclass(frozen=True)
class Availability:
    """Whether a runner can run here, and — when it cannot — why not in words.

    The reason is user-facing. "needs Apple Silicon (this is linux/x86_64)" tells
    someone what to do with the information; a False does not.
    """

    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class Runner:
    """One backend: a capability, a folder, and a rule about where it runs."""

    code: str
    capability: str
    #: Folder holding `pyproject.toml` (its declaration) and `worker.py` (the
    #: process the supervisor starts). Both are read from here and nowhere else.
    folder: str
    #: What this backend is, in one line, for the page that has to explain a
    #: capability the machine cannot serve. The FULL name, qualifier and all
    #: ("MLX LM (Apple Silicon)"), and it has exactly one home: the Preferences
    #: engine picker, where the reader is choosing between backends and the
    #: qualifier is the difference between the options.
    label: str
    #: The same backend without the qualifier ("MLX LM"), for everywhere else.
    #:
    #: A FIELD, not the label with the brackets stripped off. A regex would make
    #: the short name a side effect of how someone punctuated the long one, and
    #: the first runner whose name legitimately contains brackets would lose
    #: half of it with nothing failing. It is also the vocabulary this app
    #: already speaks informally — `skills/fused-render-ai/SKILL.md`'s runner
    #: table has been writing these names for as long as it has existed.
    #:
    #: The qualifier is noise anywhere the reader is not CHOOSING: a card
    #: saying "MLX FLUX (Apple Silicon)" tells someone on a Mac nothing they did
    #: not know, and costs roughly double the width of a tag that has to fit
    #: beside the task and the size.
    #:
    #: **Both names are PRODUCT NAMES, and they are Title Case with acronyms
    #: left uppercase — "MLX Whisper", "Diffusers", "Faster Whisper".** The AI
    #: Models page prints them all side by side in one column, so a name that
    #: keeps its upstream punctuation reads as a different KIND of thing than
    #: its neighbours rather than as a faithful citation; `faster-whisper` sat
    #: next to `Diffusers` and `MLX FLUX` and looked like a package, not a
    #: choice. The exact upstream spelling is not lost by this: the card's
    #: library tag one column over is the literal identifier (`ctranslate2`,
    #: `diffusers`, `mlx`, `gguf`) and stays lowercase, which is the split that
    #: earns the rename. Anywhere a runner is IDENTIFIED rather than named —
    #: `code`, the folder, the pyproject, the catalog keys — the upstream
    #: spelling is load-bearing and must not be touched.
    short_label: str = ""
    #: What using this backend is LIKE, for the page to say before anything is
    #: loaded. A standing fact about the runner, never a claim about this
    #: machine — the device a model actually got is the worker's to report
    #: (`worker_base.STATE["device"]`) and is not knowable until one has run.
    #:
    #: It exists because the honest answer for `transformers-text` is "this may
    #: be a great deal slower than you expect, and here is why". Empty for a
    #: runner with nothing surprising to say.
    #:
    #: **It renders under that engine's row on the AI Models page's Engines
    #: tab** (D315), beneath the select, and only for the runner actually
    #: serving the capability. It spent a while over the Discover tab's
    #: capability sections instead, which was wrong twice: only some runners
    #: have a note, so those sections were blotchy and the sentences read as
    #: noise; and the `mflux-image` one is a CAUTION about a choice — the thing
    #: that tells a 16GB Mac to go back to Diffusers — which belongs beside the
    #: control that makes that switch, not over a grid of downloads.
    note: str = ""
    _available: Callable[[], Availability] = field(repr=False, default=lambda: Availability(True))

    def available(self) -> Availability:
        """Can this runner run here — platform AND presence.

        The presence half is not paranoia about a broken install: a runner is
        registered before its folder is written (the image runner was listed
        with its worker still unbuilt), and a registry that advertises a
        capability whose folder is missing hands the user a Download button that
        fails at spawn while the API reports the capability ready. Advertising
        is a claim; this is the check that makes it true.
        """
        if not os.path.isfile(self.worker):
            return Availability(False, f"the {self.short} runner is not built yet")
        return self._available()

    @property
    def short(self) -> str:
        """`short_label`, falling back to the full one.

        The fallback is for a Runner built somewhere that has no opinion about
        display — a test's stand-in, mostly. A REGISTERED runner must set it,
        which `test_every_runner_has_both_names_and_they_differ_only_by_the_qualifier`
        requires: degrading to the long name is a cosmetic wart, degrading to ""
        would be a blank tag.
        """
        return self.short_label or self.label

    @property
    def worker(self) -> str:
        return os.path.join(self.folder, "worker.py")

    @property
    def pyproject(self) -> str:
        return os.path.join(self.folder, "pyproject.toml")


def _apple_silicon() -> Availability:
    """MLX is Metal-only: Apple Silicon, and nothing else — not Intel Macs.

    Checked at call time rather than import time so a test can monkeypatch
    `platform` and get the answer it is asserting about.
    """
    system = platform.system()
    machine = platform.machine()
    if system == "Darwin" and machine == "arm64":
        return Availability(True)
    return Availability(
        False,
        f"needs Apple Silicon — MLX runs on Metal only (this is {system.lower()}/{machine})",
    )


def _always() -> Availability:
    """torch + diffusers and CTranslate2 build on every platform we ship.

    Whether the machine is FAST enough is a different question, and not one to
    refuse on — a model answering slowly on a CPU is a model answering, and the
    device is reported (`worker_base.STATE["device"]`) so the page can say which
    case it is.
    """
    return Availability(True)


def _transformers_platform() -> Availability:
    """The transformers text runner's supported production platforms.

    MLX is preferred on Apple Silicon by registry order, but torch's MPS path is
    a working fallback when MLX is absent or unavailable. Intel macOS is not a
    distribution target, and availability drives the catalog and Load button,
    so it must not be advertised merely because torch happens to publish a
    wheel there.
    """
    system = platform.system()
    machine = platform.machine()
    if (
        system in ("Windows", "Linux")
        or (system == "Darwin" and machine == "arm64")
    ):
        return Availability(True)
    return Availability(
        False,
        f"requires Windows, Linux, or Apple Silicon macOS (this is {system.lower()}/{machine})",
    )


def _llamacpp_platform() -> Availability:
    """`llamacpp-text`'s supported platforms — a HARD exclusion, not the same
    shape as `_transformers_platform`'s Intel-macOS business decision.

    The maintainer's CPU wheel index (`llamacpp_text/pyproject.toml`, D402)
    publishes `py3-none` wheels for a specific, checked tag set:
    `macosx_11_0_arm64`, `manylinux2014_{x86_64,aarch64}.manylinux_2_17_*`,
    `musllinux_1_2_{x86_64,aarch64}`, `win_amd64`, `linux_riscv64`, and a wasm
    target. **There is no macOS x86_64 tag at all.** Where
    `_transformers_platform` excludes Intel macOS because torch's wheel there
    is not a platform this app chooses to distribute to, this excludes it
    because `uv sync` has NOTHING to install — an immediate, total failure
    the moment a machine reaches this row, not a slow or a degraded one. Kept
    to the same `(Windows or Linux) or (Darwin and arm64)` shape
    `_transformers_platform` already uses — a real check against the tags
    above, not a new mechanism — so the next person widening one accidentally
    widens both, or neither, but has to look at both to widen just one.
    """
    system = platform.system()
    machine = platform.machine()
    if system in ("Windows", "Linux") or (system == "Darwin" and machine == "arm64"):
        return Availability(True)
    if system == "Darwin":
        return Availability(
            False,
            "needs Apple Silicon — the llama.cpp wheel index publishes no "
            f"macOS x86_64 build (this is {system.lower()}/{machine})",
        )
    return Availability(
        False,
        f"requires Windows, Linux, or Apple Silicon macOS (this is {system.lower()}/{machine})",
    )


# -- the accelerator probes ------------------------------------------------------
#
# CUDA and ROCm are OPT-IN rows (the CPU torch runners sit above them and remain
# the default), and both probes are HARD GATES: an accelerated row is selectable
# only where it can actually run. That is not fastidiousness — picking one on a
# machine with no matching device buys a multi-gigabyte wheel that then fails
# several frames inside a runtime library, which is exactly the "advertising is
# a claim" failure `Runner.available` was written for.
#
# **Everything below reads the KERNEL's answer, and reads it at CALL time.**
# Every path is a module-level constant so a test can build a fake sysfs on a
# tmp_path and repoint them — the filesystem analogue of the `registry.platform`
# monkeypatching every other availability test does.
#
# **Stdlib only, no torch, no subprocess.** This module is imported on a page
# render path (`describe`, `describe_engines`, and every `resolve`), so it may
# not import a 2GB framework to ask a question sysfs answers in microseconds,
# and it may not shell out: SPEC.md's ffmpeg rule bars relying on a system
# binary the app does not ship, `nvidia-smi` is not shipped, and a cold one
# costs 50-500ms against ~25µs for the sysfs walk.

#: Where the ROCm probe looks. `/sys/class/kfd` is the amdkfd driver's own
#: topology — the same thing ROCm's runtime enumerates — and `/dev/kfd` plus a
#: render node are the two devices a HIP process opens.
KFD_NODES_DIR = "/sys/class/kfd/kfd/topology/nodes"
KFD_DEVICE = "/dev/kfd"
DRI_DIR = "/dev/dri"
#: The DRM class, which answers two questions: is there an AMD GPU at all when
#: the KFD cannot say (`_amd_gpu_present`), and WHICH render node belongs to it
#: (`_amd_render_nodes`) — both `card*` and `renderD*` appear here, and each
#: carries the PCI `device/vendor` of the card behind it.
DRM_CLASS_DIR = "/sys/class/drm"
#: The PCI vendor id every AMD/ATI GPU reports.
AMD_PCI_VENDOR = "0x1002"

#: The gfx targets the ROCm wheels these runners install were actually built
#: for (torch 2.13 + rocm7.1).
#:
#: **TIED TO THE INDEX URL THE ROCm MANIFESTS PIN, so the two must move
#: together.** A wheel from a different ROCm index has a different set, and the
#: cost of getting this wrong is asymmetric: an unlisted card offered anyway is
#: a ~6GB download that dies inside HIP with "no kernel image is available for
#: execution", several frames below anything this app wrote. So an AMD GPU that
#: is not named here is refused with a reason, not optimistically allowed.
ROCM_TARGETS = frozenset({
    "gfx900", "gfx906", "gfx908", "gfx90a", "gfx942", "gfx950",
    "gfx1030", "gfx1100", "gfx1101", "gfx1102", "gfx1103",
    "gfx1150", "gfx1151", "gfx1200", "gfx1201",
})

#: Where the CUDA probe looks on Linux: the control node and at least one
#: per-GPU node are REQUIRED, and unified memory is checked only for PERMISSION
#: when it happens to exist.
#:
#: **`/dev/nvidia-uvm` is created LAZILY and its absence proves nothing** (D382).
#: `nvidia-modprobe` loads `nvidia_uvm` and makes the node the first time any
#: process creates a CUDA context; the display path needs only `nvidia` and
#: `nvidia_drm`. So a freshly booted desktop that has not run a CUDA program yet
#: has `/dev/nvidiactl` and `/dev/nvidia0` and NO `/dev/nvidia-uvm` while
#: `torch.cuda` works perfectly — the machine this gate was meant to serve, and
#: the one an existence check refused. When the node IS there, `os.access` on it
#: still earns its place: a container given the nodes without the access is
#: exactly the state it reports.
NVIDIA_CONTROL_DEVICE = "/dev/nvidiactl"
NVIDIA_UVM_DEVICE = "/dev/nvidia-uvm"
#: Where `/dev/nvidia0`, `/dev/nvidia1`… live. A constant so the glob below is
#: repointable with the rest.
NVIDIA_DEVICE_DIR = "/dev"
#: WSL2, which has NONE of the nodes above. GPU-PV exposes the card through
#: `/dev/dxg` and ships the CUDA driver library out of `/usr/lib/wsl/lib`, so a
#: WSL2 user whose `torch.cuda` works has no `/dev/nvidiactl` and no
#: `/dev/nvidia0` to show for it (D382). Both are `os.path.exists` and nothing
#: more: dlopening `libcuda.so.1` to be sure would initialise a driver on a page
#: render, which AI-6 bars for the same reason it bars `nvidia-smi`.
WSL_DXG_DEVICE = "/dev/dxg"
WSL_CUDA_LIBRARY = "/usr/lib/wsl/lib/libcuda.so.1"
#: Windows has no device nodes to ask, so the driver's own user-mode CUDA
#: library is the cheapest evidence available. **A HINT, NOT PROOF** — it is
#: installed by the display driver whether or not the GPU is CUDA-capable, and
#: proving it would mean loading it and calling `cuInit`, which is a DLL load
#: and a driver initialisation on a page render. Documented as the weaker gate
#: it is: on Windows a user can still pick a CUDA engine on a machine whose
#: driver is installed but whose GPU is not usable, and finds out at load time
#: with torch's own message. On Linux, where the nodes exist, the gate is real.
NVCUDA_DLL = r"C:\Windows\System32\nvcuda.dll"


def decode_gfx_target(raw: int) -> str | None:
    """An amdkfd `gfx_target_version` -> the target name ROCm wheels are named for.

    `major * 10000 + minor * 100 + step`, with **MINOR AND STEP RENDERED AS
    SINGLE HEX DIGITS**: 90010 is `gfx90a` and not `gfx9010`, 90402 is `gfx942`,
    120000 is `gfx1200`. A decimal render matches nothing in `ROCM_TARGETS`,
    which would refuse every AMD GPU on the argument that it is unsupported —
    the failure mode a wrong decoder has, and why the round-trip test over
    `ROCM_TARGETS` exists.

    None for 0, which is what a CPU node reports (see `_kfd_gfx_targets`).
    """
    if raw <= 0:
        return None
    major, rest = divmod(raw, 10000)
    minor, step = divmod(rest, 100)
    return f"gfx{major}{minor:x}{step:x}"


def _kfd_gfx_targets() -> list[str] | None:
    """Every GPU the amdkfd driver reports, decoded — None when unreadable.

    **EVERY node, not node 0.** Node 0 is the CPU on a machine with a perfectly
    working GPU (`cpu_cores_count 6, simd_count 0, gfx_target_version 0` on the
    box this was written on), so a probe that read only the first node decodes
    a zero target and concludes the machine has no supported GPU. A zero target
    is skipped rather than counted, which is the same fact stated once.

    An empty list means the driver is there and reports no GPU nodes — a
    container without device passthrough. None means the topology itself could
    not be read, which is a different sentence and gets one.
    """
    try:
        entries = sorted(os.listdir(KFD_NODES_DIR))
    except OSError:
        return None
    targets: list[str] = []
    read_any = False
    for entry in entries:
        path = os.path.join(KFD_NODES_DIR, entry, "properties")
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        read_any = True
        for line in text.splitlines():
            key, _, value = line.partition(" ")
            if key != "gfx_target_version":
                continue
            try:
                raw = int(value.strip())
            except ValueError:
                continue
            target = decode_gfx_target(raw)
            if target:
                targets.append(target)
    if entries and not read_any:
        return None
    return targets


def _amd_gpu_present() -> bool:
    """Is there an AMD GPU at all — asked of the DRM class, not of the KFD.

    The fallback for the branch where the KFD cannot answer, because a missing
    `/dev/kfd` has two very different causes: the amdkfd half of amdgpu is not
    loaded (an action — `modprobe amdgpu`, or reboot after a driver update), or
    there is no AMD GPU in the machine (a fact). One reason string for both
    would be wrong for whichever reader it was not written for.

    ~41µs, and only on the failure branch — the ordinary answer never runs it.
    """
    for path in glob.glob(os.path.join(DRM_CLASS_DIR, "card*", "device", "vendor")):
        try:
            with open(path, encoding="utf-8") as handle:
                if handle.read().strip().lower() == AMD_PCI_VENDOR:
                    return True
        except OSError:
            continue
    return False


def _amd_render_nodes() -> list[str]:
    """The `/dev/dri/renderD*` nodes belonging to an AMD card — HIP's second device.

    **Pinned to the AMD card, not to any render node that happens to open**
    (D382). The first version accepted any readable `renderD*`, which is wrong on
    every hybrid machine: an Intel iGPU's `renderD128` is world-openable on most
    distributions, so a box with an open Intel node and a restricted AMD one
    passed the gate on a device HIP will never touch, and the ~6GB install then
    failed when HIP opened the node it actually needed. `_amd_gpu_present` already
    reads `device/vendor` out of the DRM class; the render nodes are in the same
    class and carry the same file, so the vendor answers WHICH node too.

    Returns the device paths under `DRI_DIR` (sorted), not the sysfs entries —
    the caller asks `os.access` of the thing HIP opens.
    """
    nodes = []
    pattern = os.path.join(DRM_CLASS_DIR, "renderD*", "device", "vendor")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as handle:
                vendor = handle.read().strip().lower()
        except OSError:
            continue
        if vendor != AMD_PCI_VENDOR:
            continue
        node = os.path.join(DRI_DIR, os.path.basename(os.path.dirname(
            os.path.dirname(path))))
        if os.path.exists(node):
            nodes.append(node)
    return nodes


def _rocm() -> Availability:
    """The ROCm torch runners: Linux, an AMD GPU, and a gfx the wheel supports.

    **Never cached, and that is deliberate.** Every failure below is one a user
    FIXES WHILE THE APP IS RUNNING — `modprobe amdgpu`, plugging in an eGPU,
    restarting a container with `--device /dev/kfd`, being added to the render
    group and logging back in. A cached "no /dev/kfd" that survived the fix is
    precisely the bug the reason string exists to prevent: the sentence tells
    someone what to do and then the app refuses to notice they did it. The cost
    is ~22µs for the whole probe on the machine it was written on (~40µs more
    for the DRM fallback, which only runs on the failure branch), against a
    resolution that happens per load, per download and per page render — not per
    token. `_cuda` measures ~41µs the same way. An `lru_cache` would also make
    test ORDER significant against the monkeypatch style every other
    availability test here uses, which is a second reason of its own.
    `preferred_code` declines caching on the same grounds.

    **Permission is asked of the kernel, never modelled.** `os.access` with
    `R_OK | W_OK`, because a group-membership or mode-arithmetic check gets real
    machines wrong in BOTH directions: this box's `/dev/kfd` is `crw-rw-rw-`
    while the user is in neither render nor video (a group check would refuse a
    working machine), and its `card1` is `crw-rw----+` — a POSIX ACL, invisible
    to mode arithmetic, which would refuse a machine the ACL permits.
    """
    system = platform.system()
    machine = platform.machine()
    if system != "Linux":
        return Availability(
            False,
            "needs Linux — the ROCm PyTorch wheels are published for Linux "
            f"only (this is {system.lower()}/{machine})",
        )
    if not os.path.exists(KFD_DEVICE):
        if _amd_gpu_present():
            return Availability(
                False,
                "needs the amdgpu kernel driver — an AMD GPU is here but "
                f"{KFD_DEVICE} is missing, so ROCm cannot see it (load the "
                "driver with `modprobe amdgpu`, or reboot after a driver update)",
            )
        return Availability(
            False,
            "needs an AMD GPU — this machine has none that the kernel reports "
            f"(this is {system.lower()}/{machine})",
        )
    if not os.access(KFD_DEVICE, os.R_OK | os.W_OK):
        return Availability(
            False,
            f"needs permission to use the GPU — {KFD_DEVICE} is not readable "
            "and writable by you (add your user to the group that owns it, "
            "usually `render`, then log out and back in)",
        )
    # HIP opens `/dev/kfd` AND the card's own render node, and the two failures
    # here are DIFFERENT SENTENCES because they have different fixes. A node that
    # is not THERE cannot be fixed by joining a group — that is a container
    # started without `--device /dev/dri`, or a `/dev/dri` the amdgpu driver has
    # not populated — while a node that is there and closed is precisely the
    # group case. One sentence for both sent half its readers after a `usermod`
    # that could not have helped.
    render_nodes = _amd_render_nodes()
    if not render_nodes:
        return Availability(
            False,
            f"needs the AMD GPU's render node — no {DRI_DIR}/renderD* device "
            "belongs to an AMD card here, which is what a container started "
            "without `--device /dev/dri` looks like",
        )
    if not any(os.access(path, os.R_OK | os.W_OK) for path in render_nodes):
        return Availability(
            False,
            f"needs permission to use the GPU — {render_nodes[0]} is the AMD "
            "card's render node and is not readable and writable by you (add "
            "your user to the `render` group, then log out and back in)",
        )
    targets = _kfd_gfx_targets()
    if targets is None:
        return Availability(
            False,
            f"needs the amdgpu driver's topology — {KFD_NODES_DIR} could not be "
            "read, so the GPU cannot be identified",
        )
    if not targets:
        return Availability(
            False,
            "needs an AMD GPU the kernel can see — the amdgpu driver reports "
            "CPU nodes only, which is what a container started without "
            "`--device /dev/kfd --device /dev/dri` looks like",
        )
    if not any(target in ROCM_TARGETS for target in targets):
        found = ", ".join(sorted(set(targets)))
        return Availability(
            False,
            f"needs a supported AMD GPU — {found} is not supported by the ROCm "
            "build this runner installs (a wheel built for another target "
            "downloads six gigabytes and then fails inside HIP)",
        )
    return Availability(True)


def _cuda() -> Availability:
    """The CUDA torch runners: an NVIDIA GPU whose driver is loaded and usable.

    A HARD GATE, like `_rocm` and for the same reason — an accelerated row that
    is selectable on a machine with no NVIDIA GPU is a multi-gigabyte download
    that fails at load. Not cached, for `_rocm`'s reasons exactly (an eGPU, a
    container restart, a driver reloaded — all fixed while the app runs).

    **Three shapes of NVIDIA machine, and only one of them has device nodes.**
    Ordinary Linux has `/dev/nvidiactl` + `/dev/nvidia[0-9]*`; WSL2 has neither
    and works anyway (`/dev/dxg`, and the driver's `libcuda.so.1` under
    `/usr/lib/wsl/lib`); Windows has no nodes at all and is gated on the
    driver's own DLL. Absence of the Linux nodes is therefore not evidence
    against WSL2, which is why that branch is asked FIRST rather than as a
    fallback after a refusal has already been written.

    **No `nvidia-smi`.** SPEC.md's rule about system binaries the app does not
    ship, and a cold `nvidia-smi` is 50-500ms on a per-page-render path against
    ~25µs of `os.access`.

    **No driver-version floor.** The floor belongs to the CUDA the runner's
    wheel pins, this module cannot read that from a file it does not have, and
    guessing high disables machines that work. The wheel's own error is the
    better reporter of a driver that is genuinely too old.
    """
    system = platform.system()
    machine = platform.machine()
    if system == "Linux":
        # WSL2 FIRST, because it has none of the nodes below and torch.cuda
        # works there anyway (D382). GPU-PV projects the Windows driver into the
        # guest as `/dev/dxg` plus a `libcuda.so.1` under `/usr/lib/wsl/lib`, so
        # a WSL2 user was told "there is no /dev/nvidiactl on this machine",
        # which was true and beside the point, and could not select the engine
        # at all. Two `os.path.exists` and no dlopen — see the constants.
        if os.path.exists(WSL_DXG_DEVICE) and os.path.exists(WSL_CUDA_LIBRARY):
            return Availability(True)
        gpus = glob.glob(os.path.join(NVIDIA_DEVICE_DIR, "nvidia[0-9]*"))
        if not os.path.exists(NVIDIA_CONTROL_DEVICE) or not gpus:
            return Availability(
                False,
                "needs an NVIDIA GPU with its driver loaded — there is no "
                f"{NVIDIA_CONTROL_DEVICE} or /dev/nvidia0 on this machine "
                f"(this is {system.lower()}/{machine})",
            )
        unusable = [path for path in [NVIDIA_CONTROL_DEVICE, *sorted(gpus)]
                    if not os.access(path, os.R_OK | os.W_OK)]
        if unusable:
            return Availability(
                False,
                f"needs permission to use the GPU — {unusable[0]} is not "
                "readable and writable by you (this is usually a container "
                "missing `--gpus all`, or a device-permission rule)",
            )
        # …and unified memory, checked for PERMISSION and never for EXISTENCE
        # (D382). `nvidia_uvm` is a separate module that `nvidia-modprobe` loads
        # the first time a process creates a CUDA context, so a freshly booted
        # desktop that has not run a CUDA program yet has the GPU nodes, no
        # `/dev/nvidia-uvm`, and a working `torch.cuda`. Refusing on its absence
        # greyed out both CUDA rows there and blamed "a driver update without a
        # reboot" — the opposite of what had happened. A node that IS present and
        # closed is still worth a sentence: that is a container handed the
        # devices without the access, which no amount of waiting fixes.
        if os.path.exists(NVIDIA_UVM_DEVICE) and not os.access(
                NVIDIA_UVM_DEVICE, os.R_OK | os.W_OK):
            return Availability(
                False,
                f"needs permission to use the GPU — {NVIDIA_UVM_DEVICE} is not "
                "readable and writable by you (this is usually a container "
                "missing `--gpus all`, or a device-permission rule)",
            )
        return Availability(True)
    if system == "Windows":
        if not os.path.isfile(NVCUDA_DLL):
            return Availability(
                False,
                "needs an NVIDIA GPU with its driver installed — the driver's "
                f"CUDA library is not at {NVCUDA_DLL} (this is "
                f"{system.lower()}/{machine})",
            )
        return Availability(True)
    return Availability(
        False,
        "needs an NVIDIA GPU — CUDA is published for Windows and Linux only "
        f"(this is {system.lower()}/{machine})",
    )


# The table. Ordered, and first-match-wins per capability — which is what lets
# TWO runners serve one: MLX takes Apple Silicon when available, and the row
# below it serves Windows and Linux plus the Apple Silicon fallback. All three
# multi-runner capabilities (text generation, image generation, speech to text)
# are arranged that way. The ordering is the whole mechanism, so the rows are
# not sorted alphabetically and must not be — it is also the DEFAULT that a
# user's engine preference overrides, so a re-order silently re-decides every
# machine set to "auto", which is all of them until somebody chooses otherwise.
#
# **The torch runners are split PER HARDWARE, and the CPU build is the default.**
# One `transformers-text` row that installed whichever wheel index a manifest
# happened to pin made the accelerator an invisible property of the build: a
# machine got CUDA or it got the CPU and nothing on the page said which, so the
# name was honest on exactly one class of hardware. There are now three rows per
# torch library — CPU, CUDA, ROCm — the CPU one sits FIRST and is what every
# "auto" machine resolves to, and the accelerated two are opt-in from the
# Engines tab and gated on the device actually being there (`_cuda`, `_rocm`).
# CPU-by-default is the conservative half of that decision: the accelerated
# wheels are much larger downloads with a hardware requirement, and a default
# that silently required one would fail on the machines least able to explain
# why. `code` on the two original rows is UNCHANGED, so a stored preference
# naming `transformers-text` or `diffusers-image` keeps meaning what it meant.
#
# **A hardware variant carries its accelerator in BOTH names**, so `label` and
# `short_label` are equal on all six torch rows ("Diffusers (CUDA)"). The short
# name is what the Local card and `servingLine` print, and three engines whose
# short names are all "Diffusers" would render as one engine on every surface
# but the picker. The MLX rows keep a PLATFORM qualifier on the long name only
# — a bracketed qualifier in the SHORT name is therefore the marker of a
# hardware variant, and the Apple-only rows stay visually distinct from them.
_RUNNERS: tuple[Runner, ...] = (
    Runner(
        code="mlx-text",
        capability=TEXT_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "mlx_text"),
        label="MLX LM (Apple Silicon)",
        short_label="MLX LM",
        _available=_apple_silicon,
    ),
    Runner(
        code="transformers-text",
        capability=TEXT_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "transformers_text"),
        # "(CPU)" rather than the old "(PyTorch)": the library is not what
        # distinguishes this row from its neighbours any more — all three are
        # PyTorch — and the accelerator is, so the qualifier names the thing a
        # reader is choosing between. Both names carry it; see the table's
        # naming note above.
        #
        # **"(CPU)" names the BUILD, not a prediction about the device.** It is
        # the `whl/cpu` pin — the install with no accelerator libraries in it —
        # and on Apple Silicon that same pin resolves to the ordinary macOS wheel
        # with MPS compiled in, so this row runs on the GPU there. What device a
        # model actually got is the worker's to report and AI-11b's to show; the
        # `note` says the Mac case out loud so the two never disagree.
        label="Transformers (CPU)",
        short_label="Transformers (CPU)",
        # ONE LINE, and that is a hard constraint rather than a summary: it sits
        # under this engine's row on the Engines tab, in the space between one
        # picker and the next, and anything that wraps twice is something nobody
        # finishes.
        #
        # **It names the Apple Silicon GPU, because this row USES it** (D382).
        # `torch_text._placement()` returns `("mps", float16)` on a Mac, which is
        # the fallback path the `whl/cpu` pin was chosen to preserve — so the old
        # wording ("Runs on the CPU on any machine, at a few words a second") had
        # the Engines tab printing a CPU speed claim while the loaded card beside
        # it reported device `mps`, one page contradicting itself. The speed
        # figure went with it: "a few words a second" is a CPU measurement and
        # the row is not always on a CPU, and the loaded card's tooltip is where
        # somebody who has stopped to ask reads about speed anyway.
        note="Runs on the CPU on any machine, or Apple Silicon's GPU — the "
             "CUDA and ROCm engines need a matching GPU.",
        # Deliberately BELOW the MLX row rather than instead of it. Apple Silicon
        # therefore gets MLX when it is present and this runner's working MPS
        # path when it is not; Windows and Linux come here directly. Intel macOS
        # is not a distribution target.
        _available=_transformers_platform,
    ),
    # …and the accelerated variants, BELOW the CPU row so that nothing about the
    # default moves: a machine set to "auto" resolves to the CPU build even with
    # a working GPU in it (`test_AUTO_STAYS_ON_THE_CPU_ROW_EVEN_WITH_AN_ACCELERATOR`
    # is that decision, named). Opting in is one radio on the Engines tab, and
    # the radio is disabled with the probe's own reason on a machine that cannot
    # take it — which is what makes offering these rows at all safe.
    Runner(
        code="transformers-text-cuda",
        capability=TEXT_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "transformers_text_cuda"),
        label="Transformers (CUDA)",
        short_label="Transformers (CUDA)",
        note="Much quicker on an NVIDIA GPU, for a much larger download.",
        _available=_cuda,
    ),
    Runner(
        code="transformers-text-rocm",
        capability=TEXT_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "transformers_text_rocm"),
        label="Transformers (ROCm)",
        short_label="Transformers (ROCm)",
        note="Much quicker on a supported AMD GPU under Linux, for a much "
             "larger download.",
        _available=_rocm,
    ),
    # A fourth text runner (SPEC AI-11, AI-2a, D402) — GGUF via llama.cpp,
    # BELOW all three transformers rows so `auto` resolution never moves: on
    # every platform a bare "auto" reaches MLX or a transformers row exactly
    # as it did before this runner existed
    # (`test_AUTO_STAYS_ON_THE_CPU_ROW_EVEN_WITH_AN_ACCELERATOR` is the named
    # test of that property, and it asserts nothing about this row precisely
    # because nothing about it should change). Reaching this runner is
    # therefore always a CHOICE made on the Engines tab, never a fallthrough —
    # which is the point: `llamacpp_text/pyproject.toml` documents that the
    # maintainer's wheel index is a coin-flip per release on macOS arm64
    # (roughly 4 of 16 sampled releases pass an integrity check), so a
    # capability whose INSTALL can silently fail must never be what a machine
    # gets without asking for it, however sound the pinned version itself is
    # once verified. `_llamacpp_platform`, not `_always`: the pin this runner
    # declares is CPU wheels, but NOT on every platform Diffusers CPU and
    # Faster Whisper reach — the maintainer's index publishes no macOS x86_64
    # tag at all, so Intel macOS is a hard exclusion (see that function).
    Runner(
        code="llamacpp-text",
        capability=TEXT_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "llamacpp_text"),
        label="llama.cpp (GGUF)",
        short_label="llama.cpp",
        # ONE LINE, per the rule the transformers row states. Leads with the
        # reason to pick it (current-generation Qwen at a fraction of the bf16
        # download) and folds the packaging caveat into the same sentence,
        # since that is the fact this row's `_available` cannot express —
        # `_llamacpp_platform` answers "does the wheel exist for this
        # platform", not "was THIS release's wheel intact when it was built".
        note="Runs current Qwen GGUF quantizations at a fraction of the "
             "unquantized download — opt-in because its wheels come from the "
             "maintainer's own index rather than PyPI.",
        _available=_llamacpp_platform,
    ),
    # Image generation is arranged like the other two: MLX takes the Macs
    # (D310). One 4.6GB repo against the ~10.1GB two-repo split the torch
    # recipe needs, ~8x quicker to load, ~15-20% quicker per image, measured
    # same model, prompt and seed.
    #
    # **The memory ceiling is a KNOWN, ACCEPTED risk rather than an unknown.**
    # MLX's allocator reported a ~23.6GB `get_cache_memory` high-water doing
    # those renders — larger than torch's ~19.1GB Metal allocation for the same
    # picture — on a 34GB machine already several GB into swap, and nothing has
    # been run on the 16GB Macs this app's own catalog says full-precision FLUX
    # already OOMs. The evidence is one machine's benchmark; the decision was to
    # take the speed and let a user who hits the ceiling move back to Diffusers
    # from the Engines tab, which is the case the engine preference (D302)
    # exists to serve in both directions. The `note` says so **under that very
    # picker** (D315) — it is the sentence a 16GB Mac needs at the moment it is
    # deciding whether to switch away, and it was over a grid of downloads on
    # another tab until then.
    Runner(
        code="mflux-image",
        capability=IMAGE_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "mflux_image"),
        label="MLX FLUX (Apple Silicon)",
        short_label="MLX FLUX",
        # ONE LINE, per the rule the transformers row states. It describes the
        # DEFAULT rather than an opt-in, so the memory caveat leads: the reader
        # it exists for is someone on a small Mac deciding whether to switch
        # AWAY, not someone deciding whether to try it — and since D315 the line
        # is rendered directly under the control that does the switching.
        note="Reserves much more memory than Diffusers and is untested below "
             "32GB, but loads far quicker from a smaller download.",
        _available=_apple_silicon,
    ),
    Runner(
        code="diffusers-image",
        capability=IMAGE_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "diffusers_image"),
        # "(CPU)" for the reason the transformers row above states.
        label="Diffusers (CPU)",
        short_label="Diffusers (CPU)",
        # This row had no note while it was the only torch image engine and
        # there was nothing to distinguish it from. Now there is, and the thing
        # worth saying is the one that decides the choice: CPU diffusion is
        # minutes per image, not seconds — SAID OF THE CPU rather than of the
        # row, because `torch_image._place()` moves the pipeline to `mps` on a
        # Mac (D382), and a flat "minutes per image" contradicted the `mps` the
        # loaded card reports on the very machine this row exists to catch when
        # MLX FLUX is unavailable.
        note="Renders on Apple Silicon's GPU, or on the CPU anywhere else at "
             "minutes per image rather than seconds.",
        _available=_always,
    ),
    # The accelerated image variants, below the CPU row for the reason the text
    # variants are below theirs.
    Runner(
        code="diffusers-image-cuda",
        capability=IMAGE_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "diffusers_image_cuda"),
        label="Diffusers (CUDA)",
        short_label="Diffusers (CUDA)",
        note="Seconds per image on an NVIDIA GPU, for a much larger download.",
        _available=_cuda,
    ),
    # THE SHARED RING, and why the ROCm image row warns about the desktop.
    #
    # On a single-GPU machine the render and the compositor submit to the SAME
    # ring, so a sustained submission starves the screen until the driver gives
    # up on it. Measured, not feared — an RX 9060 XT (gfx1200), kernel 7.1.4-zen:
    #
    #     amdgpu: ring gfx_0.0.0 timeout, signaled seq=6239674, emitted 6239675
    #     amdgpu:  Process Hyprland pid 832655 thread Hyprland:cs0
    #     amdgpu: Starting gfx_0.0.0 ring reset / Ring gfx_0.0.0 reset succeeded
    #     amdgpu: [drm] device wedged, but no recovery needed
    #
    # Note WHICH process the kernel named: the compositor, not the renderer. The
    # GPU recovered without a reboot and the session did not — the desktop went
    # down and the ring came back.
    #
    # **Honest about what produced it:** a continuous matmul loop, not a render.
    # A large 100-step render submits the same class of work, and a ~90s FLUX.2
    # klein render on that card finished without a stall — which is why the note
    # says a long render CAN stall the desktop rather than will.
    #
    # Nothing mitigates it here, deliberately. The fixes that exist are outside
    # this app — CU masking, queue priority, rendering on a card that is not
    # driving the display — none verified, and an unverified mitigation is a
    # promise made on the driver's behalf. Naming the cost and letting the user
    # choose is the bargain the download size already gets (D383). It is
    # documented HERE and not in the manifest on purpose: `state_digest` hashes
    # `pyproject.toml` whole, so a comment there would mark every already-built
    # ROCm env stale and charge existing users a resync for a paragraph.
    Runner(
        code="diffusers-image-rocm",
        capability=IMAGE_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "diffusers_image_rocm"),
        label="Diffusers (ROCm)",
        short_label="Diffusers (ROCm)",
        # The desktop clause is not padding — see THE SHARED RING above for the
        # kernel log that proved it. One line, so "much larger" pays for it.
        note="Seconds per image on a supported AMD GPU under Linux — larger "
             "download; a long render can stall the desktop.",
        _available=_rocm,
    ),
    # Speech to text, and the capability that finally USED the two-runner
    # ordering this table was built for. MLX takes the Macs; CTranslate2 below
    # it keeps every other platform — and keeps the Macs too whenever the MLX
    # folder is not built yet, which is the state `Runner.available` describes.
    Runner(
        code="mlx-whisper",
        capability=SPEECH_TO_TEXT,
        folder=os.path.join(RUNNERS_DIR, "mlx_whisper"),
        label="MLX Whisper (Apple Silicon)",
        short_label="MLX Whisper",
        note="Transcribes on the GPU. Several times quicker than the CPU path "
             "on the same Mac.",
        _available=_apple_silicon,
    ),
    # …and the third, which is the first capability to have one (D319).
    # **Below MLX Whisper deliberately**, so nothing about the default changes:
    # Parakeet-TDT beats Whisper large-v3 on English word error rate and is
    # several times quicker again on the same Mac, but v3 covers 25 European
    # languages against Whisper's ~99 — so promoting it would silently break
    # every page relying on `language` being detected, on recordings this
    # model has never heard a word of. A user opts in per capability on the
    # Engines tab (D302), which is exactly the machinery that case needs and
    # is why no new plumbing came with this runner.
    Runner(
        code="parakeet-mlx",
        capability=SPEECH_TO_TEXT,
        folder=os.path.join(RUNNERS_DIR, "parakeet_mlx"),
        label="Parakeet TDT (Apple Silicon)",
        short_label="Parakeet TDT",
        # ONE LINE, per the rule the transformers row states — it sits under
        # this engine's row on the Engines tab, between one picker and the
        # next. It describes an OPT-IN, so what it leads with is the reason to
        # take it and the reason not to, in that order.
        note="Quicker than Whisper and more accurate in English, but it "
             "handles 25 European languages only.",
        _available=_apple_silicon,
    ),
    Runner(
        code="faster-whisper",
        capability=SPEECH_TO_TEXT,
        folder=os.path.join(RUNNERS_DIR, "faster_whisper"),
        # "Faster Whisper", not the upstream "faster-whisper": the tag column is
        # product names, and `code` above still carries the exact spelling.
        label="Faster Whisper (CTranslate2)",
        short_label="Faster Whisper",
        # `_always`, and that is why speech to text SHIPPED on CTranslate2
        # rather than on MLX: text generation was already Apple-Silicon-only,
        # and a second capability that existed on a Mac and nowhere else would
        # have made "local AI" a thing Windows and Linux users read about
        # rather than used. The MLX row above is the sequel that argument
        # always allowed for — it takes the Macs and leaves everything else
        # here, and no user loses a capability to it.
        _available=_always,
    ),
)


#: Friendly task label (the vocabulary `ai_models` produces) -> the capability
#: that can actually RUN it.
#:
#: **A vision-language checkpoint is a text model when you only give it text**,
#: and that is not a technicality — it is now the NORMAL case rather than an
#: exception. Every model in this app's own MLX catalog is labelled "image +
#: text to text", because Qwen3.5 and gemma-4 ship as one checkpoint with a
#: vision tower (and, for gemma-4, an audio one) attached to the language
#: model. Leaving the label out of this table took the Load button off the
#: models the app was suggesting on the next tab over. mlx-lm loads such a
#: checkpoint through its text config; the other towers simply go unused until
#: an `mlx-vlm` runner exists to use them.
_TASK_CAPABILITIES = {
    "text generation": TEXT_GENERATION,
    "image + text to text": TEXT_GENERATION,
    "text to image": IMAGE_GENERATION,
    "image generation": IMAGE_GENERATION,
    "speech recognition": SPEECH_TO_TEXT,
}

#: The other half of the same decision: labels nothing here serves, listed
#: rather than merely absent.
#:
#: Absence is how the gemma bug happened — a label that nobody had thought about
#: and a label that had been ruled out looked identical, so the vocabulary grew
#: and the table silently did not. `test_every_task_label_is_classified` requires
#: every label the listing can produce to appear in one of these two, which turns
#: "we forgot" into a failing test instead of a missing button.
NO_RUNNER_YET = frozenset({
    # Nothing here generates embeddings, classifies, or segments — these are
    # real jobs with no local runner in this cut.
    "embeddings", "sentence embeddings", "fill mask", "text classification",
    "token classification", "question answering", "summarization", "translation",
    "image classification", "zero-shot image classification",
    "zero-shot text classification", "image segmentation", "object detection",
    "depth estimation", "image to image", "image to text", "audio classification",
    "video generation",
    # An audio-language model: a recording and a prompt in, text out. It is
    # NOT speech recognition — it is asked questions about the audio rather
    # than asked to transcribe it — and mlx-lm has no module for one, so
    # neither the text runners nor the whisper runners can serve it. Listed
    # here rather than left out, because an absent label and a ruled-out label
    # look identical from a card (see the table above).
    "audio + text to text",
    # Speech OUT, as opposed to speech in. Deliberately not folded into the
    # transcription capability as a direction flag: one capability holds one
    # resident model, so a shared "audio" capability would have a synthesis
    # model evict a Whisper model and back again on every alternation.
    "text to speech", "audio generation",
    # An encoder-decoder (T5-shaped). Not the causal-LM path mlx-lm serves, so
    # it is not text generation however much the name suggests it.
    "text-to-text generation",
    # A model that takes and returns several modalities at once. Which one a
    # caller wants is not a thing this table can decide.
    "any input to any output",
})


def capability_for_task(task: str | None) -> str | None:
    """Which capability, if any, could load a model doing `task`.

    Here rather than in the page, because the page would then hold a second copy
    of the mapping between the task vocabulary and the capability vocabulary —
    and a page that guesses "text-generation" for everything will happily try to
    load a diffusion model as a chat model.

    None for a label in `NO_RUNNER_YET`, and None for a label in NEITHER table —
    the answers are the same but the second one is a bug, which is what the
    classification test exists to catch.
    """
    if not task:
        return None
    return _TASK_CAPABILITIES.get(task)


def all_runners() -> tuple[Runner, ...]:
    return _RUNNERS


def by_code(code: str) -> Runner | None:
    return next((r for r in _RUNNERS if r.code == code), None)


#: What a capability's engine preference says when nobody has chosen: use the
#: table's order. The literal is shared with `shell/prefs.py` and the
#: Preferences page rather than spelled three times, because it is a value that
#: travels through JSON and a typo in any copy reads as an unknown runner.
AUTO = "auto"


@dataclass(frozen=True)
class Resolution:
    """Which runner serves a capability here, and whether anyone was overruled.

    `for_capability` answers only the first half, which is all almost every
    caller wants. This exists for the ones that have to EXPLAIN the answer: the
    Preferences page, which must not show a preference as being in force when it
    is not, and the AI Models page, whose suggestion list changes when the
    engine does.
    """

    #: What will actually load. None when nothing can serve the capability here.
    runner: Runner | None
    #: The preference as stored — `AUTO`, or a runner code.
    requested: str = AUTO
    #: Why the request was not honoured, in words, for a page to show. Empty
    #: when it was — including when nothing was requested, since "auto" is
    #: honoured by definition.
    ignored_reason: str = ""

    @property
    def honoured(self) -> bool:
        return not self.ignored_reason


def preferred_code(capability: str) -> str:
    """The user's engine choice for `capability` — `AUTO` when there is none.

    Read on every resolution rather than cached, for the same reason
    `prefs.selected_engine()` is: a preference is a file, changing it must not
    need a restart (CT-5), and this is not on a hot path — a resolution happens
    once per load, per download and per page render, not per token.

    Imported lazily and defended, because the registry is imported by the
    supervisor and by the worker-facing code paths, and it must not become a
    thing that cannot answer because a preferences file is unreadable. A machine
    with no prefs.json is the normal case, not an error.
    """
    try:
        from fused_render.shell import prefs

        return prefs.engine_for_capability(capability)
    except Exception:  # noqa: BLE001 - a preference must never break resolution
        return AUTO


def _first_available(capability: str) -> Runner | None:
    """Registry order, filtered by availability — the rule before D302, and
    still the rule whenever a preference is absent or unusable."""
    for runner in _RUNNERS:
        if runner.capability == capability and runner.available().ok:
            return runner
    return None


def resolve(capability: str) -> Resolution:
    """Which runner serves `capability` here, and what the user asked for.

    Availability is part of the resolution and not a check the caller does
    after: picking a runner that cannot run and failing later would report "the
    model failed to load" for a machine that was never going to be able to load
    it. That applies to the PREFERENCE too, which is the whole design of this
    function — see the module docstring. A preference naming a runner that
    cannot run here is dropped, the ordering decides instead, and the reason
    comes back so that a page can say so rather than showing a control whose
    value has no effect.

    Three ways a preference is dropped, and they are told apart because the
    remedies differ:

    * the runner does not serve this capability (a stale prefs.json, or one
      hand-edited),
    * the runner is not registered at all (a preference written by a NEWER
      build, then opened by an older one — the reason this is not an assert),
    * the runner cannot run here, which is the case that actually happens: a
      preference set on a Mac, carried to a Windows machine in a synced home
      directory.
    """
    requested = preferred_code(capability)
    if requested and requested != AUTO:
        runner = by_code(requested)
        if runner is None:
            return Resolution(_first_available(capability), requested,
                              f"{requested} is not a runner this build knows")
        if runner.capability != capability:
            return Resolution(_first_available(capability), requested,
                              f"{runner.short} does not do {capability}")
        status = runner.available()
        if not status.ok:
            return Resolution(_first_available(capability), requested,
                              status.reason or f"{runner.short} cannot run here")
        return Resolution(runner, requested)
    return Resolution(_first_available(capability), AUTO)


def for_capability(capability: str) -> Runner | None:
    """The runner that serves `capability` HERE, or None.

    The whole app's resolution, and deliberately the SAME call for the
    supervisor, the catalog and the API — a second copy of this rule is how a
    page comes to offer a model the loader then refuses (D293), and a
    preference honoured in one place and not another would be the same bug with
    a new cause.
    """
    return resolve(capability).runner


def unavailable_reason(capability: str) -> str | None:
    """Why nothing here serves `capability`, in words — or None when something does.

    The same sentence `supervisor._runner_or_raise` raises, available to a
    caller that has not got as far as starting anything. It exists because a
    request can now fail EARLIER than the supervisor for a reason that has
    nothing to do with the real one: since the catalog became per-runner (D293),
    an unavailable runner also has no curated default, so `POST /api/ai/image`
    answered "no image model is configured" — true, useless, and hiding the
    actionable "the Diffusers runner is not built yet" one layer down.

    Both messages are worth keeping, and this is what tells them apart: no
    runner is a fact about the MACHINE, and no suggestion is a fact about the
    CATALOG.

    **Every runner's reason, not the first one's**, and with two runners per
    capability that stopped being a detail. The first cut took
    `next(r for r in _RUNNERS if r.capability == capability)`, which for text
    generation is always `mlx-text` — so a Linux machine whose transformers
    worker was missing (a state `Runner.available` documents, since a runner is
    registered before its folder is written) would be told text generation
    "needs Apple Silicon", naming the one backend that was never going to serve
    it and hiding the one that would have. Reported by review on the PR that
    added the second runner.

    Joined rather than picked, because there is no rule for choosing between
    them that is not a guess about which the reader meant — and a capability
    with one runner, which is all of them but this one, reads exactly as before.
    Duplicates are dropped: two runners of the same label failing the same way
    is one sentence, said once.
    """
    if for_capability(capability) is not None:
        return None
    reasons: list[str] = []
    for runner in _RUNNERS:
        if runner.capability != capability:
            continue
        reason = runner.available().reason
        if reason and reason not in reasons:
            reasons.append(reason)
    return "; ".join(reasons) or f"no runner provides {capability!r}"


def capabilities() -> tuple[str, ...]:
    """Every capability the registry knows, servable here or not — the page
    lists them all so an unavailable one can say why."""
    seen: list[str] = []
    for runner in _RUNNERS:
        if runner.capability not in seen:
            seen.append(runner.capability)
    return tuple(seen)


def describe() -> list[dict]:
    """The registry as the API reports it: what exists, what runs here, and why
    not when it does not.

    **`available` and `active` are different questions, and they only became
    different with D302.** Availability is a fact about the hardware: can this
    backend run at all. Active is a fact about this capability right now: is
    this the backend a load would use. They were the same answer while
    resolution was purely first-available — the first available runner was the
    one that ran — so `fused.ai.models.list()` reported availability and every
    reader took it to mean "this is what serves me". With a user preference in
    the middle that reading is wrong: on an Apple Silicon machine BOTH whisper
    runners are available and exactly one is active. A page that cannot tell
    them apart cannot say which engine transcribed for it.
    """
    engines = {capability: resolve(capability) for capability in capabilities()}
    rows = []
    for runner in _RUNNERS:
        status = runner.available()
        rows.append(
            {
                "code": runner.code,
                "capability": runner.capability,
                # BOTH names, and the field names say which is which. `label`
                # keeps meaning the full one everywhere on the wire — a
                # consumer that reads it after this change gets exactly what it
                # got before — and a surface that wants the qualifier-free name
                # asks for it. The alternative, quietly shortening `label`,
                # would be a change no reader of the payload could see.
                "label": runner.label,
                "shortLabel": runner.short,
                "note": runner.note or None,
                "available": status.ok,
                "reason": status.reason or None,
                # Which of the available runners this capability is actually
                # using. False for every runner of a capability nothing can
                # serve, which is the honest answer — there is no active engine.
                "active": engines[runner.capability].runner is runner,
            }
        )
    return rows


def _choice(runner: Runner) -> dict:
    """One option under an engine picker, built from ONE probe (D382).

    A function rather than two lines inside the comprehension below because the
    bug this fixes was exactly that shape: `available` read `runner.available()`
    and `reason` read it AGAIN. That was free while every probe was a `platform`
    fact and stopped being free the moment a probe became a live device read
    (AI-6) — two calls can straddle a `modprobe`, a container restart or an eGPU
    being unplugged and disagree. Both disagreements reach the user and neither
    is a crash: the option serialises as `available: false` with `reason: null`,
    which the `<select>` renders as a disabled row with NOTHING saying why (the
    page has no copy of the reason and must not), or as `available: true` still
    carrying the refusal that has just stopped being true. Binding the status to
    a name once makes the second read impossible rather than merely unlikely.
    """
    status = runner.available()
    return {
        "code": runner.code,
        "label": runner.label,
        "note": runner.note or None,
        "available": status.ok,
        # The registry's own words ("needs Apple Silicon — MLX runs on Metal
        # only (this is windows/amd64)"), so the disabled radio explains itself
        # with the same sentence the rest of the app uses. The page must not
        # write its own copy of this, because the page cannot know it.
        "reason": status.reason or None,
    }


def describe_engines() -> list[dict]:
    """One row per capability: what was asked for, what is serving, what was
    ignored.

    Separate from `describe()` because it answers a different question and has a
    different cardinality — a preference belongs to a CAPABILITY, while
    availability belongs to a RUNNER. Folding the two would give every runner
    row a copy of its capability's preference, and two rows of one capability
    disagreeing about it would then be representable.
    """
    rows = []
    for capability in capabilities():
        resolution = resolve(capability)
        rows.append(
            {
                "capability": capability,
                # As STORED: what a PUT round-trips, and what applies again if
                # the machine that cannot honour it stops being the one in use.
                # Never rewritten to match reality — a preference silently
                # corrected on read is one the user cannot see or undo.
                "selected": resolution.requested,
                "effective": resolution.runner.code if resolution.runner else None,
                "effectiveLabel": resolution.runner.label if resolution.runner else None,
                # The summary line under the picker ("Using MLX LM.") reads the
                # short one: it sits directly beneath the options, which carry
                # the qualifier a line above, and repeating it there says
                # nothing the reader has not just read. The picker itself, and
                # the sentence that names a chosen option back to the user
                # (`ignoredWarning`), stay on `label`.
                "effectiveShortLabel": resolution.runner.short if resolution.runner else None,
                # Null when the selection is in force (including "auto", which
                # is honoured by definition). A sentence when it is not, and the
                # UI is expected to show it — a control whose value does nothing,
                # with nothing saying why, is the failure this field exists for.
                "ignoredReason": resolution.ignored_reason or None,
                "choices": [
                    _choice(runner)
                    for runner in _RUNNERS
                    if runner.capability == capability
                ],
            }
        )
    return rows
