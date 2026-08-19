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

import contextlib
import dataclasses
import hashlib
import io
import json
import logging
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

logger = logging.getLogger(__name__)

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

# How long the manual-push endpoint waits for the watcher's _op_lock before
# giving up with a 409. Short on purpose: the caller is a Claude session that
# just asked to publish, and "a sync operation is in flight, try again" is a
# better answer than a request that hangs for the whole PUSH_TIMEOUT. The
# watcher's own legs are what hold this lock, and they finish in seconds
# outside a stuck network call.
MANUAL_PUSH_LOCK_WAIT_S = 5.0

# Fused environment the canvases feature targets. One knob drives BOTH the
# workspace iframe URL and the CLI runs (`fused --env`, via the FUSED_ENV
# variable it reads) so the canvas the iframe shows is the same one the local
# clone syncs against. Prod is the default; set FUSED_RENDER_WORKBENCH_ENV=
# unstable to point canvases back at the unstable environment.
# FUSED_RENDER_WORKBENCH_URL still overrides the iframe URL alone.
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
    WORKBENCH_ENV, "https://www.fused.io"
)


def _cli_env(cli) -> dict[str, str]:
    """child_env plus the env target, so CLI runs hit the same environment
    the iframe shows — and the marker that stops a CLI child of OURS from
    being re-routed back into this server.

    Every fused CLI run this module makes goes through here, which is why the
    marker lives here rather than at the push site alone: none of these children
    (push, pull, validate, the manifest/zip shims) should ever be intercepted,
    and one chokepoint cannot be forgotten at a new call site.

    Without it the sync manager's own push ate its own tail. `_push` runs
    `[*cli.command, "workbench", "canvas", "push", …]`, and on the shim path
    `cli.command` IS `[sys.executable, _fused_cli.py]` — the file that performs
    the interception. So the push POSTed back to /api/canvases/sync/push, was
    refused because a push was already running (itself), and recorded that
    refusal as a CLI failure: push_state "error" with push_seq stuck at 0.
    """
    env = child_env(cli)
    env["FUSED_ENV"] = WORKBENCH_ENV
    env[_canvas_push_internal_env()] = "1"
    return env


def _canvas_push_internal_env() -> str:
    """The reentrancy marker's name, from the module that reads it, so the two
    ends cannot drift. Imported lazily: _canvas_push is stdlib-only by design
    and this keeps the dependency one-directional at import time."""
    from fused_render._canvas_push import INTERNAL_ENV

    return INTERNAL_ENV


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


def _busy(message: str) -> JSONResponse:
    """A "someone else has this folder right now, try again" refusal.

    Distinct from `_error` by the `code`, because the two mean opposite things
    to a caller: a push FAILURE is about the canvas (validation, auth) and wants
    fixing, while a busy refusal is about timing and wants retrying. The
    interception in _canvas_push.py reports them differently for exactly that
    reason, and nothing here records a busy refusal as sync state.
    """
    return JSONResponse({"error": message, "code": "busy"}, status_code=409)


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


# Files seeded into every clone for the Claude session working there —
# invisible to the sync in BOTH directions: excluded from the watcher's
# fingerprint/hash walks (below) so they never dirty the clone or enter the
# merge base, and listed in .fusedignore so `canvas push` never uploads them.
# The CLI's `pull --force` deletes them (they're not in the bundle), so every
# pull path re-seeds afterwards.
_SYNC_IGNORED_BASENAMES = frozenset({"CLAUDE.md", ".fusedignore"})

_CLONE_FUSEDIGNORE = "CLAUDE.md\n.fusedignore\n"

