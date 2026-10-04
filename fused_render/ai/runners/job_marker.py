"""One constant, in its own module, for one reason: `supervisor.py` needs to
read `JOB_ERROR_MARKER` and used to get it by `from fused_render.ai.runners
import worker_base` — importing that module runs its OWN module-scope
`_GENERATE_TASKS = _start_generate_thread()`, starting a permanent daemon
thread in the SUPERVISOR process merely to read one string constant.

This repo has hit real fork-after-thread SIGSEGV crashes before (see
MEMORY.md's PROJ-atfork entries) — a background thread alive in a process
that later forks is exactly that risk, and the supervisor is a process that
spawns children. `supervisor.py` never used anything else from `worker_base`
(confirmed: its only use was this one constant, at the stderr-tail read in
`_download_failure_text`), so the fix is to define the marker here, in a
module with no side effects at import time, and have BOTH `worker_base.py`
and `supervisor.py` import it from here instead of one depending on the
other's module-scope work.

Follow-up review finding 3.
"""
from __future__ import annotations

#: Item 6 of the architecture-detection brief (D1287+). `worker_base.serve`'s
#: `--download-only` except-branch prints the FULL traceback to stderr, for
#: whoever reads the raw log file — but `supervisor._fetch_only`/
#: `_download_failure_text` tails that SAME stream for the sentence it puts
#: on the job row, and before this marker existed it got the whole blob,
#: traceback included: a runner's own deliberately-written
#: `RuntimeError("...")` reached the row buried behind a wall of Python
#: frames instead of as the sentence it was written to be. This NUL-wrapped
#: marker (never legitimate text a traceback or an exception's own `str()`
#: would contain) prefixes the one line meant for machine consumption,
#: written last — `_download_failure_text` takes only what follows its final
#: occurrence, and falls back to the old whole-tail behaviour when a stderr
#: blob has no marker at all (a process killed by a signal, for instance,
#: never reaches this except branch to write one).
JOB_ERROR_MARKER = "\x00fused-render-job-error\x00"
