"""Canvases: local development on top of legacy-workbench canvases.

The workflow (SPEC-pending; see PR description): list the account's canvases,
clone one to a local folder with `fused canvas pull`, let the user edit those
files (Claude Code, the explorer, any editor), and watch the folder and push
every change set back upstream with `fused canvas push` — the embedded
workbench iframe picks up the pushed change itself, no reload from this page.

Provider note: canvas commands authenticate with `fused login`
(~/.fused/credentials, Auth0 PKCE) — a DIFFERENT provider from the fused
CLI's own `fused cloud login` store (~/.openfused). Same CLI binary (the
`fused` package resolved by fusedcli.fused_cli()), different credential
stores; this module owns the `fused login` side. Unlike `cloud login
--no-browser`, plain `fused login` never prints its authorize URL — it opens
the browser itself and blocks on a localhost callback — so the login endpoint
here spawns the child and the client simply polls status until the
credentials file appears.

Sync model (per-file three-way, local wins ties): one watcher thread per
synced canvas fingerprints the clone folder (path/mtime/size walk) once a
second; any change arms a debounce, and after DEBOUNCE_S of quiet the whole
folder is pushed with `fused canvas push --canvas <name>` — which REPLACES
the remote UDF set. To keep that replace from clobbering concurrent
workbench edits, the watcher also keeps a BASE snapshot from the last sync
point (per-file md5s in <canvases_root>/.sync/<name>.json — outside the
clone so `pull --force` can't delete it) plus the last-seen REMOTE manifest
(collection.last_updated + per-UDF server body hashes, fetched by the
_fused_canvas_manifest.py shim; hashes are only ever compared
server-vs-server, never against local files). Every PULL_POLL_S — clean OR
dirty — the manifest is probed; if the remote moved: clean clone → CLI
`pull --force` (as before); dirty clone → MERGE: download the zip
(_fused_canvas_zip.py shim) and apply per file against the base — local
untouched → remote wins (including remote deletes), local changed → local
wins, local deleted → stays deleted (the push propagates it). The same
probe+merge runs right before every push, so a push never blindly replaces
a remote that moved since the last sync. Known blind window: an edit
landing on the remote between push-complete and the post-push re-probe is
absorbed into the new baseline (server hashes aren't computable locally) —
deliberate, converges on the next workbench save. With an external
FUSED_RENDER_FUSED_BIN there is no interpreter for the shims; sync degrades
to the previous behavior (zip `pull --dry-run` poll only while clean,
local-wins wholesale push). Pushes are serialized per canvas by
construction (one thread) and the fingerprint is re-taken right after clone
so the pull's own writes never echo into a push. `push_seq`/`pull_seq`/
`merge_seq` increment on each successful push/clean-pull/merge, surfaced in
status for observability/tests — the workspace page doesn't act on them.

Three stability guards on top of the merge (D339): (1) ECHO GUARD — a probed
remote whose per-UDF hashes exactly match a sync point this watcher already
superseded (kept in a small history ring, ECHO_WINDOW_S) is a stale writer
(e.g. a browser tab autosaving pre-push state over a fresh push); it is never
pulled down — a push is queued to re-assert local. (2) TRASH — every file a
merge or clean force-pull overwrites or deletes is first copied to
.sync/trash/<name>/<timestamp>/, pruned to the newest _TRASH_MAX snapshots,
so no sync decision is ever unrecoverable. (3) VALIDATION GATE — after a
merge applies remote files, `fused canvas validate` runs on the clone; a
per-file merge can mix canvas.toml from one side with source files from the
other and break cross-file invariants, and a failing result rolls the merge
back file-by-file (clone stays dirty, push re-asserts local) instead of
wedging the push in a permanent validation error.

No import of anything under fused_render.server (server includes this router —
keep it acyclic); the X-Fused guard is duplicated locally, same as other
shell/* routers.
"""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.fusedcli import child_env, cli_error, fused_cli, workbench_env

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
# Remote-change poll cadence (a `pull --dry-run` CLI call, so much slower
# than the local fingerprint walk). Only runs while the local clone is clean.
PULL_POLL_S = 10.0
# A remote state that exactly matches a sync point we already superseded is a
# STALE WRITER (e.g. a browser tab autosaving pre-push state over a fresh
# push), not a new edit — for this long after the sync point was superseded,
# such an echo is re-pushed over instead of pulled down. Past the window a
# matching state is treated as a deliberate revert and wins normally.
ECHO_WINDOW_S = 300.0
# Superseded sync points kept for echo detection.
_HISTORY_MAX = 5
# Snapshots kept per canvas in .sync/trash before pulls/merges overwrite or
# delete local files. Oldest pruned beyond this.
_TRASH_MAX = 20
# `fused canvas validate` on the merged clone — local CLI run, no network.
VALIDATE_TIMEOUT = 60.0
# A newly constructed _SyncManager treats the clone as clean only if every
# file's mtime is younger than this — i.e. it just came out of a
# `clone --force`. Anything older is unknown provenance (server restart,
# reopening an already-cloned canvas) and starts dirty instead.
_FRESH_WINDOW_S = 10.0

# Canvas names are `[a-zA-Z0-9_]` per the CLI's own push rule; enforcing it
# here also keeps the name safe as a path segment and an argv element.
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,128}$")

# Fused environment the canvases feature targets. One knob drives BOTH the
# workspace iframe URL and the CLI runs (`fused --env`, via the FUSED_ENV
# variable it reads) so the canvas the iframe shows is the same one the local
# clone syncs against. Unstable is the current default while embed-auth ships
# there first; FUSED_RENDER_WORKBENCH_URL still overrides the iframe URL alone.
# Resolved in fusedcli.workbench_env because the `fused` wrapper handed to
# Claude sessions bakes in the same default (D334) — one knob, one reader.
WORKBENCH_ENV = workbench_env()

