"""Live CPU and memory of the processes fused-render itself runs, and on
request of every process on the machine.

`backend` is the platform module (macos.py, linux.py) or None where neither
applies, in which case `SUPPORTED` is False and the sampler reports nothing.
Every backend exposes the same functions: proc_sample, parent_pid, is_zombie,
descendants, proc_args, host_memory, host_cpu_ticks — and, for the whole-
machine Monitor page, list_procs/list_pids/proc_ident, which read EVERY
user's processes (only the current user's get CPU and memory figures).
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

backend = None
try:
    if sys.platform == "darwin":
        from fused_render.sysmon import macos as backend  # noqa: F811
    elif sys.platform.startswith("linux"):
        from fused_render.sysmon import linux as backend  # noqa: F811
except Exception:  # noqa: BLE001 — no metrics is better than no server
    logger.debug("sysmon backend failed to load", exc_info=True)
    backend = None

SUPPORTED = backend is not None
