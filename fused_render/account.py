"""In-app Fused account: sign in / sign out through the `fused` CLI.

The Deploy surface (deploy.py) needs a signed-in Fused account before a
managed-env publish can work; until now the app punted the one-time
`fused cloud login` to a terminal. This router does it in place, porting the
flow app's proven mechanics (flow repo, app/src/server/cloud.ts + spec
spec/app/connect-fused.md). The normative contract is SPEC §27 (AC-1…AC-11),
with the design rationale in DECISIONS.md D111/D112:

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
  * **Environment setup is one CLI command, run as a tracked job.**
    POST /api/account/setup spawns `fused cloud setup --no-browser
    [--org O --env E] --env-name NAME` — the CLI does everything (waits for
    the managed env to provision, mints the data-plane API key into the
    local secrets store, writes the env into envs.json) — and returns a
    job_id; GET /api/account/setup reports {state, job_id, env_name, detail}
    with the CLI's own progress lines, and the client polls it (flow's job
    model). Gated on logged_in (409) so the interactive login flow stays in
    ONE place — /api/account/login — instead of setup silently starting its
    own; and because presence isn't proof, the sign-in is VERIFIED with one
    `cloud orgs` probe before spawning (an expired credential would
    otherwise buy ~5 minutes of doomed spinner). One job at a time;
    env-name defaults follow flow's convention (`fused` for the default
    managed env, `fused-<env>` otherwise).
  * **Env management is the CLI's own env group.** POST /api/account/envs/
    default|delete shell `fused env default NAME` / `fused env delete NAME
    --yes` — delete forgets the LOCAL pointer only (the CLI's semantics; no
    cloud teardown), and the UI copy says so.
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
import uuid

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.deploy import cli_status
from fused_render.fusedcli import (
    all_envs,
    child_env,
    cli_error,
    credentials_stamp,
    envs_file,
    fused_cli,
    fused_cloud_logged_in,
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
# Backstop on the whole setup job. The CLI's own provisioning wait defaults to
# 300s; this only catches a wedged child (e.g. credentials expired mid-flight
# and the CLI dropped into an interactive login wait it can never finish).
SETUP_JOB_TIMEOUT = 900.0
# `fused env default/delete` are local envs.json edits — no network.
ENV_CMD_TIMEOUT = 30.0

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


def _ensure_dead(proc: subprocess.Popen) -> None:
    """Escalate a terminated child to SIGKILL if it ignores SIGTERM.

    Every kill path must go through this (synchronously or on a daemon
    thread): a login child left merely SIGTERM'd could survive, keep its
    loopback callback server alive, and complete a late Auth0 round-trip —
    re-writing credentials after a logout, or racing a retried login.
    """
    try:
        proc.wait(KILL_GRACE)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass  # unkillable child; the caller proceeds — poll shows it live


def _terminate_login(login: _ActiveLogin, wait: bool) -> None:
    """SIGTERM the login child, then confirm death — inline when `wait`
    (logout must not proceed past a live child), else on a daemon thread
    (request paths shouldn't block on the grace period)."""
    login.proc.terminate()
    if wait:
        _ensure_dead(login.proc)
    else:
        threading.Thread(target=_ensure_dead, args=(login.proc,), daemon=True).start()


def _cancel_active_login(wait: float = 0.0) -> bool:
    """Terminate the in-flight login child, if any; True when one was live.

    With `wait` > 0, block until it is actually dead — logout passes
    KILL_GRACE so a late Auth0 callback can't land in a child that outlives
    the credential delete and re-write a JWT. With wait 0 the kill is
    confirmed on a background thread instead.
    """
    global _active
    with _LOCK:
        login, _active = _active, None
    if login is None or login.proc.poll() is not None:
        return False
    _terminate_login(login, wait=bool(wait))
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


# -- the one environment-setup job ------------------------------------------------


@dataclasses.dataclass
class _SetupJob:
    """The single tracked `fused cloud setup` child.

    `state` moves running → done|failed exactly once, written by the watcher
    thread AFTER `error` (plain attribute writes; the GIL makes each one
    atomic and readers only act on state, so the ordering suffices). `tail`
    is the merged stdout+stderr stream — the CLI writes progress to stderr
    and the final line to stdout, and interleaving them in one pipe keeps
    the order the user would see in a terminal.
    """

    job_id: str
    env_name: str
    proc: subprocess.Popen
    tail: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=80)
    )
    state: str = "running"
    error: str | None = None
    # Set before the child is killed on sign-out, so the watcher reports the
    # cancellation instead of a confusing mid-progress CLI line.
    canceled: bool = False


_SETUP_LOCK = threading.Lock()
_setup: _SetupJob | None = None


def _pump_tail(stream, job: _SetupJob) -> None:
    for raw in stream:
        line = raw.rstrip("\n")
        if line.strip():
            job.tail.append(line)
    stream.close()