# Cap on the per-line push-error transcript kept for the UI and the fix
# session — enough for any real validation report (one line per problem plus
# a summary), small enough that a CLI stack trace can't bloat every 2s
# status poll.
_ERROR_DETAIL_MAX = 50

_ENV_WEB_URLS = {
    "prod": "https://www.fused.io",
    "unstable": "https://unstable.fused.io",
    "stg": "https://staging.fused.io",
    "staging": "https://staging.fused.io",
    "dev": "http://localhost:3000",
}

WORKBENCH_BASE_URL = os.environ.get("FUSED_RENDER_WORKBENCH_URL") or _ENV_WEB_URLS.get(
    WORKBENCH_ENV, "https://unstable.fused.io"
)


def _cli_env(cli) -> dict[str, str]:
    """child_env plus the env target, so CLI runs hit the same environment
    the iframe shows."""
    env = child_env(cli)
    env["FUSED_ENV"] = WORKBENCH_ENV
    return env


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
    # Presence-only: the CLI refreshes an expired-but-refreshable token
    # silently; the CLI stays the authority at action time.
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
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_cli_env(cli),
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
        # know the browser flow completed.
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
            env = _cli_env(cli)
            env["PYTHONUNBUFFERED"] = "1"
            # The CLI's own login gives the browser only 30s before its
            # callback server dies (fused.workbench._auth), which loses real
            # human sign-ins. When the CLI is our own interpreter, run the
            # long-timeout driver instead (same PKCE flow via the package's
            # helpers); an external FUSED_RENDER_FUSED_BIN may not even be a
            # Python we can drive, so it keeps the CLI's login.
            if cli.external:
                command = [*cli.command, "workbench", "login"]
            else:
                driver = os.path.join(os.path.dirname(__file__), "_fused_login.py")
                command = [cli.command[0], driver]
            try:
                # Unlike `cloud login --no-browser`, plain `fused login` opens
                # the browser itself (webbrowser.open) and never prints the
                # authorize URL — so there is nothing to capture; the client
                # polls /api/canvases/status until logged_in flips.
                proc = subprocess.Popen(
                    command,
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
                encoding="utf-8",
                errors="replace",
                timeout=TOKEN_TIMEOUT,
                env=_cli_env(cli),
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


def _shim_list_command(cli) -> list[str] | None:
    """argv for the in-interpreter list shim, or None when the CLI is an
    external binary we can't drive as Python (fall back to `canvas list`)."""
    if cli.external:
        return None
    shim = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_fused_canvases_list.py"
    )
    return [sys.executable, shim]


# The shim pays the fused import cost, one list call, and one sign-image call
# per canvas that has an uploaded preview.
LIST_SHIM_TIMEOUT = 60.0


def _shim_manifest_command(cli) -> list[str] | None:
    """argv for the remote-manifest probe shim, or None when the CLI is an
    external binary we can't drive as Python (sync degrades to the legacy
    dry-run poll)."""
    if cli.external:
        return None
    shim = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_fused_canvas_manifest.py"
    )
    return [sys.executable, shim]


def _shim_zip_command(cli) -> list[str] | None:
    """argv for the zip-download shim (merge path), or None for an external
    CLI — same degradation as _shim_manifest_command."""
    if cli.external:
        return None
    shim = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_fused_canvas_zip.py"
    )
    return [sys.executable, shim]


def _safe_zip_member_relpath(member: str) -> str | None:
    """Same guard as the CLI's pull extractor: the merge path extracts server
    zips with our own code, so the CLI's sanitizer no longer protects us.
    Rejects directories, absolute paths, `..`, and drive-letter members."""
    norm = member.replace("\\", "/").strip()
    if not norm or norm.endswith("/") or norm.startswith("/"):
        return None
    parts = norm.split("/")
    if ".." in parts or parts[0].endswith(":"):
        return None
    return norm


def _rmtree_quiet(path: str) -> None:
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def _iso_epoch(value) -> float | None:
    """ISO-8601 timestamp (control-plane last_updated) → epoch seconds."""
    if not isinstance(value, str) or not value:
        return None
    import datetime

    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _list_entries():
    """(entries, error): each entry is {name, id, preview_url, updated_at}.

    Preferred path is the in-interpreter shim (ids, last_updated, and resolved
    preview image URLs — the CLI's `canvas list` prints bare names only); an
    external FUSED_RENDER_FUSED_BIN keeps the CLI path, degrading gracefully
    to nameplate cards without previews.
    """
    cli = fused_cli()
    if cli is None:
        return None, _no_cli_error()
    shim_cmd = _shim_list_command(cli)
    if shim_cmd is not None:
        try:
            proc = subprocess.run(
                shim_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=LIST_SHIM_TIMEOUT,
                env=_cli_env(cli),
            )
        except subprocess.TimeoutExpired:
            return None, _error(
                f"listing canvases timed out after {int(LIST_SHIM_TIMEOUT)}s", 502
            )
        except OSError as e:
            return None, _error(f"could not run the canvases list helper: {e}")
        if proc.returncode != 0:
            message = cli_error(proc.stderr or proc.stdout, "listing canvases failed")
            # Same expired-credentials sniff as _run_cli: fall back to the
            # sign-in flow instead of a dead error page.
            if (
                "re-authenticate" in message.lower()
                or "refresh your fused credentials" in message.lower()
            ):
                return None, _error(message, 401)
            return None, _error(message, 502)
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return None, _error(
                f"the canvases list helper printed something that wasn't JSON: "
                f"{proc.stdout.strip()[-200:]!r}",
                502,
            )
        entries = []
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if isinstance(name, str) and name:
                    entries.append(
                        {
                            "name": name,
                            "id": entry.get("id"),
                            "preview_url": entry.get("preview_url"),
                            "updated_at": _iso_epoch(entry.get("last_updated")),
                        }
                    )
        return entries, None
    proc, err = _run_cli(["workbench", "--format", "json", "canvas", "list"], LIST_TIMEOUT)
    if err is not None:
        return None, err
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None, _error(
            f"the fused CLI printed something that wasn't JSON: {proc.stdout.strip()[-200:]!r}",
            502,
        )
    # Normalize defensively: the CLI has printed both bare name lists and
    # object lists across versions.
    entries = []
    raw = data if isinstance(data, list) else data.get("canvases") if isinstance(data, dict) else None
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str):
                entries.append({"name": entry, "id": None, "preview_url": None, "updated_at": None})
            elif isinstance(entry, dict):
                name = entry.get("name") or entry.get("slug")
                if isinstance(name, str) and name:
                    entries.append(
                        {"name": name, "id": entry.get("id"), "preview_url": None, "updated_at": None}
                    )
    return entries, None


