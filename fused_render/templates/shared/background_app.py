"""A background daemon's client for the background-apps API, about ITSELF
(SPEC.md §46 / D505).

Every endpoint under `/api/apps/background/*` (`server/routers/background_apps.py`)
keys off `html` — the page's own path — and resolves the app folder from it
server-side (`_folder_for` there does `os.path.dirname(os.path.abspath(html))`).
A background daemon spawned by `engine_host.ensure_background` has no page and
no `html` path of its own, so before this module it had no way to ask the
server about itself at all: it could not check whether it is still enabled,
tell the server to stop it, or turn itself off.

The missing piece is `FUSED_RENDER_APP_DIR` — exported into a `kind="background"`
child's environment only (`engine_host.py`'s `_spawn_env`, alongside
`Child.folder`) — the app folder the daemon's own manifest declared. This
module reads that var, synthesizes a stand-in `html` path inside the folder
(the server only ever takes its `dirname`, so the leaf name is arbitrary —
`"index.html"` is used because that is what a real page would be called), and
speaks the same endpoints a page's `fused.app.*` calls speak.

**Stdlib only, no `import fused_render`** — same constraint as `fused_ai.py`
and `appenv.py` beside this file (see the `adding-a-shared-template-utility`
skill): a background daemon runs in its own project's venv with `PYTHONPATH`
stripped, and can be this app's own `sys.executable` too, but must never
depend on the package being importable either way.

**Origin resolution mirrors `fused_ai.resolve_origin` exactly** (not
reinvented): `appenv.origin()` first (set for every process the server
spawned — a background daemon always qualifies), else a connect-probed
`server.json` under the shell home dir, else `ServerNotRunning`. Kept as a
second small copy rather than importing `fused_ai.py` itself: that module
pulls in the whole AI job-polling surface (`_IMAGE_WIRE_KEYS` and friends)
for a caller that wants three tiny endpoints, and this file's whole point is
to be safely vendorable on its own, the same as `fused_ai.py` is.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse


def _load_sibling_appenv():
    """Load THIS file's own `appenv.py`, by path — not `import appenv`.

    `templates/shared` is APPENDED (not inserted first) onto a user module's
    `sys.path`, so a user's own same-named `appenv.py` beside their script is
    meant to win a bare `import appenv`. Loading by this file's own location
    sidesteps that order entirely, so this module always gets ITS sibling —
    see `fused_ai.py`'s identical helper and the skill doc it cites.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "appenv.py")
    spec = importlib.util.spec_from_file_location("_background_app_appenv", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


appenv = _load_sibling_appenv()

# Same probe budget as fused_ai.py: short, because a live server answers a
# TCP handshake in microseconds and a dead one should not make every call wait.
_PROBE_TIMEOUT_S = 0.35
_DEFAULT_TIMEOUT_S = 10.0

# The basename `server.export_app_env`'s neighbour writes under the shell home
# dir (`appenv.home_dir()` — already branch-resolved). Matches fused_ai.py's
# own constant of the same name.
SERVER_JSON_NAME = "server.json"

# The env var `engine_host._spawn_env` exports for a `kind="background"`
# child only — see that function's docstring. Named here too so a test can
# pin the two together without restating the string.
APP_DIR_ENV = "FUSED_RENDER_APP_DIR"

# Arbitrary leaf name for the synthetic `html` path the server-side endpoints
# require — they only ever take its `dirname()` (see module docstring), so
# any name would do; this one reads sensibly in a log line.
_STANDIN_HTML_NAME = "index.html"


class NotUnderEngine(Exception):
    """`FUSED_RENDER_APP_DIR` is unset — this process is not a background
    daemon `engine_host.ensure_background` spawned (or it is one running an
    older server build that predates this env var). Distinct from
    `ServerNotRunning`: this is "I don't know who I am", not "no server is
    reachable" — a caller needs to tell those apart.
    """


class ServerNotRunning(Exception):
    """No reachable fused-render server for this process. Same meaning as
    `fused_ai.ServerNotRunning`, kept as a distinct type per this module's
    docstring on why it does not import that module."""


class BackgroundAppError(Exception):
    """One failed call to `/api/apps/background/*` — the `{"error": message}`
    body `server/common._error` sends, off any non-2xx response."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


# ------------------------------------------------------------- origin lookup


def _server_json_path() -> str:
    return os.path.join(appenv.home_dir(), SERVER_JSON_NAME)


def _probe(origin: str, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """True iff a plain TCP connect to `origin`'s host:port succeeds — see
    `fused_ai._probe`, which this mirrors verbatim."""
    parsed = urlparse(origin)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_origin() -> str:
    """The origin (`http://host:port`) to call, resolved exactly the way
    `fused_ai.resolve_origin` resolves it (see that function's docstring for
    the full reasoning): `FUSED_RENDER_ORIGIN` via `appenv.origin()` first,
    else a connect-probed `server.json`, else `ServerNotRunning`."""
    origin = appenv.origin()
    if origin:
        return origin

    path = _server_json_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise ServerNotRunning(
            "no fused-render server is running: FUSED_RENDER_ORIGIN is unset "
            f"and {path} could not be read"
        ) from None

    origin = data.get("origin") if isinstance(data, dict) else None
    if not isinstance(origin, str) or not origin or not _probe(origin):
        raise ServerNotRunning(
            f"{path} names {origin!r}, but nothing answered there — the "
            "server that wrote it is not running any more"
        )
    return origin


def _self_html_path() -> str:
    """The synthetic `html` path standing in for this daemon's own page —
    raises `NotUnderEngine` when `FUSED_RENDER_APP_DIR` is unset."""
    app_dir = os.environ.get(APP_DIR_ENV)
    if not app_dir:
        raise NotUnderEngine(
            f"{APP_DIR_ENV} is not set: this process is not running as a "
            "background app daemon spawned by the fused-render engine "
            "(engine_host.ensure_background), so it has no app folder to "
            "act on. This is expected outside a `kind=\"background\"` "
            "daemon — a standalone script has no such folder either."
        )
    return os.path.join(app_dir, _STANDIN_HTML_NAME)


# ------------------------------------------------------------------ transport


def _request(method: str, path: str, body: dict | None = None,
             timeout: float = _DEFAULT_TIMEOUT_S):
    """One HTTP round trip against the resolved origin. Raises
    `ServerNotRunning`/`BackgroundAppError`. `X-Fused: 1` rides on every
    POST, same rule as `fused_ai._request` — required by every mutating
    endpoint in `routers/background_apps.py` (`_require_fused`)."""
    origin = resolve_origin()
    url = origin.rstrip("/") + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if method == "POST":
        headers["X-Fused"] = "1"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        message = e.reason
        try:
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                message = payload["error"]
        except (ValueError, UnicodeDecodeError):
            pass
        raise BackgroundAppError(message, status=e.code) from None
    except urllib.error.URLError as e:
        raise BackgroundAppError(str(e.reason)) from None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ----------------------------------------------------------------------- API


def status() -> dict:
    """`GET /api/apps/background/status` for THIS daemon's own app folder —
    `{"enabled", "running", "pid", "version", "engine_id"}`."""
    html = _self_html_path()
    return _request("GET", "/api/apps/background/status?html=" + quote(html))


def stop() -> dict:
    """`POST /api/apps/background/stop` for this app: kills the running
    daemon (this process) WITHOUT disabling it — pops it from
    `engine_host._children` so a proxied page call cannot silently revive
    it, but the startup-resurrection hook still brings it back on the next
    server start. Call this to end THIS process's life cleanly instead of a
    raw self-terminate: the caller should expect the process to exit shortly
    after this returns, killed by the server on the other end of this call."""
    html = _self_html_path()
    return _request("POST", "/api/apps/background/stop", body={"html": html})


def disable() -> dict:
    """`POST /api/apps/background/disable` for this app: stops the running
    daemon (this process) AND unpersists it from the enabled store, so it
    does NOT come back at the next server start, unlike `stop()`."""
    html = _self_html_path()
    return _request("POST", "/api/apps/background/disable", body={"html": html})


def restart() -> dict:
    """`POST /api/apps/background/restart` for this app — respawns the
    daemon. Included for symmetry with the router's own endpoint set; most
    callers want `stop()` or `disable()`."""
    html = _self_html_path()
    return _request("POST", "/api/apps/background/restart", body={"html": html})