_CLONE_CLAUDE_MD = """\
# Fused canvas clone: {name}

This folder is a live clone of the Fused canvas **{name}**, two-way synced by
fused-render: a watcher pushes every quiet change set upstream and pulls
remote edits (made in the hosted workbench) back down, merging per file.

## How the sync works (know this before running sync commands yourself)

A watcher in the fused-render app syncs this folder continuously:

- **Auto-push**: after ~1.5s of file quiet it pushes this folder, which
  REPLACES the remote UDF set (deletes propagate). Before pushing it probes
  the remote and merges concurrent workbench edits in, per file. It is
  HELD while you are working — see "Publishing your work" below.
- **Auto-pull**: every ~10s it checks the remote; workbench edits are
  pulled down (clean clone) or merged per file (dirty clone — a file only
  you changed keeps your version; only they changed gets theirs; both →
  yours wins).

## Publishing your work

While you are working here the auto-push is **held** — the debounce measures
file quiet, and you go quiet mid-change-set (thinking, reading, waiting on a
tool), so shipping then would publish a half-done rename. You publish
deliberately instead:

    fused workbench canvas push .

That is the right command and it is safe here. Inside this folder fused-render
intercepts it and runs the push through its own sync manager, which first
probes the remote and merges any concurrent workbench edit in, and aborts
rather than overwriting something it cannot reconcile. You get the CLI's real
output back: if validation fails, the errors are printed here, one line per
problem — fix them and push again.

Push when a change set is COHERENT (e.g. a rename: the `.py` file *and* its
`canvas.toml` entry), not after every edit.

- If you never push, nothing is lost: the watcher pushes the folder on its
  next tick once you stop working.
- `--no-validate` / `--no-ignore` are refused here. Fix validation errors
  rather than skipping them.
- Do NOT try to work around the interception by invoking a fused from
  somewhere else (see "Running the fused CLI" below) — a raw push skips the
  merge guard and can destroy a workbench edit the user made seconds ago,
  unrecoverably.
- `fused workbench canvas pull --force` overwrites the WHOLE folder with
  remote: any unpushed local edits are lost, and it deletes this CLAUDE.md /
  .fusedignore (the watcher re-seeds them on its own pulls). You rarely need
  it — the auto-pull already brings remote edits down.
- After a manual pull, the watcher may see the changed files as fresh
  local edits and push them back — expected, mention it if surprising.

## Running the fused CLI

Always invoke it as the bare command — `fused ...` — and nothing else. That
resolves to the CLI this app ships, which is the only one that works here.

Do NOT `pip install fused`, do NOT run `python -m fused`, and do NOT call a
`fused` from any other path, venv or environment. A different fused misses the
pieces this folder's two-way sync depends on, and it bypasses the push
protection described above.

## Keeping the canvas valid

- After structural edits (renaming a node, adding/removing nodes or edges),
  check the clone with `fused workbench canvas validate .` — an invalid
  canvas is rejected at push time and the error surfaces to the user.
- `canvas.toml` defines the canvas (nodes, edges, viewport); every node
  needs its source file next to it (`<udfName>.py`; widgets are `.json`).

## Files can change between change sets — trust the filesystem, not your memory

The user may be editing this same canvas in the hosted workbench while you
work. Their edits do NOT land here while you are mid-change-set: the sync
holds its downstream pull for as long as your session is running. They land
at the next pull — which is also step 1 of your push — so this folder is
stable *within* a change set and can differ *between* them. Consequences:

- Re-read a file (Read tool) before editing it if you have pushed since you
  last looked, or if you are not sure. Your memory can be stale across a
  push, and an Edit whose old text no longer matches means the file changed
  underneath: re-read and re-apply, don't force it.
- Never reconstruct or rewrite a whole file from memory (Write over it) —
  that silently discards remote edits a pull merged in. Prefer targeted
  Edits against freshly read content.
- The sync's rules on concurrent changes: a file only you touched keeps
  your version; a file only the workbench touched gets theirs; both →
  yours wins. Because their edits arrive at push time now, a same-file
  collision surfaces there rather than mid-edit; `canvas.toml` is one file,
  so your structural edit can override their concurrent layout tweak —
  mention it if you notice.
- A file that unexpectedly disappeared or reverted was likely changed
  remotely. Before recreating it, check the state on disk and say what you
  found; overwritten/deleted versions are recoverable from
  `../.sync/trash/<canvas>/<timestamp>/` (newest last).
- Group related multi-file changes (e.g. a rename: the `.py` file AND its
  `canvas.toml` entry) into one change set and push it whole — a half-done
  rename that gets pushed or merged mid-way is exactly how invalid states
  happen.

## Skills

Load these before editing — they carry the format references and workflows
for this folder. fused-render hands them to this session itself, so they
should already be in your available-skills list:

- `workbench:canvas-toml` — canvas.toml format and folder layout
- `workbench:fused-udfs` — writing Fused UDFs
- `workbench:json-ui-schemas` — widget JSON component props
- `workbench:fused-cli` — the fused CLI reference
- `workbench:canvas-comments` — reading and resolving canvas comments

If they are absent, or listed under a different prefix, just search your
available skills for the matching names. If they genuinely are not there,
carry on without them: follow the conventions of the files already in this
folder and treat the existing `canvas.toml` as the format reference. Do not
stop to ask for them, and do not try to install anything.
"""