@router.get("/api/canvases/list")
def api_canvases_list(x_fused: str | None = Header(default=None)):
    # EXECUTES a control-plane call — guarded like whoami.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not _logged_in():
        return _error("not signed in to Fused — sign in first", 409)
    canvases, err = _list_entries()
    if err is not None:
        return err
    cloned = set()
    root = canvases_root()
    try:
        cloned = {
            d for d in os.listdir(root) if os.path.isfile(os.path.join(root, d, "canvas.toml"))
        }
    except OSError:
        pass
    for canvas in canvases:
        is_cloned = canvas["name"] in cloned
        canvas["cloned"] = is_cloned
        # Card metadata comes from the local clone (the CLI list is bare
        # names): UDF count = *.py files (widgets are .json, canvas.toml is
        # chrome), modified = newest file mtime. Null when not cloned.
        n_udfs = None
        mtime = None
        if is_cloned:
            n_udfs = 0
            for dirpath, dirs, files in os.walk(os.path.join(root, canvas["name"])):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in files:
                    if f.startswith("."):
                        continue
                    if f.endswith(".py"):
                        n_udfs += 1
                    try:
                        st = os.stat(os.path.join(dirpath, f))
                    except OSError:
                        continue
                    if mtime is None or st.st_mtime > mtime:
                        mtime = st.st_mtime
        canvas["n_udfs"] = n_udfs
        canvas["mtime"] = mtime
    return {"canvases": canvases}


