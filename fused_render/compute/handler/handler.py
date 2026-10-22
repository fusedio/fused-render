# Derived from openfused (MIT, Copyright (c) 2026 Fused Labs, Inc.); see ../NOTICE.
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

_log = logging.getLogger("openfused.handler")


# --- Writable HOME for user code -------------------------------------------
# DuckDB — and many other tools (fontconfig, matplotlib's cache, huggingface_hub,
# anything using ~/.cache or ~/.config) — write into the home directory. DuckDB in
# particular stores autoloaded/autoinstalled extensions under ~/.duckdb/extensions
# and secrets under ~/.duckdb/stored_secrets, so a query that touches httpfs/spatial
# (e.g. reading from s3://) tries to create ~/.duckdb and fails when HOME is not
# writable. On AWS Lambda the whole root filesystem is read-only except /tmp, and
# HOME points at a read-only path (or is unset), so such a UDF errors with
# "Read-only file system" / "Permission denied" — with no way to fix it from user
# code. Redirect HOME (and the XDG cache/config/data dirs, which default to subdirs
# of HOME) to a writable directory so the UDF runs unchanged.
#
# Self-detecting: when HOME is already a writable directory (the local backend on a
# developer machine), this is a no-op, so the developer's real ~/.aws / ~/.config
# keep resolving. The redirected dir lives under the temp dir (…/openfused-home),
# is created on demand, and is shared for the container's lifetime so the DuckDB
# extension cache is reused across warm invocations (each per-tenant Lambda serves
# one tenant, so a container-lifetime home never crosses a tenant boundary).
#
# Two operator escape hatches override the autodetection, for when it guesses wrong
# or a read-only home is genuinely wanted:
#   * OPENFUSED_RO_HOME (truthy: 1/true/yes/on) — disable the redirect entirely and
#     leave HOME exactly as the environment provides it, even if read-only. Wins over
#     everything else.
#   * OPENFUSED_HOME=<path> — use this path as HOME (created if missing) instead of
#     the autodetected default, unconditionally (even when the current HOME is
#     already writable).
_WRITABLE_HOME = os.path.join(tempfile.gettempdir(), "openfused-home")

# Truthy env-flag values, matching the runner's bool param coercion.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


# --- Per-call working directory --------------------------------------------
# This used to be the literal `f"/tmp/exec/{uuid4()}"`. On Lambda that is right:
# `/tmp` is the function's own private scratch and this handler is the only thing
# in the container. But the LOCAL backend runs this same file verbatim on a
# developer's machine, where `/tmp` is MACHINE-WIDE — so the first account to make
# a call created `/tmp/exec` under its own umask (`drwxr-xr-x <that user>`) and
# every other account on the host then failed EVERY call with
#
#     PermissionError: [Errno 13] Permission denied: '/tmp/exec/<uuid>'
#
# raised here, in the child, so it came back as the call's error and read as a
# failure of the user's own code. (Reported downstream by fused-render: a page
# showed that traceback where it should have rendered, on a newly created macOS
# profile of a machine whose other account had run the app first.)
#
# The fix is to stop having a shared intermediate directory at all. `exec` was the
# only thing any account had to contend over: the TEMP DIR ITSELF needs no managing,
# because the OS already guarantees it is usable by every account — `1777` and
# sticky on Linux, per-user on macOS (`/var/folders/…/T`) and Windows. A `uuid4`
# directory created directly inside it is unguessable, so there is nothing to
# pre-create, nothing to widen, and nothing to refuse: no account can lock another
# out of a name it cannot predict, and none can plant a symlink at one either.
#
# So, resolution order, most specific first:
#
#   1. `event["call_dir_root"]` — the caller's own directory. Same shape and
#      purpose as `context_path` ("local mode"): a backend that already creates a
#      private per-call workspace hands it over, so there is one directory per call
#      instead of two and the teardown rides the caller's own context manager —
#      where a hard kill used to orphan `/tmp/exec/<uuid>` for good. `LocalPythonComputeBackend`
#      sets it to the workspace it writes `run.py`/`handler.py`/`event.json` into.
#   2. `{tempfile.gettempdir()}/openfused-exec-<uuid4>` — no intermediate directory.
#      On Lambda `TMPDIR` is unset, so this is `/tmp/openfused-exec-<uuid4>`: one
#      path component shallower than before, in the same `/tmp` it always used.
#
# The `openfused-exec-` prefix is the one thing the old `exec` parent bought that a
# bare uuid would lose. `shutil.rmtree` in `lambda_handler`'s `finally` reclaims the
# dir per call, so the only way to orphan one is a hard kill (SIGKILL, a Lambda
# timeout) — and on a shared `/tmp` an orphan named nothing but a uuid is
# unattributable, where `rm -rf $TMPDIR/openfused-exec-*` is not.
#
# A caller-supplied root is never modified. Its mode is the caller's business, and
# the caller here hands over a `0o700` `mkdtemp` holding `event.json` — which
# carries the user's code — plus the extracted input files; widening that would
# expose every call to every account on the host, which is the exact opposite of
# why the parameter exists. A directory's own mode, not its parent's, decides who
# can enter it: a parent being world-writable only means others may create
# SIBLINGS, so it says nothing about a private directory already inside it.
_CALL_DIR_PREFIX = "openfused-exec-"


