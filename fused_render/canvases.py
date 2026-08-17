"""Canvases: local development on top of legacy-workbench canvases.

The workflow (SPEC-pending; see PR description): list the account's canvases,
clone one to a local folder with `fused canvas pull`, let the user edit those
files (Claude Code, the explorer, any editor), watch the folder and push every
change set back upstream with `fused canvas push`, and let the workspace page
reload its embedded workbench iframe after each successful push (the hosted
workbench has no push-driven reload of its own — the CLI literally prints
"you can refresh it now").

Provider note: canvas commands authenticate with `fused login`
(~/.fused/credentials, Auth0 PKCE) — a DIFFERENT provider from the
`fused cloud login` store account.py manages (~/.openfused). Same CLI binary
(the `fused` package resolved by fusedcli.fused_cli()), different credential
stores; this module owns the `fused login` side. Unlike `cloud login
--no-browser`, plain `fused login` never prints its authorize URL — it opens
the browser itself and blocks on a localhost callback — so the login endpoint
here spawns the child and the client simply polls status until the
credentials file appears.

Sync model (local-wins, deliberately): one watcher thread per synced canvas
fingerprints the clone folder (path/mtime/size walk) once a second; any change
arms a debounce, and after DEBOUNCE_S of quiet the whole folder is pushed with
`fused canvas push --canvas <name>` — which REPLACES the remote UDF set, so an
edit made concurrently in the hosted workbench is overwritten. Pushes are
serialized per canvas by construction (one thread) and the fingerprint is
re-taken right after clone so the pull's own writes never echo into a push.
`push_seq` increments on every successful push; the workspace page polls it
and reloads the workbench iframe when it moves.

No import of anything under fused_render.server (server includes this router —
keep it acyclic); the X-Fused guard is duplicated locally like account.py does.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import threading
import time

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.fusedcli import child_env, cli_error, fused_cli

router = APIRouter()

# `fused canvas list/whoami` are one control-plane call each.
LIST_TIMEOUT = 30.0
WHOAMI_TIMEOUT = 20.0
# A pull downloads and extracts the canvas zip; canvases are small (source
# files, not data), so a minute is generous headroom for a slow link.
PULL_TIMEOUT = 180.0
PUSH_TIMEOUT = 180.0
# The token shim pays the fused import cost plus at most one refresh call.
TOKEN_TIMEOUT = 30.0
# `fused login` blocks on its browser callback; its own HTTP server times out
# after ~30s per request, but give the child room for slow human sign-ins.
LOGIN_CHILD_TIMEOUT = 600.0
# Quiet period after the last observed file change before a push fires.
DEBOUNCE_S = 1.5
# Folder fingerprint poll cadence.
SCAN_INTERVAL_S = 1.0

# Canvas names are `[a-zA-Z0-9_]` per the CLI's own push rule; enforcing it
# here also keeps the name safe as a path segment and an argv element.
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,128}$")

# Base URL of the hosted workbench, for the workspace iframe. The CLI's own
# env selection (FUSED_ENV) maps to different hosts; production is the
# overwhelmingly common case, and the override keeps staging testable.
WORKBENCH_BASE_URL = os.environ.get(
    "FUSED_RENDER_WORKBENCH_URL", "https://www.fused.io"
)


def canvases_root() -> str:
    return os.environ.get("FUSED_RENDER_CANVASES_DIR") or os.path.expanduser(
        "~/.fused-render/canvases"
    )


def _canvas_dir(name: str) -> str:
    return os.path.join(canvases_root(), name)


def _credentials_file() -> str:
    # The `fused login` store (fused-py _options.py: ~/.fused/credentials).
    # The env override is ours (tests, relocated stores) — the fused package
    # itself points elsewhere only via its settings file.
    return os.environ.get("FUSED_RENDER_FUSED_CREDENTIALS") or os.path.expanduser(
        "~/.fused/credentials"
    )


def _logged_in() -> bool:
    # Presence-only, same rationale as fusedcli.fused_cloud_logged_in(): the
    # CLI refreshes an expired-but-refreshable token silently; the CLI stays
    # the authority at action time.
    return os.path.isfile(_credentials_file())


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server's _require_fused, duplicated to stay acyclic.
    if x_fused != "1":
        return JSONResponse(
            {"error": "missing or invalid X-Fused header"}, status_code=403
        )
    return None


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _no_cli_error() -> JSONResponse:
    return _error(
        "the fused CLI is not available: the fused package is not importable in "
        "the server's environment and no FUSED_RENDER_FUSED_BIN override is set; "
        'run: pip install "fused-render[fused]"'
    )


def _run_cli(args: list[str], timeout: float) -> tuple[subprocess.CompletedProcess | None, JSONResponse | None]:
    """Run a fused CLI command; (proc, None) or (None, error response)."""
    cli = fused_cli()
    if cli is None:
        return None, _no_cli_error()
    # Human-readable command label for error messages: the subcommand words,
    # skipping the `workbench` nesting and option tokens.
    label = "fused " + " ".join(
        a for a in args if a not in ("workbench", "--format", "json")
    )
    try:
        proc = subprocess.run(
            [*cli.command, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env(cli),
        )
    except subprocess.TimeoutExpired:
        return None, _error(f"`{label}` timed out after {int(timeout)}s", 502)
    except OSError as e:
        return None, _error(f"could not run the fused CLI ({cli.command[0]}): {e}")
    if proc.returncode != 0:
        message = cli_error(proc.stderr or proc.stdout, f"{label} failed")
        # A credentials file can exist yet be unrefreshable (e.g. issued by an
        # older Auth0 application) — presence said "logged in", but every call
        # dies with the CLI's re-authenticate message. Map that to 401 so the
        # client can fall back to the sign-in flow instead of a dead error.
        if "re-authenticate" in message.lower() or "refresh your fused credentials" in message.lower():
            return None, _error(message, 401)
        return None, _error(message, 502)
    return proc, None


# -- login (the `fused login` provider) -------------------------------------------


@dataclasses.dataclass
class _ActiveLogin:
    proc: subprocess.Popen
    started_at: float


_LOGIN_LOCK = threading.Lock()
_active_login: _ActiveLogin | None = None


def _reap_login() -> _ActiveLogin | None:
    """The live login child, dropping a dead one; callers hold no lock."""
    global _active_login
    with _LOGIN_LOCK:
        login = _active_login
        if login is not None and login.proc.poll() is not None:
            _active_login = login = None
        return login


def _watch_login(login: _ActiveLogin) -> None:
    try:
        login.proc.wait(LOGIN_CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        login.proc.kill()
        login.proc.wait()


@router.get("/api/canvases/status")
def api_canvases_status():
    """Cheap local read: CLI presence, login-file presence, in-flight login.

    The username needed for the workbench slug URL is NOT here — it costs a
    control-plane call, so it lives behind the guarded ?probe=1 of
    /api/canvases/whoami instead.
    """
    cli = fused_cli()
    try:
        stamp = os.path.getmtime(_credentials_file())
    except OSError:
        stamp = None
    return {
        "cli_found": cli is not None,
        "logged_in": _logged_in(),
        # Credentials-file mtime: a re-login over a STALE-but-present store
        # never flips logged_in, so the client watches this stamp change to
        # know the browser flow completed (same trick as account.py's
        # creds_stamp).
        "creds_stamp": stamp,
        "login_in_flight": _reap_login() is not None,
        "workbench_base_url": WORKBENCH_BASE_URL,
        "canvases_dir": canvases_root(),
    }


@router.get("/api/canvases/whoami")
def api_canvases_whoami(x_fused: str | None = Header(default=None)):
    # EXECUTES a control-plane call — D36 guard, like account's probe=1.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    proc, err = _run_cli(["workbench", "--format", "json", "whoami"], WHOAMI_TIMEOUT)
    if err is not None:
        return err
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return _error(
            f"the fused CLI printed something that wasn't JSON: {proc.stdout.strip()[-200:]!r}",
            502,
        )
    if not isinstance(data, dict):
        return _error("unexpected `fused whoami` payload", 502)
    # The slug URL needs the user's handle; the payload's field name has
    # drifted historically, so take the first plausible one.
    handle = next(
        (
            data[key]
            for key in ("handle", "username", "user_name", "nickname")
            if isinstance(data.get(key), str) and data[key]
        ),
        None,
    )
    return {"handle": handle, "raw": data}


@router.post("/api/canvases/login")
def api_canvases_login(x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    cli = fused_cli()
    if cli is None:
        return _no_cli_error()
    global _active_login
    with _LOGIN_LOCK:
        login = _active_login
        if login is None or login.proc.poll() is not None:
            env = child_env(cli)
            env["PYTHONUNBUFFERED"] = "1"
            try:
                # Unlike `cloud login --no-browser`, plain `fused login` opens
                # the browser itself (webbrowser.open) and never prints the
                # authorize URL — so there is nothing to capture; the client
                # polls /api/canvases/status until logged_in flips.
                proc = subprocess.Popen(
                    [*cli.command, "workbench", "login"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
            except OSError as e:
                return _error(f"could not run the fused CLI ({cli.command[0]}): {e}")
            login = _ActiveLogin(proc=proc, started_at=time.time())
            _active_login = login
            threading.Thread(target=_watch_login, args=(login,), daemon=True).start()
    return {"ok": True, "login_in_flight": True}


@router.post("/api/canvases/login/cancel")
def api_canvases_login_cancel(x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    login = _reap_login()
    if login is not None:
        login.proc.terminate()
    return {"ok": True, "canceled": login is not None}


# -- token for the embedded workbench ----------------------------------------------


@router.get("/api/canvases/token")
def api_canvases_token(x_fused: str | None = Header(default=None)):
    """The `fused login` access token, for seeding the workbench iframe.

    Guarded (D36): this hands a real credential to the caller — the loopback
    trust model means any local page could otherwise read it with a blind GET.
    The workspace page forwards it to the embedded workbench over postMessage
    with an exact fused.io targetOrigin (the workbench side accepts it only in
    ?fused_embed_auth=1 mode, only from localhost parent origins).

    Refresh runs through the fused package itself (_fused_token.py child) so
    the refresh/save logic is never duplicated here. With an EXTERNAL
    FUSED_RENDER_FUSED_BIN override there is no interpreter to run the shim
    in; the on-disk token is returned as-is and expiry surfaces in the iframe
    (the workbench requests a refresh, which will keep failing until the
    external CLI is used once — a documented limitation of the override).
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not _logged_in():
        return _error("not signed in to Fused — sign in from the Canvases page", 409)
    cli = fused_cli()
    if cli is not None and not cli.external:
        shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fused_token.py")
        try:
            proc = subprocess.run(
                [sys.executable, shim],
                capture_output=True,
                text=True,
                timeout=TOKEN_TIMEOUT,
                env=child_env(cli),
            )
        except subprocess.TimeoutExpired:
            return _error(f"token refresh timed out after {int(TOKEN_TIMEOUT)}s", 502)
        except OSError as e:
            return _error(f"could not run the token helper: {e}", 502)
        if proc.returncode != 0:
            return _error(cli_error(proc.stderr, "could not read the fused access token"), 502)
        token = proc.stdout.strip()
        if not token:
            return _error("the token helper printed nothing", 502)
        return {"access_token": token}
    # External-CLI fallback: the raw store, no refresh.
    try:
        with open(_credentials_file(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return _error(f"could not read the fused credentials file: {e}", 502)
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        return _error("the fused credentials file has no access token", 502)
    return {"access_token": token}


# -- listing and cloning -----------------------------------------------------------


@router.get("/api/canvases/list")
def api_canvases_list(x_fused: str | None = Header(default=None)):
    # EXECUTES a control-plane call — guarded like whoami.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not _logged_in():
        return _error("not signed in to Fused — sign in first", 409)
    proc, err = _run_cli(["workbench", "--format", "json", "canvas", "list"], LIST_TIMEOUT)
    if err is not None:
        return err
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return _error(
            f"the fused CLI printed something that wasn't JSON: {proc.stdout.strip()[-200:]!r}",
            502,
        )
    # Normalize defensively: the CLI has printed both bare name lists and
    # object lists across versions.
    canvases = []
    entries = data if isinstance(data, list) else data.get("canvases") if isinstance(data, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, str):
                canvases.append({"name": entry, "id": None})
            elif isinstance(entry, dict):
                name = entry.get("name") or entry.get("slug")
                if isinstance(name, str) and name:
                    canvases.append({"name": name, "id": entry.get("id")})
    cloned = set()
    root = canvases_root()
    try:
        cloned = {
            d for d in os.listdir(root) if os.path.isfile(os.path.join(root, d, "canvas.toml"))
        }
    except OSError:
        pass
    for canvas in canvases:
        canvas["cloned"] = canvas["name"] in cloned
    return {"canvases": canvases}


@router.post("/api/canvases/clone")
def api_canvases_clone(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    if not _logged_in():
        return _error("not signed in to Fused — sign in first", 409)
    target = _canvas_dir(name)
    os.makedirs(target, exist_ok=True)
    # --force: the clone folder is OURS (under ~/.fused-render/canvases); a
    # re-clone deliberately resets it to the remote state. The sync watcher is
    # paused around the pull so its writes never echo into a push.
    manager = _sync_manager(name, create=False)
    if manager is not None:
        manager.pause()
    try:
        proc, err = _run_cli(
            ["workbench", "canvas", "pull", name, "-o", target, "--force"], PULL_TIMEOUT
        )
    finally:
        if manager is not None:
            manager.resume()
    if err is not None:
        return err
    return {"ok": True, "dir": target}


# -- the per-canvas sync watcher -----------------------------------------------------


class _SyncManager:
    """One watcher thread: fingerprint the clone folder, debounce, push.

    All mutable fields are written by the watcher thread (plus `paused`/`stop`
    flips from request threads); plain attribute reads are safe under the GIL
    and readers only render status, so no lock is needed beyond the pause
    event semantics.
    """

    def __init__(self, name: str):
        self.name = name
        self.dir = _canvas_dir(name)
        self.stop_event = threading.Event()
        self.pause_count = 0
        self.pause_lock = threading.Lock()
        self.push_seq = 0
        self.push_state = "idle"  # idle | pending | pushing | error
        self.last_push_at: float | None = None
        self.last_error: str | None = None
        self._fingerprint = self._take_fingerprint()
        self._dirty_since: float | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def pause(self) -> None:
        with self.pause_lock:
            self.pause_count += 1

    def resume(self) -> None:
        with self.pause_lock:
            self.pause_count = max(0, self.pause_count - 1)
            if self.pause_count == 0:
                # Whatever the pull wrote is the new baseline, not a change.
                self._fingerprint = self._take_fingerprint()
                self._dirty_since = None

    def stop(self) -> None:
        self.stop_event.set()

    def _take_fingerprint(self) -> dict[str, tuple[float, int]]:
        fp: dict[str, tuple[float, int]] = {}
        for root, dirs, files in os.walk(self.dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith("."):
                    continue
                path = os.path.join(root, f)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                fp[os.path.relpath(path, self.dir)] = (st.st_mtime, st.st_size)
        return fp

    def _push(self) -> None:
        self.push_state = "pushing"
        cli = fused_cli()
        if cli is None:
            self.push_state = "error"
            self.last_error = "the fused CLI is not available"
            return
        # Baseline BEFORE the push: a save landing while the push runs must
        # re-arm the debounce, not vanish into the pushed snapshot.
        self._fingerprint = self._take_fingerprint()
        self._dirty_since = None
        try:
            proc = subprocess.run(
                [*cli.command, "workbench", "canvas", "push", self.dir, "--canvas", self.name],
                capture_output=True,
                text=True,
                timeout=PUSH_TIMEOUT,
                env=child_env(cli),
            )
        except subprocess.TimeoutExpired:
            self.push_state = "error"
            self.last_error = f"`fused canvas push` timed out after {int(PUSH_TIMEOUT)}s"
            return
        except OSError as e:
            self.push_state = "error"
            self.last_error = f"could not run the fused CLI: {e}"
            return
        if proc.returncode != 0:
            self.push_state = "error"
            self.last_error = cli_error(proc.stderr or proc.stdout, "fused canvas push failed")
            return
        self.push_seq += 1
        self.last_push_at = time.time()
        self.last_error = None
        self.push_state = "idle"

    def _run(self) -> None:
        while not self.stop_event.wait(SCAN_INTERVAL_S):
            with self.pause_lock:
                paused = self.pause_count > 0
            if paused:
                continue
            current = self._take_fingerprint()
            if current != self._fingerprint:
                self._fingerprint = current
                self._dirty_since = time.time()
                if self.push_state != "error":
                    self.push_state = "pending"
                continue
            if self._dirty_since is not None and time.time() - self._dirty_since >= DEBOUNCE_S:
                self._push()

    def status(self) -> dict:
        return {
            "name": self.name,
            "dir": self.dir,
            "watching": not self.stop_event.is_set(),
            "push_state": self.push_state,
            "push_seq": self.push_seq,
            "last_push_at": self.last_push_at,
            "error": self.last_error,
        }


_SYNC_LOCK = threading.Lock()
_syncs: dict[str, _SyncManager] = {}


def _sync_manager(name: str, create: bool) -> _SyncManager | None:
    with _SYNC_LOCK:
        manager = _syncs.get(name)
        if manager is not None and manager.stop_event.is_set():
            manager = None
        if manager is None and create:
            manager = _SyncManager(name)
            _syncs[name] = manager
        return manager


@router.post("/api/canvases/sync/start")
def api_canvases_sync_start(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    if not os.path.isfile(os.path.join(_canvas_dir(name), "canvas.toml")):
        return _error(f"canvas {name!r} is not cloned yet (no canvas.toml)", 409)
    manager = _sync_manager(name, create=True)
    return manager.status()


@router.post("/api/canvases/sync/stop")
def api_canvases_sync_stop(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str):
        return _error("'name' must be a canvas name")
    with _SYNC_LOCK:
        manager = _syncs.pop(name, None)
    if manager is not None:
        manager.stop()
    return {"ok": True, "stopped": manager is not None}


@router.get("/api/canvases/sync/status")
def api_canvases_sync_status(name: str = ""):
    if not _NAME_RE.fullmatch(name or ""):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    manager = _sync_manager(name, create=False)
    if manager is None:
        return {"name": name, "watching": False, "push_state": "idle", "push_seq": 0,
                "last_push_at": None, "error": None,
                "dir": _canvas_dir(name)}
    return manager.status()
