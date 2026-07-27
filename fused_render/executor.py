"""Runs a Python file's main() and returns its JSON result.

Two execution paths (D72):

- **User code** — a script the user is running (the `api` template's Run
  button) or a user-authored template's reader — runs in a **fresh isolated
  subprocess** per call (SPEC PY-6, D5): always-fresh code, no stale state, and
  a crash or `sys.exit` can't take down the server.
- **An allowlist of first-party helpers** (`INPROCESS_HELPERS` — the duckdb/
  table/csv/xlsx/sqlite readers and the `api` inspector) run **in-process**. They are trusted
  and, crucially, none of them import or execute user code (the readers open a
  data file; the inspector `ast`-parses a .py without importing it) and each is
  fast and bounded. Running them in the server (= app) process means the
  Downloads/Desktop/Documents access they perform is attributed to the app the
  user already granted, instead of to a freshly-spawned interpreter that macOS
  TCC re-prompts for on *every* call. That repeated prompting — one per
  preview/pagination/slider tick on a file under a protected folder — was the
  bug this split fixes; it also drops the per-call pandas/pyarrow re-import
  cost, since those stay warm in the server. Other shipped helpers under
  `templates/` (the claude/ chat agent, the geo tile servers/browsers, …) are
  deliberately NOT allowlisted — they can be slow or long-running, so they take
  the subprocess path and keep its timeout + isolation.
"""
import importlib.util
import json
import logging
import os
import subprocess
import sys
import time
import traceback

from ._binding import bind_params
from .core_templates import ensure_core_templates

logger = logging.getLogger(__name__)

CHILD = os.path.join(os.path.dirname(__file__), "_child.py")
# Built-in helpers run from the staged core-templates copy, not the bundle, so
# the allowlist realpaths must point there too (see core_templates).
_TEMPLATES_DIR = os.path.realpath(ensure_core_templates())
# 60s (not 30): a cold overview read of a large remote COG over the mount's HTTP
# serve legitimately takes ~30-40s on first open (the pyramid analyze; the
# template's own worker already allows 900s). 30s killed those mid-read. Still
# well short of a runaway-guard — a genuinely hung script is caught, just later.
DEFAULT_TIMEOUT = 60.0

# Explicit allowlist of first-party helpers that run IN-PROCESS (D72): each is
# bounded, self-contained, and never imports or executes user code — the
# data-page readers, the two grid writers, plus the api inspector (which only
# `ast`-parses). Realpaths, so a symlink can't smuggle another path in.
# Everything else under templates/ (the claude/ chat agent, the geo/h3/las/
# vector/zarr browsers + tile servers, converters, …) is NOT here and runs on
# the subprocess path, keeping its 30 s timeout and process isolation — critical
# for the slow/long-running ones. This is an allowlist, not a "path under
# templates/" check, precisely so that a new shipped helper defaults to the safe
# subprocess path; add a helper here only after confirming it is bounded and
# free of user-code execution.
#
# The duckdb/sqlite *writers* DO mutate — they rewrite the file/table being
# viewed. That's why they're in-process too: like the readers, the writes land
# under the protected folder (Downloads/Desktop/Documents) the user already
# granted the app, so a save doesn't trigger a fresh per-call macOS TCC prompt
# the way a spawned interpreter would. They stay first-party and touch only the
# single file passed in; a batch is applied atomically (temp+os.replace for
# flat files, one transaction for SQLite) so a failure leaves the file intact.
_BUNDLED_TEMPLATES_DIR = os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))

INPROCESS_HELPERS = frozenset(
    os.path.realpath(os.path.join(base, *parts))
    # BOTH the staged copy (what normally runs) and the bundled original. They
    # are the same first-party file, and listing only the staged one meant a run
    # served from the package directory fell to the subprocess path silently —
    # a per-poll spawn for the readers, and outright failure for a helper that
    # imports the package. A user FORK under ~/.fused-render/templates/ is
    # deliberately not here: once the user can edit it, it is user code and
    # keeps the subprocess timeout and isolation.
    for base in (_TEMPLATES_DIR, _BUNDLED_TEMPLATES_DIR)
    for parts in (
        ("duckdb", "reader.py"),
        ("duckdb", "writer.py"),
        ("structure", "reader.py"),
        ("csv", "reader.py"),
        ("xlsx", "reader.py"),
        ("sqlite", "reader.py"),
        ("sqlite", "writer.py"),
        ("api", "inspector.py"),
        # The call-log reader: first-party, never imports or executes user code
        # (it parses JSONL the server itself wrote), and bounded — it reads at
        # most a page of records or one pre-aggregated pass. In-process because
        # the calls view POLLS while following, and ~700 ms of subprocess spawn
        # per poll is the difference between a live tail and a slideshow.
        ("calls", "reader.py"),
    )
)


