# Derived from openfused (MIT, Copyright (c) 2026 Fused Labs, Inc.); see NOTICE.
"""Host-side broker for local fan-out (`fused.load` / `udf.map`).

A local compute call cannot spawn siblings itself, so child invocations are
brokered through the host. The executing code writes a request (code + input
files, base64) into a shared directory, passed by host path to each compute
subprocess, and this watcher picks it up and runs the child through the same
`LocalPythonComputeBackend`: a fresh subprocess per child. The result is written
back into the same directory for the waiting child to read.

Because every child gets the same dispatcher directory, recursion (the
coordinator/worker pattern) works. Three guards bound runaway fan-out:

- a depth cap (enforced child-side in `_openfused.invoke`) stops infinite
  recursive fan-out;
- a concurrency semaphore here bounds how many children run at once
  (default 8, override with `OPENFUSED_LOCAL_INVOKE_MAX_CONCURRENCY`);
- a slot-acquire timeout turns the recursive-fan-out deadlock into a clean,
  fast error. A child runs inside `_handle` while holding a slot, so a parent
  waiting on its own children keeps its slot the whole time. A tree fan-out
  whose simultaneously-live nodes exceed the cap therefore deadlocks:
  descendants can never get a slot their ancestors won't release. Rather than
  let those children run a doomed child to its per-call timeout (stacking one
  timeout per level), `_handle` waits at most `acquire_timeout` for a slot and,
  failing that, writes an `error` result explaining the limit. Benign wide
  fan-out (many leaf workers, no recursion) still just serializes through the
  semaphore: those workers finish and release, so they acquire well within the
  timeout.

Wire protocol (JSON files; writes are atomic via `.tmp` + `os.replace`):

- request `requests/<id>.json`:
  `{"id", "code", "depth", "files": {name: b64}[, "interpreter"][, "async"]}`
- result  `results/<id>.json`:  `{"stdout", "stderr", "return_value", "error", "response"}`
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import tempfile
import threading
import weakref
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fused_render.compute.backend import LocalPythonComputeBackend

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CONCURRENCY = 8


def _rmtree_quiet(path: Path) -> None:
    """Best-effort recursive remove; never raises (used by the dispatcher finalizer).

    Deliberately a module-level function, NOT a lambda or method closing over the
    dispatcher: `weakref.finalize` holds a strong reference to its callable and
    its args, so a callable capturing `self` would keep the dispatcher alive and
    defeat GC-driven cleanup. Keep it free of any reference to the dispatcher.
    """
    shutil.rmtree(path, ignore_errors=True)


class LocalInvokeDispatcher:
    """Runs child invocations for the local compute backend."""

    def __init__(
        self,
        compute: LocalPythonComputeBackend,
        *,
        max_concurrency: int | None = None,
        poll_interval: float = 0.05,
        acquire_timeout: float | None = None,
    ) -> None:
        self._compute = compute
        self._poll_interval = poll_interval
        self._root = Path(tempfile.mkdtemp(prefix="openfused-invoke-"))
        self._requests = self._root / "requests"
        self._results = self._root / "results"
        self._requests.mkdir()
        self._results.mkdir()
        # Remove the scratch dir when this dispatcher is GC'd or at interpreter
        # exit, even if stop() is never called (the common case). Without this,
        # every process leaks its mkdtemp dir; on a tmpfs /tmp with a per-user
        # quota they accumulate until writes fail with EDQUOT (Errno 122), the
        # disk-quota error that breaks every call. Once ensure_started() runs,
        # the daemon watcher thread holds a reference to this dispatcher, so GC
        # won't reclaim it mid-process, but weakref.finalize still fires at
        # interpreter exit. stop() releases it eagerly.
        self._finalizer = weakref.finalize(self, _rmtree_quiet, self._root)
        if max_concurrency is None:
            max_concurrency = int(
                os.environ.get("OPENFUSED_LOCAL_INVOKE_MAX_CONCURRENCY") or _DEFAULT_MAX_CONCURRENCY
            )
        self._max_concurrency = max_concurrency
        self._sem = threading.Semaphore(max_concurrency)
        # How long a child may wait for a concurrency slot before the fan-out is
        # declared deadlocked and the child failed cleanly. Defaults to the
        # compute call timeout: if a child cannot get a slot within the time a
        # child is even allowed to run, the pool is exhausted by waiting parents
        # (a real deadlock), not merely busy.
        if acquire_timeout is None:
            try:
                acquire_timeout = float(getattr(compute, "_timeout_seconds", 300))
            except (TypeError, ValueError):
                acquire_timeout = 300.0
        self._acquire_timeout = acquire_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._seen: set[str] = set()

    @property
    def host_dir(self) -> str:
        """The dispatcher directory passed to compute subprocesses."""
        return str(self._root)

    def ensure_started(self) -> None:
        """Start the watcher thread once (idempotent)."""
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._watch, name="openfused-invoke-dispatcher", daemon=True
                )
                self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Tear down the scratch dir now (idempotent: a no-op if the finalizer
        # already ran at GC/exit).
        self._finalizer()

    # -- internals ----------------------------------------------------------

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                pending = sorted(self._requests.glob("*.json"))
            except OSError as exc:  # the dir was torn down; stop quietly
                logger.debug("invoke watcher stopping: %s", exc)
                return
            for req_file in pending:
                if req_file.name in self._seen:
                    continue
                self._seen.add(req_file.name)
                threading.Thread(target=self._handle, args=(req_file,), daemon=True).start()
            self._stop.wait(self._poll_interval)

    def _handle(self, req_file: Path) -> None:
        # The parent polls results/<id>.json where <id> is the request filename
        # stem, so derive the result path from the filename, never the body, and
        # ALWAYS write a result. An unparseable or malformed request comes back
        # as an `error` rather than letting the parent poll until
        # OPENFUSED_LOCAL_INVOKE_TIMEOUT.
        result_path = self._results / f"{req_file.stem}.json"
        try:
            req = json.loads(req_file.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("Invalid invoke request %s: %s", req_file, exc)
            payload = {
                "stdout": "",
                "stderr": str(exc),
                "return_value": None,
                "error": f"invalid invoke request: {exc}",
                "response": None,
            }
        else:
            if not isinstance(req, dict) or "code" not in req:
                err = "request missing 'code'"
                logger.warning("Invalid invoke request %s: %s", req_file, err)
                payload = {
                    "stdout": "",
                    "stderr": err,
                    "return_value": None,
                    "error": f"invalid invoke request: {err}",
                    "response": None,
                }
            else:
                payload = self._run_child_bounded(req, req_file.name)
        self._write_atomic(result_path, json.dumps(payload))
        try:
            req_file.unlink()
        except OSError:
            pass

    def _run_child_bounded(self, req: dict, req_name: str) -> dict:
        """Acquire a concurrency slot (bounded wait), run the child, release.

        If no slot frees up within `acquire_timeout` the pool is exhausted by
        parents waiting on their own children (recursive/tree-fan-out deadlock):
        fail this child cleanly with an explanatory error instead of running a
        doomed child or hanging until the parent's poll timeout.
        """
        if self._sem.acquire(timeout=self._acquire_timeout):
            try:
                return self._run_child(req)
            finally:
                self._sem.release()
        msg = (
            f"local fan-out concurrency limit ({self._max_concurrency}) could not "
            f"be acquired within {self._acquire_timeout:g}s: a recursive or tree "
            "fan-out exceeded capacity (each waiting parent holds a slot). Reduce "
            "fan-out width/depth, or raise OPENFUSED_LOCAL_INVOKE_MAX_CONCURRENCY."
        )
        logger.warning("%s (request %s)", msg, req_name)
        return {
            "stdout": "",
            "stderr": msg,
            "return_value": None,
            "error": msg,
            "response": None,
        }

    def _run_child(self, req: dict) -> dict:
        """Run one child execution and return the wire result dict.

        This is total: it never raises. Every failure is reported back as the
        child's `error` so the waiting parent fails cleanly instead of polling
        until timeout: a malformed request (bad base64 in `files`, a non-integer
        `depth`, a missing `code`) as well as a child run failure (build/IO).
        `binascii.Error` (invalid base64) is a `ValueError` subclass, so it is
        covered below.
        """
        try:
            files = {name: base64.b64decode(b64) for name, b64 in (req.get("files") or {}).items()}
            # Children inherit the parent's interpreter so fan-out workers see
            # the same dependencies as the coordinator.
            interpreter = req.get("interpreter")
            if interpreter is not None and not isinstance(interpreter, str):
                raise ValueError("request 'interpreter' must be a string")
            result = self._compute._execute_sync(
                req["code"],
                input_files=files or None,
                invoke_depth=int(req.get("depth", 1)),
                interpreter=interpreter,
            )
        except (RuntimeError, OSError, ValueError, TypeError, KeyError) as exc:
            return {
                "stdout": "",
                "stderr": str(exc),
                "return_value": None,
                "error": str(exc) or exc.__class__.__name__,
                "response": None,
            }
        # Surface the child's failure as `error` so _openfused.invoke raises on
        # the parent side. Prefer the handler's explicit error; fall back to
        # stderr on a null result.
        error = result.error or (
            result.stderr if (result.return_value is None and result.stderr) else None
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_value": result.return_value,
            "error": error,
            "response": result.response.to_wire() if result.response else None,
        }

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