def _watch_setup(job: _SetupJob, pump: threading.Thread) -> None:
    try:
        job.proc.wait(SETUP_JOB_TIMEOUT)
    except subprocess.TimeoutExpired:
        job.proc.kill()
        job.proc.wait()
        pump.join(2.0)
        if job.state == "running":  # a concurrent cancel already wrote failed
            job.error = f"`fused cloud setup` timed out after {int(SETUP_JOB_TIMEOUT)}s"
            job.state = "failed"
        return
    pump.join(2.0)  # let the pipe drain to EOF so the tail is complete
    if job.state != "running":
        return  # canceled — _cancel_setup_job already wrote the terminal state
    if job.canceled:
        job.error = "environment setup was canceled by signing out"
        job.state = "failed"
    elif job.proc.returncode == 0:
        job.state = "done"
    else:
        job.error = cli_error("\n".join(job.tail), "fused cloud setup failed")
        job.state = "failed"


def _cancel_setup_job() -> bool:
    """Kill a running setup job, if any; True when one was live.

    Sign-out cancels account-scoped work: a setup child left running would
    keep provisioning/minting with credentials the user just revoked, and its
    `running` record would 409 every new setup until the 900s backstop. The
    watcher reports the cancellation (job.canceled) instead of the CLI's last
    mid-progress line.
    """
    with _SETUP_LOCK:
        job = _setup
    if job is None or job.state != "running" or job.proc.poll() is not None:
        return False
    job.canceled = True
    # Write the terminal state HERE, not in the watcher: the watcher only
    # observes the death after SIGTERM grace + pipe drain (seconds), and a
    # `running` corpse would 409 a new setup started right after sign-out.
    # The watcher skips jobs already terminal.
    job.error = "environment setup was canceled by signing out"
    job.state = "failed"
    job.proc.terminate()
    threading.Thread(target=_ensure_dead, args=(job.proc,), daemon=True).start()
    return True


def _default_env_name(env: str | None) -> str:
    # Flow's convention (cloud.ts): plain `fused` for the default managed
    # env, `fused-<env>` otherwise.
    if not env or env == "default":
        return "fused"
    return f"fused-{env}"


def _run_env_cmd(args: list[str]) -> JSONResponse | None:
    """Run `fused env <args>`; None on success, else the error response."""
    cli = fused_cli()
    if cli is None:
        return _error(
            "the fused CLI is not available: install it from the account page or set "
            "FUSED_RENDER_FUSED_BIN"
        )
    try:
        proc = subprocess.run(
            [*cli.command, "env", *args],
            capture_output=True,
            text=True,
            timeout=ENV_CMD_TIMEOUT,
            env=child_env(cli),
        )
    except subprocess.TimeoutExpired:
        return _error(f"`fused env {args[0]}` timed out after {int(ENV_CMD_TIMEOUT)}s")
    except OSError as e:
        return _error(f"could not run the fused CLI ({cli.command[0]}): {e}")
    if proc.returncode != 0:
        return _error(cli_error(proc.stderr, f"fused env {args[0]} failed"))
    return None


# -- status ----------------------------------------------------------------------


def _probe_failure(message: str) -> dict:
    return {"ok": False, "admitted": None, "orgs": [], "error": message}


def _probe_orgs(cli) -> dict:
    """`fused cloud orgs` — the authoritative signed-in view: whether the
    account is admitted and which org/envs it can target. Unlike the
    presence-only `logged_in` signal this exercises the token (refreshing it
    if needed), so a stale credentials file surfaces here as ok=False with
    the CLI's own message. Takes the already-resolved CLI so one request
    resolves it exactly once (and a mid-request resolution flip can't NoneType
    this path)."""
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
        "logged_in": logged_in,
        "login_in_flight": login is not None,
        # A fingerprint of the credentials file (its mtime): lets the client
        # drop a cached `cloud orgs` probe when the credentials CHANGE — a
        # re-login as a different account, even one that never flips
        # logged_in false in this tab — rather than showing the prior
        # account's orgs (AC-8).
        "creds_stamp": credentials_stamp(),
        # The raw env store view for the management table (all backends, each
        # flagged hosted, plus the store's own default pointer). The deploy
        # picker's derived view stays deploy's own (GET /api/deploy/config).
        "store": all_envs(),
        "envs_file": envs_file(),
        "probe": None,
    }
    # The probe is pointless without credentials (it would just fail with
    # "not logged in") or without a CLI to run it.
    if probe and logged_in and cli["found"]:
        resolved = fused_cli()
        payload["probe"] = (
            _probe_orgs(resolved)
            if resolved is not None
            else _probe_failure("the fused CLI is no longer available")
        )
    return payload


# -- routes ----------------------------------------------------------------------


@router.get("/api/account/status")
def api_account_status(probe: str = "0", x_fused: str | None = Header(default=None)):
    # The plain status is a cheap local read (files + find_spec) and stays an
    # open GET like deploy's config. probe=1 EXECUTES — it spawns a
    # `fused cloud orgs` child making a real control-plane call — so it takes
    # the D36 guard: without it, any foreign page could loop no-preflight
    # cross-origin GETs and turn this server into a subprocess/network spammer.
    if probe == "1":
        guard = _require_fused(x_fused)
        if guard is not None:
            return guard
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
        # Kill-and-confirm (daemon thread): a merely-SIGTERM'd child could
        # keep its callback server alive and race a retried login.
        _terminate_login(login, wait=False)
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