def _seed_clone_claude_files(target: str, name: str) -> None:
    """Best-effort: (re)write CLAUDE.md + .fusedignore into a clone. Runs
    after clone and after every CLI force pull (which deletes them).

    Skipped for an external FUSED_RENDER_FUSED_BIN: that path syncs via
    `pull --dry-run`, which reports the seeded local-only files as a diff
    on every poll — a permanent pull/reseed churn (until the CLI's
    _PULL_DELETE_IGNORE_BASENAMES learns these names)."""
    cli = fused_cli()
    if cli is None or _shim_manifest_command(cli) is None:
        return
    for basename, content in (
        ("CLAUDE.md", _CLONE_CLAUDE_MD.format(name=name)),
        (".fusedignore", _CLONE_FUSEDIGNORE),
    ):
        try:
            with open(os.path.join(target, basename), "w", encoding="utf-8") as f:
                f.write(content)
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
    # Fetch/refresh the `workbench` skills, whose names the CLAUDE.md seeded
    # below uses (`workbench:canvas-toml` and friends), and publish the root for
    # the session in the right pane. THIS is the moment for it: the canvases path
    # is the only place those skills are ever handed out, so a user who never
    # opens a canvas pays nothing, a skills release made since startup is picked
    # up without a server restart, and the network work happens on a request
    # rather than before the server's bind (see server/app.py).
    #
    # Bounded and swallowed both: git may be missing, the network may be gone,
    # and a canvas clone must still succeed — the session then simply gets no
    # second --plugin-dir and the CLAUDE.md degrades to the folder's own
    # conventions.
    try:
        from fused_render.skill_plugin import sync_workbench_plugin

        sync_workbench_plugin()
    except Exception:  # noqa: BLE001 — never fail a clone over a skill fetch
        logger.debug("workbench skills sync failed", exc_info=True)
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
    if err is None:
        # Seed BEFORE resume's rebaseline (harmless either way — both files
        # are excluded from the fingerprint) and after the CLI pull, which
        # deletes any previous seed (not in the bundle).
        _seed_clone_claude_files(target, name)
    if manager is not None:
        # Only adopt the pulled content as the clean baseline on success — a
        # failed pull leaves the folder as it was, so any local edits pending
        # before this clone must stay dirty, not get silently orphaned.
        manager.resume(rebaseline=err is None)
    if err is not None:
        return err
    return {"ok": True, "dir": target}


# -- "is a Claude session editing this clone?" ---------------------------------
#
# One question, two consumers: the watcher suppresses its debounced auto-push
# while a session is mid-change-set, and the workspace makes the embedded
# workbench read-only so the user cannot edit the same canvas from the other
# pane at the same time.
#
# The answer is PID-BASED (agent.py's `_live_run`, whose liveness check is a live
# process), never transcript activity. The distinction is the whole reason
# `active_fix_run_id` is written the way it is, a few dozen lines below: a "no
# recent activity" read carries a grace window that is fine for a status badge
# and wrong for a lock, because a slow tool call mid-edit reads as "finished" and
# would unlock the workbench underneath a session that is still writing.
# `active_fix_run_id` itself is NOT reusable here — it only tracks fix sessions
# this module spawned, not a chat the user started in the right pane themselves.

# How long a liveness answer is reused. `_run()` refreshes it once a second on
# EVERY tick, clean clone or not, so the request thread behind
# `/api/canvases/sync/status` (polled every 2s) almost always finds a warm
# cache and never itself pays for a scan (a meta.json read per run dir,
# unbounded — see _live_run's `limit`; deliberately not result-capped, since a
# capped scan can miss a live run buried under newer ones and silently stop
# reporting it live). The TTL is kept a bit above the watcher's own 1s tick so
# a slow tick still leaves margin before the request thread's poll interval
# catches up and has to do the scan itself; the cost of being briefly stale is
# at worst one suppressed-then-allowed push, which the debounce tolerates.
AGENT_LIVE_CACHE_S = 3.0

_AGENT_MOD = None
_AGENT_MOD_TRIED = False
_AGENT_MOD_LOCK = threading.Lock()