# Exactly starlette's JSONResponse.render kwargs — `dumps_result` below IS the
# response encoding for the in-process path, so its bytes must match what
# JSONResponse would have produced down to separators/escaping.
_JSON_KW = {"ensure_ascii": False, "allow_nan": False, "separators": (",", ":")}


class _PreEncodedRun(dict):
    """A run result whose `result` payload is already serialized (D72).

    The in-process path has to serialize main()'s return value anyway, to tell
    a non-JSON-serializable result apart with a useful message. A parquet
    structure dump is many MB, so serializing it again on the way out of
    /api/run doubled the cost of every request. This carries that one encoded
    string alongside the live payload: dict consumers still read
    `result["result"]` as a real object, and `dumps_result` splices the string
    straight into the response body instead of re-encoding it.
    """

    __slots__ = ("payload_json",)

    def __init__(self, mapping: dict, payload_json: str):
        super().__init__(mapping)
        self.payload_json = payload_json


def dumps_result(result: dict) -> str:
    """Render a run result as the /api/run response body.

    Byte-identical to `json.dumps(result, **_JSON_KW)` (a flat object is just
    "key":value joined by commas), except that a `_PreEncodedRun`'s payload is
    reused verbatim rather than serialized a second time.
    """
    payload_json = getattr(result, "payload_json", None)
    if payload_json is None:
        return json.dumps(result, **_JSON_KW)
    return "{%s}" % ",".join(
        "%s:%s" % (
            json.dumps(key, **_JSON_KW),
            payload_json if key == "result" else json.dumps(value, **_JSON_KW),
        )
        for key, value in result.items()
    )


def _error(err_type: str, message: str, detail: str = "") -> dict:
    return {
        "ok": False,
        "error": {"type": err_type, "message": message, "traceback": detail},
        "stdout": "",
    }


def _child_env() -> dict:
    """The worker's environment, with THIS package's location on PYTHONPATH.

    The worker is spawned as a script, so its `sys.path[0]` is the package
    directory rather than its parent, and `import fused_render` resolves there
    only when the package happens to be pip-installed into `sys.executable`.
    A first-party helper that delegates to the package (the call-log reader
    reads the store through `fused_render.calls`) otherwise fails with
    *No module named 'fused_render'* — reported from the Calls view, while
    log_studio's stdlib-only reader was unaffected.

    Handing the path down from the PARENT rather than deriving it in the child
    is the load-bearing part: this process IS the package, so it knows where the
    package is even when the child's own `__file__` arithmetic cannot say (a
    frozen or relocated layout), and it applies to a worker script that has not
    itself been updated. `_child.py` keeps its own fallback for direct
    invocation, but this is the path that has to be right.

    Prepended, so the child imports the same code the server is running; the
    user's own module directory still wins, since `run()` puts it at sys.path[0].
    """
    parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    existing = os.environ.get("PYTHONPATH") or ""
    return {**os.environ,
            "PYTHONPATH": parent + (os.pathsep + existing if existing else "")}