@router.post("/api/account/setup")
def api_account_setup(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    org, env, env_name = body.get("org"), body.get("env"), body.get("env_name")
    if (org is None) != (env is None):
        return _error("'org' and 'env' go together — give both or neither")
    for field_name, value in (("org", org), ("env", env)):
        if value is not None and (not isinstance(value, str) or not value):
            return _error(f"'{field_name}' must be a non-empty string when given")
    if env_name is None:
        env_name = _default_env_name(env)
    if not isinstance(env_name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", env_name
    ):
        return _error("'env_name' must be a short name (letters, digits, . _ -)")
    cli = fused_cli()
    if cli is None:
        return _error(
            "the fused CLI is not available: install it from this page or set "
            "FUSED_RENDER_FUSED_BIN"
        )
    # Gate on login instead of letting `cloud setup` auto-run its own — the
    # interactive browser flow lives in ONE place (POST /api/account/login);
    # a setup child silently waiting on a sign-in URL nobody sees would just
    # burn its timeout.
    if not fused_cloud_logged_in():
        return _error("sign in to Fused first — environment setup needs a signed-in account", 409)
    # Presence isn't proof: an expired credential with a dead refresh token
    # passes the file check, and `cloud setup` would then drop into its own
    # invisible login wait (~5 min of spinner for a guaranteed failure). One
    # `cloud orgs` probe up front converts that into an immediate,
    # actionable 409.
    verified = _probe_orgs(cli)
    if not verified["ok"]:
        return _error(
            "your Fused sign-in could not be verified — sign in again before setting "
            f"up an environment ({verified['error']})",
            409,
        )

    global _setup
    with _SETUP_LOCK:
        if _setup is not None and _setup.state == "running":
            return _error(
                f"an environment setup is already running (job {_setup.job_id})", 409
            )
        args = [*cli.command, "cloud", "setup", "--no-browser"]
        if org and env:
            args += ["--org", org, "--env", env]
        args += ["--env-name", env_name]
        child = child_env(cli)
        # Progress must stream into `detail` while the job runs, not arrive in
        # one buffered lump at exit (same reason as login's URL capture).
        child["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=child,
            )
        except OSError as e:
            return _error(f"could not run the fused CLI ({cli.command[0]}): {e}")
        job = _SetupJob(job_id=uuid.uuid4().hex[:12], env_name=env_name, proc=proc)
        pump = threading.Thread(target=_pump_tail, args=(proc.stdout, job), daemon=True)
        pump.start()
        threading.Thread(target=_watch_setup, args=(job, pump), daemon=True).start()
        _setup = job
    return JSONResponse({"job_id": job.job_id, "env_name": env_name}, status_code=202)


@router.get("/api/account/setup")
def api_account_setup_status():
    with _SETUP_LOCK:
        job = _setup
    if job is None:
        return {"state": "idle", "job_id": None, "env_name": None, "detail": None}
    detail = job.error if job.state == "failed" else "\n".join(job.tail) or None
    return {"state": job.state, "job_id": job.job_id, "env_name": job.env_name, "detail": detail}


def _env_name_arg(body: dict) -> str | JSONResponse:
    """The validated env name from an envs/* body, or the 400 to return.

    Rejects flag-shaped names: the name lands in `fused env <verb> <name>`
    argv, where "--help" would be parsed as an option (click prints help and
    exits 0 — a silent no-op this endpoint would report as success)."""
    name = body.get("name")
    if not isinstance(name, str) or not name or name.startswith("-"):
        return _error("'name' must be an environment name (it cannot start with '-')")
    return name


@router.post("/api/account/envs/default")
def api_account_env_default(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = _env_name_arg(body)
    if isinstance(name, JSONResponse):
        return name
    err = _run_env_cmd(["default", name])
    if err is not None:
        return err
    return status_payload(probe=False)


@router.post("/api/account/envs/delete")
def api_account_env_delete(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = _env_name_arg(body)
    if isinstance(name, JSONResponse):
        return name
    # `fused env delete --yes` forgets the LOCAL pointer only — no cloud
    # teardown, no key revocation (the CLI's own semantics; the UI copy and
    # the confirm dialog say so).
    err = _run_env_cmd(["delete", name, "--yes"])
    if err is not None:
        return err
    return status_payload(probe=False)


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
    # Kill any in-flight login BEFORE deleting credentials, and wait for it:
    # a completed-after-logout child would otherwise re-write the JWT. A
    # running setup job is account-scoped work too — cancel it (no wait
    # needed: it can't resurrect the JWT, only finish minting a key the user
    # no longer wants). This runs BEFORE the CLI check: killing our own
    # children needs no CLI, and a sign-out attempt must never leave them
    # alive on any early-return path.
    _cancel_active_login(wait=KILL_GRACE)
    _cancel_setup_job()
    cli = fused_cli()
    if cli is None:
        return _error(
            "the fused CLI is not available, so its stored credentials can't be "
            "cleared from here (they live in the CLI's own store)"
        )
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
