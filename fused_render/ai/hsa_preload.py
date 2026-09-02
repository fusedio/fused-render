"""Whether to launch a ROCm AI worker with the SYSTEM `libhsa-runtime64.so`
preloaded ahead of the one torch bundles into its own wheel.

**The bug this works around.** On Linux/ROCm, the moment a process creates a
HIP context, torch's bundled `libhsa-runtime64.so` starts a background thread
(`rocr::core::Runtime::AsyncEventsLoop`) that busy-waits on `/dev/kfd` for as
long as the context lives -- pinning ~100% of one CPU core even with the
model fully idle and nothing queued on the GPU. It reproduces with no model
loaded at all, just `import torch` plus a trivial `cuda` allocation, and
neither `AMD_DIRECT_DISPATCH=0` nor `HSA_ENABLE_INTERRUPT=1` help. Measured on
the machine this was diagnosed on, `LD_PRELOAD=/opt/rocm/lib/
libhsa-runtime64.so.1` -- the system's own, newer ROCr runtime, standing in
for torch's bundled copy -- drops idle CPU from ~92% of a core to exactly
zero jiffies, with no correctness regression across the handful of ops it was
checked against (see DECISIONS.md for the exact figures and their honest
limits).

**Why this module never imports torch, and never execs anything.** The
supervisor process's own venv deliberately has no torch installed -- it is
not an AI runtime, it launches one -- and a worker's venv is a separate
on-disk tree the supervisor only ever reasons about from the outside (spawn a
python, watch a status file). So this reads `torch/version.py` as a plain
text file and regexes it, the same way it reads `/opt/rocm/.info/version`;
importing either, or `subprocess`-probing a `python -c` one-liner, would work
too but would make every worker spawn pay for a second interpreter start just
to answer a yes/no question this module can answer from two small files.

**Why this is conservative rather than a blanket "ROCm means preload".** An
OLDER system runtime substituted in for a NEWER bundled one is not a
downgrade in the "-y is worse" sense a version bump usually implies -- it can
be missing symbols, kernel driver assumptions, or fixes the bundled build was
built against, and the failure mode is not "back to the CPU-pinning bug", it
is "the worker fails to start, or starts and misbehaves silently". Hence the
version guard: same ROCm major, system minor at or above torch's. See
`resolve_preload`'s docstring for the exact rule and the escape hatch.
"""
from __future__ import annotations

import glob
import os
import re
import sys

#: Explicit operator override, checked before any detection runs.
#: `0`/`off`/`false` (case-insensitive) disables the whole mechanism outright;
#: any other non-empty value is used VERBATIM as the `.so` path to preload
#: (still Linux-only, still must exist) -- for a system runtime this module's
#: version guard would refuse, or a path outside `$ROCM_PATH`/`/opt/rocm`
#: entirely. `FUSED_AI_HSA_PRELOAD=0` is also the documented recovery step
#: when a preloaded worker won't come up on a machine this module misjudged.
ENV_OVERRIDE = "FUSED_AI_HSA_PRELOAD"

_DISABLE_VALUES = {"0", "off", "false"}

#: Fallback system ROCm root when `$ROCM_PATH` is unset. A module-level
#: constant, not an inline literal, so a test can point it at a fake tree
#: instead of a dev machine's real `/opt/rocm`.
_DEFAULT_ROCM_ROOT = "/opt/rocm"

#: `__version__ = '2.13.0+rocm7.1'` -- torch's own ROCm build marker. Absent
#: entirely on a CUDA or CPU build, which is the common case this function
#: must leave untouched.
_TORCH_ROCM_VERSION_RE = re.compile(r"""__version__\s*=\s*['"][^'"]*\+rocm(\d+)\.(\d+)[^'"]*['"]""")

#: `hip = '7.1.52802'` (a ROCm build) vs. `hip: Optional[str] = None` (a CUDA
#: or CPU build) -- a second, independent signal so a `+rocmX.Y` string alone
#: (which nothing but this module's own test fixtures would ever fabricate)
#: doesn't count without torch's own runtime agreeing it's a ROCm build.
_TORCH_HIP_SET_RE = re.compile(r"""^hip\s*(?::[^=\n]+)?=\s*(?!None\b)['"][^'"]*['"]""", re.MULTILINE)

#: The system ROCm version file is a bare `X.Y.Z` line (`/opt/rocm/.info/version`).
_SYSTEM_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