def _is_builtin_helper(path: str) -> bool:
    """True only for the allowlisted in-process helpers (D72): the duckdb/structure/
    csv/xlsx/sqlite readers and the api inspector. Exact realpath membership — every other
    script (user code, and other shipped helpers like the claude agent or the
    geo tile servers/browsers) stays on the subprocess path with its timeout and
    isolation.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return real in INPROCESS_HELPERS


def _run_inprocess(path: str, params: dict) -> dict:
    """Execute a first-party helper's main() in this process. Same result shape
    and param binding as the subprocess path; catches BaseException so a helper
    error (or a stray SystemExit) surfaces as a normal error dict instead of
    tearing down the server thread. No timeout: these are bounded local-file
    reads / ast parses, not arbitrary user code.

    Thread-safe under FastAPI's threadpool (RH-4): it mutates no process-global
    state. The helper module is built with `module_from_spec` + `exec_module`
    and is *never* inserted into `sys.modules`, so the fixed spec name is inert
    and concurrent calls get independent module objects. `sys.path` is left
    untouched — built-in helpers are self-contained (stdlib + the data stack,
    never a sibling imported by name), so there is nothing to add, and mutating
    the shared path would race concurrent imports. stdout is likewise NOT
    captured: helpers don't print, and redirecting the process-global
    `sys.stdout` would race concurrent calls.
    """
    spec = importlib.util.spec_from_file_location("__fused_builtin__", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        fn = getattr(mod, "main", None)
        if not callable(fn):
            raise AttributeError(
                f"{os.path.basename(path)} does not define a callable 'main' function"
            )
        result = fn(**bind_params(fn, params))
        # Serialize once: this pass both validates the payload (so a bad return
        # gets the message below instead of an opaque 500 from the response
        # encoder) and *is* the bytes /api/run sends, via dumps_result. A
        # multi-MB structure dump used to be encoded twice per request.
        try:
            payload_json = json.dumps(result, **_JSON_KW)
        except (TypeError, ValueError):
            raise TypeError(
                f"main() returned {type(result).__name__}, which is not JSON-serializable; "
                "return dict/list/str/number/bool/None (e.g. df.to_dict('records'))"
            ) from None
        # Union of two sides of a merge: #290's pre-encoded payload (encode a
        # multi-MB result once, not twice) AND the explicit empty `stderr` the
        # call log's records rely on (in-process helpers capture no stderr, and
        # the record contract wants the field present-and-empty, not absent).
        return _PreEncodedRun(
            {"ok": True, "result": result, "stdout": "", "stderr": ""}, payload_json)
    except BaseException as e:  # noqa: BLE001 — mirror the child's catch-all
        return {
            "ok": False,
            "error": {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
            },
            "stdout": "",
            "stderr": "",
        }


def run_python(path: str, params: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    # Time the run here so this engine reports `duration_ms` like the fused one
    # (engine.py) does. Without it the call log's `run_ms` is mysteriously
    # empty for the DEFAULT engine, which reads as a bug rather than a gap —
    # and the two engines' numbers stop being comparable.
    started = time.monotonic()
    result = _run_python(path, params, timeout)
    result.setdefault("duration_ms", round((time.monotonic() - started) * 1000))
    if not result.get("ok"):
        # A failed run is the common "something wrong with right-click open"
        # symptom, and the browser only flashes it in an error overlay. Record
        # it here — with the worker's traceback in `detail` — so the log file
        # explains a failure the user has since clicked away from.
        err = result.get("error") or {}
        logger.warning(
            "run failed for %s: %s: %s\n%s",
            path,
            err.get("type", "Error"),
            err.get("message", ""),
            err.get("traceback", ""),
        )
    return result


def _run_python(path: str, params: dict, timeout: float) -> dict:
    if not os.path.isfile(path):
        return _error("FileNotFoundError", f"no such Python file: {path}")

    # First-party helper -> in-process so its protected-folder access reuses
    # the app's TCC grant (D72). Everything else is user code -> subprocess.
    if _is_builtin_helper(path):
        return _run_inprocess(path, params or {})

    request = json.dumps({"path": path, "params": params or {}})
    try:
        proc = subprocess.run(
            [sys.executable, CHILD],
            input=request,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_env(),
            # close_fds=False forces CPython to spawn via posix_spawn instead of
            # fork()+exec (verified: the default close_fds=True takes the fork
            # path on macOS/Linux). This is a native-crash fix, not an fd-policy
            # choice. The server process has libproj resident with a live SQLite
            # handle to proj.db in PROJ's SQLiteHandleCache (pyproj is pulled in
            # transitively — e.g. via `fused`/geopandas). fork() runs every
            # registered pthread_atfork *child* handler in the forked child
            # before exec; PROJ's handler calls sqlite3_close/VFSClose on that
            # inherited-but-now-invalid handle and segfaults (SIGSEGV) — so the
            # child dies with code -11 before it can exec the worker, and the
            # run surfaces as "worker exited with code -11 without producing a
            # result". posix_spawn does NOT run atfork handlers, eliminating the
            # crash path entirely. The worker is short-lived and inherits only
            # the pipes it needs, so not closing inherited fds is harmless here.
            close_fds=False,
            # a windowless server (Explorer-opener spawn) must not flash a
            # console window per run
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        return _error("TimeoutError", f"execution exceeded {timeout:g}s and was killed")
    except OSError as e:
        # Couldn't even spawn the worker (bad interpreter path, out of fds, …).
        # Return the normal wire shape rather than letting it 500 unlabeled.
        return _error("ExecutorError", f"could not start worker process: {e}")

    lines = proc.stdout.strip().splitlines()
    if lines:
        try:
            parsed = json.loads(lines[-1])
        except json.JSONDecodeError:
            pass
        else:
            # The worker's own stderr, which _child.py never captures (it
            # redirects only stdout, to keep the result protocol clean) — so a
            # warning or a C-library message printed by a run is otherwise
            # lost. Tail, not head: the end is where the failure is.
            if isinstance(parsed, dict) and proc.stderr:
                parsed.setdefault("stderr", proc.stderr[-4000:])
            return parsed
    return _error(
        "ExecutorError",
        f"worker exited with code {proc.returncode} without producing a result",
        proc.stderr[-4000:],
    )