def _make_private(path):
    """chmod `path` to owner-only, best effort (a failing chmod is not fatal here)."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _make_call_dir(event):
    """This invocation's unique working directory, created. See the block above."""
    root = event.get("call_dir_root")
    if root:
        # The caller's own directory: make sure it exists and touch NOTHING else —
        # in particular not its mode.
        os.makedirs(root, exist_ok=True)
        call_dir = os.path.join(root, str(uuid.uuid4()))
    else:
        call_dir = os.path.join(tempfile.gettempdir(), _CALL_DIR_PREFIX + str(uuid.uuid4()))
    # `mkdir`, not `makedirs(exist_ok=True)`: the parent always exists by here, and
    # on a fresh uuid name an "already there" is a surprise worth raising rather
    # than a state worth accepting.
    os.mkdir(call_dir, 0o700)
    # And a chmod, because `mkdir`'s mode is masked by the umask: under a shared
    # temp dir the usual 022 left `user_code.py`, the extracted inputs and
    # `_result.json` readable by every account on the host for the length of the
    # call. Nothing reads a call dir from another uid — the runner subprocess is
    # our own child — so there is nothing to trade for it.
    _make_private(call_dir)
    return call_dir


def _env_flag(value):
    return bool(value) and value.strip().lower() in _TRUTHY


def _home_is_writable(home):
    return bool(home) and os.path.isdir(home) and os.access(home, os.W_OK)


