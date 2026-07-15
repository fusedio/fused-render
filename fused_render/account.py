"""In-app Fused account: sign in / sign out through the `fused` CLI.

The Deploy surface (deploy.py) needs a signed-in Fused account before a
managed-env publish can work; until now the app punted the one-time
`fused cloud login` to a terminal. This router does it in place, porting the
flow app's proven mechanics (flow repo, app/src/server/cloud.ts + spec
spec/app/connect-fused.md — see docs/PLAN-fused-account.md):

  * **Login is the CLI's own browser flow, embedded.** POST /api/account/login
    spawns `fused cloud login --no-browser` (Auth0 Authorization-Code + PKCE;
    the child prints the authorize URL and then blocks up to ~5 min on a
    localhost callback), captures the first URL from its output, and returns
    it — the BROWSER side (window.open) is the client's job. Two load-bearing
    child-env details from flow: PYTHONUNBUFFERED=1 (Python block-buffers
    piped stdout — without it the URL line never arrives), and
    OPENFUSED_LOGIN_RETURN_URL=<the client's page> so the CLI's callback page
    302s the browser straight back into this app instead of a "return to
    your terminal" page (loopback-validated — this server is loopback-only,
    and the CLI enforces the same rule).
  * **One login at a time.** A second POST while one is in flight joins it
    (same authorize URL) instead of racing a second callback server;
    POST /api/account/login/cancel kills the child.
  * **Completion is polled, not pushed.** There is no event channel from the
    CLI — the client polls GET /api/account/status until `logged_in` flips
    (the same presence-of-credentials signal deploy.py already uses; the
    optional ?probe=1 shells `fused cloud orgs` for the authoritative
    admitted/orgs view).
  * **Logout kills any in-flight login FIRST** (and waits for it to die), so
    a late browser callback can't silently re-write a JWT after
    `fused cloud logout` deleted it — then runs the CLI logout.
  * **No credential ever touches this app.** The CLI owns the JWT
    (~/.openfused/fused-cloud-credentials.json) and the data-plane keys; this
    module only reads *status* and runs the CLI. Nothing is persisted under
    ~/.fused-render.

No import of server.py (server includes this router — keep it acyclic); the
X-Fused guard is duplicated locally like deploy.py does. The CLI seam
(resolution, child-env hygiene, error mapping, store reads) is fusedcli.py,
shared with deploy.py.
"""
from __future__ import annotations

import collections
import dataclasses
import json
import re
import subprocess
import threading
import urllib.parse

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.deploy import cli_status
from fused_render.fusedcli import (
    child_env,
    cli_error,
    eligible_envs,
    fused_cli,
    fused_cloud_logged_in,
    setup_cli_hint,
)

router = APIRouter()

# How long the login child gets to print its authorize URL. The URL itself is
# printed before any network wait (the CLI builds it locally), but the child
# pays the fused package's import cost first — and a COLD first run (fresh
# venv/install, no bytecode cache yet) has been observed to take well over
# 15s. This ceiling only bounds the pathological silent-hang; a child that
# exits without a URL fails fast regardless (the exit watcher wakes waiters).
URL_CAPTURE_TIMEOUT = 30.0
# `cloud logout --no-browser` only deletes local state (JWT; with --env also
# that env's keyring entry) — no network. `cloud orgs` is a control-plane call.
LOGOUT_TIMEOUT = 60.0
ORGS_TIMEOUT = 20.0
# Grace for a terminated login child to exit before it is SIGKILLed (logout
# blocks on this so the dead child can't resurrect credentials).
KILL_GRACE = 3.0

_URL_RE = re.compile(r"https?://\S+")


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused (a custom header forces a CORS
    # preflight that fails cross-origin, blocking blind foreign POSTs).
    # Duplicated deliberately: account.py must not import server (no cycle —
    # server includes this router).
    if x_fused != "1":
        return JSONResponse({"error": "missing or invalid X-Fused header"}, status_code=403)
    return None


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


# -- the one in-flight login ----------------------------------------------------


@dataclasses.dataclass
class _ActiveLogin:
    """The single in-flight `fused cloud login` child.

    `authorize_url` is written once by whichever pump thread sees a URL first,
    then `url_event` is set — waiters read the field only after the event
    (the Event's internal lock gives the happens-before). `tail` collects the
    last output lines of BOTH streams for error reporting; deque.append is
    atomic under the GIL, so the pumps don't need a lock of their own.
    """

    proc: subprocess.Popen
    url_event: threading.Event = dataclasses.field(default_factory=threading.Event)
    authorize_url: str | None = None
    tail: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=40)
    )