@router.post("/api/canvases/create")
def api_canvases_create(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    if not _logged_in():
        return _error("not signed in to Fused — sign in first", 409)
    proc, err = _run_cli(["workbench", "canvas", "create", name], LIST_TIMEOUT)
    if err is not None:
        return err
    return {"ok": True, "name": name}


@router.post("/api/canvases/logout")
def api_canvases_logout(x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    # A completing browser login could rewrite the credentials file right
    # after we clear it — cancel it first.
    login = _reap_login()
    if login is not None:
        login.proc.terminate()
    # Pause every watcher for the CLI call (no push/pull spam using
    # about-to-be-cleared credentials) but don't stop them until logout
    # actually succeeds — a failed logout leaves the user signed in, and
    # stopping first would leave sync dead until a canvas is reopened.
    with _SYNC_LOCK:
        managers = list(_syncs.values())
    for manager in managers:
        manager.pause()
    proc, err = _run_cli(["workbench", "logout"], WHOAMI_TIMEOUT)
    if err is not None:
        # Still signed in — resume so sync keeps working until the user
        # retries. Resuming a manager we're about to stop (success path)
        # would let a briefly-unpaused thread start a push/pull against
        # already-cleared credentials, and block stop()'s join on it.
        for manager in managers:
            manager.resume(rebaseline=False)
        return err
    with _SYNC_LOCK:
        managers = list(_syncs.values())
        _syncs.clear()
    for manager in managers:
        manager.stop()
    return {"ok": True}


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
    proc, err = _run_cli(
        ["workbench", "canvas", "pull", name, "-o", target, "--force"], PULL_TIMEOUT
    )
    if manager is not None:
        # Only adopt the pulled content as the clean baseline on success — a
        # failed pull leaves the folder as it was, so any local edits pending
        # before this clone must stay dirty, not get silently orphaned.
        manager.resume(rebaseline=err is None)
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
        # Held by the watcher thread for the duration of any _push/
        # _pull_if_remote_changed call. pause() waits on it so it never
        # returns while one of those is still writing to `self.dir` — a
        # caller pausing to run its own CLI call (clone's re-pull) on the
        # same folder would otherwise race it.
        self._op_lock = threading.Lock()
        self.push_seq = 0
        self.last_push_at: float | None = None
        self.last_error: str | None = None
        # The push's FULL stderr lines when it failed, newest failure only.
        # cli_error() keeps one line — the right shape for a status pill, and
        # exactly wrong for validation failures, where the CLI prints one line
        # per problem ("node X has no source file", ...) and then a summary
        # counting them: the pill showed "failed with 4 error(s)" and threw
        # away the 4 lines that name the files. Kept verbatim so the fix
        # endpoint can hand them to a Claude session and the UI can list them.
        self.error_detail: list[str] = []
        # Set the instant a "Fix with Claude" session is spawned, cleared when
        # the recorder thread that follows it exits for ANY reason (D336
        # follow-up) — done, an exception, or the poll cap — never guessed
        # from transcript activity: a "no recent activity" read has a grace
        # window that's fine for a status badge and wrong for a lock, since a
        # slow tool call mid-fix would read as "finished". fix_lock makes the
        # check-then-spawn-then-set atomic across concurrent requests (two
        # workspace tabs); spawn_helper alone can run for seconds, wide open
        # for both to pass the guard before either had set the id.
        self.active_fix_run_id: str | None = None
        self.fix_lock = threading.Lock()
        self.pull_seq = 0
        self.last_pull_at: float | None = None
        self.merge_seq = 0
        # Sync-point state for the three-way merge: per-file md5s of the
        # clone at the last sync point, and the last-seen remote manifest.
        # Persisted OUTSIDE the clone dir (a CLI `pull --force` removes any
        # in-dir file that isn't in the bundle). None = unknown provenance:
        # merge classification degrades to local-wins until the next sync
        # point rebuilds it.
        self._base_path = os.path.join(canvases_root(), ".sync", f"{name}.json")
        self._base_files: dict[str, str] | None = None
        self._remote: dict | None = None
        # Superseded remote manifests ({"udfs": ..., "at": rotation time}),
        # newest last — the echo-guard's memory (see ECHO_WINDOW_S).
        self._history: list[dict] = []
        self.echo_seq = 0
        self.merge_rollback_seq = 0
        self._load_base()
        self._fingerprint = self._take_fingerprint()
        # A fresh manager (server restart, self-heal after a dropped
        # watcher, or just opening an already-cloned canvas again) has no
        # idea whether what's on disk was ever pushed — trusting it as the
        # clean baseline would silently orphan real unpushed edits made
        # while no watcher was running. But a manager created right after a
        # `clone --force` (the common case: opening a canvas the first
        # time) IS genuinely clean, and treating that as dirty would fire a
        # pointless push every time — the exact case the "no push without a
        # change" test guards. Tell them apart by file age: a clone's writes
        # are all seconds old; anything else has at least one file older
        # than that.
        now = time.time()
        fresh = all(now - mtime < _FRESH_WINDOW_S for mtime, _ in self._fingerprint.values())
        self._dirty_since: float | None = None if fresh else now
        self.push_state = "idle" if fresh else "pending"  # idle | pending | pushing | error
        self._last_pull_poll = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def pause(self) -> None:
        with self.pause_lock:
            self.pause_count += 1
        # Block until any push/pull already in flight finishes. pause_count
        # is now >0, so the watcher loop won't start a NEW one once it does.
        with self._op_lock:
            pass

    def resume(self, *, rebaseline: bool = True) -> None:
        with self.pause_lock:
            self.pause_count = max(0, self.pause_count - 1)
            if self.pause_count == 0 and rebaseline:
                # Whatever the pull wrote is the new baseline, not a change.
                # Callers pausing for something OTHER than "I just overwrote
                # this dir with known-good content" (logout, pausing around a
                # CLI call that might fail) must pass rebaseline=False — a
                # pending local edit from before the pause would otherwise be
                # silently adopted as clean and never pushed.
                self._fingerprint = self._take_fingerprint()
                self._dirty_since = None
                # The pulled content IS the remote content, so it's the new
                # merge base too. The remote manifest is dropped (not
                # probed here — request thread, no CLI call): the watcher
                # re-adopts a fresh probe as baseline on its next poll.
                self._base_files = self._take_file_hashes()
                self._rotate_remote(None)
                self._save_base()
                # A stale "pending"/"error" from before the pause would
                # otherwise stick forever: the UI keeps showing a queued
                # push, and the remote-pull leg of the watcher loop only
                # runs while push_state == "idle".
                self.push_state = "idle"
                self.error_detail = []

    def stop(self) -> None:
        # Join, not just signal: every subprocess call in the loop carries its
        # own timeout (PUSH_TIMEOUT/PULL_TIMEOUT), so this is bounded. Without
        # the join, a caller (logout, test teardown) can race ahead while the
        # thread is still mid-subprocess — e.g. logout clearing credentials
        # out from under an in-flight push, or (in tests) the thread's next
        # subprocess call landing in the NEXT test's env/log after monkeypatch
        # has moved on.
        self.stop_event.set()
        self.thread.join()

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

    # -- sync-point state (merge base + remote manifest) -------------------

    def _take_file_hashes(self) -> dict[str, str]:
        """Per-file md5 of the clone, same walk/skip rules as the
        fingerprint (relpaths are os-native, matching zip relpaths after
        os.path.join normalization on this platform)."""
        hashes: dict[str, str] = {}
        for root, dirs, files in os.walk(self.dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith("."):
                    continue
                path = os.path.join(root, f)
                try:
                    with open(path, "rb") as fh:
                        digest = hashlib.md5(fh.read()).hexdigest()
                except OSError:
                    continue
                hashes[os.path.relpath(path, self.dir)] = digest
        return hashes

    def _load_base(self) -> None:
        try:
            with open(self._base_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        files = data.get("files")
        remote = data.get("remote")
        if isinstance(files, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in files.items()
        ):
            self._base_files = files
        if isinstance(remote, dict) and isinstance(remote.get("udfs"), dict):
            self._remote = remote
        history = data.get("history")
        if isinstance(history, list):
            self._history = [
                h for h in history
                if isinstance(h, dict) and isinstance(h.get("udfs"), dict)
            ][-_HISTORY_MAX:]

    def _save_base(self) -> None:
        payload = json.dumps(
            {
                "files": self._base_files,
                "remote": self._remote,
                "history": self._history,
            }
        )
        tmp = self._base_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._base_path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self._base_path)
        except OSError:
            pass  # state stays in memory; rebuilt at the next sync point

    def _rotate_remote(self, new_remote: dict | None) -> None:
        """Replace the manifest baseline, remembering the superseded one for
        echo detection. Every _remote assignment after construction goes
        through here so the history can't silently miss a sync point."""
        old = self._remote
        if (
            old is not None
            and isinstance(old.get("udfs"), dict)
            and (new_remote is None or old.get("udfs") != new_remote.get("udfs"))
        ):
            self._history.append({"udfs": old["udfs"], "at": time.time()})
            del self._history[:-_HISTORY_MAX]
        self._remote = new_remote

    def _is_stale_echo(self, probe: dict) -> bool:
        """True when the probed remote state is byte-identical (per-UDF
        hashes) to a sync point this watcher already superseded within
        ECHO_WINDOW_S — the signature of a stale writer (a browser tab
        autosaving pre-push state over a fresh push). Layout-only echoes
        can't be told apart from real layout edits (collection.last_updated
        is a fresh stamp either way) — only the destructive UDF-level
        revert is caught, which is the data-loss case."""
        cur = self._remote
        if cur is None or probe.get("udfs") == cur.get("udfs"):
            return False
        now = time.time()
        return any(
            now - entry.get("at", 0.0) <= ECHO_WINDOW_S
            and entry.get("udfs") == probe.get("udfs")
            for entry in self._history
        )

    def _new_trash_dir(self) -> str | None:
        """A fresh timestamped folder under .sync/trash/<name>/ for the files
        the current pull/merge is about to overwrite or delete; prunes the
        oldest snapshots beyond _TRASH_MAX. None if it can't be created."""
        root = os.path.join(canvases_root(), ".sync", "trash", self.name)
        path = os.path.join(root, time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}")
        try:
            os.makedirs(path, exist_ok=True)
            snapshots = sorted(
                d for d in os.listdir(root)
                if os.path.isdir(os.path.join(root, d))
            )
            for stale in snapshots[:-_TRASH_MAX]:
                _rmtree_quiet(os.path.join(root, stale))
        except OSError:
            return None
        return path

    def _backup_to(self, trash: str | None, rel: str, data: bytes) -> None:
        if trash is None:
            return
        dest = os.path.join(trash, rel)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
        except OSError:
            pass  # best-effort safety net, never blocks the sync

    def _snapshot_clone(self) -> None:
        """Copy the whole clone into a trash snapshot — run before the CLI's
        wholesale `pull --force`, whose overwrites/deletes we can't
        intercept per file."""
        trash = self._new_trash_dir()
        if trash is None:
            return
        for root, dirs, files in os.walk(self.dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith("."):
                    continue
                path = os.path.join(root, f)
                try:
                    with open(path, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                self._backup_to(trash, os.path.relpath(path, self.dir), data)

    def _validate_clone(self) -> bool | None:
        """`fused canvas validate` on the clone: True = valid, False =
        invalid, None = couldn't run (no CLI/timeout) — treated as valid so
        a broken CLI can't wedge every merge into a rollback."""
        cli = fused_cli()
        if cli is None:
            return None
        try:
            proc = subprocess.run(
                [*cli.command, "workbench", "canvas", "validate", self.dir],
                capture_output=True, text=True, timeout=VALIDATE_TIMEOUT,
                encoding="utf-8", errors="replace",
                env=_cli_env(cli),
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        return proc.returncode == 0

    def _probe_remote(self) -> dict | None:
        """The remote manifest via the shim, or None (external CLI, not
        signed in, transient failure, junk output — all mean 'can't tell',
        never 'changed')."""
        cli = fused_cli()
        if cli is None:
            return None
        cmd = _shim_manifest_command(cli)
        if cmd is None:
            return None
        try:
            proc = subprocess.run(
                [*cmd, self.name],
                capture_output=True, text=True, timeout=LIST_SHIM_TIMEOUT,
                encoding="utf-8", errors="replace",
                env=_cli_env(cli),
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("udfs"), dict):
            return None
        if not isinstance(data.get("id"), str) or not data["id"]:
            return None
        return data

    @staticmethod
    def _remote_moved(prev: dict, cur: dict) -> bool:
        return prev.get("last_updated") != cur.get("last_updated") or prev.get(
            "udfs"
        ) != cur.get("udfs")

    def _download_zip(self, collection_id: str) -> zipfile.ZipFile | None:
        cli = fused_cli()
        if cli is None:
            return None
        cmd = _shim_zip_command(cli)
        if cmd is None:
            return None
        try:
            proc = subprocess.run(
                [*cmd, collection_id],
                capture_output=True, timeout=PULL_TIMEOUT,
                env=_cli_env(cli),
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0 or not proc.stdout:
            return None
        try:
            return zipfile.ZipFile(io.BytesIO(proc.stdout), "r")
        except zipfile.BadZipFile:
            return None

    def _merge_remote(self, probe: dict) -> None:
        """Apply remote changes into a DIRTY clone, per file, three-way.

        base = last sync point. For each bundle file: local untouched since
        base → remote wins; local changed → local wins (the debounced push
        publishes it); local deleted a file that's still in the bundle → it
        stays deleted (the push propagates the delete). Files in base but
        gone from the bundle are remote deletes: removed locally only if
        untouched. Without a base (unknown provenance) nothing is applied —
        local wins wholesale, exactly the pre-merge behavior.

        Every file the merge overwrites or deletes is first copied into a
        .sync/trash snapshot. After applying, the clone is validated
        (`fused canvas validate`): a per-FILE merge can mix canvas.toml
        from one side with source files from the other and break the
        cross-file invariants (a node whose source file is gone) — a state
        the push then rejects forever. If the merge broke the clone, it is
        rolled back file-by-file and the clone stays dirty: local wins
        wholesale, the push re-asserts it."""
        base = self._base_files
        if base is None:
            self._rotate_remote(probe)
            self._save_base()
            return
        zf = self._download_zip(probe["id"])
        if zf is None:
            return  # transient — the next poll retries with the same probe
        with zf:
            bundle: dict[str, bytes] = {}
            for info in zf.infolist():
                rel = _safe_zip_member_relpath(info.filename)
                if rel is None:
                    continue
                bundle[os.path.join(*rel.split("/"))] = zf.read(info.filename)
        new_base = dict(base)
        trash: str | None = None
        _MISSING = object()
        # (rel, previous file bytes or None if the merge created it,
        #  previous base entry or _MISSING) — enough to undo every write.
        rollback: list[tuple[str, bytes | None, object]] = []
        for rel, data in bundle.items():
            dest = os.path.join(self.dir, rel)
            remote_hash = hashlib.md5(data).hexdigest()
            try:
                with open(dest, "rb") as fh:
                    local_bytes: bytes | None = fh.read()
            except OSError:
                local_bytes = None
            local_hash = (
                hashlib.md5(local_bytes).hexdigest()
                if local_bytes is not None
                else None
            )
            if local_hash == remote_hash:
                new_base[rel] = remote_hash  # converged — refresh the base
                continue
            if local_hash is None:
                if rel in base:
                    continue  # local delete wins; push propagates it
                # new remote file, absent locally → create
            elif local_hash != base.get(rel):
                continue  # local edit wins; push publishes it
            if local_bytes is not None:
                if trash is None:
                    trash = self._new_trash_dir()
                self._backup_to(trash, rel, local_bytes)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(data)
            except OSError:
                continue
            rollback.append((rel, local_bytes, base.get(rel, _MISSING)))
            new_base[rel] = remote_hash
        for rel in list(new_base):
            if rel in bundle:
                continue
            # In base, gone from the bundle: remote deleted it. Remove
            # locally only if untouched since the sync point.
            dest = os.path.join(self.dir, rel)
            try:
                with open(dest, "rb") as fh:
                    local_bytes = fh.read()
            except OSError:
                local_bytes = None
            local_hash = (
                hashlib.md5(local_bytes).hexdigest()
                if local_bytes is not None
                else None
            )
            if local_hash is not None and local_hash == new_base[rel]:
                if trash is None:
                    trash = self._new_trash_dir()
                self._backup_to(trash, rel, local_bytes)
                try:
                    os.remove(dest)
                except OSError:
                    continue
                rollback.append((rel, local_bytes, base.get(rel, _MISSING)))
            del new_base[rel]
        if rollback and self._validate_clone() is False:
            # The merged mix is unpushable. Restore what the merge touched;
            # the clone stays (or becomes) dirty and the debounced push
            # re-asserts local wholesale. _remote still advances to the
            # probe so the same broken mix isn't re-attempted every poll.
            for rel, prev_bytes, prev_base in reversed(rollback):
                dest = os.path.join(self.dir, rel)
                try:
                    if prev_bytes is None:
                        os.remove(dest)
                    else:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with open(dest, "wb") as fh:
                            fh.write(prev_bytes)
                except OSError:
                    continue
                if prev_base is _MISSING:
                    new_base.pop(rel, None)
                else:
                    new_base[rel] = prev_base  # type: ignore[assignment]
            self.merge_rollback_seq += 1
            rollback = []
        self._base_files = new_base
        self._rotate_remote(probe)
        self._save_base()
        if rollback:
            self.merge_seq += 1
            self.last_pull_at = time.time()

    def _poll_remote(self, probe: dict) -> None:
        """Shim-backed poll leg: runs clean or dirty (unlike the legacy
        dry-run poll). Clean → CLI force pull; dirty → merge."""
        if self._remote is None:
            # First look (fresh manager, or right after clone/push dropped
            # it): adopt as baseline. A remote edit made before this first
            # look is indistinguishable from the sync point — same blind
            # spot the pre-merge sync had; converges on the next remote save.
            self._remote = probe
            self._save_base()
            return
        if not self._remote_moved(self._remote, probe):
            return
        if self._is_stale_echo(probe):
            # A stale writer resurrected a superseded revision — never pull
            # it down (that's how a fresh local file gets deleted); re-assert
            # local by queueing a push instead. _remote is NOT advanced: the
            # push's own success re-probe re-baselines past the echo.
            self.echo_seq += 1
            if self._dirty_since is None:
                self._dirty_since = time.time()
            if self.push_state == "idle":
                self.push_state = "pending"
            return
        if self._dirty_since is not None:
            self._merge_remote(probe)
            return
        if self.push_state != "idle":
            return
        # Clean clone: re-check that a local edit didn't land while the
        # probe ran (local wins — it'll merge on the next poll), then let
        # the CLI replace the folder wholesale.
        if self._take_fingerprint() != self._fingerprint:
            return
        cli = fused_cli()
        if cli is None:
            return
        # The CLI's --force overwrites/deletes without us seeing each file —
        # snapshot the clone first so nothing is ever unrecoverable.
        self._snapshot_clone()
        try:
            applied = subprocess.run(
                [*cli.command, "workbench", "canvas", "pull", self.name,
                 "-o", self.dir, "--force"],
                capture_output=True, text=True, timeout=PULL_TIMEOUT,
                encoding="utf-8", errors="replace",
                env=_cli_env(cli),
            )
        except (subprocess.TimeoutExpired, OSError):
            return
        if applied.returncode != 0:
            return
        self.pull_seq += 1
        self.last_pull_at = time.time()
        self._fingerprint = self._take_fingerprint()
        self._dirty_since = None
        self._base_files = self._take_file_hashes()
        self._rotate_remote(probe)
        self._save_base()

    def _push(self) -> None:
        self.push_state = "pushing"
        cli = fused_cli()
        if cli is None:
            self.push_state = "error"
            self.last_error = "the fused CLI is not available"
            self.error_detail = []
            return
        # Guard the wholesale replace: if the remote moved since the last
        # sync point, fold its changes in first (per-file, local wins ties)
        # so the push can't clobber a concurrent workbench edit.
        if self._remote is not None:
            probe = self._probe_remote()
            if (
                probe is not None
                and self._remote_moved(self._remote, probe)
                # A stale-writer echo must NOT be merged in — the push about
                # to run replaces it with local, which is the cure.
                and not self._is_stale_echo(probe)
            ):
                self._merge_remote(probe)
        # Baseline BEFORE the push: a save landing while the push runs must
        # re-arm the debounce, not vanish into the pushed snapshot. The file
        # hashes are taken at the same instant — they describe the snapshot
        # this push publishes, which becomes the merge base on success.
        self._fingerprint = self._take_fingerprint()
        self._dirty_since = None
        base_snapshot = self._take_file_hashes()
        try:
            proc = subprocess.run(
                [*cli.command, "workbench", "canvas", "push", self.dir, "--canvas", self.name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PUSH_TIMEOUT,
                env=_cli_env(cli),
            )
        except subprocess.TimeoutExpired:
            self.push_state = "error"
            self.last_error = f"`fused canvas push` timed out after {int(PUSH_TIMEOUT)}s"
            self.error_detail = []
            return
        except OSError as e:
            self.push_state = "error"
            self.last_error = f"could not run the fused CLI: {e}"
            self.error_detail = []
            return
        if proc.returncode != 0:
            self.push_state = "error"
            self.last_error = cli_error(proc.stderr or proc.stdout, "fused canvas push failed")
            # Everything the CLI printed, one entry per line, capped so a
            # pathological run can't grow the status payload without bound.
            # Includes the summary line last_error already shows — the reader
            # (UI list, fix prompt) wants the verbatim transcript, not a
            # de-duplicated one.
            lines = [ln.strip() for ln in
                     ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines()
                     if ln.strip()]
            self.error_detail = lines[:_ERROR_DETAIL_MAX]
            return
        self.push_seq += 1
        self.last_push_at = time.time()
        self.last_error = None
        self.error_detail = []
        self.push_state = "idle"
        # New sync point: the pushed snapshot is the merge base, and the
        # remote is re-probed so its post-push hashes become the manifest
        # baseline (a workbench edit landing inside this probe window gets
        # absorbed — the documented blind window). Probe failure → None →
        # the next poll adopts a fresh baseline.
        self._base_files = base_snapshot
        self._rotate_remote(self._probe_remote())
        self._save_base()

    def _pull_if_remote_changed(self) -> None:
        """Legacy downstream leg — external-CLI fallback only (no manifest
        shim). The shim-backed path is _poll_remote/_merge_remote.

        Only ever called with a CLEAN local clone (no unpushed edits), where
        local == remote is the steady state — so any diff a `pull --dry-run`
        reports means the REMOTE moved (edits made in the hosted workbench),
        and a `pull --force` brings them down. A local edit landing between
        the dry-run and the force pull is re-checked right before applying;
        when in doubt the local side wins (it will push, which replaces the
        remote set wholesale — last writer wins, local preferred).

        A local edit can still land WHILE `--force` itself is running — that
        window can't be closed with fingerprints (the pull's own writes and a
        concurrent local edit both just look like "the file changed"), so we
        ask the CLI instead: a `--dry-run` right after applying compares local
        against remote directly. Still up to date → clean, as before. Still a
        diff → something moved local away from what we just pulled; mark
        dirty (not clean) so the normal debounced push resolves it, local
        wins. A concurrent edit to a file the pull itself overwrote is lost
        either way — this only recovers the untouched-file case.
        """
        cli = fused_cli()
        if cli is None:
            return
        base = ["workbench", "canvas", "pull", self.name, "-o", self.dir]
        try:
            probe = subprocess.run(
                [*cli.command, *base, "--dry-run"],
                capture_output=True, text=True, timeout=PULL_TIMEOUT,
                encoding="utf-8", errors="replace",
                env=_cli_env(cli),
            )
        except (subprocess.TimeoutExpired, OSError):
            return
        if probe.returncode != 0:
            return  # transient (network, auth) — the next poll retries
        # canvas_pull.py's up-to-date sentinel; anything else is a diff.
        if "already up to date" in (probe.stdout or ""):
            return
        # Re-check: did a local edit land while the dry-run ran? Local wins.
        if self._take_fingerprint() != self._fingerprint:
            return
        try:
            applied = subprocess.run(
                [*cli.command, *base, "--force"],
                capture_output=True, text=True, timeout=PULL_TIMEOUT,
                encoding="utf-8", errors="replace",
                env=_cli_env(cli),
            )
        except (subprocess.TimeoutExpired, OSError):
            return
        if applied.returncode != 0:
            return
        self.pull_seq += 1
        self.last_pull_at = time.time()
        # Did local diverge from remote again during the force pull? If so,
        # local wins — queue a push instead of baselining as clean.
        try:
            recheck = subprocess.run(
                [*cli.command, *base, "--dry-run"],
                capture_output=True, text=True, timeout=PULL_TIMEOUT,
                encoding="utf-8", errors="replace",
                env=_cli_env(cli),
            )
        except (subprocess.TimeoutExpired, OSError):
            recheck = None
        self._fingerprint = self._take_fingerprint()
        still_diff = recheck is not None and recheck.returncode == 0 and (
            "already up to date" not in (recheck.stdout or "")
        )
        self._dirty_since = time.time() if still_diff else None
        if still_diff and self.push_state != "error":
            self.push_state = "pending"

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
                with self._op_lock:
                    self._push()
                continue
            if time.time() - self._last_pull_poll >= PULL_POLL_S:
                self._last_pull_poll = time.time()
                cli = fused_cli()
                if cli is not None and _shim_manifest_command(cli) is not None:
                    # Manifest probe: runs clean OR dirty (the merge makes a
                    # dirty-time remote change safe to fold in). Probing is
                    # read-only, so it stays outside _op_lock.
                    probe = self._probe_remote()
                    if probe is not None:
                        with self._op_lock:
                            self._poll_remote(probe)
                elif self._dirty_since is None and self.push_state == "idle":
                    # External CLI (no shims): legacy dry-run poll, clean only.
                    with self._op_lock:
                        self._pull_if_remote_changed()

    def status(self) -> dict:
        return {
            "name": self.name,
            "dir": self.dir,
            "watching": not self.stop_event.is_set(),
            "push_state": self.push_state,
            "push_seq": self.push_seq,
            "last_push_at": self.last_push_at,
            "pull_seq": self.pull_seq,
            "last_pull_at": self.last_pull_at,
            "merge_seq": self.merge_seq,
            "merge_rollback_seq": self.merge_rollback_seq,
            "echo_seq": self.echo_seq,
            "error": self.last_error,
            "error_detail": list(self.error_detail),
            "fix_active": self.active_fix_run_id is not None,
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


def _fix_prompt(name: str, detail: list[str], error: str | None) -> str:
    """The first message of a fix session: the verbatim CLI output (never
    reworded — rewording makes an error unsearchable, D328) plus what the
    session must and must not do. The no-push rule matters most: the watcher
    auto-pushes this folder on every quiet period, so a session that pushes
    by hand races it, and a session that pushes --no-validate defeats the
    reason it was spawned."""
    report = "\n".join(detail) or (error or "the push failed")
    return (
        f"The automatic `fused workbench canvas push` for the canvas "
        f"{name!r} (this folder) is failing. The CLI reported:\n\n"
        f"{report}\n\n"
        "Fix these problems in this folder's files. Check your work with "
        "`fused workbench canvas validate .` until it passes. Do NOT run "
        "`fused workbench canvas push` (with or without --no-validate) and "
        "do not change the canvas name: fused-render watches this folder "
        "and pushes automatically as soon as the files change."
    )


@router.post("/api/canvases/fix")
def api_canvases_fix(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Spawn a detached Claude session on the canvas clone, primed with the
    failing push's own output (D336). Mirrors the apps API's session spawn
    (routers/apps.py): the fork-safe helper, permission mode "auto" for the
    same reason given there (nobody is polling `decide` until the page
    attaches, so "prompt" would park the first tool call for an hour), and a
    recorder thread so the run lands in the folder's session sidecar. The
    caller attaches its chat iframe with the returned run_id.

    `active_fix_run_id` guards against a concurrent second fixer on the same
    clone — set the instant this spawn succeeds, cleared when the recorder
    thread that follows it exits for any reason, never by a guess from
    transcript activity. `fix_lock` covers the check-then-spawn-then-set
    itself: spawn_helper's subprocess can run for seconds, wide open for two
    concurrent requests (two workspace tabs) to both read "no active fix"
    before either had set it."""
    from fused_render import claude_spawn

    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    manager = _sync_manager(name, create=False)
    if manager is None or manager.push_state != "error":
        # Nothing to fix: the button only renders on an error, so reaching
        # this means the push recovered between the click and the POST.
        return _error("this canvas has no failing push to fix", 409)
    with manager.fix_lock:
        if manager.active_fix_run_id is not None:
            return _error("a fix is already running for this canvas", 409)
        prompt = _fix_prompt(name, manager.error_detail, manager.last_error)
        try:
            res = claude_spawn.spawn_helper(manager.dir, prompt, "auto")
        except Exception as exc:  # noqa: BLE001 — spawn failure is the answer
            return _error(f"failed to start Claude session: {exc}", 500)
        if res.get("error") or not res.get("run_id"):
            return _error(str(res.get("error") or "failed to start Claude session"), 500)
        run_id = str(res["run_id"])
        manager.active_fix_run_id = run_id

    def _run_recorder() -> None:
        # load_agent() runs HERE, inside the background thread, rather than
        # eagerly as a Thread(args=...) value in the request thread — if it
        # raised there, active_fix_run_id would already be set with nothing
        # left to clear it. The try/finally is what actually closes that
        # hole: it clears the lock on the ordinary "done" exit AND on every
        # abnormal one (an exception here, or record_session_when_ready
        # hitting its poll cap without ever seeing done) — one path for all
        # of them, rather than only the callback firing on success.
        try:
            claude_spawn.record_session_when_ready(claude_spawn.load_agent(), run_id)
        except Exception:  # noqa: BLE001 — bookkeeping only, never re-raise
            pass
        finally:
            if manager.active_fix_run_id == run_id:
                manager.active_fix_run_id = None

    threading.Thread(
        target=_run_recorder, daemon=True, name="fused-canvas-fix-record",
    ).start()
    return {"ok": True, "run_id": run_id}


@router.get("/api/canvases/sync/status")
def api_canvases_sync_status(name: str = ""):
    if not _NAME_RE.fullmatch(name or ""):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    manager = _sync_manager(name, create=False)
    if manager is None:
        return {"name": name, "watching": False, "push_state": "idle", "push_seq": 0,
                "last_push_at": None, "pull_seq": 0, "last_pull_at": None,
                "error": None, "error_detail": [], "dir": _canvas_dir(name),
                "fix_active": False}
    return manager.status()