def _point_home_at(env, home):
    """Return a copy of `env` with HOME (and the XDG dirs that default to subdirs of
    it) rooted at `home`, creating `home` if missing. Never mutates the caller's dict."""
    os.makedirs(home, exist_ok=True)
    env = dict(env)
    env["HOME"] = home
    env.setdefault("XDG_CACHE_HOME", os.path.join(home, ".cache"))
    env.setdefault("XDG_CONFIG_HOME", os.path.join(home, ".config"))
    env.setdefault("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
    return env


def _with_writable_home(env):
    """Return `env` with HOME pointed at a writable dir when the current HOME is
    missing or read-only (the Lambda case); unchanged when HOME is already writable
    (the local backend). Never mutates the caller's dict. See the module note above
    for the OPENFUSED_RO_HOME / OPENFUSED_HOME operator overrides."""
    # Escape hatch: keep the environment's HOME untouched (even if read-only).
    if _env_flag(env.get("OPENFUSED_RO_HOME")):
        return env
    # Explicit override: use the operator-chosen home unconditionally.
    override = env.get("OPENFUSED_HOME")
    if override:
        return _point_home_at(env, override)
    # Autodetect: redirect only when the current HOME is missing or read-only.
    if _home_is_writable(env.get("HOME")):
        return env
    return _point_home_at(env, _WRITABLE_HOME)


# Absolute path to this file's directory — used to locate sibling handler files
# (_runner.py, _openfused.py, fused.py).
_HANDLER_DIR = os.path.dirname(os.path.abspath(__file__))


def _read_sibling(name):
    with open(os.path.join(_HANDLER_DIR, name), encoding="utf-8") as _f:
        return _f.read()


def _write_fused_mock(call_dir, work_dir):
    content = _read_sibling("fused.py")
    with open(os.path.join(call_dir, "fused.py"), "w", encoding="utf-8") as _f:
        _f.write(content)
    if work_dir != call_dir:
        with open(os.path.join(work_dir, "fused.py"), "w", encoding="utf-8") as _f:
            _f.write(content)


def _write_openfused_shim(call_dir, work_dir):
    content = _read_sibling("openfused.py")
    with open(os.path.join(call_dir, "openfused.py"), "w", encoding="utf-8") as _f:
        _f.write(content)
    if work_dir != call_dir:
        with open(os.path.join(work_dir, "openfused.py"), "w", encoding="utf-8") as _f:
            _f.write(content)


def _write_sdk(call_dir, work_dir):
    # Prepend the per-call value so it is defined before the module body runs.
    # _openfused.py leaves `_WORK_DIR` undefined in the static file.
    header = "_WORK_DIR = " + repr(work_dir) + chr(10)
    content = header + _read_sibling("_openfused.py")
    with open(os.path.join(call_dir, "_openfused.py"), "w", encoding="utf-8") as _f:
        _f.write(content)
    if work_dir != call_dir:
        with open(os.path.join(work_dir, "_openfused.py"), "w", encoding="utf-8") as _f:
            _f.write(content)


def lambda_handler(event, context):
    call_dir = _make_call_dir(event)
    try:
        # Input files the caller already wrote to disk, else the call dir itself.
        work_dir = event.get("context_path") or call_dir

        _write_sdk(call_dir, work_dir)
        _write_fused_mock(call_dir, work_dir)
        _write_openfused_shim(call_dir, work_dir)
        code = event.get("code", "")

        code_path = os.path.join(call_dir, "user_code.py")
        # Copy _runner.py into call_dir so the subprocess's sys.path[0] is call_dir
        # (not _HANDLER_DIR), ensuring user code imports the per-call _openfused.py
        # and fused.py written to work_dir rather than the raw files in _HANDLER_DIR.
        runner_path = os.path.join(call_dir, "_runner.py")
        shutil.copy(os.path.join(_HANDLER_DIR, "_runner.py"), runner_path)
        out_path = os.path.join(call_dir, "_result.json")

        with open(code_path, "w", encoding="utf-8") as _f:
            _f.write(code)

        # Upper bound for the user code's own process; the host backend's call
        # timeout (`timeout_seconds`) is far shorter and fires first.
        child_timeout = 290

        # Export the execution bundle root so the openfused shim's project_root()/
        # asset_path() resolve against the dir user code runs in (sdk-openfused.md).
        # On the local backend the per-call root (project dir) is already in the
        # environment via _child_env; only fall back to work_dir when unset (the
        # plain AWS/deployed-serve path), so a project's on-disk root is preserved.
        runner_env = dict(os.environ)
        runner_env["OPENFUSED_PROJECT_ROOT"] = runner_env.get("OPENFUSED_PROJECT_ROOT") or work_dir
        # Point HOME at a writable dir so ~/.duckdb (extensions/secrets) and other
        # home-rooted caches work on Lambda's read-only fs (no-op locally).
        runner_env = _with_writable_home(runner_env)

        # Optional explicit entrypoint (runtime-lambda.md § Result logic rule 1):
        # threaded to the runner as argv[3]. Meaningless in test mode (tests
        # import user_code and pick functions themselves), which returned above.
        runner_cmd = [sys.executable, runner_path, code_path, out_path]
        entrypoint = event.get("entrypoint")
        if entrypoint:
            runner_cmd.append(str(entrypoint))

        exec_t0 = time.monotonic()
        # `close_fds` stays at its default True: this is the boundary where USER
        # code starts, and dropping inherited descriptors here is what keeps them
        # out of it on BOTH backends that share this handler — notably the local
        # one, whose parent spawns this process with close_fds=False on purpose
        # (posix_spawn / PROJ atfork; see compute/backend.py), so
        # host descriptors really are open in this process. Do not set
        # close_fds=False here.
        try:
            proc = subprocess.run(
                runner_cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=child_timeout,
                env=runner_env,
            )
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": f"Execution timed out after {child_timeout}s",
                "return_value": None,
                "error": f"TimeoutError: code execution exceeded {child_timeout}s",
            }

        # On POSIX, subprocess returns -N when the child is killed by signal N.
        # SIGKILL = -9 from Python's subprocess; 137 (128+9) is the shell convention
        # and does not apply here. Check both defensively.
        if proc.returncode in (-9, 137):
            return {
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "Process was killed",
                "return_value": None,
                "error": f"OOMError: child process was killed (exit code {proc.returncode})",
            }

        if os.path.exists(out_path):
            with open(out_path) as _f:
                _res = json.load(_f)
            _res["duration_ms"] = int((time.monotonic() - exec_t0) * 1000)
            return _res

        # Runner exited before writing a result (crash or import error)
        return {
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or f"Runner exited with code {proc.returncode}",
            "return_value": None,
            "error": (
                f"RuntimeError: runner process exited with code {proc.returncode}"
                " without writing a result"
            ),
        }
    finally:
        shutil.rmtree(call_dir, ignore_errors=True)