def _agent_module():
    """The claude template's agent.py, loaded once, or None if it won't load.

    Reached through `claude_spawn.load_agent()`, which is the sanctioned seam for
    in-process READ paths — canvases.py must not import agent.py directly (it is
    a template, outside the package's import graph by design, SPEC PY-15).

    Cached because `load_agent` execs the whole module on every call, and this is
    on a once-a-second loop. A failure is cached too: if it cannot load now it
    will not load on the next tick either, and retrying it 60 times a minute
    would turn one broken import into a busy loop.
    """
    global _AGENT_MOD, _AGENT_MOD_TRIED
    with _AGENT_MOD_LOCK:
        if not _AGENT_MOD_TRIED:
            _AGENT_MOD_TRIED = True
            try:
                from fused_render import claude_spawn

                _AGENT_MOD = claude_spawn.load_agent()
            except Exception:  # noqa: BLE001 — no agent module is an answer
                logger.warning("could not load the claude agent module; canvas "
                               "sync cannot tell whether a session is live",
                               exc_info=True)
                _AGENT_MOD = None
        return _AGENT_MOD


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
        # True for the duration of a force-pull, three-way merge, or its
        # validation-failure rollback — set/cleared around the two _run() call
        # sites, both already under _op_lock. Read by the workspace lock
        # (canvas-lock-lib.ts "pulling" hold): the clone's files are moving on
        # disk right now, which is exactly the condition the push side of the
        # lock exists for too.
        self._pulling = False
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
        # Cached answer to "is a Claude session live in this clone?" — see
        # AGENT_LIVE_CACHE_S. Written by whichever of the watcher thread and a
        # status request asks first; a lost race just means one extra scan, so
        # the plain-attribute rule the class docstring states still holds.
        self._agent_run_id = ""
        self._agent_checked_at = 0.0
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

    def agent_run_id(self, *, fresh: bool = False) -> str:
        """The id of a Claude run live in this clone, or "" — cached.

        `limit=None` on purpose: the capped default scan can miss a live run
        buried under 60 newer run dirs, and nothing prunes RUNS. For a badge
        that miss is cosmetic; for the workbench lock it means silently not
        locking, so this caller pays for the reliable answer and caches it.
        """
        now = time.time()
        if not fresh and now - self._agent_checked_at < AGENT_LIVE_CACHE_S:
            return self._agent_run_id
        agent = _agent_module()
        run_id = ""
        if agent is not None:
            try:
                run_id = str(agent._live_run(self.dir, limit=None).get("run_id") or "")
            except Exception:  # noqa: BLE001 — a failed read must not stop the
                # watcher, and "cannot tell" is reported as "not live": the
                # alternative is a lock that never releases.
                logger.debug("live-run lookup failed for %s", self.dir, exc_info=True)
        self._agent_run_id = run_id
        self._agent_checked_at = now
        return run_id

    def _take_fingerprint(self) -> dict[str, tuple[float, int]]:
        fp: dict[str, tuple[float, int]] = {}
        for root, dirs, files in os.walk(self.dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith(".") or f in _SYNC_IGNORED_BASENAMES:
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
                if f.startswith(".") or f in _SYNC_IGNORED_BASENAMES:
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

    @staticmethod
    def _udf_hashes(udfs) -> dict:
        """slug → body hash, from a manifest's `udfs` mapping or a history
        entry's. Timestamps are deliberately dropped: a stale writer
        re-saving old bodies stamps FRESH per-UDF last_updated values, so
        any comparison that includes them can never match — the hashes are
        the identity."""
        if not isinstance(udfs, dict):
            return {}
        return {
            slug: (info.get("hash") if isinstance(info, dict) else info)
            for slug, info in udfs.items()
        }

    def _rotate_remote(self, new_remote: dict | None) -> None:
        """Replace the manifest baseline, remembering the superseded one for
        echo detection. Every _remote assignment after construction goes
        through here so the history can't silently miss a sync point."""
        old = self._remote
        if old is not None and isinstance(old.get("udfs"), dict):
            old_hashes = self._udf_hashes(old.get("udfs"))
            new_hashes = (
                None if new_remote is None else self._udf_hashes(new_remote.get("udfs"))
            )
            if old_hashes != new_hashes:
                self._history.append({"udfs": old_hashes, "at": time.time()})
                del self._history[:-_HISTORY_MAX]
        self._remote = new_remote

    def _is_stale_echo(self, probe: dict) -> bool:
        """True when the probed remote state is byte-identical (per-UDF
        BODY HASHES — never timestamps, see _udf_hashes) to a sync point
        this watcher already superseded within ECHO_WINDOW_S — the
        signature of a stale writer (a browser tab autosaving pre-push
        state over a fresh push). Layout-only echoes can't be told apart
        from real layout edits (collection.last_updated is a fresh stamp
        either way) — only the destructive UDF-level revert is caught,
        which is the data-loss case."""
        cur = self._remote
        if cur is None:
            return False
        probe_hashes = self._udf_hashes(probe.get("udfs"))
        if probe_hashes == self._udf_hashes(cur.get("udfs")):
            return False
        now = time.time()
        return any(
            now - entry.get("at", 0.0) <= ECHO_WINDOW_S
            and self._udf_hashes(entry.get("udfs")) == probe_hashes
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

    def _merge_remote(self, probe: dict) -> bool:
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
        wholesale, the push re-asserts it.

        Returns True when the remote state was reconciled (merged, rolled
        back, or nothing to do) and False on a transient failure (the zip
        download) — the caller must NOT push over an unreconciled remote."""
        base = self._base_files
        if base is None:
            self._rotate_remote(probe)
            self._save_base()
            return True
        zf = self._download_zip(probe["id"])
        if zf is None:
            return False  # transient — retried with the same probe
        with zf:
            bundle: dict[str, bytes] = {}
            for info in zf.infolist():
                rel = _safe_zip_member_relpath(info.filename)
                if rel is None:
                    continue
                # A seeded helper file that somehow reached the remote (a
                # push from elsewhere with --no-ignore) must not overwrite
                # the local seed.
                if rel.rsplit("/", 1)[-1] in _SYNC_IGNORED_BASENAMES:
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
        return True

    def _baseline_pending(self) -> bool:
        """Whether the next downstream poll would do nothing but ADOPT a
        baseline — the one part of the leg that still runs while a Claude session
        is live (see the watcher's hold).

        `_remote is None` is the whole condition on the shim path: `_poll_remote`
        takes its first-look branch and returns before it can touch a file. The
        shim check is not decoration — under an external CLI the legacy leg never
        sets `_remote` at all, so a bare "`_remote` is None" would read as
        "harmless first look" forever and leave that leg's force-pull ungated.
        """
        if self._remote is not None:
            return False
        cli = fused_cli()
        return cli is not None and _shim_manifest_command(cli) is not None

    @contextlib.contextmanager
    def _pull_writes(self):
        """Marks the window in which a downstream leg is actually WRITING the
        clone's files — the workspace lock (canvas-lock-lib.ts) holds the
        embedded workbench read-only for exactly this, and for the same reason a
        push does.

        Entered only past every probe-and-decide step (`_remote_moved`, the
        stale-echo check, the dirty/clean branch, the clean path's re-check):
        setting it any earlier meant a poll that found nothing still registered a
        full lock engagement that the 2s status poll could sample, i.e. a
        read-only flicker for a pull that never happened. `finally` on every
        path, including the merge's validation-failure rollback, because a
        `pulling` that never clears is a permanently read-only pane.
        """
        self._pulling = True
        try:
            yield
        finally:
            self._pulling = False

    def _poll_remote(self, probe: dict) -> None:
        """Shim-backed poll leg: runs clean or dirty (unlike the legacy
        dry-run poll). Clean → CLI force pull; dirty → merge."""
        if self._remote is None:
            # First look (fresh manager, or right after clone/push dropped
            # it): adopt as baseline. A remote edit made before this first
            # look is indistinguishable from the sync point — same blind
            # spot the pre-merge sync had; converges on the next remote save.
            self._remote = probe
            # No merge base yet (first open after upgrade — no .sync file)
            # and the watcher believes the clone is clean: local == remote
            # is the steady state, so the disk IS the sync point. Without
            # this the base stays None and every later merge degrades to
            # local-wins wholesale. A dirty clone is left alone — hashing
            # it would bless unpushed edits as "already synced".
            if self._base_files is None and self._dirty_since is None and self.push_state == "idle":
                self._base_files = self._take_file_hashes()
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
            with self._pull_writes():
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
        # Past every decision now, and about to overwrite the clone: this is
        # where the lock's `pulling` window starts, not at the top of the leg.
        with self._pull_writes():
            self._force_pull(probe, cli)

    def _force_pull(self, probe: dict, cli) -> None:
        """The clean-clone branch of `_poll_remote`, after it has committed to
        writing: CLI force pull, post-pull divergence recheck, re-baseline.

        Split out only so `_pull_writes` can wrap exactly the writing part —
        every line here either writes the clone or records what the write did.
        """
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
        # Did a local edit land WHILE --force was running? That window cannot
        # be closed with fingerprints — the pull's own writes and a concurrent
        # local edit both just look like "the file changed" — so ask the CLI,
        # exactly as the legacy leg does after its own force pull. Without
        # this the shim leg re-baselined unconditionally, so a file an active
        # session wrote mid-pull was overwritten AND adopted as the sync
        # point: the edit was lost with nothing left to notice it.
        #
        # Re-seed the Claude helper files ONLY AFTER this recheck, not before:
        # they are .fusedignore'd on push, so the remote bundle never contains
        # them, and the CLI's own delete-ignore list doesn't know their names
        # either — seeding first would make the dry-run report them as a
        # perpetual diff (`plan.deletes`), so `still_diff` would be True on
        # every single poll and the manager would go "pending" → push forever,
        # re-pushing the content it just pulled right back down each cycle.
        try:
            recheck = subprocess.run(
                [*cli.command, "workbench", "canvas", "pull", self.name,
                 "-o", self.dir, "--dry-run"],
                capture_output=True, text=True, timeout=PULL_TIMEOUT,
                encoding="utf-8", errors="replace",
                env=_cli_env(cli),
            )
        except (subprocess.TimeoutExpired, OSError):
            recheck = None
        still_diff = recheck is not None and recheck.returncode == 0 and (
            "already up to date" not in (recheck.stdout or "")
        )
        # The CLI's --force removed the seeded Claude helper files (they're
        # not in the bundle) — put them back now that the recheck has run.
        _seed_clone_claude_files(self.dir, self.name)
        self._fingerprint = self._take_fingerprint()
        # The remote genuinely IS at `probe` — we just pulled it — so rotate
        # either way; not rotating would make the next poll see the same move
        # again and re-run this destructive branch.
        self._rotate_remote(probe)
        if still_diff:
            # Local diverged from what was pulled. Local wins: go dirty so the
            # debounced push re-asserts it, and leave the merge base ALONE.
            # Hashing the diverged disk here would record the concurrent edit
            # as "already synced", and the next remote move would then classify
            # that file as local-untouched and let remote overwrite it — the
            # same clobber one step later. A stale base errs toward
            # "local changed", which is the module's tie policy.
            self._dirty_since = time.time()
            if self.push_state != "error":
                self.push_state = "pending"
            self._save_base()
            return
        self._dirty_since = None
        self._base_files = self._take_file_hashes()
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
                if not self._merge_remote(probe):
                    # The remote moved and could not be reconciled (zip
                    # download failed) — pushing now would wholesale-replace
                    # edits we haven't seen, the exact clobber the merge
                    # exists to prevent. Re-arm and retry after the debounce.
                    #
                    # This is a benign, retryable deferral, not a push
                    # failure — clear any STALE last_error/error_detail from
                    # an earlier failed push. They only clear on a successful
                    # push otherwise, so without this a merge-abort here would
                    # report last time's validation errors verbatim: the
                    # status endpoint's caller (and _fix_prompt, and the CLI
                    # interception's error_detail passthrough) would send the
                    # session to fix a problem this attempt never even
                    # encountered, one it may have already fixed.
                    self.push_state = "pending"
                    self.last_error = None
                    self.error_detail = []
                    self._dirty_since = time.time()
                    return
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
        # Committed to writing now — everything above was probe-and-decide, and
        # marking `pulling` for that made the lock engage on polls that turned
        # out to be no-ops.
        with self._pull_writes():
            self._apply_legacy_pull(cli, base)

    def _apply_legacy_pull(self, cli, base: list) -> None:
        """The writing half of `_pull_if_remote_changed` — see `_pull_writes`."""
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
        # local wins — queue a push instead of baselining as clean. Re-seed
        # the Claude helper files only AFTER this recheck (see the shim leg's
        # `_poll_remote` for why: seeding first makes the seeded, never-bundled
        # files look like a permanent diff to the recheck).
        try:
            recheck = subprocess.run(
                [*cli.command, *base, "--dry-run"],
                capture_output=True, text=True, timeout=PULL_TIMEOUT,
                encoding="utf-8", errors="replace",
                env=_cli_env(cli),
            )
        except (subprocess.TimeoutExpired, OSError):
            recheck = None
        still_diff = recheck is not None and recheck.returncode == 0 and (
            "already up to date" not in (recheck.stdout or "")
        )
        _seed_clone_claude_files(self.dir, self.name)
        self._fingerprint = self._take_fingerprint()
        self._dirty_since = time.time() if still_diff else None
        if still_diff and self.push_state != "error":
            self.push_state = "pending"

    def _run(self) -> None:
        while not self.stop_event.wait(SCAN_INTERVAL_S):
            with self.pause_lock:
                paused = self.pause_count > 0
            if paused:
                continue
            # Keep the live-run cache warm from the WATCHER thread, on every
            # tick, clean clone or not — not just from the dirty/debounce
            # branch below. Without this a clean clone (the common case: a
            # chat-only session, or between edits) never refreshes it here at
            # all, so the unbounded os.walk-of-RUNS scan `agent_run_id()` runs
            # (AGENT_LIVE_CACHE_S has a short TTL, so it re-triggers on the
            # cache's own schedule) happened on the REQUEST thread instead,
            # roughly every other `/api/canvases/sync/status` poll
            # (SYNC_POLL_MS's 2s is close to AGENT_LIVE_CACHE_S's 2s). This
            # call is a no-op read of the cache except once every
            # AGENT_LIVE_CACHE_S seconds, so it does not add a scan per tick.
            self.agent_run_id()
            current = self._take_fingerprint()
            if current != self._fingerprint:
                self._fingerprint = current
                self._dirty_since = time.time()
                if self.push_state != "error":
                    self.push_state = "pending"
                continue
            if self._dirty_since is not None and time.time() - self._dirty_since >= DEBOUNCE_S:
                # Hold the debounced auto-push while a Claude session is live in
                # this clone. The debounce measures file quiet, and a session
                # goes quiet for far longer than DEBOUNCE_S in the middle of a
                # change set — thinking, reading, waiting on a tool — so the
                # watcher would ship a half-done rename (the .py file without
                # its canvas.toml entry) as a validation failure the user sees.
                # The session publishes deliberately instead, via
                # /api/canvases/sync/push.
                #
                # The watcher stays the BACKSTOP: _dirty_since is left armed, so
                # the moment no run is live a still-dirty clone pushes on the
                # next tick. A session that never pushes therefore degrades to
                # exactly today's behaviour — never to a lost change set.
                if not self.agent_run_id():
                    with self._op_lock:
                        self._push()
                    continue
            if time.time() - self._last_pull_poll >= PULL_POLL_S:
                # HOLD the whole downstream leg while a Claude session is live in
                # this clone — the same signal and the same backstop shape as the
                # auto-push hold above.
                #
                # This supersedes the earlier rule (D354's note here) that the
                # remote-poll leg must keep running through a session so workbench
                # edits keep arriving. It must not: the clone's files moving under
                # a session mid-change-set is the thing the seeded CLAUDE.md and
                # the workbench lock both exist to avoid, and every pull was also
                # a `pulling` window the lock engaged on, so a chat of any length
                # flickered the embedded workbench read-only every PULL_POLL_S.
                # The accepted cost, stated rather than hidden: a workbench edit
                # made during a session no longer arrives mid-chat. It arrives at
                # the next pull — which is also step 1 of the push — where D338's
                # per-file three-way merge folds it in, local winning ties. So a
                # same-file collision surfaces at PUSH time instead of at edit
                # time; nothing is lost, the timing changes.
                #
                # Backstop, exactly like the push hold: `_last_pull_poll` is NOT
                # stamped when we skip, so the leg runs on the very next tick
                # after the session ends rather than up to PULL_POLL_S later. And
                # `_remote` is NOT rotated while held — forgetting a remote move
                # that happened during the hold would mean never applying it.
                #
                # EXEMPT: the first look, which only ADOPTS a baseline
                # (`_poll_remote`'s `self._remote is None` branch) and writes
                # nothing to the clone. Gating that too would leave `_base_files`
                # None for the whole session, and every later merge would then
                # degrade to local-wins wholesale.
                if self.agent_run_id() and not self._baseline_pending():
                    continue
                self._last_pull_poll = time.time()
                cli = fused_cli()
                if cli is not None and _shim_manifest_command(cli) is not None:
                    # Manifest probe: runs clean OR dirty (the merge makes a
                    # dirty-time remote change safe to fold in). Probing is
                    # read-only, so it stays outside _op_lock.
                    probe = self._probe_remote()
                    if probe is not None:
                        with self._op_lock:
                            # NOTE: `_pulling` is set INSIDE _poll_remote, not
                            # here. Wrapping probe-and-decide made a poll that
                            # decided to do nothing register as a full lock
                            # engagement the 2s status poll could sample — a
                            # read-only flicker for a pull that never happened.
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
            # Whether a Claude session is live in this clone right now.
            # PID-based and NOT `fix_active`: that one only knows about fix
            # sessions this module spawned, while this also covers a chat the
            # user started in the right pane. Informational only — reported
            # for the badge/banner copy, but the workspace's left-pane lock no
            # longer keys off it (a live session with no actual edits yet,
            # e.g. a plain "hi", must not lock the workbench for the whole
            # length of the chat).
            "agent_active": bool(self.agent_run_id()),
            # True for the duration of a force-pull/three-way-merge leg. The
            # lock holds for this exactly as it does for push_state
            # pending/pushing: the clone's files are moving on disk right now.
            "pulling": self._pulling,
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


@router.post("/api/canvases/sync/push")
def api_canvases_sync_push(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Push this canvas NOW, through the watcher's own _push().

    Exists so a Claude session working in the clone can publish a coherent
    change set on purpose instead of waiting out the debounce — and, more
    importantly, so it never has a reason to run `fused workbench canvas push`
    itself. That raw call is not a faster version of this one, it is a
    different and unsafe one:

      * it skips the probe+merge+abort guard at the top of `_push`, which is
        the only thing standing between `canvas push`'s wholesale REPLACE and a
        concurrent workbench edit. The clobber is unrecoverable: `.sync/trash`
        only ever protects local files, and the watcher's next probe sees
        remote == local, so the merge no-ops.
      * it moves the remote behind the watcher's back. With a clean clone the
        next poll cannot tell that from a workbench edit and takes the
        wholesale force-pull branch — pulling the agent's own push back down,
        deleting every unignored local file the push did not publish, and
        showing a phantom "pulled from workbench".

    Running the real `_push` under the real `_op_lock` keeps the module's
    "pushes are serialized per canvas" invariant true and makes the sync point
    move WITH the push, so the poll that follows has nothing to react to.

    Refuses rather than queues (409) when the watcher is paused, when a push is
    already running, or when another sync leg holds the lock: the caller is an
    agent that can read the answer and retry, and a silent no-op would leave it
    believing it had published.

    Returns the FULL status, `error_detail` included, because the transcript
    is the point — a validation failure has to land in the session's own
    context, one line per broken node, so it can fix and retry without a human
    relaying the output.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    manager = _sync_manager(name, create=False)
    if manager is None:
        # Deliberately not create=True: constructing one here just to push
        # once would also start its background thread as a side effect of a
        # single request, and a manager built without ever having watched
        # this open (no probe, no in-flight remote manifest) is not a
        # meaningfully more guarded push than the raw CLI's — even though
        # `_load_base()` may load a real merge base from a PRIOR sync
        # session's `.sync/<name>.json`. The workspace starts the real
        # watcher when it opens the canvas; that is the supported way back in.
        #
        # `code` is for _canvas_push.py: since D350's finding-3 correction, a
        # positively-identified clone target REFUSES on this code rather than
        # falling through — a merge base from a prior session, or a remote
        # move via the hosted workbench, can both be invisible right here and
        # the raw push is never provably inert for a clone. `code` still lets
        # the CLI interception tell this apart from a genuine push failure it
        # must report verbatim, and matching on prose would break the moment
        # this wording changes.
        return JSONResponse(
            {"error": f"canvas {name!r} is not being synced (no watcher is running)",
             "code": "no_watcher"}, status_code=409)
    # The three refusals below are all "someone else has this folder right
    # now", which is BENIGN — nothing is wrong with the canvas or its files.
    # They carry code "busy" so neither the caller nor this manager mistakes
    # them for a failed push: none of them touches push_state, last_error or
    # error_detail, so the clone stays dirty, the watcher's backstop still
    # publishes it, and the Fix-with-Claude button (gated on
    # push_state == "error") never lights up for a race there is nothing to fix.
    if manager.push_state == "pushing":
        return _busy("a push is already running for this canvas")
    if not manager._op_lock.acquire(timeout=MANUAL_PUSH_LOCK_WAIT_S):
        return _busy(
            "a sync operation is in flight for this canvas; try again in a moment")
    try:
        # Re-checked under the lock: pause() sets the count and then waits on
        # this same lock, so holding it is what makes the answer stable.
        with manager.pause_lock:
            if manager.pause_count > 0:
                return _busy(f"syncing for {name!r} is paused; try again in a moment")
        before = manager.push_seq
        manager._push()
    finally:
        manager._op_lock.release()
    status = manager.status()
    # "idle AND the counter moved" is the only success: _push also returns in
    # "pending" when it aborted because the remote moved and could not be
    # reconciled, which must not read as published.
    status["ok"] = status["push_state"] == "idle" and manager.push_seq > before
    return status


def _fix_prompt(name: str, detail: list[str], error: str | None) -> str:
    """The first message of a fix session: the verbatim CLI output (never
    reworded — rewording makes an error unsearchable, D328) plus what the
    session must do.

    It used to forbid pushing, because a hand-push raced the watcher and a
    `--no-validate` one defeated the reason the session was spawned. Neither
    holds now: the auto-push is HELD while this session is live, so there is
    nothing to race, and `fused workbench canvas push` inside a clone is
    intercepted into the guarded server-side push, which refuses
    --no-validate outright. So the session is told to push — that is how it
    confirms the fix actually landed, instead of finishing blind and leaving
    the user to discover on the next tick whether it worked."""
    report = "\n".join(detail) or (error or "the push failed")
    return (
        f"The automatic canvas push for {name!r} (this folder) is failing. "
        f"The CLI reported:\n\n"
        f"{report}\n\n"
        "Fix these problems in this folder's files. Check your work with "
        "`fused workbench canvas validate .` until it passes, then publish "
        "with `fused workbench canvas push .` — inside this folder that runs "
        "through fused-render's sync manager, so it is safe, and it prints "
        "any remaining errors straight back to you. Do not change the canvas "
        "name, and do not use --no-validate (it is refused here anyway)."
    )


@router.post("/api/canvases/fix")
def api_canvases_fix(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Spawn a detached Claude session on the canvas clone, primed with the
    failing push's own output (D336). Mirrors the apps API's session spawn
    (routers/apps.py): the fork-safe helper, permission mode "auto" for the
    same reason given there (nobody is polling `decide` until the page
    attaches, so "prompt" would park the first tool call for an hour), and a
    recorder thread so the run's session id and its commit land even though
    nobody is polling the chat. The
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
                "fix_active": False,
                # No watcher means nothing this page can be waiting on, so the
                # lock must read "off" — a missing field would leave a locked
                # pane with nothing left to unlock it (a dropped watcher or a
                # server restart mid-lock).
                "agent_active": False,
                "pulling": False}
    return manager.status()
