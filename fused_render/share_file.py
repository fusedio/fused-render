"""Share any file whose extension resolves to a Fused catalog file-preview
UDF, generalising `share_app.py`'s app-only pipeline.

Reuses share_app.py's machinery wholesale rather than duplicating it: the
same record store (`shared_apps.json`, keys namespaced `file:<id>` against
the app shares' bare ids — one file, one writer, one lock), the same
`_app_lock` (keyed on path, already generic), the same `_run_shim`/
`_shim_command` plumbing that spawns `_fused_share_app.py`, the same
`_require_fused`/`_logged_in`/`_not_signed_in` sign-in guards.

Identity is `<slug>_<h6>`: `slug` is the slugified filename stem, `h6` is
`sha1(abs path)[:6]`, so re-sharing the same path updates one canvas and two
same-named files in different folders get different canvases.

Viewer resolution (`share_file_rules.py`) needs the real SDK, which the
server process does not have — `resolve_viewer` reads whatever rule table is
already cached on disk (warm in the common case: `publish` always leaves a
fresh one behind) and falls back to the built-in rules alone when there is
none yet, rather than blocking a status call on a subprocess spawn.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render import share_app
from fused_render import share_file_rules
from fused_render.fusedcli import child_env, fused_cli, workbench_env

logger = logging.getLogger(__name__)

router = APIRouter()

PUBLISH_TIMEOUT = share_app.PUBLISH_TIMEOUT
LOOKUP_TIMEOUT = share_app.LOOKUP_TIMEOUT
REMOVE_TIMEOUT = share_app.REMOVE_TIMEOUT
RULES_TIMEOUT = 45.0

# A file at or under this size is published inline, blocking, the same way
# an app export is — one round trip, no polling to wire up. Above it, the
# caller must run /upload first and hand publish the finished job's id
# (§4's detached transfer): PUBLISH_TIMEOUT is generous for five control-
# plane calls but not for streaming an arbitrarily large file.
INLINE_PUBLISH_MAX_BYTES = 8 * 1024 * 1024

UPLOADS_SUBDIR = "share_uploads"
# Nothing is embedded or read into memory for this route — the CLI streams
# straight to S3 — so the cap exists to keep one reader's giant file from
# tying up a share_uploads/ slot forever, not to protect memory.
MAX_UPLOAD_BYTES = 1 * 1024 * 1024 * 1024

_error = share_app._error
_not_signed_in = share_app._not_signed_in
_require_fused = share_app._require_fused
_logged_in = share_app._logged_in
_creds_stamp = share_app._creds_stamp
get_record = share_app.get_record
put_record = share_app.put_record
drop_record = share_app.drop_record


# -- identity --------------------------------------------------------------


def file_identity(path: str) -> str:
    """`<slug>_<h6>` — stable for a given absolute path, distinct for two
    same-named files in different folders."""
    stem = os.path.splitext(os.path.basename(path))[0]
    slug = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_").lower()[:40] or "file"
    h6 = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:6]
    return f"{slug}_{h6}"


# file_identity() itself only ever mints `[a-z0-9_]{1,40}_[0-9a-f]{6}` (the
# slug, lower-cased and truncated, plus 6 hex chars) — no "-", no ".", no
# "/". Hyphens are allowed here too even though file_identity() never
# produces one, since nothing about them can escape share_uploads/ and other
# id-minting call sites in this codebase (and its tests) use them freely.
# What must never pass is anything that can steer
# os.path.join(_uploads_root(), upload_id) outside share_uploads/ — no "..",
# no "/", no "." — so a caller-supplied upload/share id is rejected outright
# unless it is built only from that safe alphabet (finding 3: cancel_upload
# SIGTERMs a process group named by that id, and the same traversal reaches
# /upload/status?id=).
_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _valid_upload_id(upload_id: str) -> bool:
    return bool(upload_id) and bool(_UPLOAD_ID_RE.match(upload_id))


def _record_key(file_id: str) -> str:
    return f"file:{file_id}"


def _record_for(file_id: str) -> dict | None:
    return get_record(_record_key(file_id))


# -- viewer resolution -------------------------------------------------------


#: Set once this process has a background rebuild ACTUALLY UNDER WAY with
#: credentials — so a lean server's first few reads don't each spawn their
#: own shim subprocess while the first one is still running.
#: `_startup_warm_share_rules` sets it too (through `_kick_warm_once`
#: below), so the two never race: whichever gets there first wins, and the
#: other becomes a plain disk read. NOT a bare one-shot, though: a caller
#: that finds nobody signed in never sets this (nothing was kicked, so
#: nothing should be "used up"), and a kick that starts but fails clears it
#: again — see `_kick_warm_once`/`_run_warm_and_untrack` — so sign-in later
#: in the same process, or a transient failure, both still get retried
#: rather than being stuck for the rest of this process's life.
_warm_kicked = False
_warm_kicked_lock = threading.Lock()

#: The earliest a THIRD, failed/no-op attempt may retry after a prior one —
#: bounds how often a signed-out (or persistently failing) process re-spawns
#: a thread on every empty-cache read, e.g. a share sheet left open and
#: polling status. Not applied to the very first attempt, and not needed on
#: the signed-out path at all (that path never sets `_warm_kicked`, so it
#: would retry every read anyway) — it only matters once a kick has actually
#: started and lost its flag back to `_run_warm_and_untrack` on failure.
_WARM_RETRY_BACKOFF_S = 5.0
_warm_last_attempt = 0.0


def _kick_warm_once(*, name: str = "fused-share-file-rules-warm") -> threading.Thread | None:
    """Fire `warm_rules_cache` on a background thread, at most once per
    process while it can plausibly succeed — the thread, or `None` if
    nothing was kicked this time (already running, backed off, or nobody is
    signed in to build anything from). Non-blocking: a caller that read an
    empty/missing cache still gets the built-in rules for THIS read, and a
    later read (this request's retry, or the next open of the share sheet)
    sees the real table once the thread finishes.

    Checking `_logged_in()` here — before touching `_warm_kicked` — is what
    makes retry work: a signed-out reader's call never sets the flag, so it
    costs nothing but a cheap file-existence check on every such read, and
    the FIRST read after sign-in still finds `_warm_kicked` false and kicks
    a real attempt. Bugbot correctly flagged the earlier version, which set
    the flag unconditionally: a signed-out reader's very first empty-cache
    read spent the one-shot guard on a call that immediately no-opped
    inside `warm_rules_cache`, so no read for the rest of the process's
    life — including ones long after sign-in — ever tried again."""
    if not _logged_in():
        return None
    global _warm_kicked, _warm_last_attempt
    now = time.monotonic()
    with _warm_kicked_lock:
        if _warm_kicked:
            return None
        if now - _warm_last_attempt < _WARM_RETRY_BACKOFF_S:
            return None
        _warm_kicked = True
        _warm_last_attempt = now
    thread = threading.Thread(target=_run_warm_and_untrack, daemon=True, name=name)
    thread.start()
    return thread


def _run_warm_and_untrack() -> None:
    """`_kick_warm_once`'s thread target: runs the real warm, then — unless
    it actually wrote a fresh rule table — clears `_warm_kicked` so a LATER
    empty-cache read gets its own attempt (subject to the backoff above)
    instead of finding the guard permanently spent by one that didn't pan
    out (offline mid-attempt, the shim erroring, a stale token)."""
    global _warm_kicked
    wrote = warm_rules_cache()
    if not wrote:
        with _warm_kicked_lock:
            _warm_kicked = False


def reset_for_tests() -> None:
    """Clear the process-wide "already kicked" guard (and its backoff
    timer). `_warm_kicked` is process state, not per-`create_app()` state
    (`_cached_rules()` has no `app` to key it off) — left standing, the
    second test in an xdist worker to read an empty cache would see
    `_kick_warm_once` silently return `None` for a warm-up the first test's
    OWN tmp-path cache already satisfied, same shape as
    `queue_manager.reset_for_tests` in tests/conftest.py."""
    global _warm_kicked, _warm_last_attempt
    with _warm_kicked_lock:
        _warm_kicked = False
        _warm_last_attempt = 0.0


def _cached_rules() -> list[dict]:
    """Disk-only read of whatever rule table the cache already holds
    (however stale) — never itself blocks on the SDK subprocess a rebuild
    needs. A missing cache degrades to the built-in rules alone for this
    call (so `.fused` still resolves) rather than blocking `status` on a
    shim spawn, but ALSO kicks a background rebuild the first time this
    process sees an empty cache — the case a lean server (`fused-render
    open`), which never runs `_startup_warm_share_rules`, hits on its very
    first read on a machine that has never run a full server before. A full
    server's own startup thread normally wins that race, so this is a no-op
    there in the common case; it only matters when nothing warmed the cache
    first."""
    rules, _built_at = share_file_rules._read_cache()
    if rules is None:
        _kick_warm_once(name="fused-share-file-rules-warm-lazy")
    return share_file_rules._with_builtin(rules or [])


def resolve_viewer(path: str) -> dict | None:
    return share_file_rules.resolve(path, _cached_rules())


def warm_rules_cache() -> bool:
    """Actually build the catalog rule cache, through the shim's `rules`
    action — nothing else in the product does this (code review finding 1):
    `_cached_rules()` above is disk-only by design, so a cache that is never
    written stays `None` forever and every extension but the built-in
    `.fused` refuses to share. Called once from a server startup thread
    (`server/app.py`'s `_startup_warm_share_rules`) so the table is usually
    warm well before a reader opens the share sheet.

    Best-effort and silent: no CLI, not signed in, offline, or a stale token
    all leave the cache exactly as before (missing, or whatever it already
    held) rather than raising into a startup thread nobody is watching.

    Returns whether it actually wrote a fresh rule table — `_kick_warm_once`
    (via `_run_warm_and_untrack`) uses this to decide whether the one-shot
    guard should stay set or be released for a later retry."""
    if not _logged_in():
        return False
    out, err = share_app._run_shim(
        {"action": "rules", "ttl": share_file_rules.RULES_TTL_PUBLISH}, RULES_TIMEOUT)
    if err is not None:
        return False
    rules = out.get("rules") if isinstance(out, dict) else None
    if isinstance(rules, list):
        share_file_rules._write_cache(rules)
        return True
    return False


def _refusal_for(path: str, rule: dict | None) -> str | None:
    if rule is not None:
        return None
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext:
        return f"no Fused viewer opens .{ext} files"
    return "no Fused viewer opens files with no extension"


MODES = ("public", "temporary")


def _parse_expiry(value) -> float | None:
    """`session_expires` as the shim hands it back — an ISO 8601 string in
    the common case, but treated generically since the SDK's own type is not
    pinned in the spec (artifact-sharing.md §3: `.expires_at`)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        try:
            import datetime

            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # A naive ISO string is what the SDK returns in practice — no
                # `Z`, no offset. `.timestamp()` on a naive datetime reads it
                # in the SERVER'S local zone; the SDK means UTC (finding 7).
                # On UTC+2 a fresh 30-minute token read as ~90 minutes
                # expired, so /status flagged a working link "Expired".
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return None
    return None


def is_expired(record: dict) -> bool:
    """A `temporary` share's session token past its `session_expires` — the
    sheet uses this to offer a fresh link without a round trip (task 5)."""
    if record.get("mode") != "temporary":
        return False
    expiry = _parse_expiry(record.get("session_expires"))
    if expiry is None:
        return False
    return expiry <= time.time()


def _resolve_path(path: str) -> tuple[str | None, str | None]:
    """(abs path, refusal). A directory is refused the same as a missing one
    — Share is a file action, not a folder one (§7's `.fused` app stays on
    share_app.py's own route)."""
    if not path or not os.path.isabs(path):
        return None, "path must be an absolute file path"
    if not os.path.isfile(path):
        return None, f"no such file: {path}"
    return path, None


# -- detached upload -----------------------------------------------------------
#
# A large file's transfer alone can exceed PUBLISH_TIMEOUT, so it runs
# outside any request at all: /upload spawns it and returns immediately,
# /upload/status polls it, and /publish (for a file over
# INLINE_PUBLISH_MAX_BYTES) takes the finished job's remote/s3_uri and does
# only the bounded canvas work. State lives in three marker files under
# <STATE_DIR>/share_uploads/<id>/ — `done` is authoritative (SPEC
# artifact-fileudf.md §4.1): no `done` and no live pid means the worker was
# killed outright and nothing will ever write one, so the poll ends
# `cancelled` rather than spinning forever.


class ShareUploadError(Exception):
    pass


def _uploads_root() -> str:
    return os.path.join(share_app.STATE_DIR, UPLOADS_SUBDIR)


def _upload_paths(upload_id: str) -> dict:
    d = os.path.join(_uploads_root(), upload_id)
    return {
        "dir": d,
        "log": os.path.join(d, "log.txt"),
        "pid": os.path.join(d, "pid"),
        "done": os.path.join(d, "done"),
        "result": os.path.join(d, "result.json"),
        "meta": os.path.join(d, "meta.json"),
    }


def _pid_alive(pid: int) -> bool:
    # os.kill(pid, 0) is the POSIX no-op liveness check, but on Windows signal 0
    # aliases CTRL_C_EVENT: it does NOT probe the target, it broadcasts a real
    # Ctrl+C via GenerateConsoleCtrlEvent to the whole console process group —
    # including, when `pid` is our own (as it legitimately can be: the test
    # suite's own worker pid, or a recycled pid from a finished upload), this
    # very process and any sibling it shares a console with. Probe the exit
    # code via the Win32 API instead of signalling anything.
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _tail(path: str, limit: int = 4000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_upload_state(upload_id: str) -> dict:
    """Pure marker-file read: `running|done|failed|cancelled|none`, with
    `bytes` (the source file's size) and `elapsed` when known."""
    paths = _upload_paths(upload_id)
    if not upload_id or not os.path.isdir(paths["dir"]):
        return {"id": upload_id, "state": "none"}

    meta: dict = {}
    try:
        with open(paths["meta"], encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        pass
    started_at = meta.get("started_at")
    size = meta.get("size")
    elapsed = (time.time() - started_at) if isinstance(started_at, (int, float)) else None

    done_code: int | None = None
    if os.path.isfile(paths["done"]):
        try:
            with open(paths["done"], encoding="utf-8") as f:
                done_code = int(f.read().strip())
        except (OSError, ValueError):
            done_code = -1  # present but unreadable: a failure, not a hang

    if done_code is not None:
        if done_code == 0:
            result: dict = {}
            try:
                with open(paths["result"], encoding="utf-8") as f:
                    result = json.load(f)
            except (OSError, ValueError):
                pass
            return {"id": upload_id, "state": "done", "bytes": size, "elapsed": elapsed,
                    "remote": result.get("remote"), "s3_uri": result.get("s3_uri")}
        return {"id": upload_id, "state": "failed", "bytes": size, "elapsed": elapsed,
                "error": _tail(paths["log"]).strip() or f"upload exited with code {done_code}"}

    pid: int | None = None
    if os.path.isfile(paths["pid"]):
        try:
            with open(paths["pid"], encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            pid = None
    if pid is not None and _pid_alive(pid):
        return {"id": upload_id, "state": "running", "bytes": size, "elapsed": elapsed}
    return {"id": upload_id, "state": "cancelled", "bytes": size, "elapsed": elapsed}


def _spawn_upload(upload_id: str, path: str, share_id: str) -> None:
    paths = _upload_paths(upload_id)
    if os.path.isdir(paths["dir"]):
        shutil.rmtree(paths["dir"], ignore_errors=True)
    os.makedirs(paths["dir"], exist_ok=True)
    with open(paths["meta"], "w", encoding="utf-8") as f:
        json.dump({"started_at": time.time(), "size": os.path.getsize(path)}, f)
    request_path = os.path.join(paths["dir"], "request.json")
    with open(request_path, "w", encoding="utf-8") as f:
        json.dump({"action": "upload", "share_id": share_id, "file": path}, f)

    command, err = share_app._shim_command()
    if err is not None:
        raise ShareUploadError("the fused CLI is not available")
    cli = fused_cli()
    env = child_env(cli)
    env["FUSED_ENV"] = workbench_env()

    # A shell wrapper, not a plain Popen + a reaper thread: the worker must
    # write its own `done` file so the state machine survives a fused-render
    # restart mid-transfer, and `start_new_session` gives /upload/cancel a
    # whole process group to signal.
    shell = " ".join(shlex.quote(c) for c in [*command, request_path])
    full = (f"{shell} > {shlex.quote(paths['result'])} 2> {shlex.quote(paths['log'])}; "
            f"echo $? > {shlex.quote(paths['done'])}")
    proc = subprocess.Popen(["sh", "-c", full], env=env, stdin=subprocess.DEVNULL,
                            start_new_session=True, close_fds=True)
    with open(paths["pid"], "w", encoding="utf-8") as f:
        f.write(str(proc.pid))
    # `proc` is never otherwise waited on — the state machine reads the
    # `done` marker file, not the child's exit as seen by this process — so
    # without this the shell wrapper becomes a zombie child of the server
    # the moment it exits (normally, or SIGTERMed by cancel_upload), and
    # `_pid_alive` (`os.kill(pid, 0)`) reports a zombie as alive: a cancelled
    # upload could read `running` forever instead of `cancelled` until
    # CPython's own opportunistic subprocess._cleanup() happened to reap it
    # (finding 11). A daemon reaper thread, not a blocking wait() here: this
    # function returns immediately to the request that spawned it.
    threading.Thread(target=proc.wait, daemon=True,
                     name=f"share-upload-reap-{upload_id}").start()


def start_upload(path: str, upload_id: str) -> dict:
    """Spawn the detached transfer, or attach to one already running for this
    id — idempotent while running, so a reloaded sheet or a double click
    never races a second copy onto the same remote path."""
    state = read_upload_state(upload_id)
    if state["state"] == "running":
        return state
    _spawn_upload(upload_id, path, upload_id)
    return read_upload_state(upload_id)


def cancel_upload(upload_id: str) -> dict:
    paths = _upload_paths(upload_id)
    pid: int | None = None
    if os.path.isfile(paths["pid"]):
        try:
            with open(paths["pid"], encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            pid = None
    if pid is not None:
        if os.name == "nt":
            # `_spawn_upload`'s POSIX `sh -c ... start_new_session=True` has no
            # Windows equivalent, but a live pid from a prior POSIX run (or a
            # test) can still reach here — os.getpgid/os.killpg do not exist on
            # Windows at all (not just unsupported: the attribute is absent),
            # so this must not fall through to the POSIX branch below. Mirror
            # engine_host._kill_tree's Windows path instead.
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
            except (OSError, AttributeError, ValueError):
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True)
            return read_upload_state(upload_id)
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    return read_upload_state(upload_id)


# -- routes --------------------------------------------------------------------


@router.get("/api/share/file/status")
def api_share_file_status(path: str = ""):
    """Disk only, and read-only, like share_app.py's status: the resolved
    viewer (or the refusal naming why not), whether Fused is signed in, and
    the local share record if there is one."""
    abspath, path_refusal = _resolve_path(path)
    if path_refusal is not None:
        return {
            "file_id": None,
            "can_share": False,
            "refusal": path_refusal,
            "viewer": None,
            "cli_found": fused_cli() is not None,
            "logged_in": _logged_in(),
            "creds_stamp": _creds_stamp(),
            "shared": None,
        }
    rule = resolve_viewer(abspath)
    refusal = _refusal_for(abspath, rule)
    file_id = file_identity(abspath)
    shared = _record_for(file_id)
    if shared is not None:
        shared = {**shared, "expired": is_expired(shared)}
    return {
        "file_id": file_id,
        "can_share": refusal is None,
        "refusal": refusal,
        "viewer": rule.get("name") if rule else None,
        "cli_found": fused_cli() is not None,
        "logged_in": _logged_in(),
        "creds_stamp": _creds_stamp(),
        "shared": shared,
    }


@router.post("/api/share/file/publish")
def api_share_file_publish(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Publish (or update) a public share of the file. A plain `def`, for the
    same reason share_app.py's publish is: the shim blocks for the whole
    upload + canvas push, and an `async def` would hold the event loop."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path") if isinstance(body.get("path"), str) else ""
    abspath, path_refusal = _resolve_path(path)
    if path_refusal is not None:
        return _error(path_refusal)
    rule = resolve_viewer(abspath)
    refusal = _refusal_for(abspath, rule)
    if refusal is not None:
        return _error(refusal)
    if not _logged_in():
        return _not_signed_in()

    mode = body.get("mode") if isinstance(body.get("mode"), str) else "public"
    if mode not in MODES:
        return _error(f"unknown share mode {mode!r}; expected one of {MODES}")

    file_id = file_identity(abspath)
    size = os.path.getsize(abspath)
    upload_id = body.get("upload_id") if isinstance(body.get("upload_id"), str) else None
    if upload_id is not None and upload_id != file_id:
        # The only upload_id /upload ever mints for a file is file_identity(
        # abspath) itself (api_share_file_upload calls start_upload(abspath,
        # file_id)) — a caller-supplied id that disagrees is either stale (a
        # second tab, an old sheet) or crafted, and blindly trusting it would
        # hand THIS file's canvas another upload's remote/s3_uri (finding 4:
        # publishing one file's uploaded object under a different file's
        # name and canvas).
        return _error("upload_id does not match this file", 400)
    remote = s3_uri = None
    if upload_id is not None or size > INLINE_PUBLISH_MAX_BYTES:
        upload_state = read_upload_state(upload_id or file_id)
        if upload_state["state"] != "done":
            return _error(
                f"the upload has not finished (state: {upload_state['state']}); call "
                "/api/share/file/upload first for a file this size", 409)
        remote, s3_uri = upload_state.get("remote"), upload_state.get("s3_uri")

    lock = share_app._app_lock(abspath)
    if not lock.acquire(blocking=False):
        return JSONResponse({"error": "this file is already being shared", "code": "busy"},
                            status_code=409)
    try:
        key = _record_key(file_id)
        name = os.path.basename(abspath)
        previous = get_record(key) or {}
        request = {"action": "publish", "share_id": file_id, "viewer_token": rule["token"],
                   "name": name, "previous_remote": previous.get("remote"), "mode": mode}
        if s3_uri:
            request["remote"], request["s3_uri"] = remote, s3_uri
        else:
            request["file"] = abspath
        out, err = share_app._run_shim(request, PUBLISH_TIMEOUT)
        if err is not None:
            return err
        now = time.time()
        record = {
            "file_id": file_id,
            "path": abspath,
            "name": name,
            "viewer": rule["name"],
            "url": out.get("url"),
            "canvas_id": out.get("canvas_id"),
            "canvas_name": out.get("canvas_name"),
            "share_token": out.get("share_token"),
            "slug": out.get("slug"),
            "remote": out.get("remote"),
            "workbench_url": out.get("workbench_url"),
            "mode": out.get("mode", mode),
            "session_token": out.get("session_token"),
            "session_expires": out.get("session_expires"),
            "shared_at": previous.get("shared_at") or now,
            "updated_at": now,
        }
        put_record(key, record)
        return {"ok": True, "shared": {**record, "expired": is_expired(record)}}
    finally:
        lock.release()


@router.post("/api/share/file/lookup")
def api_share_file_lookup(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Ask Fused whether a canvas for this file already exists — the case
    with no local record (shared from another machine)."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path") if isinstance(body.get("path"), str) else ""
    abspath, path_refusal = _resolve_path(path)
    if path_refusal is not None:
        return _error(path_refusal)
    if not _logged_in():
        return _not_signed_in()
    file_id = file_identity(abspath)
    key = _record_key(file_id)
    name = os.path.basename(abspath)
    out, err = share_app._run_shim({"action": "lookup", "share_id": file_id, "name": name},
                                   LOOKUP_TIMEOUT)
    if err is not None:
        return err
    if not out.get("found"):
        return {"found": False, "file_id": file_id}
    now = time.time()
    existing = get_record(key) or {}
    # The shim's `lookup` only ever returns the bare, session-less
    # _share_url() — it never mints a fresh session token (finding 8). For a
    # `temporary` share that bare URL 403s for the recipient; keep the
    # stored one (which still carries `?fused_session_token=...`) instead of
    # letting a lookup silently swap in a dead link while `status` keeps
    # showing it as live.
    if existing.get("mode") == "temporary":
        url = existing.get("url")
    else:
        url = out.get("url") or existing.get("url")
    record = {
        **existing,
        "file_id": file_id,
        "path": abspath,
        "name": name,
        "url": url,
        "canvas_id": out.get("canvas_id"),
        "canvas_name": out.get("canvas_name"),
        "share_token": out.get("share_token"),
        "slug": out.get("slug") or existing.get("slug"),
        "workbench_url": out.get("workbench_url") or existing.get("workbench_url"),
        "shared_at": existing.get("shared_at") or now,
        "updated_at": existing.get("updated_at") or now,
        "adopted": True,
    }
    put_record(key, record)
    return {"found": True, "file_id": file_id, "shared": record}


@router.post("/api/share/file/remove")
def api_share_file_remove(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Delete the canvas and the uploaded copies — Stop sharing."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path") if isinstance(body.get("path"), str) else ""
    abspath, path_refusal = _resolve_path(path)
    if path_refusal is not None:
        return _error(path_refusal)
    if not _logged_in():
        return _not_signed_in()
    file_id = file_identity(abspath)
    key = _record_key(file_id)
    lock = share_app._app_lock(abspath)
    if not lock.acquire(blocking=False):
        return JSONResponse({"error": "this file is already being shared", "code": "busy"},
                            status_code=409)
    try:
        rec = get_record(key) or {}
        out, err = share_app._run_shim(
            {"action": "remove", "share_id": file_id, "canvas_id": rec.get("canvas_id")},
            REMOVE_TIMEOUT)
        if err is not None:
            return err
        drop_record(key)
        return {"ok": True, "deleted_canvas": bool(out.get("deleted_canvas"))}
    finally:
        lock.release()


@router.post("/api/share/file/upload")
def api_share_file_upload(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Spawn (or attach to) the detached transfer for a file too large to
    publish inline. Returns the current state immediately — the caller polls
    /upload/status until it settles."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path") if isinstance(body.get("path"), str) else ""
    abspath, path_refusal = _resolve_path(path)
    if path_refusal is not None:
        return _error(path_refusal)
    rule = resolve_viewer(abspath)
    refusal = _refusal_for(abspath, rule)
    if refusal is not None:
        return _error(refusal)
    if not _logged_in():
        return _not_signed_in()
    size = os.path.getsize(abspath)
    if size > MAX_UPLOAD_BYTES:
        return _error(f"{os.path.basename(abspath)} is {size} bytes, over the "
                      f"{MAX_UPLOAD_BYTES}-byte share cap")
    file_id = file_identity(abspath)
    try:
        return start_upload(abspath, file_id)
    except ShareUploadError as exc:
        return _error(str(exc), 502)


@router.get("/api/share/file/upload/status")
def api_share_file_upload_status(id: str = ""):
    if not id:
        return _error("missing id")
    if not _valid_upload_id(id):
        return _error("invalid id")
    return read_upload_state(id)


@router.post("/api/share/file/upload/cancel")
def api_share_file_upload_cancel(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    upload_id = body.get("id") if isinstance(body.get("id"), str) else ""
    if not upload_id:
        return _error("missing id")
    if not _valid_upload_id(upload_id):
        return _error("invalid id")
    return cancel_upload(upload_id)