_LOCK = threading.Lock()
_active: _ActiveLogin | None = None


def _pump(stream, login: _ActiveLogin) -> None:
    for raw in stream:
        line = raw.rstrip("\n")
        if line.strip():
            login.tail.append(line)
        if login.authorize_url is None:
            match = _URL_RE.search(line)
            if match:
                login.authorize_url = match.group(0)
                login.url_event.set()
    stream.close()


def _watch_exit(login: _ActiveLogin, pumps: list[threading.Thread]) -> None:
    """Reap the child and wake URL waiters the moment it exits.

    A child that dies WITHOUT printing a URL (not admitted, CLI misconfigured)
    must fail the login request immediately, not after the full capture
    timeout. Joining the pumps first means the tail is complete (both pipes
    at EOF) before any waiter reads it for the error message; setting the
    event with authorize_url still None is the failure signal.
    """
    for pump in pumps:
        pump.join()
    login.proc.wait()
    login.url_event.set()


def _start_login(cli, return_url: str | None) -> _ActiveLogin:
    """Spawn `fused cloud login --no-browser` and start its output pumps.

    Raises OSError when the command can't start (caller maps it to a 400).
    """
    env = child_env(cli)
    # Python block-buffers piped stdout — without this the authorize-URL line
    # sits in the child's buffer past the capture timeout (flow's hard-won fix).
    env["PYTHONUNBUFFERED"] = "1"
    if return_url:
        env["OPENFUSED_LOGIN_RETURN_URL"] = return_url
    proc = subprocess.Popen(
        [*cli.command, "cloud", "login", "--no-browser"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    login = _ActiveLogin(proc=proc)
    pumps = [
        threading.Thread(target=_pump, args=(stream, login), daemon=True)
        for stream in (proc.stdout, proc.stderr)
    ]
    for pump in pumps:
        pump.start()
    threading.Thread(target=_watch_exit, args=(login, pumps), daemon=True).start()
    return login


def _cancel_active_login(wait: float = 0.0) -> bool:
    """Terminate the in-flight login child, if any; True when one was live.

    With `wait` > 0, block up to that long for it to actually die (escalating
    to SIGKILL) — logout passes KILL_GRACE so a late Auth0 callback can't
    land in a child that outlives the credential delete and re-write a JWT.
    """
    global _active
    with _LOCK:
        login, _active = _active, None
    if login is None or login.proc.poll() is not None:
        return False
    login.proc.terminate()
    if wait:
        try:
            login.proc.wait(wait)
        except subprocess.TimeoutExpired:
            login.proc.kill()
            try:
                login.proc.wait(KILL_GRACE)
            except subprocess.TimeoutExpired:
                pass  # unkillable child; logout proceeds — poll shows it live
    return True


def _is_loopback_url(url: str) -> bool:
    """Whether `url` is an http(s) URL on a loopback host — the only place the
    login callback may redirect the browser (mirrors the CLI's own rule for
    OPENFUSED_LOGIN_RETURN_URL and flow's isLoopbackUrl)."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and host in ("localhost", "127.0.0.1", "::1")


# -- status ----------------------------------------------------------------------


def _probe_failure(message: str) -> dict:
    return {"ok": False, "admitted": None, "orgs": [], "error": message}


def _probe_orgs() -> dict:
    """`fused cloud orgs` — the authoritative signed-in view: whether the
    account is admitted and which org/envs it can target. Unlike the
    presence-only `logged_in` signal this exercises the token (refreshing it
    if needed), so a stale credentials file surfaces here as ok=False with
    the CLI's own message."""
    cli = fused_cli()
    try:
        proc = subprocess.run(
            [*cli.command, "cloud", "orgs"],
            capture_output=True,
            text=True,
            timeout=ORGS_TIMEOUT,
            env=child_env(cli),
        )
    except subprocess.TimeoutExpired:
        return _probe_failure(f"`fused cloud orgs` timed out after {int(ORGS_TIMEOUT)}s")
    except OSError as e:
        return _probe_failure(f"could not run the fused CLI ({cli.command[0]}): {e}")
    if proc.returncode != 0:
        return _probe_failure(cli_error(proc.stderr, "fused cloud orgs failed"))
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        tail = proc.stdout.strip()[-200:]
        return _probe_failure(f"the fused CLI printed something that wasn't JSON: {tail!r}")
    orgs = []
    raw = data.get("orgs")
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                orgs.append(
                    {
                        "org": entry.get("org"),
                        "env": entry.get("env"),
                        "provision_state": entry.get("provision_state"),
                        "role": entry.get("role"),
                    }
                )
    return {"ok": True, "admitted": bool(data.get("admitted")), "orgs": orgs, "error": None}


def status_payload(probe: bool) -> dict:
    global _active
    with _LOCK:
        login = _active
        if login is not None and login.proc.poll() is not None:
            # The child exited (completed, failed, or was canceled elsewhere) —
            # forget it so login_in_flight reads false on the next poll.
            _active = login = None
    cli = cli_status()
    logged_in = fused_cloud_logged_in()
    payload = {
        "cli": cli,
        "setup_cli": setup_cli_hint(),
        "logged_in": logged_in,
        "login_in_flight": login is not None,
        **eligible_envs(),
        "probe": None,
    }
    # The probe is pointless without credentials (it would just fail with
    # "not logged in") or without a CLI to run it.
    if probe and logged_in and cli["found"]:
        payload["probe"] = _probe_orgs()
    return payload


# -- routes ----------------------------------------------------------------------


@router.get("/api/account/status")
def api_account_status(probe: str = "0"):
    return status_payload(probe == "1")


@router.post("/api/account/login")
def api_account_login(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return_url = body.get("return_url")
    if return_url is not None:
        if not isinstance(return_url, str) or not _is_loopback_url(return_url):
            return _error("'return_url' must be an http(s) URL on a loopback host")
    cli = fused_cli()
    if cli is None:
        return _error(
            "the fused CLI is not available: the fused package is not importable in "
            "the server's environment and no FUSED_RENDER_FUSED_BIN override is set; "
            "install it from this page or run: pip install \"fused-render[fused]\""
        )

    global _active
    with _LOCK:
        login = _active
        if login is None or login.proc.poll() is not None:
            # Single-flight: only start a child when none is live. A joiner's
            # return_url is ignored — the first caller's child owns the flow.
            try:
                login = _start_login(cli, return_url)
            except OSError as e:
                return _error(f"could not run the fused CLI ({cli.command[0]}): {e}")
            _active = login

    # The event fires on the first URL line OR on child exit (_watch_exit) —
    # a doomed login fails fast instead of burning the whole capture window.
    if not login.url_event.wait(URL_CAPTURE_TIMEOUT) or login.authorize_url is None:
        with _LOCK:
            if _active is login:
                _active = None
        tail = "\n".join(login.tail)
        if login.proc.poll() is not None:
            # The child died without printing a URL — its own last line is the
            # most actionable message (cli_error also appends the packaged
            # app's wrapper path to `fused cloud login` instructions).
            return _error(cli_error(tail, "fused cloud login failed"), 502)
        login.proc.terminate()
        return _error(
            f"`fused cloud login` did not print a sign-in URL within "
            f"{int(URL_CAPTURE_TIMEOUT)}s" + (f"; last output: {tail!r}" if tail else ""),
            502,
        )
    return {"authorize_url": login.authorize_url}


@router.post("/api/account/login/cancel")
def api_account_login_cancel(x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return {"ok": True, "canceled": _cancel_active_login()}


@router.post("/api/account/logout")
def api_account_logout(
    body: dict | None = Body(default=None), x_fused: str | None = Header(default=None)
):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    env_name = (body or {}).get("env")
    if env_name is not None and (not isinstance(env_name, str) or not env_name):
        return _error("'env' must be an environment name when given")
    cli = fused_cli()
    if cli is None:
        return _error(
            "the fused CLI is not available, so there is nothing to sign out of "
            "from here (credentials live in the CLI's own store)"
        )
    # Kill any in-flight login BEFORE deleting credentials, and wait for it:
    # a completed-after-logout child would otherwise re-write the JWT.
    _cancel_active_login(wait=KILL_GRACE)
    args = [*cli.command, "cloud", "logout", "--no-browser"]
    if env_name:
        # Also drop that managed env's stored data-plane API key (a full
        # sign-out, the CLI's --env semantics).
        args += ["--env", env_name]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=LOGOUT_TIMEOUT, env=child_env(cli)
        )
    except subprocess.TimeoutExpired:
        return _error(f"`fused cloud logout` timed out after {int(LOGOUT_TIMEOUT)}s")
    except OSError as e:
        return _error(f"could not run the fused CLI ({cli.command[0]}): {e}")
    if proc.returncode != 0:
        return _error(cli_error(proc.stderr, "fused cloud logout failed"))
    return status_payload(probe=False)