def _venv_root(python: str) -> str:
    """`<venv>/bin/python` -> `<venv>`. Also accepts a venv root directly,
    since `<venv>/lib/python*/...` below simply won't match under a root
    that isn't one -- callers can pass whichever they have on hand."""
    parent = os.path.dirname(python)
    if os.path.basename(parent) in ("bin", "Scripts"):
        return os.path.dirname(parent)
    return python


def _torch_rocm_version(python: str) -> tuple[int, int] | None:
    """The worker venv's torch ROCm `(major, minor)`, or `None` for no torch,
    no ROCm build, or a `version.py` this regex can't parse -- all three read
    identically to a caller deciding whether to preload."""
    venv = _venv_root(python)
    candidates = glob.glob(os.path.join(venv, "lib", "python*", "site-packages", "torch", "version.py"))
    if not candidates:
        return None
    try:
        with open(candidates[0], encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    match = _TORCH_ROCM_VERSION_RE.search(text)
    if not match or not _TORCH_HIP_SET_RE.search(text):
        return None
    try:
        return int(match.group(1)), int(match.group(2))
    except ValueError:
        return None


def _system_rocm() -> tuple[tuple[int, int], str] | None:
    """The system ROCm root's `(major, minor)` and its `libhsa-runtime64.so.1`
    path, or `None` if the root doesn't look like a real ROCm install -- a
    missing/unreadable `.so`, a missing/unreadable/unparseable version file,
    or no root at all."""
    root = os.environ.get("ROCM_PATH") or _DEFAULT_ROCM_ROOT
    so_path = os.path.join(root, "lib", "libhsa-runtime64.so.1")
    resolved = os.path.realpath(so_path)
    if not os.path.isfile(resolved) or not os.access(resolved, os.R_OK):
        return None
    version_path = os.path.join(root, ".info", "version")
    try:
        with open(version_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    match = _SYSTEM_VERSION_RE.search(text)
    if not match:
        return None
    try:
        version = int(match.group(1)), int(match.group(2))
    except ValueError:
        return None
    return version, so_path


def _override() -> tuple[bool, str | None]:
    """`(handled, path)`. `handled` is True when `ENV_OVERRIDE` short-circuits
    detection entirely (set to anything, on or off); `path` is the preload to
    use in that case, or `None` for "disabled" or "invalid path given"."""
    raw = os.environ.get(ENV_OVERRIDE, "")
    if not raw:
        return False, None
    if raw.strip().lower() in _DISABLE_VALUES:
        return True, None
    resolved = os.path.realpath(raw)
    if os.path.isfile(resolved) and os.access(resolved, os.R_OK):
        return True, raw
    return True, None


def resolve_preload(python: str) -> str | None:
    """The `LD_PRELOAD` path to add for a worker launched on venv `python`,
    or `None` to leave `LD_PRELOAD` untouched.

    Linux only -- every other platform returns `None` unconditionally, before
    even the env override is consulted, since the bug this exists for is a
    Linux `/dev/kfd` ioctl loop and nothing here has been reasoned about
    anywhere else.

    With no `FUSED_AI_HSA_PRELOAD` override, the rule is: the venv's torch
    must be a ROCm build (`+rocmX.Y` in `__version__`, `hip` set -- see
    `_torch_rocm_version`), the system ROCm root (`$ROCM_PATH` or `/opt/rocm`)
    must have a readable `libhsa-runtime64.so.1` and a readable
    `.info/version`, and the system's `(major, minor)` must equal torch's on
    major and be `>=` it on minor. A CUDA build, a CPU build, no torch at all,
    a missing system runtime, a DIFFERENT major, or an OLDER system minor all
    answer `None` -- this is deliberately narrow: substituting an older
    system runtime for a newer bundled one trades a known CPU-pinning bug for
    an unknown one, so the guard only fires where the swap has actually been
    measured safe (same major, newer-or-equal system minor).
    """
    if sys.platform != "linux":
        return None

    handled, override_path = _override()
    if handled:
        return override_path

    torch_version = _torch_rocm_version(python)
    if torch_version is None:
        return None

    system = _system_rocm()
    if system is None:
        return None
    system_version, so_path = system

    if system_version[0] != torch_version[0]:
        return None
    if system_version < torch_version:
        return None
    return so_path
