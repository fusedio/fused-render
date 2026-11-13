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

Sync model — the host is one more workbench tab. One watcher thread per
synced canvas fingerprints the clone folder (path/mtime/size walk) once a
second; any change arms a debounce, and after DEBOUNCE_S of quiet the whole
folder is pushed with `fused canvas push --canvas <name>` — which REPLACES
the remote UDF set. Every PULL_POLL_S the remote manifest is probed
(collection.last_updated + per-UDF server body hashes, via the
_fused_canvas_manifest.py shim; hashes are only ever compared
server-vs-server).

THE REVISION SPINE (2026-11-11, design YLb9ef6k §5–7). A server with
`collection.revision` makes the engine the only sync actor: a move is a
revision change (`_remote_moved`; stamps and hashes only on an old server);
every move first downloads the bundle and, when it is byte-for-byte the sync
point, just rebaselines (a re-serialised widget, a share-settings stamp, a
probe that read our own import late). A real move is MERGED by the watcher
itself (`_merge_now`, the same per-file merge Pull runs) when no Claude run
is live in the clone, and HELD (`server_stale`, the push held, the session
told which files through its PostToolUse hook) while one is — merged on the
first tick after it ends, or at its turn end (the Stop hook → `turn_end`,
which then publishes the folder). Every push carries `--if-match
<base_revision>` (persisted in `.sync/<name>.json`): a 412 (exit 3) merges
and pushes once more, a second one waits for the next debounce. A lease
(`_fused_canvas_lease.py`, renewed every LEASE_RENEW_S) tells the hosted
workbench who has the canvas; another owner's lease holds the pushes. The
session has NO sync verbs: `_canvas_push.py` answers a clone push with the
state line and refuses a pull. Every sync event is also journaled to
`.sync/<name>.log`.

PULL (`_SyncManager.pull`, the page's button / POST sync/pull) brings the
remote in NOW: the same per-file merge, clean clone or dirty (Hard pull alone
is a wholesale CLI `pull --force`, clone snapshot to trash first): download
the zip (_fused_canvas_zip.py shim) and apply
against the sync point BASE (per-file md5s + the compact rows of canvas.toml,
in <canvases_root>/.sync/<name>.json — outside the clone so `pull --force`
can't delete it). Source files merge per file: local untouched → remote wins
(including remote deletes), local changed → local wins, local deleted →
stays deleted. canvas.toml — the one file every writer touches — merges PER
NODE (canvas_snapshot.merge_toml): a row only one side changed takes that
side, both → field-wise with local winning a tie, a node new on the remote
arrives with its file. The held push then ships the merged folder. The same
probe+merge guard runs right before every push as a last line of defence.
With an external FUSED_RENDER_FUSED_BIN there is no interpreter for the
shims; sync degrades to a `pull --dry-run` probe (stale detection only) and a
wholesale `pull --force` on Pull. Pushes and pulls are serialized per canvas
by `_flight_lock`, and the fingerprint is re-taken right after a pull so its
own writes never echo into a push. `push_seq`/`pull_seq`/`merge_seq`
increment on each successful push/pull/merge (push_seq is on sync/state).

Two guards on top of the merge: (1) TRASH — every file a merge or force-pull
overwrites or deletes is first copied to .sync/trash/<name>/<timestamp>/,
pruned to the newest _TRASH_MAX snapshots, so no sync decision is ever
unrecoverable. (2) VALIDATION GATE — after a merge applies remote files,
`fused canvas validate` runs on the clone; a merge can still mix sides in a
way that breaks a cross-file invariant, and a failing result rolls the merge
back file-by-file (clone stays dirty, Pull can be retried) instead of
wedging the push in a permanent validation error.

Two loops, two locks (the 2026-10-19 "two loops" build). The LOCAL loop —
embed snapshot → disk, disk → embed delta — must never wait on the network,
so `_op_lock` is held only while files and sync-point state are read or
written: a push freezes the clone into `.sync/push/<name>/` under it and runs
the CLI on that copy with it released; probes and the merge's zip download
run before it is taken. `_flight_lock` serializes whole pushes and pulls,
network included. One fused-render process writes a clone
(`.sync/<name>.lock`, canvas_sync_state.py); `sync/state`, `sync/log` and
the `sync/events` socket report every leg with its age and reason, and a
watchfiles thread turns a file write into a `disk_rev` bump within ~100 ms
(the 1 s scan stays as the push debounce's backstop).

A canvas is (env, name), never a name alone. Clones live at
`~/Fused/canvas/<env>/<name>` (D1382), every canvas-scoped endpoint takes a
required `env`, sync managers are keyed `(env, name)` and pass their own env
to every CLI run — so a prod canvas and an unstable canvas, even of one name,
sync side by side (owner, 2026-10-20). See the block above `_ENV_WEB_URLS`.

No import of anything under fused_render.server (server includes this router —
keep it acyclic); the X-Fused guard is duplicated locally, same as other
shell/* routers.
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import io
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.parse
import zipfile
import zlib
from collections.abc import Callable, Iterable

from fastapi import APIRouter, Body, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from fused_render import canvas_snapshot, canvas_sync_state
from fused_render._platform import is_within
from fused_render.fusedcli import (
    CUSTOM_WORKBENCH_ENV,
    WORKBENCH_ENV_CHOICES,
    WORKBENCH_ENV_VAR,
    child_env,
    cli_error,
    fused_cli,
    set_fused_env,
    workbench_env,
    workbench_env_locked,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# `fused canvas list/whoami` are one control-plane call each.
LIST_TIMEOUT = 30.0
# 40 s, not 20: the CLI child is an interpreter importing `fused` before one
# round trip, and on a loaded Mac (load avg 20+, 2026-10-23) that alone took
# 17 s — the old 20 s tripped, the workspace bannered, and the workbench frame
# never loaded. The answer is the user's handle, which does not change.
WHOAMI_TIMEOUT = 40.0
# …and that answer is kept for an hour rather than 45 s: every reopen of a
# canvas within the hour frames the workbench without spawning anything.
_WHOAMI_CACHE_TTL_S = 3600.0
# whoami / list / token each spawn an interpreter (importing `fused`) for a
# control-plane answer that rarely changes: the parsed RESULT is reused this
# long (see `_cached_cli`). Errors are never reused.
_CLI_CACHE_TTL_S = 45.0
# A token expiring within this window is not reused: re-running the shim
# refreshes it.
_TOKEN_EXPIRY_MARGIN_S = 120.0
_CLI_CACHE_LOCK = threading.Lock()
_cli_cache: dict[tuple, tuple[float, object]] = {}  # key -> (monotonic expiry, value)
_cli_cache_gen = 0


class _Flight:
    """One in-flight computation other callers of the same key wait on."""

    __slots__ = ("event", "result")

    def __init__(self):
        self.event = threading.Event()
        self.result = None


_cli_flights: dict[tuple, _Flight] = {}

# A pull downloads and extracts the canvas zip; canvases are small (source
# files, not data), so a minute is generous headroom for a slow link.
PULL_TIMEOUT = 180.0
PUSH_TIMEOUT = 180.0
# The token shim pays the fused import cost plus at most one refresh call.
TOKEN_TIMEOUT = 30.0
# `fused login` blocks on its browser callback; its own HTTP server times out
# after ~30s per request, but give the child room for slow human sign-ins.
# Five minutes: long enough to find the window and a password manager, short
# enough that an abandoned or wedged attempt frees the page (and the one-at-a-
# time login slot) before the person gives up on it. The page watches
# `login_in_flight` and reports the timeout when the child dies at this mark.
LOGIN_CHILD_TIMEOUT = 300.0
# Quiet period after the last observed file change before a push fires.
DEBOUNCE_S = 1.5
# Folder fingerprint poll cadence.
SCAN_INTERVAL_S = 1.0
# Remote-change poll cadence: the manifest probe (a network call, so much
# slower than the local fingerprint walk), clean or dirty — a move is merged
# (held while a Claude run is live); with an external CLI the legacy `pull
# --dry-run` probe instead, clean clones only, detect only.
PULL_POLL_S = 10.0
# The user's canvas changes kept for Claude sessions' hooks, per canvas.
JOURNAL_MAX = 200
# The canvas's server-side edit lease (revision spine, 2026-11-11): acquired
# with this TTL when a manager starts, renewed every LEASE_RENEW_S from the
# watcher thread, released on stop. A lease held by ANOTHER owner holds the
# pushes ("checked out by <owner>") until it lapses or is released.
LEASE_TTL_S = 90
LEASE_RENEW_S = 30.0
LEASE_SHIM_TIMEOUT = 15.0
# The whole lease release at quit, across every canvas (in parallel): the
# lease lapses after LEASE_TTL_S anyway, so quit never waits on it longer.
LEASE_RELEASE_QUIT_S = 2.0
# Owner (live testing, 2026-11-12): a canvas nobody has open stops syncing
# IDLE_STOP_S after its last page socket (sync/events) closed — held merge,
# a final push, lease released, watcher stopped and forgotten — never under
# a live Claude run, nor while a Claude session is open on the folder.
# Opening the canvas restarts it through sync/start; a session's hooks
# restart it too (`_revive_for_session`).
IDLE_STOP_S = 60.0
# A held server move or unpublished edits left after the idle stop's merge and
# push: tried again this many times, IDLE_RETRY_S apart, before it stops anyway
# (a reopened manager picks both up: the clone reads dirty, the saved base
# revision re-detects the move).
IDLE_STOP_RETRIES = 3
IDLE_RETRY_S = 10.0
# How long sync/state shows `server_to_disk: merged` after an auto-merge.
MERGED_SHOW_S = 10.0
# `.sync/<name>.log` (the persistent sync journal) rotates to `.log.1` here.
SYNC_LOG_ROTATE_BYTES = 2 * 1024 * 1024
# A tombstone (a node the user deleted) whose file stays gone is forgotten
# after this long; one whose file is back without a row is kept.
TOMBSTONE_TTL_S = 7 * 24 * 3600.0
# How long after a landed hard pull the frame's snapshots are acked stale
# without a delta ack from it (see _SyncManager._hard_fence).
HARD_FENCE_S = 60.0
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

# How long the manual pull endpoint waits for the manager's _flight_lock (and a
# push or pull for each short _op_lock phase) before giving up with a 409.
MANUAL_PUSH_LOCK_WAIT_S = 5.0
# How long /api/canvases/snapshot waits on _op_lock before answering 409.
SNAPSHOT_LOCK_WAIT_S = 1.0
# stop()'s bound on joining the watcher thread. A push still inside its CLI
# can hold that thread for up to PUSH_TIMEOUT; the quit rung terminates the
# CLI first (terminate_push), and no caller should hang behind a network call.
STOP_JOIN_S = 5.0
# SIGTERM → SIGKILL grace for a push CLI the quit rung stops.
PUSH_TERMINATE_GRACE_S = 2.0

# The Fused environment is part of a canvas's IDENTITY, not a process-wide
# knob. The `<env>` segment of `~/Fused/canvas/<env>/<name>` (D1382) is what
# binds a clone to the Fused it was pulled from; every canvas-scoped endpoint
# takes a required, validated `env` (`_require_env`); managers are keyed
# `(env, name)` and hand THEIR env to every CLI run (`_cli_env(cli, env)`), and
# the iframe URL is `workbench_base_url(env)`. That is what lets a prod canvas
# and an unstable canvas be open and syncing at once (owner, 2026-10-20). The
# `workbench_env` preference (fusedcli.workbench_env) survives only as the
# DEFAULT — the catalog's initial pick, the `fused` wrapper outside a clone,
# share uploads — so it is read here in four places only: the status payload,
# the FUSED_RENDER_WORKBENCH_URL override, the FUSED_RENDER_WORKBENCH_ENV pin
# (`_require_env`), and the env-less credential calls (`_auth_env`). Never
# frozen into a module constant: the preference moves at runtime. The third
# env, `custom` (fusedcli.CUSTOM_WORKBENCH_ENV), is no Fused of its own: its
# runs get FUSED_ENV unset so the CLI keeps `~/.fused/settings.toml`'s
# endpoint, and its iframe loads that endpoint's web origin.

# Cap on the per-line push-error transcript kept for the UI and the fix
# session — enough for any real validation report (one line per problem plus
# a summary), small enough that a CLI stack trace can't bloat every sync/state
# payload (and socket message) that carries it.
_ERROR_DETAIL_MAX = 50

_ENV_WEB_URLS = {
    "prod": "https://www.fused.io",
    "unstable": "https://unstable.fused.io",
    "stg": "https://staging.fused.io",
    "staging": "https://staging.fused.io",
    "dev": "http://localhost:3000",
}


# fused's own settings files, in its load order (fused `_options.py`
# OPTIONS_PATH, then OPTIONS_JSON_PATH): where the custom environment's
# endpoint comes from. Read here, not by importing fused, because the server
# never imports fused (every fused call is a subprocess) and these are read
# per status call — cheap, and an edit to the file shows on the next one.
_FUSED_SETTINGS_FILES = ("~/.fused/settings.toml", "~/.fused/settings.json")
# fused's `Options.base_url` default — what an absent file or key means.
_FUSED_DEFAULT_API_URL = "https://www.fused.io/server/v1"
# fused's `Options.base_web_url`: the hosted API URLs whose web origin is not
# simply the API URL's own origin. Anything else is `scheme://netloc`.
_FUSED_API_WEB_URLS = {
    "https://staging.fused.io/server/v1": "https://staging.fused.io",
    "https://unstable.fused.io/server/v1": "https://unstable.fused.io",
    "http://localhost:8783/v1": "http://localhost:3000",
}


def _fused_settings() -> dict:
    """The settings fused loads from `~/.fused/settings.toml` (else
    `settings.json`), or {} — what a custom-environment run, with FUSED_ENV
    unset, actually uses. Like fused, a file that does not parse is skipped
    for the next one."""
    for raw in _FUSED_SETTINGS_FILES:
        path = os.path.expanduser(raw)
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh) if path.endswith(".toml") else json.load(fh)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:  # TOMLDecodeError is a ValueError
            logger.warning("could not read %s for the custom canvas environment: %s",
                           path, exc)
            continue
        return data if isinstance(data, dict) else {}
    return {}


def fused_settings_base_url() -> str:
    """The API `base_url` fused loads from its settings file, else fused's
    default (a parsed file without `base_url` means the default)."""
    value = _fused_settings().get("base_url")
    return value if isinstance(value, str) and value else _FUSED_DEFAULT_API_URL


def _fused_settings_credentials_path() -> str:
    """Where a custom-environment run keeps its `fused login` credentials:
    the settings file's `[auth] credentials_path` (a custom deployment has its
    own Auth0 tenant, D1412), else fused's `AuthOptions` default."""
    auth = _fused_settings().get("auth")
    value = auth.get("credentials_path") if isinstance(auth, dict) else None
    return value if isinstance(value, str) and value else "~/.fused/credentials"


def fused_settings_embed_proxy() -> bool | None:
    """`[render] canvases_embed_proxy_enabled` in the settings file: True or
    False when the file sets it (it then wins over the Preferences switch,
    shell/prefs.py), None when it does not. fused ignores a table it does not
    know, so the deployment's own file can carry it."""
    render = _fused_settings().get("render")
    value = render.get("canvases_embed_proxy_enabled") if isinstance(render, dict) else None
    return value if isinstance(value, bool) else None


def fused_settings_present() -> bool:
    """Whether there is a `~/.fused/settings.toml` (or `settings.json`) for
    the custom environment to follow."""
    return any(os.path.isfile(os.path.expanduser(raw)) for raw in _FUSED_SETTINGS_FILES)


def fused_settings_configured() -> bool:
    """Whether the settings file actually points the custom environment
    somewhere: present AND naming a `base_url`. Without one a custom run is
    just prod under another clone folder. The Canvases page offers custom
    regardless and opens the settings editor when this is false (the empty
    file the editor starts from must NOT count as configured, or dismissing
    it would leave custom silently equal to prod); the Settings picker
    offers it too."""
    value = _fused_settings().get("base_url")
    return isinstance(value, str) and bool(value.strip())


def fused_settings_path() -> str:
    """The file the settings editor reads and writes: fused's TOML settings
    file (the first in its load order), expanded."""
    return os.path.expanduser(_FUSED_SETTINGS_FILES[0])


def _read_fused_settings_text() -> tuple[str, bool]:
    """(text, exists) of the TOML settings file. A missing file is ("", False);
    one that cannot be read raises OSError for the caller to report."""
    try:
        with open(fused_settings_path(), encoding="utf-8") as fh:
            return fh.read(), True
    except FileNotFoundError:
        return "", False


def _write_fused_settings_text(text: str) -> None:
    """Write the TOML settings file atomically (temp + os.replace): a status
    poll reading it mid-write must never see half a file. The temp name is
    unique per call (handlers run in FastAPI's threadpool, so two Saves can
    overlap), and a failed write leaves no temp behind."""
    path = fused_settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=os.path.dirname(path),
            prefix=f"{os.path.basename(path)}.", suffix=".tmp", delete=False,
        ) as fh:
            tmp = fh.name
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def _create_fused_settings_empty() -> bool:
    """Create the TOML settings file empty, exclusively. True when this call
    created it; False when another writer (an overlapping open, a Save) got
    there first — in which case the caller re-reads rather than clobbering
    what was just written."""
    path = fused_settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8"):
            pass
    except FileExistsError:
        return False
    return True


def _fused_settings_web_url() -> str:
    """The workbench origin for `fused_settings_base_url()`, by fused's own
    `Options.base_web_url` rule."""
    api = fused_settings_base_url()
    known = _FUSED_API_WEB_URLS.get(api)
    if known:
        return known
    try:
        parts = urllib.parse.urlparse(api)
        parts.port  # noqa: B018 — raises on a non-numeric port, which `new URL()` rejects
    except ValueError:  # e.g. an unclosed IPv6 bracket, `https://[::1`, or `host:bogus`
        return _ENV_WEB_URLS["prod"]
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return _ENV_WEB_URLS["prod"]
    return f"{parts.scheme}://{parts.netloc}"


# The pinned env's workbench origin override (dev recipe: a local workbench).
_WORKBENCH_URL_VAR = "FUSED_RENDER_WORKBENCH_URL"


def workbench_base_url(env: str) -> str:
    """The hosted workbench origin `env`'s canvases embed.

    FUSED_RENDER_WORKBENCH_URL replaces it ONLY under a pin, and only for the
    pinned env (FUSED_RENDER_WORKBENCH_ENV, which `_require_env` makes the one
    env this process serves). The dev recipe points it at a local workbench
    (localhost:3000) standing in for unstable. Keyed off the mutable DEFAULT
    instead, flipping the preference to prod would dress prod canvases in
    that local frame; without a pin the var is ignored
    (`log_url_override_without_pin` says so once at startup). The custom
    env's origin comes from `~/.fused/settings.toml` (`_fused_settings_web_url`)."""
    override = os.environ.get(_WORKBENCH_URL_VAR)
    if override and workbench_env_locked() and env == workbench_env():
        return override
    if env == CUSTOM_WORKBENCH_ENV:
        return _fused_settings_web_url()
    return _ENV_WEB_URLS.get(env, "https://www.fused.io")


def log_url_override_without_pin() -> None:
    """Startup breadcrumb (server/app.py `export_app_env`): a
    FUSED_RENDER_WORKBENCH_URL set without FUSED_RENDER_WORKBENCH_ENV is
    ignored (`workbench_base_url`), and a dev whose local workbench silently
    stopped loading has no other clue why."""
    if os.environ.get(_WORKBENCH_URL_VAR) and not workbench_env_locked():
        logger.warning("%s is ignored without %s: the URL override applies only to "
                       "the pinned environment", _WORKBENCH_URL_VAR, WORKBENCH_ENV_VAR)


def _embed_proxy_wanted() -> bool:
    """The custom env's embed goes through the loopback proxy
    (canvas_embed_proxy.py) when the preference is on and custom is usable."""
    from fused_render.shell.prefs import canvases_embed_proxy_enabled

    return canvases_embed_proxy_enabled() and fused_settings_present()


def _custom_token() -> str:
    """The proxy's token source: the custom env's own login, or an exception
    whose text the proxy relays as a 502 (it runs off the event loop)."""
    token, problem, _status = access_token(CUSTOM_WORKBENCH_ENV)
    if token is None:
        raise RuntimeError(problem or "could not read the fused access token")
    return token


def _custom_creds_sig() -> tuple | None:
    """The proxy's credentials stamp: a sign-out, sign-in or account switch
    on the custom env changes it, and the proxy drops its cached token."""
    return _creds_sig(CUSTOM_WORKBENCH_ENV)


def _apply_embed_proxy() -> None:
    """Start, retarget or stop the proxy to match the preference and the
    settings file's current origin. Idempotent; called from status."""
    from fused_render.canvas_embed_proxy import controller

    if _embed_proxy_wanted():
        controller.apply(workbench_base_url(CUSTOM_WORKBENCH_ENV), _custom_token,
                         _custom_creds_sig)
    else:
        controller.apply(None, None)


def embed_base_url(env: str) -> str:
    """The origin the workspace frames for `env` and posts its messages to:
    `workbench_base_url(env)`, except the custom env while its proxy runs."""
    if env == CUSTOM_WORKBENCH_ENV:
        from fused_render.canvas_embed_proxy import controller

        url = controller.url()
        if url:
            return url
    return workbench_base_url(env)


def _cli_env(cli, env: str) -> dict[str, str]:
    """child_env plus the env target, so a CLI run hits the canvas's own
    Fused — and the marker that stops a CLI child of OURS from being re-routed
    back into this server.

    `env` is REQUIRED: a request's validated `env`, or a _SyncManager's
    `self.env` (its `dir` is that env's folder for life). There is no
    default-to-the-preference any more, because with both environments
    syncing at once a run that guessed its env from the preference is exactly
    how one Fused's files would land in the other's same-named canvas. The
    env-less credential calls pass `_auth_env()`.

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

    The custom env gets FUSED_ENV REMOVED (`set_fused_env`), so the CLI keeps
    the endpoint `~/.fused/settings.toml` names.
    """
    child = child_env(cli)
    set_fused_env(child, env)
    child[_canvas_push_internal_env()] = "1"
    return child


def _auth_env() -> str:
    """FUSED_ENV for the env-less credential calls — login, logout, the token
    shim. prod and unstable share one Auth0 tenant and one
    `~/.fused/credentials` (verified: one `fused login` answers `workbench
    whoami` under both), so between them the choice is moot; the DEFAULT env
    is used so that a FUSED_RENDER_WORKBENCH_ENV pin to a dev/PR environment
    (fused's `_dev_auth`: its own tenant and `credentials-dev` store) signs in
    where that environment reads. A custom default runs with FUSED_ENV unset
    (`_cli_env`), so it signs in wherever `~/.fused/settings.toml` points."""
    return workbench_env()


def _canvas_push_internal_env() -> str:
    """The reentrancy marker's name, from the module that reads it, so the two
    ends cannot drift. Imported lazily: _canvas_push is stdlib-only by design
    and this keeps the dependency one-directional at import time."""
    from fused_render._canvas_push import INTERNAL_ENV

    return INTERNAL_ENV


def canvases_base() -> str:
    """`~/Fused/canvas` — the parent of every environment's clones. The
    `FUSED_RENDER_CANVASES_DIR` override (tests) replaces THIS, the base, and
    it is what server/app.py exports to spawned sessions: an env-independent
    path, so nothing downstream freezes the environment at startup."""
    override = os.environ.get("FUSED_RENDER_CANVASES_DIR")
    if override:
        return override
    from fused_render.shell.seed import fused_dir

    return os.path.join(fused_dir(), "canvas")


def canvases_root(env: str) -> str:
    """Where `env`'s clones live: `~/Fused/canvas/<env>/<name>` (D1382). The
    environment is a path segment because a clone is bound to the Fused it
    was pulled from: prod's `alpha` and unstable's `alpha` are two canvases,
    two folders, two `.sync/` trees (merge base, trash, writer lock), and so
    two managers that never share a byte."""
    return os.path.join(canvases_base(), env)


def _canvas_dir(env: str, name: str) -> str:
    return os.path.join(canvases_root(env), name)


def _require_env(value) -> tuple[str | None, JSONResponse | None]:
    """`(env, None)` for a usable canvas environment, else `(None, refusal)`.

    The pin is checked FIRST: when FUSED_RENDER_WORKBENCH_ENV pins the process
    to another env the answer is 423 `env_locked`, even for a valid choice
    and even when the pin itself is outside the choices (stg, dev, a PR
    number — which therefore refuses every canvas call as locked, the true
    reason, rather than as a bad request). The dev recipe points the pinned
    env's iframe at a local workbench, and a canvas of another env served
    from that same server would talk to a real Fused the developer did not
    mean to touch. Unpinned, anything outside WORKBENCH_ENV_CHOICES is 400."""
    if workbench_env_locked():
        pinned = workbench_env()
        if value != pinned:
            return None, JSONResponse(
                {"error": f"the environment is pinned to {pinned} by {WORKBENCH_ENV_VAR}",
                 "code": "env_locked", "env": pinned}, status_code=423)
    if value not in WORKBENCH_ENV_CHOICES:
        return None, _error(f"'env' must be one of: {', '.join(WORKBENCH_ENV_CHOICES)}")
    return value, None


# One in-flight workbench-skills fetch per process, ever. A non-blocking acquire
# is the whole mechanism: the hooks below fire on user actions that repeat (a
# canvas open, and the page re-arms the watcher on every poll that finds it
# dropped), and without this a flaky network would pile up threads each waiting
# out its own clone timeout.
_SKILLS_FETCH_LOCK = threading.Lock()


def _kick_workbench_skills() -> None:
    """Fetch/refresh the `workbench` skills OFF-THREAD, best-effort.

    Called from the canvas paths — the clone route and the watcher start the page
    does when it opens a canvas. The open hook is the one that matters in the
    field: hanging the fetch on /clone alone meant a canvas that already existed
    (i.e. nearly every real canvas) never got the skills at all, and a session
    that cannot find the `workbench:canvas-toml` skills its own seeded CLAUDE.md
    names goes looking for the format elsewhere — a `find /` over the whole disk is not a
    hypothetical, it is what happened.

    Off-thread because both callers are requests the canvas page AWAITS before it
    renders, and a shallow clone against a dead network sits there for
    _CLONE_TIMEOUT_S. Off-thread also means the answer may land a moment after
    the page opens, which is fine: what has to be set is the server process's env
    by the time a SESSION spawns, and that is a user typing a message later.

    Rate limiting stays in `skill_plugin` (`_attempt_due`), so repeated opens
    cost a lock check and nothing else. Nothing here can fail a canvas open:
    the thread swallows everything and the caller never waits on it.

    Also retires the pre-rename `fused` plugin, on this thread and this trigger —
    see the call below for why supplying the new skills is only half the fix.
    """
    if not _SKILLS_FETCH_LOCK.acquire(blocking=False):
        return  # one already running — its result serves this caller too

    def run() -> None:
        try:
            from fused_render.skill_plugin import (retire_legacy_fused_plugin,
                                                   sync_workbench_plugin)

            sync_workbench_plugin()
            # Same thread and the same trigger, because it is the same problem
            # from the other side: supplying the current `workbench:*` skills
            # does nothing while the pre-rename `fused` plugin is still enabled
            # globally and shadowing them with stale copies under the prefix an
            # old seeded CLAUDE.md names. Gated on the canvas path like the fetch
            # — a user who never opens a canvas gets their config left alone —
            # and idempotent, so the repeat opens that re-arm the watcher cost a
            # settings read. Ordered AFTER the fetch on purpose: the flip removes
            # the fallback answer, so do it once the real one is on disk.
            retire_legacy_fused_plugin()
        except Exception:  # noqa: BLE001 — never surface out of a helper thread
            logger.debug("workbench skills sync failed", exc_info=True)
        finally:
            _SKILLS_FETCH_LOCK.release()

    threading.Thread(target=run, name="workbench-skills-fetch",
                     daemon=True).start()


def credentials_file(env: str | None = None) -> str:
    # The `fused login` store `env`'s runs read (None = the auth env, the
    # default). prod/unstable: fused's default ~/.fused/credentials —
    # `fused._env()` resets `[auth]` to the hosted tenant. custom: FUSED_ENV is
    # unset, so fused keeps the settings file's `[auth] credentials_path`.
    # The env override is ours (tests, relocated stores) and wins for all.
    override = os.environ.get("FUSED_RENDER_FUSED_CREDENTIALS")
    if override:
        return override
    if (env or _auth_env()) == CUSTOM_WORKBENCH_ENV:
        return os.path.expanduser(_fused_settings_credentials_path())
    return os.path.expanduser("~/.fused/credentials")


def _logged_in(env: str | None = None) -> bool:
    # Presence-only: the CLI refreshes an expired-but-refreshable token
    # silently; the CLI stays the authority at action time.
    return os.path.isfile(credentials_file(env))


def _creds_sig(env: str | None = None) -> tuple | None:
    """`(mtime_ns, size)` of the credentials store, None when absent. A login,
    logout or on-disk token refresh changes it, which invalidates cached CLI
    results by construction."""
    try:
        st = os.stat(credentials_file(env))
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _cli_cache_clear(keep_listing: bool = False) -> None:
    """Drop the memory cache, and (by default) the on-disk listing baselines
    with it — see `_listing_cache_write`. `keep_listing` is for mutations the
    page does not act on right away (a push: only `updated_at` moves), where
    serving the baseline for one more visit beats blocking on the control
    plane every time a canvas was saved."""
    global _cli_cache_gen
    with _CLI_CACHE_LOCK:
        _cli_cache.clear()
        _cli_cache_gen += 1
    if not keep_listing:
        _listing_cache_clear()


def _fresh(result):
    """A result with its payload deep-copied (`api_canvases_list` mutates its
    entries, so a hit must never hand out the stored object)."""
    if result is not None and result[0] == "ok":
        return ("ok", copy.deepcopy(result[1]))
    return result


def _cached_cli(kind: str, env: str, compute, ttl_for=None):
    """Run `compute()` once per (kind, env, credentials signature) per TTL, and
    share one run between concurrent callers (single flight).

    `compute()` returns `("ok", payload)` or `("err", message, status)`; the
    same shape comes back, payload deep-copied on every return. Errors reach
    the running caller and its current waiters but are never stored. `ttl_for(
    payload)` overrides the TTL (<= 0 means do not store)."""
    key = (kind, env, _creds_sig(env))
    with _CLI_CACHE_LOCK:
        hit = _cli_cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return _fresh(("ok", hit[1]))
        flight = _cli_flights.get(key)
        leader = flight is None
        if leader:
            flight = _cli_flights[key] = _Flight()
            gen = _cli_cache_gen
    if not leader:
        flight.event.wait(TOKEN_TIMEOUT + LIST_SHIM_TIMEOUT)
        if flight.result is not None:
            return _fresh(flight.result)
        return _fresh(compute())
    result = None
    try:
        result = compute()
        return _fresh(result)
    finally:
        flight.result = result
        flight.event.set()
        with _CLI_CACHE_LOCK:
            if _cli_flights.get(key) is flight:
                del _cli_flights[key]
        if result is not None and result[0] == "ok" and gen == _cli_cache_gen:
            ttl = ttl_for(result[1]) if ttl_for is not None else _CLI_CACHE_TTL_S
            if ttl > 0:
                # The child may have refreshed the credentials file, which
                # changes its signature: store under the POST signature so the
                # next call (which reads the new one) hits.
                post_key = (kind, env, _creds_sig(env))
                # Credentials gone WHILE the child ran (a logout): the answer
                # is already stale, so a long TTL (whoami's hour) is capped
                # to the default — the next minute re-asks (CodeRabbit #1918).
                if post_key[2] is None:
                    ttl = min(ttl, _CLI_CACHE_TTL_S)
                with _CLI_CACHE_LOCK:
                    if gen == _cli_cache_gen:
                        _cli_cache[post_key] = (
                            time.monotonic() + ttl, copy.deepcopy(result[1]))


def _err_of(response: JSONResponse) -> tuple:
    """`("err", message, status)` from an `_error(...)` response."""
    try:
        message = json.loads(response.body)["error"]
    except (ValueError, KeyError, TypeError):
        message = "request failed"
    return ("err", message, response.status_code)


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


def _run_cli(args: list[str], timeout: float,
             env: str) -> tuple[subprocess.CompletedProcess | None, JSONResponse | None]:
    """Run a fused CLI command against `env`; (proc, None) or (None, error response)."""
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
            env=_cli_env(cli, env),
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
    # The credentials store this login writes (`credentials_file(env)`): prod
    # and unstable share one, custom's may be its own (D1412). Reuse, status
    # and logout match on it, never on "a login is running".
    store: str


_LOGIN_LOCK = threading.Lock()
_active_login: _ActiveLogin | None = None


def _login_for(store: str) -> _ActiveLogin | None:
    """The live login child if it writes `store`, else None."""
    login = _reap_login()
    return login if login is not None and login.store == store else None


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
    # Whoever the login now belongs to, earlier whoami/list/token answers are stale.
    _cli_cache_clear()


def _status_auth_env(env: str) -> str:
    """The env whose login `/api/canvases/status?env=` reports: `env` when it
    is one this process serves, else the auth env. Lenient on purpose — the
    catalog asks with its URL's pick before it knows about a pin, and a cheap
    unguarded read should not 400 over that; `auth_env` in the answer says
    which login was described."""
    if env:
        value, bad = _require_env(env)
        if bad is None:
            return value
    return _auth_env()


@router.get("/api/canvases/status")
def api_canvases_status(env: str = ""):
    """Cheap local read: CLI presence, login-file presence, in-flight login,
    and per environment its workbench origin, clone folder and clone count.

    `logged_in` / `creds_stamp` describe ONE environment's `fused login`
    store: `env`'s when given (the catalog names its pick), else the default
    env's. prod and unstable share one store; custom has its own
    (`credentials_file`). The rest is every env at once: the page needs each
    env's `base_url` (the workspace's iframe and postMessage origin) and
    counts (the Preferences row) in one read. The
    username needed for the workbench slug URL is NOT here — it costs a
    control-plane call per env, so it lives behind guarded
    /api/canvases/whoami?env= instead.
    """
    cli = fused_cli()
    auth_env = _status_auth_env(env)
    try:
        stamp = os.path.getmtime(credentials_file(auth_env))
    except OSError:
        stamp = None
    # The one place the embed proxy's lifecycle is driven: every page that
    # frames a canvas reads status first, so the proxy is up (or retargeted
    # after a settings.toml edit) by the time `embed_url` below is used.
    _apply_embed_proxy()
    return {
        "cli_found": cli is not None,
        "logged_in": _logged_in(auth_env),
        # Which env's login the two fields around this describe.
        "auth_env": auth_env,
        # Credentials-file mtime: a re-login over a STALE-but-present store
        # never flips logged_in, so the client watches this stamp change to
        # know the browser flow completed.
        "creds_stamp": stamp,
        # Only a login writing THIS store: another env's sign-in in the
        # browser is not one this answer's `logged_in` will flip on.
        "login_in_flight": _login_for(credentials_file(auth_env)) is not None,
        # The DEFAULT environment (the catalog's initial pick), and whether
        # FUSED_RENDER_WORKBENCH_ENV pins it — the catalog then disables its
        # environment picker, since `_require_env` refuses the other env.
        "workbench_env": workbench_env(),
        "locked": workbench_env_locked(),
        # Clone counts are a local listdir, no CLI — informational on the
        # Preferences row. `available` is false only for custom whose
        # settings file names no `base_url` (`fused_settings_configured`):
        # both pickers still offer it; the Canvases page opens the settings
        # editor instead of the sign-in.
        "envs": {
            env: {
                "base_url": workbench_base_url(env),
                # Where the workspace's iframe loads from and posts to: the
                # workbench origin itself, or — custom env, embed proxy on —
                # the loopback proxy standing in front of it.
                "embed_url": embed_base_url(env),
                "canvases_dir": canvases_root(env),
                "cloned_count": len(_cloned_names(env)),
                "available": env != CUSTOM_WORKBENCH_ENV or fused_settings_configured(),
            }
            for env in WORKBENCH_ENV_CHOICES
        },
        # Where the custom environment's settings live — what the Canvases
        # page's editor shows the reader.
        "settings_path": fused_settings_path(),
    }


@router.post("/api/canvases/settings/open")
def api_canvases_settings_open(x_fused: str | None = Header(default=None)):
    """The settings editor's read: the TOML settings file's text, creating
    it EMPTY when there is none so the reader is editing a real file at a
    real path from the first keystroke. An empty file configures nothing
    (`fused_settings_configured`), so creating it changes no answer."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        text, exists = _read_fused_settings_text()
        created = False
        if not exists:
            created = _create_fused_settings_empty()
            if not created:
                text, _ = _read_fused_settings_text()
    except OSError as exc:
        return _error(f"could not open {fused_settings_path()}: {exc}", 500)
    return {"path": fused_settings_path(), "text": text, "created": created,
            "configured": fused_settings_configured()}


@router.post("/api/canvases/settings/save")
def api_canvases_settings_save(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Write the TOML settings file. Parsed first, so a paste that is not
    TOML is refused with the parser's own sentence (400) and the file on disk
    stays what it was; fused itself would silently skip an unparsable file
    and fall through to prod."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    text = body.get("text")
    if not isinstance(text, str):
        return _error("'text' must be a string")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return JSONResponse({"error": f"not valid TOML: {exc}", "code": "invalid_toml"},
                            status_code=400)
    try:
        _write_fused_settings_text(text)
    except OSError as exc:
        return _error(f"could not write {fused_settings_path()}: {exc}", 500)
    # Cached CLI answers (whoami, listings) were computed against the old
    # endpoint; the next call must run afresh.
    _cli_cache_clear()
    base_url = data.get("base_url")
    return {"ok": True, "path": fused_settings_path(),
            "configured": fused_settings_configured(),
            "base_url": base_url if isinstance(base_url, str) else None,
            "web_url": workbench_base_url(CUSTOM_WORKBENCH_ENV)}


@router.post("/api/canvases/settings/delete")
def api_canvases_settings_delete(x_fused: str | None = Header(default=None)):
    """Remove the TOML settings file: the custom environment goes back to
    unconfigured and the Canvases page offers the editor again. Only the
    TOML file — a `settings.json` beside it is fused's own fallback and not
    something this editor wrote. An already-missing file is a no-op."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = fused_settings_path()
    try:
        os.remove(path)
        removed = True
    except FileNotFoundError:
        removed = False
    except OSError as exc:
        return _error(f"could not delete {path}: {exc}", 500)
    _cli_cache_clear()
    return {"ok": True, "path": path, "removed": removed,
            "configured": fused_settings_configured()}


def _cloned_names(env: str) -> set[str]:
    """The clone dirs under canvases_root(env): a dir holding a canvas.toml,
    the same rule /api/canvases/list uses to mark a card cloned.

    A root that does not exist yet is simply empty, and so is any other
    listing failure (permissions, an unmounted volume): the count is
    informational only."""
    root = canvases_root(env)
    try:
        entries = os.listdir(root)
    except OSError:
        return set()
    return {d for d in entries if os.path.isfile(os.path.join(root, d, "canvas.toml"))}


def cloned_canvases() -> list[tuple[str, str, str]]:
    """Every clone on this machine, both environments: ``(env, name,
    clone_dir)`` in env order (`WORKBENCH_ENV_CHOICES`), names sorted
    casefold within an env. The launcher's canvas rows (launcher.py): a
    clone is findable by typing whether or not it is on the desk, the way
    a workspace app is. The clone rule is `_cloned_names`'s (a dir holding
    a canvas.toml); a root that is missing or unreadable is simply empty."""
    from fused_render.fusedcli import WORKBENCH_ENV_CHOICES

    out: list[tuple[str, str, str]] = []
    for env in WORKBENCH_ENV_CHOICES:
        for name in sorted(_cloned_names(env), key=str.casefold):
            if _NAME_RE.match(name):
                out.append((env, name, _canvas_dir(env, name)))
    return out


def clone_dir_for(path: str) -> tuple[str, str, str] | None:
    """The canvas CLONE `path` is, or sits inside — `(env, name, clone_dir)`
    — or None. A clone is `<canvases_root(env)>/<name>` (one level under an
    environment's root, a CLI canvas name). Compared through `realpath` as
    well as the plain path: the CLI wrapper reaches a clone through `pwd -P`,
    and a task made from inside one may name it either way. No existence
    check — the desk keeps a row for a clone that is gone, as it does for an
    app folder, until the user hides it.

    The desk's canvas rule (current_apps.py): a canvas is a project on the
    sidebar's desk like any app folder (its own Canvases section), so a task
    under a clone puts the clone there and `list_apps` says which canvas a
    row is."""
    from fused_render.fusedcli import WORKBENCH_ENV_CHOICES

    path = str(path or "").rstrip("/\\")
    if not path or not os.path.isabs(path):
        return None
    # NO syscall on `path` (CodeRabbit on #1795): this runs on every task
    # project the desk observes, including ones on a mount that may be
    # wedged, and `app_dir_for` promises an answer before any syscall. The
    # ROOTS are resolved instead — a fixed local set — so a path the CLI
    # already resolved (`pwd -P`) matches the real root and a plain one the
    # plain root. A symlink from elsewhere INTO a clone is not matched; it
    # is not a documented way to reach one.
    cand = os.path.normpath(path)
    for env in WORKBENCH_ENV_CHOICES:
        root = canvases_root(env)
        for base in {os.path.normpath(root), os.path.realpath(root)}:
            if not is_within(cand, base, strict=True):
                continue
            name = cand[len(base) + 1:].split(os.sep)[0]
            if not _NAME_RE.match(name):
                continue
            return env, name, os.path.join(canvases_root(env), name)
    return None


@router.get("/api/canvases/whoami")
def api_canvases_whoami(env: str = "", x_fused: str | None = Header(default=None)):
    # EXECUTES a control-plane call — D36 guard, like account's probe=1.
    # `env` is OPTIONAL: the Settings account row and the Share sheet only ask
    # "is this login alive, and whose is it", which one credentials file
    # answers for both envs, so they omit it and get the `_auth_env()` run.
    # The workspace names its canvas's env: the account behind the one login
    # is a different user record in each Fused (same handle today, different
    # user id), and the slug URL wants the one its iframe loads. Validated
    # (400/423) whenever given.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not env:
        env = _auth_env()
    else:
        env, bad = _require_env(env)
        if bad is not None:
            return bad

    def compute():
        proc, err = _run_cli(["workbench", "--format", "json", "whoami"], WHOAMI_TIMEOUT, env)
        if err is not None:
            return _err_of(err)
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return (
                "err",
                f"the fused CLI printed something that wasn't JSON: {proc.stdout.strip()[-200:]!r}",
                502,
            )
        if not isinstance(data, dict):
            return ("err", "unexpected `fused whoami` payload", 502)
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
        return ("ok", {"handle": handle, "raw": data})

    # An hour only for an answer WITH a handle; a logged-out "no handle" keeps
    # the short default so a login lands within a minute.
    result = _cached_cli(
        "whoami", env, compute,
        lambda payload: _WHOAMI_CACHE_TTL_S if payload.get("handle") else _CLI_CACHE_TTL_S,
    )
    if result[0] == "err":
        return _error(result[1], result[2])
    return result[1]


def _optional_env(env: str) -> tuple[str | None, JSONResponse | None]:
    """A credential call's env: validated when given (400/423), else the auth
    env. Credentials are per env only through custom's own `[auth]`, so the
    callers that just mean "this machine's Fused login" (Settings, Share)
    omit it."""
    if not env:
        return _auth_env(), None
    return _require_env(env)


@router.post("/api/canvases/login")
def api_canvases_login(env: str = "", x_fused: str | None = Header(default=None)):
    """Start the browser `fused login` for `env` (default: the auth env).
    A custom deployment signs in against the settings file's `[auth]`
    tenant, and only a run with FUSED_ENV unset (`_cli_env`) reads it. One
    login at a time, whatever its env."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    env, bad = _optional_env(env)
    if bad is not None:
        return bad
    cli = fused_cli()
    if cli is None:
        return _no_cli_error()
    global _active_login
    store = credentials_file(env)
    with _LOGIN_LOCK:
        login = _active_login
        if login is not None and login.proc.poll() is None and login.store != store:
            # One browser sign-in at a time, and this one writes another
            # store: reusing it would leave the caller waiting on a stamp that
            # never changes; killing it would drop a sign-in someone is
            # mid-way through.
            return _busy("a Fused sign-in for another environment is already waiting "
                         "in the browser — finish or cancel it first")
        if login is None or login.proc.poll() is not None:
            child = _cli_env(cli, env)
            child["PYTHONUNBUFFERED"] = "1"
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
                    env=child,
                )
            except OSError as e:
                return _error(f"could not run the fused CLI ({cli.command[0]}): {e}")
            _cli_cache_clear()
            login = _ActiveLogin(proc=proc, started_at=time.time(), store=store)
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
def api_canvases_token(env: str = "", x_fused: str | None = Header(default=None)):
    """The `fused login` access token, for seeding the workbench iframe.

    `env` is the CANVAS's environment, and the token comes from that env's
    login: the iframe is that env's workbench origin, so a custom canvas must
    get the custom deployment's own `[auth]` token — never the hosted one,
    which its tenant would reject and which must not be handed to another
    origin (D1412). Omitted, it is the auth env (older callers).

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
    env, bad = _optional_env(env)
    if bad is not None:
        return bad
    token, problem, status = access_token(env)
    if token is None:
        return _error(problem or "could not read the fused access token", status)
    return {"access_token": token}


def access_token(env: str) -> tuple[str | None, str | None, int]:
    """`env`'s `fused login` access token, refreshed through the package's
    own child when the CLI is our interpreter: `(token, None, 200)`, or
    `(None, reason, http_status)`. Shared by /api/canvases/token and the
    custom env's embed proxy (canvas_embed_proxy.py), so the two never
    disagree on which store an env reads or how a refresh runs."""
    if not _logged_in(env):
        return None, "not signed in to Fused — sign in from the Canvases page", 409
    cli = fused_cli()
    if cli is not None and not cli.external:
        shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fused_token.py")

        def compute():
            try:
                proc = subprocess.run(
                    [sys.executable, shim],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=TOKEN_TIMEOUT,
                    env=_cli_env(cli, env),
                    close_fds=False,
                )
            except subprocess.TimeoutExpired:
                return ("err", f"token refresh timed out after {int(TOKEN_TIMEOUT)}s", 502)
            except OSError as e:
                return ("err", f"could not run the token helper: {e}", 502)
            if proc.returncode != 0:
                return ("err", cli_error(proc.stderr, "could not read the fused access token"), 502)
            token = proc.stdout.strip()
            if not token:
                return ("err", "the token helper printed nothing", 502)
            return ("ok", token)

        def ttl_for(_payload):
            # Reuse only while the stored token stays comfortably valid: one
            # expiring within the margin is re-run (which refreshes it).
            try:
                with open(credentials_file(env), encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                return 0
            exp = _iso_epoch(data.get("expires_at")) if isinstance(data, dict) else None
            if exp is None:
                return 0
            return min(_CLI_CACHE_TTL_S, exp - time.time() - _TOKEN_EXPIRY_MARGIN_S)

        result = _cached_cli("token", env, compute, ttl_for)
        if result[0] == "err":
            return None, result[1], result[2]
        return result[1], None, 200
    # External-CLI fallback: the raw store, no refresh.
    try:
        with open(credentials_file(env), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"could not read the fused credentials file: {e}", 502
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        return None, "the fused credentials file has no access token", 502
    return token, None, 200


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


# The shim pays the fused import cost and one list call. Preview signing is
# NOT in here any more (D364) — see _shim_previews_command.
LIST_SHIM_TIMEOUT = 60.0


def _shim_previews_command(cli) -> list[str] | None:
    """argv for the batch preview-signing shim, or None for an external CLI
    (which never produced previews at all — the `canvas list` fallback prints
    bare names, so there is nothing to sign)."""
    if cli.external:
        return None
    shim = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_fused_canvas_previews.py"
    )
    return [sys.executable, shim]


def _shim_favourites_command(cli) -> list[str] | None:
    """argv for the favourites shim, or None for an external CLI (no Python
    to drive; the card's star is hidden there)."""
    if cli.external:
        return None
    shim = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_fused_canvas_favourites.py"
    )
    return [sys.executable, shim]


# One control-plane round trip plus the fused import.
FAVOURITES_SHIM_TIMEOUT = 30.0
# Collection ids are UUIDs; anything else never reaches the URL path.
_COLLECTION_ID_RE = re.compile(r"[0-9A-Fa-f-]{8,64}")


# The shim signs the whole batch on a thread pool, so this is the fused import
# cost plus roughly ONE control-plane round trip, not one per id.
PREVIEWS_SHIM_TIMEOUT = 60.0
# Bound on one request's work. Far above any real account; it exists so a
# malformed or hostile body can't ask the server for unbounded signing.
PREVIEWS_MAX_IDS = 500


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


def _cli_json_detail(stderr: str) -> dict:
    """The one-line JSON `detail` the fused CLI prints on stderr for a 412
    (`revision_mismatch`) or a 409 (`leased`) push: the LAST line that
    parses as an object, unwrapped from `{"detail": …}` when wrapped. {}
    when there is none."""
    for line in reversed((stderr or "").splitlines()):
        line = line.strip()
        start = line.find("{")
        if start < 0:
            continue
        try:
            data = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(data, dict):
            inner = data.get("detail")
            return inner if isinstance(inner, dict) else data
    return {}


def _pushed_revision(stdout: str | None) -> int | None:
    """`Revision: <n>` — the collection revision a successful `canvas push`
    prints last (fused-py with the revision spine); None from an older CLI."""
    for line in reversed((stdout or "").splitlines()):
        m = re.fullmatch(r"\s*Revision:\s*(\d+)\s*", line)
        if m:
            return int(m.group(1))
    return None


def _shim_lease_command(cli) -> list[str] | None:
    """argv for the edit-lease shim, or None for an external CLI (no lease:
    the sync runs without one, exactly as before the revision spine)."""
    if cli.external:
        return None
    shim = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_fused_canvas_lease.py"
    )
    return [sys.executable, shim]


def _lease_owner() -> str:
    """`fused-render:<user>@<hostname>` — this machine's name on a canvas
    lease (owner decision Q3, 2026-11-11). The hosted workbench shows it in
    its "being edited in fused-render" banner."""
    import getpass
    import socket

    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 — no passwd entry, no env
        user = "user"
    try:
        host = socket.gethostname() or "localhost"
    except OSError:
        host = "localhost"
    return f"fused-render:{user}@{host}"


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
_SYNC_IGNORED_BASENAMES = canvas_snapshot.IGNORED_BASENAMES

_CLONE_FUSEDIGNORE = "CLAUDE.md\n.fusedignore\n"

_CLONE_CLAUDE_MD = """\
# Fused canvas clone: {name}

This folder is a live clone of the Fused canvas **{name}**, two-way synced by
fused-render: the folder publishes itself when your turn ends, edits the user
makes in the canvas view beside this chat land here LIVE (see "The user edits
this canvas live, beside you" below), and edits made in a separate hosted
workbench tab are merged in automatically, per file.

## Skills — invoke these FIRST

**Before you read or edit anything in this folder, invoke the `Skill` tool for
the skills below that apply to the task.** They carry the authoritative format
references, and this folder's files cannot be edited correctly from their own
appearance alone — a `canvas.toml` that looks obvious has required fields and
node/edge rules that are not visible in an example, and getting them wrong is
rejected at push time and surfaces to the user as a broken canvas.

fused-render hands these to this session itself, so they are already in your
available-skills list. Do not search for them, do not ask the user to install
anything, and do not skip one because the change "looks small":

| Skill | Invoke it when |
|---|---|
| `workbench:canvas-toml` | ALWAYS — before the first edit. canvas.toml format, folder layout |
| `workbench:fused-udfs` | writing or changing any `.py` UDF |
| `workbench:json-ui-schemas` | writing or changing any `.json` widget |
| `workbench:html-template-nodes` | writing or changing any `.html` node |
| `workbench:fused-workbench-cli` | running any `fused` command |
| `workbench:canvas-comments` | reading or resolving canvas comments |
| `workbench:fused-runtime` | importing a library in a UDF, or a UDF fails with `ModuleNotFoundError` |
| `workbench:fused-h3` | a UDF ingests to or queries H3 hex datasets (`fused.h3`) |
| `workbench:fused-integrations` | a UDF reads, writes or calls a connected service (Snowflake, BigQuery, S3, Slack, LLM providers, …) |

`workbench:` is the correct prefix. If you also see these skill names under a
different prefix (`fused:` in particular), that is a stale copy from an older
release — **ignore it and use the `workbench:` one**, which is the version this
app supplied for this session.

If, and only if, no `workbench:`-prefixed match exists at all, work from THIS
FOLDER and nothing else: the existing `canvas.toml` and the files beside it are
your format reference, and their conventions are the answer. Do not stop to ask
for the skills, and do not try to install anything.

**Do not search outside this folder for format references.** No `find`, `ls
-R`, `grep -r`, glob or any other recursive walk over `/`, your home
directory, `~/.fused-render`, or the fused-render source tree — the app's
internal files are not documentation and looking through them is a detour
that ends nowhere. If this folder does not answer the question, say what you could not
determine and stop there — do not go looking for it elsewhere on disk.

## How the sync works

fused-render keeps this folder and the canvas on the server in step. You only
edit files; you never run a sync command.

- **Your work publishes itself.** When your turn ends, fused-render publishes
  this folder to the canvas (and also whenever the folder goes quiet while no
  turn is running). A change set you leave half-done at the end of a turn is
  published half-done, so finish it within the turn (e.g. a rename: the `.py`
  file AND its `canvas.toml` entry).
- **The server's changes are merged into this folder automatically.** When
  someone edits the canvas in the hosted workbench, fused-render merges their
  change in per file (canvas.toml per node; a file only you changed keeps
  your version). While your turn runs a server change is held and merged the
  moment the turn ends; either way you are told which files, after your next
  tool call. Re-read those files before editing them.
- **Never run `fused workbench canvas pull` or `fused workbench canvas push`
  here.** Both are refused inside this folder (a push only prints the
  folder's sync state). There is nothing to pull and nothing to push by hand.
- **Verify your work** with the `canvas_screenshot` tool and
  `fused workbench canvas validate .` — an invalid canvas is not published,
  and its errors reach the user. If a publish failed, the canvas view shows
  why; fix the files and the next publish ships the fix.

## Running the fused CLI

Always invoke it as the bare command — `fused ...` — and nothing else. That
resolves to the CLI this app ships, which is the only one that works here.

Do NOT `pip install fused`, do NOT run `python -m fused`, and do NOT call a
`fused` from any other path, venv or environment. A different fused misses the
pieces this folder's two-way sync depends on, and it bypasses the sync
protection described above.

## Keeping the canvas valid

- After structural edits (renaming a node, adding/removing nodes or edges),
  check the clone with `fused workbench canvas validate .` — an invalid
  canvas is rejected at push time and the error surfaces to the user.
- `canvas.toml` defines the canvas (nodes, edges, viewport); every node
  needs its source file next to it (`<udfName>.py`; widgets are `.json`).
  A file without a row gets a default row at push (write the row when its
  place matters); to remove a node, delete its file and its row. A node
  whose file is missing is removed from the server at the next push.
- **Folders** are `[[canvas.nodes]]` rows with `type = "udf-folder"`. Give a
  folder row `udfName` equal to its `folderName`. Membership lives ONLY in
  the folder's `childUdfOrder` (the child rows' `udfName`s; one folder per
  child, never a folder inside a folder). A child's `x`/`y` are RELATIVE to
  its folder's `x`/`y`, so moving a node into or out of a folder means
  converting its coordinates too. Deleting or renaming a node means
  removing or renaming it in its folder's `childUdfOrder` as well: a stale
  name there is tolerated, but it is a bug to fix. A node in a folder is
  still a root-level file in this folder, never a subdirectory. After
  re-arranging folders or moving nodes, take one canvas screenshot (the
  `canvas_screenshot` tool, `what: "canvas"`) and fix what is missing,
  misplaced or overlapping before you report the layout done.

## The user edits this canvas live, beside you

The canvas view next to this chat writes to THIS folder as the user works:
adding, deleting or renaming a node, dragging or resizing one, grouping nodes
into folders and editing a node's code all land in `canvas.toml` and the
source files within a fraction of a second — while you are mid change set.
(Edits made in a separate hosted workbench tab are different: they are merged
in by fused-render, held until your turn ends.) So:

- **Re-read `canvas.toml` right before every edit to it.** It changes under
  you whenever the user moves something. An Edit that fails with "modified
  since read" or "string not found" means exactly that: Read it again and
  re-apply your change to what is there now. Never force it.
- **Edit `canvas.toml` with targeted Edits only.** Never rewrite the whole
  file (Write, a heredoc, a script that dumps it): that throws away whatever
  the user did since you read it.
- **A file that vanished was deleted by the user.** If a node's file is gone
  when you go to edit it, the user deleted that node on the canvas (a copy is
  in `../.sync/trash/<canvas>/<timestamp>/`). Do not recreate it unless the
  user asks; say what you found. A recreated file with no row in
  `canvas.toml` stays off the canvas.
- **When you and the user change the same property, the user's wins.** If
  you set a node's title, position or folder and the user changes that same
  property of that node at the same moment, theirs is kept (your other
  changes stay); re-read and adjust.
- After your tool calls you may get a note listing what the user changed
  meanwhile (nodes added, deleted, renamed, moved), or which files a server
  change was merged into. Take it as the current state of the folder.

A file that unexpectedly reverted, or a whole folder that changed, can also
come from a server change merged in, or from the user's Hard pull
(replacing this folder with the server's copy; a backup is always made under
`../.sync/trash/<canvas>/<timestamp>/`). Either way: re-read the files you are
about to edit rather than rewriting them from memory, check the state on disk
and say what you found. Restore from the trash only what the user asks for.

Group related multi-file changes (e.g. a rename: the `.py` file AND its
`canvas.toml` entry) into one change set and finish it within your turn — a
half-done rename that gets published or merged mid-way is exactly how invalid
states happen.

"""


def _no_backup_error(root: str) -> str:
    return (f"could not create the backup folder under {root}; nothing was changed. "
            "Free disk space or fix its permissions, then pull again")


def _seed_clone_claude_files(target: str, name: str) -> None:
    """Best-effort: (re)write CLAUDE.md + .fusedignore into a clone. Runs
    after clone, on every sync/start, and after every pull (the CLI's
    `--force` deletes them; a merge re-seeds too).

    Skipped for an external FUSED_RENDER_FUSED_BIN: that path detects remote
    moves with `pull --dry-run`, which would report the seeded local-only
    files as a diff on every poll — a permanently "server changed" canvas
    (until the CLI's _PULL_DELETE_IGNORE_BASENAMES learns these names)."""
    cli = fused_cli()
    if cli is None or _shim_manifest_command(cli) is None:
        return
    try:
        with open(os.path.join(target, "CLAUDE.md"), "w", encoding="utf-8") as f:
            f.write(_CLONE_CLAUDE_MD.format(name=name))
        with open(os.path.join(target, ".fusedignore"), "a+", encoding="utf-8",
                  errors="replace") as f:
            f.seek(0)
            text = f.read()
            add = [line for line in _CLONE_FUSEDIGNORE.splitlines()
                   if line not in text.splitlines()]
            if add:
                f.write("\n" * bool(text and not text.endswith("\n"))
                        + "".join(f"{line}\n" for line in add))
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


# -- listing disk cache (stale-while-revalidate) ---------------------------------
#
# The control plane's lite listing costs ~1s fixed (connect + TLS + auth) plus
# ~17ms per canvas, so a 90-canvas account waits ~3s on every visit once the
# 45s memory cache lapsed. The hosted workbench hides the same cost with a
# persisted baseline served instantly while a refresh runs; this is that for
# the catalog: the last successful listing per env lives on disk, the page
# gets it in milliseconds flagged `stale`, and asks again with `fresh=1` for
# the real one (which goes through `_cached_cli`'s single flight, so two tabs
# share one control-plane hit).
#
# Keyed by env, NOT by credentials signature: `_creds_sig` changes on every
# silent token refresh, which would wipe the baseline for no reason. Identity
# changes (login, logout, settings edits) and list mutations the page acts on
# right away (create, delete) drop the files through `_cli_cache_clear`
# instead; a push keeps them — only `updated_at` goes briefly stale.

_LISTING_CACHE_VERSION = 1
# A baseline older than this is dropped rather than flashed in: the fresh call
# follows anyway, and a weeks-old list popping up first reads as a bug.
_LISTING_CACHE_MAX_AGE_S = 7 * 24 * 3600.0


def _listing_cache_dir() -> str:
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    # Not under `canvases/`: that is a clone root (`canvases_root`), and a
    # record file next to canvas folders would be an odd neighbour.
    return os.path.join(base, "cache")


def _listing_cache_path(env: str) -> str:
    return os.path.join(_listing_cache_dir(), f"canvases-listing-{env}.json")


def _listing_account(env: str) -> str | None:
    """A non-reversible tag for WHO the credentials store signs in as (the
    access token's `sub` claim, hashed), or None when it cannot be read.

    The baseline must not outlive the account: a `fused login` as someone
    else from a terminal never passes through `_cli_cache_clear`, and
    `_creds_sig` cannot stand in because a silent token refresh moves it
    too. Decoded locally — no verification needed, nothing is trusted from
    it beyond "same subject as last time"."""
    try:
        with open(credentials_file(env), encoding="utf-8") as f:
            data = json.load(f)
        token = data.get("access_token") if isinstance(data, dict) else None
        header, payload, _sig = token.split(".")
        payload += "=" * (-len(payload) % 4)
        import base64

        claims = json.loads(base64.urlsafe_b64decode(payload))
        sub = claims.get("sub") if isinstance(claims, dict) else None
        if not isinstance(sub, str) or not sub:
            return None
        return hashlib.sha256(sub.encode("utf-8")).hexdigest()
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _listing_deployment(env: str) -> list[str]:
    """What the listing was taken from: the workbench origin, and for the
    custom env the API `base_url` too — two deployments can share a host and
    differ only in the API path."""
    urls = [workbench_base_url(env)]
    if env == CUSTOM_WORKBENCH_ENV:
        urls.append(fused_settings_base_url())
    return urls


def _listing_cache_write(env: str, entries: list) -> None:
    """Best effort: a cache that cannot be written is a cache that misses."""
    path = _listing_cache_path(env)
    record = {
        "version": _LISTING_CACHE_VERSION,
        "env": env,
        # The custom env can be retargeted through settings.toml; a listing
        # from the previous deployment must not stand in for the new one.
        "deployment": _listing_deployment(env),
        "account": _listing_account(env),
        "saved_at": time.time(),
        "entries": entries,
    }
    # Unique per writer: two listings finishing together must not share a
    # staging file, or one replaces the other's half-written one.
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _listing_cache_read(env: str) -> list | None:
    """The stored listing for `env`, or None when absent, malformed, from
    another deployment, or past `_LISTING_CACHE_MAX_AGE_S`."""
    try:
        with open(_listing_cache_path(env), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("version") != _LISTING_CACHE_VERSION:
        return None
    if record.get("env") != env or record.get("deployment") != _listing_deployment(env):
        return None
    # Another account signed in since (see `_listing_account`): their names
    # are not this user's to see, even for one round trip.
    account = _listing_account(env)
    if account is None or record.get("account") != account:
        return None
    saved_at = record.get("saved_at")
    if not isinstance(saved_at, (int, float)) or time.time() - saved_at > _LISTING_CACHE_MAX_AGE_S:
        return None
    entries = record.get("entries")
    if not isinstance(entries, list):
        return None
    if not all(isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"] for e in entries):
        return None
    return entries


def _listing_cache_clear() -> None:
    """Drop every env's baseline: prod and unstable share one credentials
    store, so a sign-out of one is a sign-out of both."""
    for env in WORKBENCH_ENV_CHOICES:
        try:
            os.remove(_listing_cache_path(env))
        except OSError:
            pass


def _list_entries(env: str, allow_stale: bool = False):
    """`_list_entries_uncached` behind the CLI result cache (see `_cached_cli`):
    `(entries, error, stale)`, errors never cached.

    `allow_stale`: on a memory miss, hand back the disk baseline (stale=True)
    instead of calling the control plane; the caller is expected to ask again
    without it."""

    def compute():
        with _CLI_CACHE_LOCK:
            gen = _cli_cache_gen
        entries, err = _list_entries_uncached(env)
        if err is not None:
            return _err_of(err)
        # Same rule `_cached_cli` applies to the memory cache: a login, logout
        # or mutation that landed while the control plane was answering has
        # already wiped the baseline, and this answer is the one it wiped.
        with _CLI_CACHE_LOCK:
            current = _cli_cache_gen
        if gen == current:
            _listing_cache_write(env, entries)
        return ("ok", entries)

    if allow_stale:
        key = ("list", env, _creds_sig(env))
        with _CLI_CACHE_LOCK:
            hit = _cli_cache.get(key)
            fresh = hit is not None and hit[0] > time.monotonic()
        if not fresh:
            stale = _listing_cache_read(env)
            if stale is not None:
                return stale, None, True
    result = _cached_cli("list", env, compute)
    if result[0] == "err":
        return None, _error(result[1], result[2]), False
    return result[1], None, False


def _list_entries_uncached(env: str):
    """(entries, error) for `env`'s account: each entry is {name, id,
    preview_url, n_code_udfs, updated_at}.

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
                env=_cli_env(cli, env),
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
                            # Sentinel preview: the URL has to be signed, which
                            # the client asks for separately (D364).
                            "preview_pending": bool(entry.get("preview_pending"))
                            and entry.get("id") is not None,
                            # The workbench's own code-UDF count, computed from
                            # the node list the lite payload already carried.
                            "n_code_udfs": entry.get("n_code_udfs")
                            if isinstance(entry.get("n_code_udfs"), int)
                            else None,
                            "updated_at": _iso_epoch(entry.get("last_updated")),
                            "share_token": entry.get("share_token")
                            if isinstance(entry.get("share_token"), str)
                            and entry.get("share_token")
                            else None,
                        }
                    )
        return entries, None
    proc, err = _run_cli(["workbench", "--format", "json", "canvas", "list"], LIST_TIMEOUT, env)
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
                entries.append(
                    {"name": entry, "id": None, "preview_url": None,
                     "preview_pending": False, "n_code_udfs": None,
                     "updated_at": None, "share_token": None}
                )
            elif isinstance(entry, dict):
                name = entry.get("name") or entry.get("slug")
                if isinstance(name, str) and name:
                    entries.append(
                        {"name": name, "id": entry.get("id"), "preview_url": None,
                         "preview_pending": False, "n_code_udfs": None,
                         "updated_at": None, "share_token": None}
                    )
    return entries, None


@router.get("/api/canvases/list")
def api_canvases_list(
    env: str = "", fresh: int = 0, x_fused: str | None = Header(default=None)
):
    # EXECUTES a control-plane call — guarded like whoami. `env` picks the
    # account listed AND the folder `cloned` is judged against: a prod clone
    # of `alpha` says nothing about unstable's `alpha`.
    #
    # Without `fresh`, a memory miss answers from the disk baseline flagged
    # `stale: true` (see `_list_entries`); the page then asks again with
    # `fresh=1` and swaps the real list in when it lands.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    env, bad = _require_env(env)
    if bad is not None:
        return bad
    if not _logged_in(env):
        return _error("not signed in to Fused — sign in first", 409)
    canvases, err, stale = _list_entries(env, allow_stale=not fresh)
    if err is not None:
        return err
    cloned = set()
    root = canvases_root(env)
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
    return {"canvases": canvases, "stale": stale}


@router.post("/api/canvases/previews")
def api_canvases_previews(
    body: dict = Body(...), x_fused: str | None = Header(default=None)
):
    """Presigned preview-image URLs for the collection ids asked for (D364).

    POST rather than a guarded GET for one reason: the ids are a list, and an
    account with hundreds of canvases would push a comma-joined query string
    past what proxies are willing to carry. It EXECUTES control-plane calls, so
    it carries the same X-Fused guard the other executing endpoints do.

    Never an error when a signature can't be had: the response simply has no
    entry (or a null one) for that id, and the card keeps its letter thumb.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    # The collection ids are `env`'s; signing them against the other Fused
    # finds nothing.
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    if not _logged_in(env):
        return _error("not signed in to Fused — sign in first", 409)
    ids = body.get("ids")
    if not isinstance(ids, list):
        return _error("'ids' must be a list of collection ids")
    wanted = [i for i in ids if isinstance(i, str) and i][:PREVIEWS_MAX_IDS]
    if not wanted:
        return {"previews": {}}
    cli = fused_cli()
    if cli is None:
        return _no_cli_error()
    shim_cmd = _shim_previews_command(cli)
    if shim_cmd is None:
        # External CLI: that path never reports a pending preview in the first
        # place, so an empty answer is the truthful one, not a failure.
        return {"previews": {}}
    try:
        proc = subprocess.run(
            shim_cmd,
            input=json.dumps(wanted),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PREVIEWS_SHIM_TIMEOUT,
            env=_cli_env(cli, env),
        )
    except subprocess.TimeoutExpired:
        return _error(
            f"signing canvas previews timed out after {int(PREVIEWS_SHIM_TIMEOUT)}s",
            502,
        )
    except OSError as e:
        return _error(f"could not run the canvas previews helper: {e}")
    if proc.returncode != 0:
        message = cli_error(proc.stderr or proc.stdout, "signing canvas previews failed")
        # Same expired-credentials sniff as _list_entries.
        if (
            "re-authenticate" in message.lower()
            or "refresh your fused credentials" in message.lower()
        ):
            return _error(message, 401)
        return _error(message, 502)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return _error(
            f"the canvas previews helper printed something that wasn't JSON: "
            f"{proc.stdout.strip()[-200:]!r}",
            502,
        )
    if not isinstance(data, dict):
        return _error("the canvas previews helper printed a non-object", 502)
    previews = {
        k: v
        for k, v in data.items()
        if isinstance(k, str) and (v is None or isinstance(v, str))
    }
    return {"previews": previews}


def _run_favourites_shim(env: str, req: dict):
    """(data, error) from the favourites shim for `env`'s account."""
    cli = fused_cli()
    if cli is None:
        return None, _no_cli_error()
    shim_cmd = _shim_favourites_command(cli)
    if shim_cmd is None:
        return None, _error("favourites need the bundled fused CLI", 501)
    try:
        proc = subprocess.run(
            shim_cmd,
            input=json.dumps(req),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FAVOURITES_SHIM_TIMEOUT,
            env=_cli_env(cli, env),
        )
    except subprocess.TimeoutExpired:
        return None, _error(
            f"favourites timed out after {int(FAVOURITES_SHIM_TIMEOUT)}s", 502
        )
    except OSError as e:
        return None, _error(f"could not run the favourites helper: {e}")
    if proc.returncode != 0:
        message = cli_error(proc.stderr or proc.stdout, "favourites request failed")
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
            f"the favourites helper printed something that wasn't JSON: "
            f"{proc.stdout.strip()[-200:]!r}",
            502,
        )
    if not isinstance(data, dict):
        return None, _error("the favourites helper printed a non-object", 502)
    return data, None


@router.get("/api/canvases/favourites")
def api_canvases_favourites(env: str = "", x_fused: str | None = Header(default=None)):
    """The account's favourite canvas (collection) ids — the hosted home's
    star, read from the same control-plane list so the two agree. EXECUTES a
    control-plane call, so guarded like the listing."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    env, bad = _require_env(env)
    if bad is not None:
        return bad
    if not _logged_in(env):
        return _error("not signed in to Fused — sign in first", 409)
    data, err = _run_favourites_shim(env, {"op": "get"})
    if err is not None:
        return err
    ids = data.get("collection_ids")
    return {"collection_ids": [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []}


@router.post("/api/canvases/favourite")
def api_canvases_favourite(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Star (`on: true`) or unstar one canvas by collection id."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    if not _logged_in(env):
        return _error("not signed in to Fused — sign in first", 409)
    cid = body.get("id")
    if not isinstance(cid, str) or not _COLLECTION_ID_RE.fullmatch(cid):
        return _error("'id' must be a collection id")
    on = body.get("on")
    if not isinstance(on, bool):
        return _error("'on' must be true or false")
    _, err = _run_favourites_shim(env, {"op": "put" if on else "delete", "id": cid})
    if err is not None:
        return err
    return {"ok": True, "id": cid, "on": on}


@router.post("/api/canvases/create")
def api_canvases_create(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    if not _logged_in(env):
        return _error("not signed in to Fused — sign in first", 409)
    proc, err = _run_cli(["workbench", "canvas", "create", name], LIST_TIMEOUT, env)
    if err is not None:
        return err
    _cli_cache_clear()
    try:
        _seed_default_udf(env, name)
    except Exception as e:  # noqa: BLE001 — best effort, see _seed_default_udf
        logger.warning("canvas %s: starter UDF not seeded — %s", _sync_key(env, name), e)
    return {"ok": True, "name": name}


# The starter UDF a new canvas opens with — the workbench's own
# `constants/default_udf.py`, under the same `udf` name it gives it.
_DEFAULT_UDF_NAME = "udf"
# Per seed CLI call. The canvas is one tiny file; the create request waits on
# both calls, so a stalled Fused costs seconds, not PULL_TIMEOUT twice.
SEED_TIMEOUT = 45.0
_DEFAULT_UDF_CODE = '''@fused.udf(cache_max_age="0s")
def udf(name: str = "world"):
    import pandas as pd

    return pd.DataFrame({"hello": [name]})
'''


def _seed_default_udf(env: str, name: str) -> None:
    """Give a just-created canvas its starter UDF ON THE SERVER, before
    anything clones it.

    `fused workbench canvas create` makes an EMPTY collection. The workbench
    seeds a default UDF into an empty canvas client-side only (unsaved,
    waiting on autosave), and in the embed the host's full disk state
    (`fused-embed-hello` → delta, `full=1`) arrives first and removes every
    UDF it does not name — so the starter flashed up and vanished. Seeding it
    in the clone would not help: a fresh watcher adopts seconds-old files as
    its clean baseline and never pushes them. So the canvas is pulled into a
    scratch folder, the file and its row added, and pushed; the clone that
    follows pulls a canvas that already holds it.

    Best effort: a failure leaves the canvas created and empty (what it was
    before this existed), logged, never a failed create."""
    key = _sync_key(env, name)
    with tempfile.TemporaryDirectory(prefix="fused-canvas-seed-") as scratch:
        target = os.path.join(scratch, name)
        _, err = _run_cli(
            ["workbench", "canvas", "pull", name, "-o", target, "--force"], SEED_TIMEOUT, env
        )
        if err is not None:
            logger.warning("canvas %s: starter UDF not seeded — pull failed: %s",
                           key, err.body.decode("utf-8", "replace"))
            return
        try:
            doc = canvas_snapshot._load_toml(os.path.join(target, "canvas.toml"))
            # Not empty after all (a row, or any source file even without
            # one): nothing to seed, and nothing of the user's to overwrite.
            if canvas_snapshot.rows_of_doc(doc)["nodes"] or canvas_snapshot.node_files(target):
                return
            canvas_snapshot._write_atomic(os.path.join(target, f"{_DEFAULT_UDF_NAME}.py"),
                                          _DEFAULT_UDF_CODE)
            canvas_snapshot.reconcile_rows(target)
        except Exception as e:  # noqa: BLE001 — best effort, see docstring
            logger.warning("canvas %s: starter UDF not seeded — %s", key, e)
            return
        _, err = _run_cli(
            ["workbench", "canvas", "push", target, "--canvas", name], SEED_TIMEOUT, env
        )
        if err is not None:
            logger.warning("canvas %s: starter UDF not seeded — push failed: %s",
                           key, err.body.decode("utf-8", "replace"))


@router.post("/api/canvases/logout")
def api_canvases_logout(env: str = "", x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    env, bad = _optional_env(env)
    if bad is not None:
        return bad
    store = credentials_file(env)
    # A completing browser login could rewrite the credentials file right
    # after we clear it — cancel it first. Only one writing THIS store: a
    # custom sign-out must not drop a hosted sign-in in progress.
    login = _login_for(store)
    if login is not None:
        login.proc.terminate()
    # Pause every watcher on the store being cleared — prod's and unstable's
    # share one; custom's may be its own (`credentials_file`) — for the CLI
    # call (no push/pull spam using about-to-be-cleared credentials) but
    # don't stop them until logout actually succeeds — a failed logout leaves
    # the user signed in, and stopping first would leave sync dead until a
    # canvas is reopened.

    def on_store() -> list:
        return [(key, m) for key, m in _syncs.items() if credentials_file(m.env) == store]

    with _SYNC_LOCK:
        managers = [m for _, m in on_store()]
    for manager in managers:
        manager.pause()
    proc, err = _run_cli(["workbench", "logout"], WHOAMI_TIMEOUT, env)
    if err is not None:
        # Still signed in — resume so sync keeps working until the user
        # retries. Resuming a manager we're about to stop (success path)
        # would let a briefly-unpaused thread start a push/pull against
        # already-cleared credentials, and block stop()'s join on it.
        for manager in managers:
            manager.resume(rebaseline=False)
        return err
    _cli_cache_clear()
    with _SYNC_LOCK:
        signed_out = on_store()
        for key, _ in signed_out:
            del _syncs[key]
    for _, manager in signed_out:
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
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    if not _logged_in(env):
        return _error("not signed in to Fused — sign in first", 409)
    # Fetch/refresh the `workbench` skills, whose names the CLAUDE.md seeded
    # below uses (`workbench:canvas-toml` and friends), and publish the root for
    # the session in the right pane. A canvases path is the right moment for it:
    # the only place those skills are ever handed out, so a user who never opens
    # a canvas pays nothing, a skills release made since startup is picked up
    # without a server restart, and the network work happens on a request rather
    # than before the server's bind (see server/app.py). Off-thread and
    # swallowed — a clone must still succeed when git is missing or the network
    # is gone, and it must not wait out a clone timeout to find that out.
    _kick_workbench_skills()
    gate = _clone_gate(env, name)
    if not gate.acquire(timeout=PULL_TIMEOUT):
        return _busy("this canvas is already being cloned; try again in a moment")
    try:
        return _clone(env, name)
    finally:
        gate.release()


def _clone(env: str, name: str):
    target = _canvas_dir(env, name)
    os.makedirs(target, exist_ok=True)
    # --force: the clone folder is OURS (under ~/Fused/canvas/<env>); a
    # re-clone deliberately resets it to the remote state. The sync watcher is
    # paused around the pull so its writes never echo into a push.
    manager = _sync_manager(env, name, create=False)
    if manager is not None:
        manager.pause()
    proc, err = _run_cli(
        ["workbench", "canvas", "pull", name, "-o", target, "--force"], PULL_TIMEOUT, env
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
# The watcher suppresses its debounced auto-push while a session is
# mid-change-set (the session publishes deliberately through sync/push), and
# sync/state names that as the pending push's reason.
#
# The answer is PID-BASED (agent.py's `_live_run`, whose liveness check is a live
# process), never transcript activity: a "no recent activity" read carries a
# grace window, and a slow tool call mid-edit would read as "finished" and let
# the auto-push ship a half-done change set. `active_fix_run_id` is NOT reusable
# here — it only tracks fix sessions this module spawned, not a chat the user
# started in the right pane themselves.

# How long a liveness answer is reused. Only the watcher thread asks
# (`agent_run_id()` on every tick, clean clone or not); sync/state reads the
# cached answer and never scans. The scan is a meta.json read per run dir,
# unbounded (see _live_run's `limit`; deliberately not result-capped, since a
# capped scan can miss a live run buried under newer ones and silently stop
# reporting it live), so the 1 s tick re-scans only every AGENT_LIVE_CACHE_S;
# the cost of being briefly stale is at worst one suppressed-then-allowed push,
# which the debounce tolerates.
AGENT_LIVE_CACHE_S = 3.0

def _agent_module():
    """The chat backend's agent module, or None if it will not import.

    `project_queue.agent_module()` — THE one instance and its one failure
    cache. This used to exec agent.py into a cache of its own (a second copy of
    the module's state in the server, beside the queue's), back when the agent
    was a template reachable only through `claude_spawn.load_agent()`. Kept as
    a name here because the canvases suite replaces exactly this one with a
    stand-in, and None is still an answer the sync handles (no liveness signal,
    the watcher keeps pushing).
    """
    from fused_render import project_queue

    return project_queue.agent_module()


def forget_file_sessions(files: list[str], *, backup=None, log=None) -> dict:
    """Erase every Claude session that was opened on one of `files` — node
    source files a canvas snapshot just deleted.

    A node's chat is filed under its FILE (the explorer embed's per-file
    sessions, `agent._cli_sessions`): delete `udf_2`, make a new `udf_2`, and
    without this the new node's pane offers — and the Tasks page lists — every
    conversation about the old one (owner, 2026-10-25: "the new udf_2 comes
    back with all the claude context for the deleted udf_2"). The file is
    gone, so the chats about it go the way `POST /api/tasks/erase` takes a
    task: transcript, sidecars, triage, read marks, drafts (`tasks.erase_task`).

    A session still RUNNING is left alone and reported (the erase route 409s
    it too; its transcript is under a writing process). `backup(rel, bytes)`
    receives each transcript before it goes. Returns `{erased: [sid],
    skipped: [sid], files: n}`; never raises for one bad session."""
    out: dict = {"erased": [], "skipped": [], "files": len(files)}
    agent = _agent_module()
    listing = getattr(agent, "_cli_sessions", None) if agent is not None else None
    if listing is None:
        return out
    from fused_render.server.routers import tasks as tasks_router

    collected: dict | None = None
    for file in files:
        try:
            rows = listing(file, limit=None)
        except Exception:  # noqa: BLE001 — one unreadable store, not a failure
            logger.warning("canvas: listing sessions on %s failed", file, exc_info=True)
            continue
        for row in rows:
            sid = str(row.get("id") or "")
            if not sid:
                continue
            if row.get("running"):
                out["skipped"].append(sid)
                continue
            if backup is not None and row.get("path"):
                data = canvas_snapshot._read_bytes(row["path"])
                if data is not None:
                    backup(os.path.join("_sessions", str(row.get("backend") or "claude"),
                                        sid + ".jsonl"), data)
            if collected is None:
                collected = tasks_router._collect()
            task = collected.get(sid)
            try:
                if task is not None:
                    tasks_router.erase_task(sid, task)
                else:
                    # A transcript the Tasks page does not list (not a task):
                    # the files and the per-session state still go.
                    _removed, _erased, failed, refused = tasks_router._erase_session_files(
                        sid, row.get("path"))
                    if failed or refused:
                        # As the route: a transcript still on disk is not an
                        # erased session, and its state stays with it.
                        raise OSError(f"{failed + refused} session file(s) not removed")
                    tasks_router.sessions.forget_triage(sid)
                    tasks_router.tasks_store.forget_session(sid)
            except Exception:  # noqa: BLE001 — HTTPException 409/500 included
                logger.warning("canvas: erasing session %s on %s failed", sid, file,
                               exc_info=True)
                out["skipped"].append(sid)
                continue
            out["erased"].append(sid)
    if log is not None and (out["erased"] or out["skipped"]):
        log(f"node sessions erased={len(out['erased'])} skipped={len(out['skipped'])}")
    return out


class _SyncManager:
    """One watcher thread: fingerprint the clone folder, debounce, push.

    Clone files and sync-point state are written under `_op_lock` (short) and
    whole pushes/pulls are serialized by `_flight_lock`; plain attribute reads
    for sync/state are safe under the GIL.
    """

    def __init__(self, env: str, name: str, *, lock_since: float | None = None):
        # (env, name) is the identity: `env` is the `<env>` segment of `dir`,
        # handed to every CLI run of this manager (`_cli_env(cli, self.env)`),
        # and `root` is where its `.sync/` bookkeeping (merge base, trash,
        # push copy, writer lock) lives — beside THIS clone, apart from the
        # other environment's same-named one.
        self.env = env
        self.name = name
        self.root = canvases_root(env)
        self.dir = _canvas_dir(env, name)
        # Observability first: the property setters below (push_state,
        # server_stale) wake the state socket on every change.
        self._log = canvas_sync_state.SyncLog(
            _sync_key(env, name), self._bump,
            path=os.path.join(self.root, ".sync", f"{name}.log"),
            rotate_bytes=SYNC_LOG_ROTATE_BYTES)
        self._push_state = "idle"
        # SERVER_STALE: the remote moved since the sync point and the move is
        # NOT merged yet — held while a Claude run is live, or its merge
        # rolled back — {since, udfs: [{slug, kind}], layout, files: [{path,
        # kind, identical}], key}. While set the auto-push is held; cleared by
        # any merge that lands (the watcher's, turn end, Pull).
        self._server_stale: dict | None = None
        self.stop_event = threading.Event()
        self.pause_count = 0
        self.pause_lock = threading.Lock()
        # SHORT: held only while this process reads or writes the clone's
        # files and the sync-point state that describes them — a snapshot
        # apply, a delta cut, a merge's apply, a push's freeze and fold.
        # NEVER across a network call (invariant 5 of the two-loops build):
        # the push runs the CLI on a frozen copy with this released, and the
        # probe and zip download run before it is taken. An embed snapshot
        # therefore waits milliseconds behind a push, not the push's length.
        # (The one exception is a CLI `pull --force`, which writes the clone
        # itself — see pull().)
        self._op_lock = threading.Lock()
        # LONG: one push or pull at a time, network included — what the old
        # whole-push _op_lock gave for free. Taken non-blocking by the
        # watcher (a busy tick just skips) and with a bounded wait by the
        # manual routes; pause() waits on it too, so a caller pausing to run
        # its own CLI on the folder (clone's re-pull) never races a push's
        # fold-in landing after its rebaseline.
        self._flight_lock = threading.Lock()
        self._push_started_at: float | None = None
        self._push_proc: subprocess.Popen | None = None
        self._rowless: set[str] = set()
        self._fileless: set[str] = set()
        # leg → the last folder-fix set logged (`_log_folder_fixes`).
        self._folder_fixes_logged: dict[str, frozenset] = {}
        self._pull_started_at: float | None = None
        self._pull_in_flight = False
        self._last_pull_error: str | None = None
        # disk → embed leg bookkeeping for sync/state: when each disk_rev was
        # cut (the age of the oldest one the frame has not acked), the last
        # cut's hold reason, a refused delta, and whether any frame has spoken
        # to this manager at all (no frame = nothing owed).
        self._rev_times: dict[int, float] = {}
        self._last_cut_held: str | None = None
        self._delta_error: str | None = None
        self._frame_seen = False
        self._lock_path = _lock_path(env, name)
        self._lock_since = lock_since
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
        self._base_path = os.path.join(self.root, ".sync", f"{name}.json")
        self._base_files: dict[str, str] | None = None
        self._remote: dict | None = None
        # (server state, local file hashes) of an auto-merge that rolled back
        # (fused canvas validate failed on the mix): not retried every poll —
        # a new server state or a local change gets a fresh try.
        self._merge_failed_key: tuple | None = None
        # REVISION SPINE (server with `revision`, 2026-11-11). BASE_REVISION:
        # the collection revision the sync point describes — persisted beside
        # the file hashes, sent as `--if-match` on every push (CAS: a server
        # that moved since answers 412 and the push merges and retries once).
        # Set by every `_rotate_remote` to a manifest that carries one, never
        # cleared by a None rotation: a stale one costs a 412 and a merge,
        # never a lost write. `_server_revision` is the latest probe's.
        self._base_revision: int | None = None
        # The collection the sync point belongs to (`_adopt_revision`; None
        # leaves it standing, like the revision): a canvas deleted and
        # re-created restarts at revision 1 under a new id, so a first look
        # whose id differs is a move even at the same revision.
        self._base_collection_id: str | None = None
        self._server_revision: int | None = None
        # An old fused CLI without `push --if-match`: CAS off for this manager.
        self._cas_unsupported = False
        # The `--if-match` the push being frozen carries (read by _push_argv).
        self._push_if_match: int | None = None
        # The canvas's edit LEASE: this machine's owner name, the lease we
        # hold ({owner, lease_until}), the one ANOTHER owner holds (pushes
        # held: "checked out by <owner>"), and the heartbeat's schedule.
        self._lease_owner = _lease_owner()
        self._lease_mine: dict | None = None
        self._lease_held_by: dict | None = None
        self._lease_disabled = False
        self._lease_next_at = 0.0
        # The last auto/manual merge that changed files here: {at, files} —
        # sync/state shows `server_to_disk: merged` for MERGED_SHOW_S.
        self._last_merge: dict | None = None
        self._last_merge_files: list[str] = []
        # `server_stale` was set because a Claude run was live (the move is
        # held, Q8): the first tick after the run ends merges it.
        self._held_for_run = False
        # IDLE STOP (IDLE_STOP_S): when this manager started (an idle stretch
        # never begins before it), the deadline while no page socket is open
        # (None while one is: sync/state's `idle_stop_at`), and the live run
        # an expired deadline is waiting out (logged once).
        self._started_at = time.time()
        self._idle_stop_at: float | None = None
        self._idle_deferred_for = ""
        # Idle stops put off because a held move or unpublished edits remained
        # (IDLE_STOP_RETRIES), and when the next one may run.
        self._idle_retries = 0
        self._idle_retry_at = 0.0
        # The compact rows of canvas.toml at the sync point (merge_toml's base).
        self._base_toml: dict | None = None
        self.merge_rollback_seq = 0
        self._last_validate_output: list[str] = []
        # `_adopt_converged_deletes`' memo of the server state whose merge
        # rolled back, so the poll does not retry it every tick.
        self._adopt_failed: tuple | None = None
        # Cached answer to "is a Claude session live in this clone?" — see
        # AGENT_LIVE_CACHE_S. Written by whichever of the watcher thread and a
        # status request asks first; a lost race just means one extra scan, so
        # the plain-attribute rule the class docstring states still holds.
        self._agent_run_id = ""
        self._agent_checked_at = 0.0
        # The same for "is a Claude SESSION open on this clone, turn or no
        # turn?" (`agent_session_id`): the idle stop's question.
        self._agent_session_id = ""
        self._agent_session_checked_at = 0.0
        # Embed v3 snapshots (canvas_snapshot.py): the last compact state this
        # manager applied from the embedded workbench, per hydration
        # generation, and the (gen, seq) pairs already applied so a replayed
        # snapshot (the embed re-posts its unacked queue after a reload) is
        # acked again without a second write. Both reset when `gen` moves:
        # a new generation means the embed re-hydrated from the server, so
        # its base is whatever the clone held at that push, not our memory.
        self._embed_gen = 0
        # EMBED_VIEW: the compact state the embed is believed to hold — the
        # `prev` of the snapshot leg and the base of the delta leg
        # (canvas_snapshot.py, "the disk → embed leg"). Its rows are always
        # DISK rows: the whole disk reading after an acked delta, disk
        # restricted to the nodes the embed sent after its own snapshot
        # (canvas_snapshot.held_view — never wider than what the frame holds,
        # or a node Claude added on disk would be "known", skipped by the next
        # delta and deleted by the next snapshot). None = nothing known: the
        # next delta is a full state and the next snapshot falls back to its
        # own `base_nodes`.
        self._embed_view: dict | None = None
        # A generation switch whose view reset waits for that generation's
        # first ACCEPTED post (`_embed_commit_gen`); a delta ack in between
        # re-seeds the view from the frame's own ack and cancels it.
        self._view_reset_pending = False
        # The view was re-seeded by a FULL delta the frame acked (its hello)
        # since the last snapshot was applied: it is that frame's picture, so
        # a generation switch noticed only now (its first snapshot) has
        # nothing to reset. A snapshot applied since clears it.
        self._view_from_hello = False
        # The disk revision the view mirrors; a delta ack for an older rev is
        # stale (a snapshot write re-read disk since) and is ignored.
        self._view_rev = 0
        # DISK_REV: bumped whenever the clone's fingerprint moves — a scan
        # tick, a snapshot write, a merge — or the remote manifest's ids do
        # (a push gave a new node its server id; the ids live outside the
        # clone, so no file moves). sync/state and the events socket carry it;
        # the page asks for a delta when it advances.
        self.disk_rev = 0
        self._rev_key: tuple | None = None
        # WATCHER_EPOCH: identifies THIS manager to the page. A replacement
        # manager (stop/start, server restart) counts disk_rev from 0 again
        # and knows nothing of the frame; the page cannot read that off a
        # counter that may land on the very number the old one left (a quiet
        # canvas sits at 1), so it resets its high-water mark when this moves.
        self.watcher_epoch = f"{int(time.time() * 1000)}-{id(self) & 0xFFFF:04x}"
        # _note_disk runs on the watcher thread (its scan) and on request
        # threads (under _op_lock): the compare-and-bump needs its own lock.
        self._rev_lock = threading.Lock()
        # Disk readings handed to the page, by rev, until the frame acks.
        self._delta_views: dict[int, dict] = {}
        # Node names each cut delta carried an op for, and the names a frame
        # ack reported SKIPPED (it kept its own newer change) that no later
        # acked delta has re-sent yet: a delta cut behind the skipping one
        # (diffed against its reading) never mentions them, and its ack must
        # not make the view forget what the frame still shows for them.
        self._delta_names: dict[int, set] = {}
        # name → the disk_rev when the hold began: only a delta cut AFTER it
        # (its base carries the held picture) settles the name.
        self._skip_hold: dict[str, int] = {}
        self._embed_applied: dict[tuple[int, int], dict] = {}
        self._gen_applied_max: dict[int, int] = {}
        # Causality between the two legs (audit 2026-11-10): the highest delta
        # rev this manager CUT WITH OPS (a full state counts, an empty cut
        # does not). A snapshot names the rev the frame had applied when it
        # serialized it (`body.rev`); one below `_cut_rev` was built before a
        # delta the host had already sent — a stale picture — and is BOUNCED
        # (apply_snapshot): nothing written, the frame applies the delta and
        # posts again. The frame stays the one merge engine.
        self._cut_rev = 0
        # The snapshot seq the last skip-triggered re-cut was made at (see
        # ack_delta): never two re-cuts for one seq, so a frame that keeps
        # skipping cannot drive a delta loop.
        self._skip_recut_seq: tuple[int, int] | None = None
        # What the user did to the canvas from the workbench, for the Claude
        # sessions working in this clone (owner, 2026-11-10: Claude is told in
        # real time). `_journal` is a bounded ring of {seq, at, kind, name,
        # …}; each session reads what it has not seen through the hook
        # endpoint (`hook_post`), `_hook_cursors` keeping its place.
        self._journal: list[dict] = []
        self._journal_seq = 0
        self._hook_cursors: dict[str, int] = {}
        self._hook_denied: set[tuple[str, str]] = set()
        # session → its turn-start and tool hooks (start/pre/post) so far.
        # The Stop route captures it and `turn_end` publishes only while it is
        # unchanged: a newer turn of that session has begun, and its own Stop
        # publishes the change set it is in the middle of. The start counts,
        # not only the tools: a turn's first tool may be a Bash that writes
        # the folder, and that one reports only when it is done (PreToolUse
        # matches the edit tools alone).
        self._tool_hooks: dict[str, int] = {}
        # TOMBSTONES: nodes the user deleted on the canvas, by name →
        # {file, at}, persisted beside the merge base (a restart must not
        # forget them). The user's delete wins (owner, 2026-11-10): a file of
        # that name written back WITHOUT a row (Claude finishing an edit it
        # started before the delete) stays on disk but off the canvas — no
        # default row, not in the push. A row for the name (Claude writing
        # one on purpose, the user re-adding it, a pull bringing it) ends
        # the tombstone.
        self._tombstones_path = os.path.join(self.root, ".sync", f"{name}.tombstones.json")
        self._tombstones: dict[str, dict] = self._load_tombstones()
        self._held_out_logged: list[str] = []
        # Highest seq applied in this generation. A snapshot is the embed's
        # FULL state, so one that arrives after a later seq (a 409 retry that
        # lost the race, a replayed queue) is superseded: it is acked as
        # applied and never touches disk — applied, it would read the newer
        # snapshot's nodes as deletions and trash their files.
        self._embed_max_seq = -1
        # Set by a landed HARD pull: `{rev, until}` — the disk_rev the pull
        # left and a deadline. Until the frame acks a delta at or past that
        # rev (it now shows the server's copy), or a new generation hydrates,
        # every snapshot of the current generation is acked stale and never
        # touches disk: those snapshots describe the canvas the pull just
        # discarded (one that met the pull's 409 and is retrying, one the
        # frame cut before applying the delta), and applied they would write
        # the old canvas straight back. The deadline keeps a frame that never
        # acks (closed, reloading) from fencing the clone for good.
        self._hard_fence: dict | None = None
        # The trash folder the last pull() snapshotted the clone into (None
        # when it took no snapshot); pull()'s answer carries it.
        self._last_trash: str | None = None
        # The body channel's own fence after a hard pull: the disk_rev the
        # pull left, held until the frame ACKS a delta at or past it (or
        # re-hydrates from the server). No deadline: a snapshot fenced too
        # long is a lost layout edit, but a body let through would put the
        # discarded text — or a file the pull removed, which hashes as the
        # empty text — straight back. A frame that never acks posts nothing
        # worth taking.
        self._body_fence_rev: int | None = None
        # Body channel (canvas_snapshot.body_hash): node → the hash of the text
        # this channel last wrote for it. A body whose `base_hash` is that hash
        # overwrites the frame's own previous write, so nothing goes to trash;
        # any other base (Claude's text, a pull) is trashed before the write.
        self._body_written: dict[str, str] = {}
        self._load_base()
        # Bumped on every `_fingerprint` assignment (the property below), so the
        # scan can tell that a pull or push re-baselined while it was walking.
        self._fp_gen = 0
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
        try:
            canvas_snapshot.add_canvas_type(self.dir)
        except Exception as exc:
            self.log_event("server_to_disk", "type not added", ok=False, detail=str(exc))
        self._dirty_since: float | None = None if fresh else now
        self.push_state = "idle" if fresh else "pending"  # idle | pending | pushing | error
        self._last_pull_poll = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"fused-canvas-sync-{name}")
        self.thread.start()
        # Disk → events: a file write wakes the delta leg within ~100 ms
        # instead of on the next 1 s scan (which stays as the push debounce's
        # backstop, and the only disk signal when watchfiles is missing).
        self.fs_thread = threading.Thread(target=self._watch_fs, daemon=True,
                                          name=f"fused-canvas-fs-{name}")
        self.fs_thread.start()

    # -- observable state (each change wakes the sync/events socket) ---------

    @property
    def push_state(self) -> str:
        return self._push_state

    @push_state.setter
    def push_state(self, value: str) -> None:
        if value != self._push_state:
            self._push_state = value
            self._bump()

    @property
    def server_stale(self) -> dict | None:
        return self._server_stale

    @server_stale.setter
    def server_stale(self, value: dict | None) -> None:
        if value != self._server_stale:
            self._server_stale = value
            self._bump()

    @property
    def _fingerprint(self) -> dict:
        """The push debounce's baseline. Assigned by the scan (a change it
        saw) and by every pull/push re-baseline; each assignment bumps
        `_fp_gen` (see `_run`)."""
        return self._fingerprint_v

    @_fingerprint.setter
    def _fingerprint(self, value: dict) -> None:
        self._fingerprint_v = value
        self._fp_gen += 1

    def _bump(self) -> None:
        canvas_sync_state.notify(_sync_key(self.env, self.name))

    def log_event(self, leg: str, action: str, *, ms: float | None = None,
                  ok: bool = True, detail: str = "") -> dict:
        return self._log.add(leg, action, ms=ms, ok=ok, detail=detail)

    def _paused(self) -> bool:
        with self.pause_lock:
            return self.pause_count > 0

    def pause(self) -> None:
        with self.pause_lock:
            self.pause_count += 1
        # Block until any push/pull already in flight finishes — its network
        # leg (_flight_lock) and any file write (_op_lock). pause_count is
        # now >0, so neither the watcher nor a route starts a NEW one.
        with self._flight_lock:
            pass
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
                self._set_sync_point()
                self._rotate_remote(None)
                self._save_base()
                # A stale "pending"/"error" from before the pause would
                # otherwise stick forever: sync/state keeps showing a queued
                # push, and the external-CLI poll leg only runs while
                # push_state == "idle".
                self.push_state = "idle"
                self.error_detail = []

    def stop(self, *, release_lease: bool = True) -> None:
        # Join, not just signal: without the join, a caller (logout, test
        # teardown) can race ahead while the thread is still mid-subprocess —
        # e.g. logout clearing credentials out from under an in-flight push,
        # or (in tests) the thread's next subprocess call landing in the NEXT
        # test's env/log after monkeypatch has moved on. Bounded by
        # STOP_JOIN_S all the same: a push inside its CLI can hold the thread
        # for PUSH_TIMEOUT, and the quit rung terminates that CLI first.
        self.stop_event.set()
        self.thread.join(timeout=STOP_JOIN_S)
        if self.thread.is_alive():
            logger.warning("canvas %s: watcher still busy after %.0fs; stopping without it",
                           _sync_key(self.env, self.name), STOP_JOIN_S)
        # watchfiles polls stop_event every `step` ms; bounded anyway so a
        # wedged notify backend can never hang logout or a test teardown.
        self.fs_thread.join(timeout=2.0)
        # The canvas's edit lease goes back with the watcher (stop, logout),
        # bounded: a lapsed lease (LEASE_TTL_S) is the fallback when this
        # cannot reach the server. The quit rung releases every manager's
        # lease itself, in parallel under one LEASE_RELEASE_QUIT_S budget,
        # and stops them with `release_lease=False`.
        if release_lease:
            try:
                self.release_lease()
            except Exception:  # noqa: BLE001 — the lock release below still runs
                logger.debug("canvas %s: lease release failed",
                             _sync_key(self.env, self.name), exc_info=True)
        if canvas_sync_state.release_lock(self._lock_path, self._lock_since):
            self.log_event("watcher", "lock released",
                           detail=f"pid {os.getpid()}")
        self._bump()

    def _watch_fs(self) -> None:
        """fs-event leg of the disk signal: every batch of changes in the
        clone (dot-names and the seeded helper files excluded, the same set
        the fingerprint skips) advances `disk_rev` right away."""
        try:
            import watchfiles
        except ImportError:
            logger.info("canvas %s: watchfiles is not installed; disk changes reach "
                        "the embed on the %.1fs scan only", _sync_key(self.env, self.name), SCAN_INTERVAL_S)
            return

        root = os.path.realpath(self.dir)

        def keep(_change, path: str) -> bool:
            try:
                rel = os.path.relpath(os.path.realpath(path), root)
            except ValueError:
                return False
            parts = rel.split(os.sep)
            if parts[0] == os.pardir:
                return False
            if any(p.startswith(".") for p in parts):
                return False
            return parts[-1] not in _SYNC_IGNORED_BASENAMES

        try:
            for _changes in watchfiles.watch(self.dir, watch_filter=keep,
                                             stop_event=self.stop_event,
                                             debounce=50, step=20,
                                             yield_on_timeout=False,
                                             raise_interrupt=False):
                if self.stop_event.is_set():
                    break
                self._note_disk()
        except Exception as exc:  # noqa: BLE001 — the 1 s scan carries on alone
            if not self.stop_event.is_set():
                logger.warning("canvas %s: fs watcher stopped (%s); falling back to "
                               "the %.1fs scan", _sync_key(self.env, self.name), exc, SCAN_INTERVAL_S)
                self.log_event("watcher", "fs watch stopped", ok=False, detail=str(exc))

    def agent_run_id(self, *, fresh: bool = False) -> str:
        """The id of a Claude run live in this clone, or "" — cached.

        `limit=None` on purpose: the capped default scan can miss a live run
        buried under 60 newer run dirs, and nothing prunes RUNS. Here a miss
        means the auto-push fires mid-change-set, so this caller pays for the
        reliable answer and caches it.
        """
        now = time.time()
        if not fresh and now - self._agent_checked_at < AGENT_LIVE_CACHE_S:
            return self._agent_run_id
        agent = _agent_module()
        run_id = ""
        if agent is not None:
            try:
                # Any run in the clone: the folder chat AND a chat opened on a
                # node's file (its target is `<clone>/<node>.<ext>`), which an
                # exact-target `_live_run(self.dir)` never matched — so a node
                # chat's half-done change set was auto-pushed. `_live_run` stays
                # the fallback for an agent module without the folder lookup.
                live_in = getattr(agent, "_live_run_in", None)
                found = (live_in(self.dir) if live_in is not None
                         else agent._live_run(self.dir, limit=None))
                run_id = str(found.get("run_id") or "")
            except Exception:  # noqa: BLE001 — a failed read must not stop the
                # watcher, and "cannot tell" is reported as "not live": the
                # alternative is a lock that never releases.
                logger.debug("live-run lookup failed for %s", self.dir, exc_info=True)
        self._agent_run_id = run_id
        self._agent_checked_at = now
        return run_id

    def agent_session_id(self, *, fresh: bool = False) -> str:
        """The id of a run whose Claude SESSION is still up in this clone —
        between turns too — or "" — cached like `agent_run_id`. "" as well
        for an agent module without the lookup."""
        now = time.time()
        if not fresh and now - self._agent_session_checked_at < AGENT_LIVE_CACHE_S:
            return self._agent_session_id
        agent = _agent_module()
        run_id = ""
        live_in = getattr(agent, "_live_session_in", None) if agent is not None else None
        if live_in is not None:
            try:
                run_id = str(live_in(self.dir).get("run_id") or "")
            except Exception:  # noqa: BLE001 — "cannot tell" reads as "none"
                logger.debug("live-session lookup failed for %s", self.dir, exc_info=True)
        self._agent_session_id = run_id
        self._agent_session_checked_at = now
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

    def _take_file_hashes(self, top: str) -> dict[str, str]:
        """Per-file md5 of `top` (the clone, or the frozen copy a push
        ships), same walk/skip rules as the fingerprint (relpaths are
        os-native, matching zip relpaths after os.path.join normalization
        on this platform)."""
        hashes: dict[str, str] = {}
        for root, dirs, files in os.walk(top):
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
                hashes[os.path.relpath(path, top)] = digest
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
        base_toml = data.get("base_toml")
        if isinstance(base_toml, dict) and isinstance(base_toml.get("nodes"), dict):
            self._base_toml = base_toml
        base_revision = data.get("base_revision")
        if isinstance(base_revision, int) and not isinstance(base_revision, bool):
            self._base_revision = base_revision
        # Saved beside the revision; a base from before that keeps it only
        # in its manifest.
        collection_id = data.get("collection_id") or (self._remote or {}).get("id")
        if isinstance(collection_id, str) and collection_id:
            self._base_collection_id = collection_id

    def _save_base(self) -> None:
        payload = json.dumps(
            {
                "files": self._base_files,
                "remote": self._remote,
                "base_toml": self._base_toml,
                # The revision spine's sync point: the collection revision the
                # files above were last in step with (None on an old server),
                # and the collection it belongs to (a re-created canvas starts
                # over at revision 1 under a new id).
                "base_revision": self._base_revision,
                "collection_id": self._base_collection_id,
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
        """slug → body hash, from a manifest's `udfs` mapping (what
        `_note_server_stale` lists as changed). Timestamps are deliberately
        dropped: a stale writer
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
        """Replace the manifest baseline (the sync point's view of the
        remote). Every _remote assignment after construction goes through
        here, except the poll leg's first-look adopt (_poll_remote), which
        must not clear a stale flag; a rotation means the remote is no longer
        stale."""
        self._remote = new_remote
        self._adopt_revision(new_remote)
        self.server_stale = None
        self._held_for_run = False
        self._last_pull_error = None
        # New ids reach the frame through the delta leg, which only wakes on
        # a disk_rev move: count a manifest rotation as one.
        self._note_disk()

    def _adopt_revision(self, manifest: dict | None) -> None:
        """The sync point now describes `manifest`: its revision (when the
        server has the spine) becomes `base_revision`. A None or revision-less
        manifest leaves the last one standing (see `_base_revision`)."""
        rev = (manifest or {}).get("revision")
        if isinstance(rev, int) and not isinstance(rev, bool):
            self._base_revision = rev
        cid = (manifest or {}).get("id")
        if isinstance(cid, str) and cid:
            self._base_collection_id = cid

    def _reconcile_rows(self, leg: str, directory: str, **kw) -> list[str]:
        try:
            result = canvas_snapshot.reconcile_rows(directory, **kw)
        except Exception as exc:
            self.log_event(leg, "rows not reconciled", ok=False, detail=str(exc))
            return []
        if result["changed"]:
            self.log_event(leg, "rows reconciled",
                           detail=f"dropped {', '.join(result['dropped']) or 'none'}; "
                                  f"added {', '.join(result['added']) or 'none'}")
        return result["dropped"]

    def trash_root(self) -> str:
        """Where this canvas's trash snapshots go: `.sync/trash/<name>/`, one
        timestamped folder per pull/merge that overwrote or deleted files.
        Shown on sync/state so the page can say in advance where a Hard
        pull's backup lands."""
        return os.path.join(self.root, ".sync", "trash", self.name)

    def _new_trash_dir(self, *, prune: bool = True) -> str | None:
        """A fresh timestamped folder under .sync/trash/<name>/ for the files
        the current pull/merge is about to overwrite or delete; prunes the
        oldest snapshots beyond _TRASH_MAX (`prune=False` defers that to
        `_prune_trash`, for a caller that must not burn a recovery point
        before its own snapshot is known complete). None if it can't be
        created."""
        root = self.trash_root()
        path = os.path.join(root, time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            return None
        if prune:
            self._prune_trash(keep=path)
        return path

    def _prune_trash(self, *, keep: str) -> None:
        """Drop the oldest trash snapshots beyond _TRASH_MAX, never `keep`."""
        root = self.trash_root()
        try:
            snapshots = sorted(
                d for d in os.listdir(root)
                if os.path.isdir(os.path.join(root, d))
            )
        except OSError:
            return
        for stale in snapshots[:-_TRASH_MAX]:
            if os.path.join(root, stale) != keep:
                _rmtree_quiet(os.path.join(root, stale))

    def _backup_to(self, trash: str | None, rel: str, data: bytes) -> bool:
        """Copy one file into the trash snapshot. False when it could not be
        written (full disk, permissions): a best-effort safety net for the
        merge legs, but `_snapshot_clone` counts it so a hard pull can refuse."""
        if trash is None:
            return False
        dest = os.path.join(trash, rel)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
        except OSError:
            return False  # never blocks the sync
        return True

    def _snapshot_clone(self) -> str | None:
        """Copy the whole clone into a trash snapshot — run before the CLI's
        wholesale `pull --force`, whose overwrites/deletes we can't
        intercept per file. Returns the snapshot folder, or None when it
        could not be created OR any file could not be read or copied into
        it — a folder on a full disk, or one missing a file a live session
        held mid-replace, is not a backup (a hard pull then refuses to run,
        see _pull; the merge legs never call this). A failed snapshot is
        removed, and older snapshots are pruned only after a complete one,
        so retrying on a full disk never burns a real recovery point."""
        trash = self._new_trash_dir(prune=False)
        if trash is None:
            return None
        failed: list[str] = []
        for root, dirs, files in os.walk(self.dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith("."):
                    continue
                path = os.path.join(root, f)
                rel = os.path.relpath(path, self.dir)
                try:
                    with open(path, "rb") as fh:
                        data = fh.read()
                except OSError:
                    failed.append(rel)
                    continue
                if not self._backup_to(trash, rel, data):
                    failed.append(rel)
        if failed:
            self.log_event("server_to_disk", "backup incomplete", ok=False,
                           detail=f"{len(failed)} file(s) could not be copied into {trash}: "
                                  + ", ".join(failed[:5]))
            _rmtree_quiet(trash)
            return None
        self._prune_trash(keep=trash)
        return trash

    def _validate_clone(self) -> bool | None:
        """`fused canvas validate` on the clone: True = valid, False =
        invalid, None = couldn't run (no CLI/timeout) — treated as valid so
        a broken CLI can't wedge every merge into a rollback."""
        cli = fused_cli()
        if cli is None:
            return None
        try:
            proc = subprocess.run(
                [*cli.command, "workbench", "canvas", "validate", self.dir, "--skip", "node_files"],
                capture_output=True, text=True, timeout=VALIDATE_TIMEOUT,
                encoding="utf-8", errors="replace",
                env=_cli_env(cli, self.env),
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        # Kept for the rollback's pull error: "validate failed" alone leaves
        # the reader guessing which node or edge the merge broke.
        self._last_validate_output = [
            ln.strip() for ln in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines()
            if ln.strip()][:_ERROR_DETAIL_MAX]
        return proc.returncode == 0

    def _log_folder_fixes(self, leg: str, fixes: list[str]) -> None:
        """Log `normalise_folders`' lines for `leg` once per distinct set:
        the same stale grammar re-arrives on every snapshot and every pull
        until someone fixes it, and one line says so."""
        key = frozenset(fixes)
        if self._folder_fixes_logged.get(leg) == key:
            return
        self._folder_fixes_logged[leg] = key
        if fixes:
            self.log_event(leg, "folder fixes",
                           detail="; ".join(fixes[:5])
                           + (f" (+{len(fixes) - 5})" if len(fixes) > 5 else ""))

    def _normalise_clone_toml(self) -> bool:
        """`_normalised_toml` over the clone's canvas.toml in place (a CLI
        force pull wrote it); True when it rewrote the file."""
        path = os.path.join(self.dir, "canvas.toml")
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return False
        fixed = self._normalised_toml(data)
        if fixed == data:
            return False
        try:
            canvas_snapshot._write_atomic(path, fixed.decode("utf-8"))
        except OSError:
            return False
        return True

    def _normalised_toml(self, data: bytes) -> bytes:
        """A canvas.toml the pull is about to write wholesale, with its
        folder grammar normalised (`canvas_snapshot.normalise_folders`) —
        never refused: the server accepts what this cannot honour, and a
        refused pull would leave both pull and push stuck. `data` itself
        when nothing needed fixing or it does not parse."""
        try:
            import rtoml
            doc = rtoml.loads(data.decode("utf-8", "replace"))
            canvas = doc.get("canvas")
            rows = canvas.get("nodes") if isinstance(canvas, dict) else None
            if not isinstance(rows, list):
                return data
            fixes = canvas_snapshot.normalise_folders(rows)
            self._log_folder_fixes("server_to_disk", fixes)
            if not [f for f in fixes if not f.endswith("(not fixed)")]:
                return data
            return canvas_snapshot._dump_toml(doc).encode("utf-8")
        except Exception:  # noqa: BLE001 — rtoml's own error types
            return data

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
                env=_cli_env(cli, self.env),
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
        """Did the server's canvas change between two manifests? With the
        revision spine on both (a server from 2026-11-11 on): the collection
        `revision` moved — nothing else, no stamp or hash (a re-serialised
        body moves its hash with no write). Otherwise the old rule: the
        collection stamp (layout/dashboard saves) and each UDF's hash and
        stamp — NOT its `id`. The id is identity, not an edit: it is absent
        from a baseline saved by a build before the shim emitted ids and
        from a UDF the push has only just created, and read as a move it
        would hold every push behind a "server changed" chip that lists
        nothing (_note_server_stale compares hashes and finds no change)."""
        if "revision" in prev and "revision" in cur:
            # A canvas deleted and re-created restarts at revision 1 under a
            # new id: the id is part of the identity here.
            return (prev.get("revision") != cur.get("revision")
                    or prev.get("id") != cur.get("id"))
        if prev.get("last_updated") != cur.get("last_updated"):
            return True
        return _SyncManager._udf_edits(prev) != _SyncManager._udf_edits(cur)

    @staticmethod
    def _udf_edits(manifest: dict) -> dict:
        """Each UDF's (hash, stamp) — the edit half of _remote_moved."""
        udfs = manifest.get("udfs")
        if not isinstance(udfs, dict):
            return {}
        return {
            slug: ((info.get("hash"), info.get("last_updated"))
                   if isinstance(info, dict) else info)
            for slug, info in udfs.items()
        }

    @classmethod
    def _move_key(cls, probe: dict) -> object:
        """One server state, for the once-per-state memos: its revision when
        the server has the spine, else its stamp and UDF edits."""
        if "revision" in probe:
            # The id too: a canvas deleted and re-created restarts at
            # revision 1 under a new id (`_remote_moved` reads it as a move).
            return ("rev", probe.get("id"), probe.get("revision"))
        return ("stamp", probe.get("last_updated"),
                tuple(sorted((k, repr(v)) for k, v in cls._udf_edits(probe).items())))

    def _bundle_matches_sync_point(self, bundle: dict[str, bytes]) -> bool:
        """Is the server's bundle the sync point's files? Same paths, and each
        file's md5 equals the base hash — except a .toml the server
        re-serialized (canvas.toml comes back from its database, not as the
        bytes pushed). That one counts when it parses equal to the local file
        that is still the base; or, the local file edited since (an unpushed
        layout change), when its [canvas] rows equal the sync point's
        (`_base_toml`) and every other table equals the local file's.
        Under _op_lock."""
        base = self._base_files
        if base is None or set(bundle) != set(base):
            return False
        for rel, data in bundle.items():
            if hashlib.md5(data).hexdigest() == base[rel]:
                continue
            if not rel.endswith(".toml"):
                return False
            try:
                with open(os.path.join(self.dir, rel), "rb") as f:
                    local = f.read()
            except OSError:
                return False
            try:
                remote_doc = tomllib.loads(data.decode("utf-8"))
                local_doc = tomllib.loads(local.decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                return False
            if hashlib.md5(local).hexdigest() == base[rel]:
                if remote_doc != local_doc:
                    return False
                continue
            if rel != "canvas.toml" or self._base_toml is None:
                return False
            rest = lambda doc: {k: v for k, v in doc.items() if k != "canvas"}  # noqa: E731
            if (canvas_snapshot.rows_of_doc(remote_doc) != self._base_toml
                    or rest(remote_doc) != rest(local_doc)):
                return False
        return True

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
                env=_cli_env(cli, self.env),
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0 or not proc.stdout:
            return None
        try:
            return zipfile.ZipFile(io.BytesIO(proc.stdout), "r")
        except zipfile.BadZipFile:
            return None

    def _fetch_bundle(self, probe: dict) -> dict[str, bytes] | None:
        """The remote's files for `probe`, as {os-native relpath: bytes} — the
        network half of a merge, run WITHOUT _op_lock. None on a transient
        failure (no CLI, no shim, download error, junk zip)."""
        zf = self._download_zip(probe["id"])
        if zf is None:
            return None
        bundle: dict[str, bytes] = {}
        with zf:
            for info in zf.infolist():
                rel = _safe_zip_member_relpath(info.filename)
                if rel is None:
                    continue
                # A seeded helper file that somehow reached the remote (a
                # push from elsewhere with --no-ignore) must not overwrite
                # the local seed.
                if rel.rsplit("/", 1)[-1] in _SYNC_IGNORED_BASENAMES:
                    continue
                try:
                    data = zf.read(info.filename)
                except (zipfile.BadZipFile, zlib.error, OSError, EOFError):
                    # A corrupt member is a transient failure like a failed
                    # download — never an exception that kills the watcher.
                    return None
                bundle[os.path.join(*rel.split("/"))] = data
        return bundle

    def _merge_remote(self, probe: dict, bundle: dict[str, bytes] | None, *,
                      two_way: bool = False) -> bool:
        """Apply remote changes into a DIRTY clone, per file, three-way.
        Under _op_lock; `bundle` is `_fetch_bundle(probe)`, fetched by the
        caller BEFORE taking the lock (None = that download failed).

        base = last sync point. For each bundle file: local untouched since
        base → remote wins; local changed → local wins (the debounced push
        publishes it); local deleted a file that's still in the bundle → it
        stays deleted (the push propagates the delete). Files in base but
        gone from the bundle are remote deletes: removed locally only if
        untouched. EXCEPT canvas.toml, which both sides changing merges per
        node (canvas_snapshot.merge_toml against `_base_toml`). Without a
        base (unknown provenance) nothing is applied — local wins wholesale,
        exactly the pre-merge behavior.

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
        download) — the caller must NOT push over an unreconciled remote.

        `two_way` (an explicit Pull on a dirty clone with no sync point, e.g.
        after its `.sync/<name>.json` was lost): merge against an EMPTY base
        instead — every local file that differs from the server's wins, files
        only the server has come in, nothing local is deleted, and
        canvas.toml takes only the nodes local lacks. It used to be a
        wholesale `pull --force` whose trash copy was the only one left of
        the session's edits, and the next push published the old files."""
        base = self._base_files
        if base is None and two_way:
            base = {}
        if base is None:
            self._rotate_remote(probe)
            self._save_base()
            return True
        if bundle is None:
            return False  # transient — retried with the same probe
        new_base = dict(base)
        trash: str | None = None
        _MISSING = object()
        # Node names whose source file this merge removed (a server delete).
        remote_deleted: set[str] = set()
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
            if (rel == "canvas.toml" and local_bytes is not None
                    and local_hash != base.get(rel) and remote_hash != base.get(rel)):
                # Both sides moved the one file every writer touches: merge it
                # per NODE (canvas_snapshot.merge_toml), not per file — a
                # per-file "local wins" here is what drops a teammate's new
                # node row whenever the clone has any unpushed layout edit.
                merged = self._merge_toml_bytes(local_bytes, data)
                if merged is None:
                    continue  # unparsable on a side: local wins, as for any file
                if merged != local_bytes:
                    if trash is None:
                        trash = self._new_trash_dir()
                    self._backup_to(trash, rel, local_bytes)
                    try:
                        with open(dest, "wb") as fh:
                            fh.write(merged)
                    except OSError:
                        continue
                    rollback.append((rel, local_bytes, base.get(rel, _MISSING)))
                # The base for the NEXT three-way is the remote's rows: what
                # differs from them on disk is this clone's unpushed edits.
                new_base[rel] = remote_hash
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
            if rel == "canvas.toml":
                # Taken whole from the remote: normalised like a merged one
                # (the base stays the remote's hash, so a fix it needed is
                # this clone's edit and the push publishes it).
                data = self._normalised_toml(data)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(data)
            except OSError:
                continue
            rollback.append((rel, local_bytes, base.get(rel, _MISSING)))
            new_base[rel] = remote_hash
        try:
            node_file_set = set(canvas_snapshot.node_files(self.dir).values())
        except OSError:
            node_file_set = set()
        overridden: list[str] = []
        for rel in list(new_base):
            if rel in bundle:
                continue
            # In base, gone from the bundle: remote deleted it. Removed
            # locally if untouched since the sync point — and, for a NODE's
            # source file, even when edited here: the server's delete wins
            # over an edit it never saw (owner, co-edit audit 2026-11-10).
            # Kept, the file would come back as a node with a default row
            # and its layout lost; the edit is in the trash copy.
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
            node_file = os.sep not in rel and rel in node_file_set
            if local_hash is not None and (local_hash == new_base[rel] or node_file):
                if local_hash != new_base[rel]:
                    overridden.append(rel.replace(os.sep, "/"))
                if trash is None:
                    trash = self._new_trash_dir()
                self._backup_to(trash, rel, local_bytes)
                try:
                    os.remove(dest)
                except OSError:
                    continue
                rollback.append((rel, local_bytes, base.get(rel, _MISSING)))
                if os.sep not in rel and rel.endswith(canvas_snapshot.BODY_SUFFIXES):
                    remote_deleted.add(os.path.splitext(rel)[0])
            del new_base[rel]
        if overridden:
            self.log_event("server_to_disk", "server delete won over a local edit",
                           detail=", ".join(overridden) + f" (a copy is in {trash})")
        if remote_deleted and os.path.exists(os.path.join(self.dir, "canvas.toml")):
            # The server deleted these nodes and the merge removed their files,
            # but a row this clone had only MOVED (a frame drag, an unpushed
            # layout edit) survives the per-node toml merge: a fileless row the
            # push leaves out, so disk, server and frame would never agree
            # again. The server's delete wins over a layout-only edit — drop
            # those rows (no default rows added).
            try:
                with open(os.path.join(self.dir, "canvas.toml"), "rb") as fh:
                    toml_before = fh.read()
                rowed = set(canvas_snapshot.rows_of_doc(canvas_snapshot._load_toml(
                    os.path.join(self.dir, "canvas.toml")))["nodes"])
            except Exception:  # noqa: BLE001 — unreadable: nothing to drop here
                toml_before, rowed = None, set()
            # Only when such a row is really there: a reconcile rewrites the
            # file (normalising it), and a canvas.toml the merge took
            # verbatim from the server must stay byte-for-byte.
            if toml_before is not None and rowed & remote_deleted and self._reconcile_rows(
                    "server_to_disk", self.dir, drop=remote_deleted, add=False):
                if trash is None:
                    trash = self._new_trash_dir()
                self._backup_to(trash, "canvas.toml", toml_before)
                rollback.append(("canvas.toml", toml_before,
                                 new_base.get("canvas.toml", _MISSING)))
        # What this merge wrote or deleted here (`_merge_now` reports it to
        # the strip, the log and the Claude session's hook).
        self._last_merge_files = [r for r, _b, _p in rollback]
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
            self.log_event("server_to_disk", "pull rollback", ok=False,
                           detail="merged clone failed `fused canvas validate`; restored "
                                  + ", ".join(r for r, _b, _p in rollback[:5]))
            rollback = []
            rolled_back = True
            # Nothing of the server's move is on disk any more: nobody may
            # be told it was merged (strip, journal, the session's hook).
            self._last_merge_files = []
        else:
            rolled_back = False
        if not (rolled_back and self._base_files is None):
            # A two-way merge that rolled back restored every file it wrote:
            # the clone still has no sync point, and a partial dict here would
            # pass for one (three-way Pulls, `_converged_deletes`, the push
            # refusal's wording).
            self._base_files = new_base
        if not rolled_back:
            remote_toml = bundle.get("canvas.toml")
            if remote_toml is not None:
                try:
                    import rtoml
                    self._base_toml = canvas_snapshot.rows_of_doc(
                        rtoml.loads(remote_toml.decode("utf-8", "replace")))
                except Exception:  # noqa: BLE001 — keep the previous base
                    pass
            # The remote is reconciled: it is the new sync point, and the
            # canvas is no longer server-stale.
            self._rotate_remote(probe)
        # Rolled back: the remote baseline stays at the sync point, so the
        # next probe re-detects the move and the chip stays up — the held
        # push must NOT ship local over a remote change the merge could not
        # take. (It used to advance to avoid re-attempting every poll; with an
        # explicit Pull there is no loop to avoid.)
        self._save_base()
        if rollback:
            self.merge_seq += 1
            self.last_pull_at = time.time()
        return True

    def _poll_remote(self, probe: dict, bundle: dict[str, bytes] | None = None) -> str:
        """The poll leg's read of a fresh manifest, under _op_lock. Adopts a
        baseline on the first look; after that answers what the caller must
        do: "same" (no move — any stale flag clears), "rebaselined" (the
        server moved but its `bundle` is byte-for-byte the sync point — a
        share-settings stamp bump, a re-serialised widget body, a probe that
        read our own import late — so the probe becomes the baseline, no
        merge, no chip; every server, every move), or "moved" (a real change:
        the caller merges it, or holds it while a Claude run is live). A
        move with no `bundle` (not fetched yet) answers "moved"."""
        if self._remote is None:
            rev = probe.get("revision")
            cid = probe.get("id")
            recreated = (self._base_collection_id is not None and isinstance(cid, str)
                         and cid != self._base_collection_id)
            if (self._base_files is not None and self._base_revision is not None
                    and isinstance(rev, int)
                    and (rev != self._base_revision or recreated)):
                # No manifest baseline (the post-push probe failed, a restart,
                # a resume) but the sync point KNOWS its revision: a live one
                # past it is a server MOVE since our files, to merge — never a
                # new baseline (adopting it would If-Match the newer revision
                # with the older files and skip the 412 that merges it). So is
                # the same revision under another id (re-created canvas): the
                # stand-in keeps OUR id so `_remote_moved` reads the move.
                self._remote = {**probe, "revision": self._base_revision,
                                "id": self._base_collection_id or cid}
                self._note_disk()
                self._save_base()
                return "moved"
            # First look (fresh manager, or right after clone/push dropped
            # it): adopt as baseline. A remote edit made before this first
            # look is indistinguishable from the sync point — same blind
            # spot the pre-merge sync had; converges on the next remote save.
            self._remote = probe
            self._adopt_revision(probe)
            # The manifest's ids reach the frame through the delta leg, which
            # wakes on disk_rev alone (see _note_disk): a push whose probe
            # failed dropped the manifest, and this re-adoption is where the
            # new server ids come back.
            self._note_disk()
            # No merge base yet (first open after upgrade — no .sync file)
            # and the watcher believes the clone is clean: local == remote
            # is the steady state, so the disk IS the sync point. Without
            # this the base stays None and every later merge degrades to
            # local-wins wholesale. A dirty clone is left alone — hashing
            # it would bless unpushed edits as "already synced".
            if self._base_files is None and self._dirty_since is None and self.push_state == "idle":
                self._set_sync_point()
            self._save_base()
            return "same"
        if not self._remote_moved(self._remote, probe):
            if not self._held_for_run:
                self.server_stale = None
            return "same"
        if bundle is not None and self._bundle_matches_sync_point(bundle):
            self._rotate_remote(probe)
            self._save_base()
            self.log_event("server_to_disk", "server moved, same bytes, rebaselined",
                           detail=self._revision_text(probe))
            return "rebaselined"
        return "moved"

    @staticmethod
    def _revision_text(probe: dict | None) -> str:
        rev = (probe or {}).get("revision")
        return f"server revision {rev}" if rev is not None else ""

    def _stale_files(self, bundle: dict[str, bytes] | None) -> list[dict]:
        """What the server changed since the sync point, per file, with
        `identical`: the server's file is already what this clone holds (a
        .toml compared parsed — the server re-serialises it), so merging it
        changes nothing here. [] without a bundle or a sync point."""
        base = self._base_files
        if bundle is None or base is None:
            return []
        out: list[dict] = []
        for rel in sorted(set(bundle) | set(base)):
            data = bundle.get(rel)
            remote_hash = hashlib.md5(data).hexdigest() if data is not None else None
            if remote_hash == base.get(rel):
                continue
            kind = "added" if rel not in base else "removed" if data is None else "changed"
            try:
                with open(os.path.join(self.dir, rel), "rb") as fh:
                    local: bytes | None = fh.read()
            except OSError:
                local = None
            identical = local == data
            if not identical and local is not None and data is not None and rel.endswith(".toml"):
                try:
                    identical = (tomllib.loads(local.decode("utf-8"))
                                 == tomllib.loads(data.decode("utf-8")))
                except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                    identical = False
            out.append({"path": rel.replace(os.sep, "/"), "kind": kind, "identical": identical})
        return out

    def _merge_now(self, probe: dict | None = None, bundle: dict[str, bytes] | None = None,
                   *, wait: float | None = None, manual: bool = False,
                   fence: Callable[[], bool] | None = None) -> dict:
        """Bring a server move into the clone: the ONE merge every road runs —
        the poll when no Claude run is live, the first tick after a run ends,
        the turn-end (Stop hook), a 412 push, the page's Pull. Per file
        against the sync point (`_merge_remote`; canvas.toml per node), clean
        clone or dirty: a file only the server changed comes in, one only
        this clone changed stays, a remote delete of an untouched file lands.
        Never a wholesale `pull --force` (that is Hard pull's alone).

        The caller holds `_flight_lock` and NOT `_op_lock`: the probe and the
        zip download run unlocked, only the apply takes _op_lock (bounded by
        `wait` when given — a busy clone answers code "busy"). Answers
        `{ok, error?, code?, files}`; ok False leaves the clone untouched
        (download failed, or the merged mix failed `fused canvas validate`
        and was rolled back — then `server_stale` carries the reason).
        `fence()` is asked once the apply holds _op_lock, after the
        download: True answers code "superseded" with nothing applied (the
        turn end whose session started its next turn while this ran)."""
        if probe is None:
            probe = self._probe_remote()
            if probe is None:
                return {"ok": False, "error": "could not read the canvas from the server",
                        "files": []}
        if bundle is None:
            bundle = self._fetch_bundle(probe)
            if bundle is None:
                return {"ok": False, "files": [],
                        "error": "could not download the canvas from the server; try again"}
        local_key = None
        if not manual:
            local_key = (self._move_key(probe),
                         tuple(sorted(self._take_file_hashes(self.dir).items())))
            if local_key == self._merge_failed_key:
                return {"ok": False, "files": [], "code": "rolled_back",
                        "error": self._last_pull_error or "the last merge of this server "
                                 "state failed validation"}
        if wait is None:
            self._op_lock.acquire()
        elif not self._op_lock.acquire(timeout=wait):
            return {"ok": False, "code": "busy", "files": [],
                    "error": "a sync operation is in flight for this canvas; try again in a moment"}
        try:
            if fence is not None and fence():
                return {"ok": False, "code": "superseded", "files": [],
                        "error": "a newer turn started in this folder"}
            if self._base_files is not None and self._bundle_matches_sync_point(bundle):
                self._rotate_remote(probe)
                self._save_base()
                self.log_event("server_to_disk", "server moved, same bytes, rebaselined",
                               detail=self._revision_text(probe))
                return {"ok": True, "files": []}
            rollbacks = self.merge_rollback_seq
            self._last_merge_files = []
            if not self._merge_remote(probe, bundle, two_way=self._base_files is None):
                return {"ok": False, "files": [],
                        "error": "could not download the canvas from the server; try again"}
            if self.merge_rollback_seq != rollbacks:
                why = "; ".join(self._last_validate_output[:5])
                error = ("merging the server's changes would break the canvas "
                         "(fused canvas validate failed" + (f": {why}" if why else "")
                         + "); nothing was changed. Fix or revert the local files it "
                         "names, then pull again")
                self._merge_failed_key = local_key
                self._note_server_stale(probe, bundle)
                self._last_pull_error = error
                return {"ok": False, "files": [], "error": error, "code": "rolled_back"}
            self._merge_failed_key = None
            files = [f.replace(os.sep, "/") for f in self._last_merge_files]
            _seed_clone_claude_files(self.dir, self.name)
            # Dirty iff the clone still differs from the new sync point: its
            # own unpushed edits (which the push then ships, with the new
            # revision as If-Match), or a canvas.toml the merge normalised.
            self._fingerprint = self._take_fingerprint()
            if self._take_file_hashes(self.dir) != self._base_files:
                self._dirty_since = self._dirty_since or time.time()
                if self.push_state not in ("error", "pushing"):
                    self.push_state = "pending"
            else:
                self._dirty_since = None
                if self.push_state == "pending":
                    self.push_state = "idle"
            if manual:
                self.pull_seq += 1
            self.last_pull_at = time.time()
            if files:
                self._last_merge = {"at": time.time(), "files": files}
                self._journal_add("merged", "", files=files)
        finally:
            self._op_lock.release()
        rev = self._revision_text(probe)
        self.log_event("server_to_disk", "server merged",
                       detail=(", ".join(files[:20]) + (f" (+{len(files) - 20} more)"
                                                        if len(files) > 20 else "")
                               if files else "nothing to change here")
                              + (f"; {rev}" if rev else ""))
        return {"ok": True, "files": files}

    def _converged_deletes(self, probe: dict) -> list[str]:
        """The UDFs the server deleted that this clone has ALREADY deleted
        too, when that is the remote move's only UDF change; [] otherwise.

        The common case is the workbench's own "Delete UDF permanently": a
        workbench build that still deletes on the server while it also drops
        the node from the snapshot, which removes the file here. Nothing is
        in conflict, yet read as "server changed" it held every push until
        someone pressed Pull — and a Claude session cannot press it. Under
        _op_lock or not: it only reads."""
        remote = self._remote
        if remote is None or self._base_files is None:
            return []
        before = self._udf_hashes(remote.get("udfs"))
        after = self._udf_hashes(probe.get("udfs"))
        if any(slug not in before or before[slug] != after[slug] for slug in after):
            return []  # an add or an edit on the server: a real pull
        removed = sorted(set(before) - set(after))
        if not removed:
            return []
        try:
            local = canvas_snapshot.node_files(self.dir)
        except OSError:
            return []
        return [] if any(slug in local for slug in removed) else removed

    def _adopt_converged_deletes(self, probe: dict | None = None) -> bool:
        """Fold a remote move that only deleted UDFs this clone has deleted as
        well (`_converged_deletes`) into the sync point: the same three-way
        merge a Pull runs (the layout keeps local edits), done without asking.
        True when it landed and `server_stale` is clear. The caller holds
        `_flight_lock` and NOT `_op_lock`; `probe` is fetched when not given."""
        if probe is None:
            probe = self._probe_remote()
        if probe is None or not self._converged_deletes(probe):
            return False
        # One merge attempt per server state: one that rolled back would roll
        # back again on every 10 s poll (a download, a validate run and a
        # trash snapshot each time). A new remote move, or a local change
        # since, gets a fresh try.
        # Local content, not the fingerprint generation: a rollback rewrites
        # the files it touched with the same bytes but new mtimes, so a
        # generation key would re-arm the retry it exists to stop.
        key = (probe.get("last_updated"), tuple(sorted(self._udf_hashes(probe.get("udfs")).items())),
               tuple(sorted(self._take_file_hashes(self.dir).items())))
        if key == self._adopt_failed:
            return False
        bundle = self._fetch_bundle(probe)
        if bundle is None:
            return False  # transient: retried next poll
        with self._op_lock:
            gone = self._converged_deletes(probe)  # re-read: disk may have moved
            if not gone:
                return False
            rollbacks = self.merge_rollback_seq
            if not self._merge_remote(probe, bundle):
                return False  # transient: retried next poll
            if self.merge_rollback_seq != rollbacks:
                self._adopt_failed = key
                return False
        self.log_event("server_to_disk", "server delete adopted",
                       detail=f"{', '.join(gone)}: already deleted here")
        return True

    def _force_pull(self, probe: dict, cli, hard: bool = False) -> str | None:
        """pull()'s shim-backed wholesale branch (clean clone, or a hard
        pull), after it has committed to writing: CLI force pull,
        post-pull divergence recheck, re-baseline.

        Returns None when the pull LANDED, else the user-facing reason it did
        not. pull() must not report success on a pull that never ran: the
        remote baseline only rotates here, so a failed `--force` leaves
        `server_stale` set (the push stays held, correctly) and the Pull
        button has to say why instead of answering ok with the banner still up.
        Under _op_lock for its whole length: the CLI writes the clone itself.
        """
        # The CLI's --force overwrites/deletes without us seeing each file —
        # snapshot the clone first so nothing is ever unrecoverable. A hard
        # pull is the one leg allowed over a live Claude session, so its
        # backup is not best-effort: no snapshot folder, no pull.
        self._last_trash = self._snapshot_clone()
        if hard and self._last_trash is None:
            return _no_backup_error(self.trash_root())
        try:
            applied = subprocess.run(
                [*cli.command, "workbench", "canvas", "pull", self.name,
                 "-o", self.dir, "--force"],
                capture_output=True, text=True, timeout=PULL_TIMEOUT,
                encoding="utf-8", errors="replace",
                env=_cli_env(cli, self.env),
            )
        except subprocess.TimeoutExpired:
            return "pulling the canvas from the server timed out; try again"
        except OSError as exc:
            return f"could not run the fused CLI to pull the canvas: {exc}"
        if applied.returncode != 0:
            return cli_error(applied.stderr or applied.stdout, "fused canvas pull failed")
        self.pull_seq += 1
        self.last_pull_at = time.time()
        # The disk as the pull left it, before the recheck: a write landing
        # AFTER the recheck read the folder would otherwise be baselined as
        # synced by the fingerprint below (see `_settle_after_force_pull`).
        pulled = self._take_fingerprint()
        # Did a local edit land WHILE --force was running? That window cannot
        # be closed with fingerprints — the pull's own writes and a concurrent
        # local edit both just look like "the file changed" — so ask the CLI,
        # exactly as the legacy leg does after its own force pull. Without
        # this the shim leg re-baselined unconditionally, so a file an active
        # session wrote mid-pull was overwritten AND adopted as the sync
        # point: the edit was lost with nothing left to notice it. (A write
        # the CLI then OVERWROTE mid-run is gone before any check can see it,
        # and the trash snapshot predates it. Under _op_lock the only writer
        # that can land there is a Claude session, which is why pull()
        # defers a PLAIN wholesale pull while one is live; a hard pull runs
        # anyway, with the whole clone in the trash first.)
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
                env=_cli_env(cli, self.env),
            )
        except (subprocess.TimeoutExpired, OSError):
            recheck = None
        still_diff = recheck is not None and recheck.returncode == 0 and (
            "already up to date" not in (recheck.stdout or "")
        )
        # The CLI's --force removed the seeded Claude helper files (they're
        # not in the bundle) — put them back now that the recheck has run.
        _seed_clone_claude_files(self.dir, self.name)
        still_diff = self._settle_after_force_pull(pulled, still_diff)
        # The remote genuinely IS at `probe` — we just pulled it — so rotate
        # either way; not rotating would make the next poll see the same move
        # again and re-run this destructive branch.
        self._rotate_remote(probe)
        if not still_diff:
            # The sync point is what the server sent; a folder fix below is
            # this clone's own edit on top of it, which the push publishes.
            self._set_sync_point()
        if self._normalise_clone_toml():
            still_diff = True
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
            return None
        self._dirty_since = None
        # A scan that read the pull's own half-written files may have flagged
        # the clone "pending" before this re-baseline; the recheck just said
        # local == remote, so nothing is pending.
        if self.push_state == "pending":
            self.push_state = "idle"
        self._save_base()
        return None

    def _settle_after_force_pull(self, pulled: dict, still_diff: bool) -> bool:
        """Re-baseline after a CLI force pull and its recheck. `pulled` is the
        fingerprint taken the moment `--force` landed; anything that moved
        between then and now (seeded helper files excluded — the fingerprint
        never sees them) was written by someone else while the recheck ran and
        is a local edit, exactly like a recheck diff. Returns whether the
        clone diverged from what was pulled."""
        now = self._take_fingerprint()
        self._fingerprint = now
        if now != pulled and not still_diff:
            self.log_event("server_to_disk", "pull raced a local write", ok=False,
                           detail="a file changed while the pull was being checked; "
                                  "keeping it as a local edit")
            return True
        return still_diff

    def _push_copy_dir(self) -> str:
        return os.path.join(self.root, ".sync", "push", self.name)

    def _freeze_copy(self, exclude: Iterable[str] = ()) -> str:
        """Copy the clone to `.sync/push/<name>/` (fresh each push) for the
        CLI to read while _op_lock is free: dot-dirs and what `.fusedignore`
        names are left out; `.fusedignore` itself travels; so do top-level
        `exclude` basenames (a node the user deleted whose file came back
        without a row — `held_tombstones`). Under _op_lock, so no snapshot
        write lands half-way through the copy. Raises OSError/shutil.Error
        on failure."""
        exclude = set(exclude)
        dest = self._push_copy_dir()
        _rmtree_quiet(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        rules = canvas_snapshot.fusedignore_rules(self.dir)
        root = os.path.normpath(self.dir)

        def ignore(folder: str, names: list[str]) -> list[str]:
            at_root = os.path.normpath(folder) == root
            out = []
            for n in names:
                is_dir = os.path.isdir(os.path.join(folder, n))
                if at_root and n in exclude:
                    out.append(n)
                    continue
                if ((is_dir and n.startswith("."))
                        or (n != ".fusedignore" and canvas_snapshot.fusedignored(
                            rules, n, is_dir=is_dir, at_root=at_root))):
                    out.append(n)
            return out

        shutil.copytree(self.dir, dest, ignore=ignore, ignore_dangling_symlinks=True)
        return dest

    def _push_argv(self, cli, copy_dir: str) -> list[str]:
        argv = [*cli.command, "workbench", "canvas", "push", copy_dir, "--canvas", self.name]
        if self._push_if_match is not None:
            argv += ["--if-match", str(self._push_if_match)]
        return argv

    def _run_push_cli(self, cli, copy_dir: str) -> subprocess.CompletedProcess:
        """`fused workbench canvas push <frozen copy> --canvas <name>` — the
        one network call of a push, run with _op_lock RELEASED. Its own seam
        so a test can stand in a slow push. The child is kept on the manager
        (`_push_proc`) while it runs, so the quit rung can stop it
        (`terminate_push`) instead of leaving it to outlive the app."""
        argv = self._push_argv(cli, copy_dir)
        if self.stop_event.is_set():
            # Stopping (quit, logout) between the freeze and here: never start
            # a CLI that terminate_push has already looked past.
            raise OSError("canvas sync is stopping")
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_cli_env(cli, self.env),
        )
        self._push_proc = proc
        if self.stop_event.is_set():
            proc.terminate()  # stop landed between the check above and here
        try:
            try:
                out, err = proc.communicate(timeout=PUSH_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
        finally:
            self._push_proc = None
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    def terminate_push(self, grace_s: float = PUSH_TERMINATE_GRACE_S) -> bool:
        """Stop a push CLI in flight: SIGTERM, then SIGKILL after `grace_s`.
        The push then folds as failed (no base rotation). Returns whether one
        was running."""
        proc = self._push_proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.terminate()
            try:
                proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            pass
        self.log_event("disk_to_server", "push stopped", ok=False,
                       detail="Fused Render is quitting")
        return True

    def _push(self, *, lock_wait: float | None = None,
              fence: Callable[[], bool] | None = None) -> str:
        """Publish the clone. The caller holds `_flight_lock` and NOT
        `_op_lock`, and has checked the pause count under the flight lock.

        Three phases, `push_state == "pushing"` across all of them:
          1. (network, no lock) the pre-push guard's probe — and, if the
             remote moved, its bundle download; then the merge applies under
             _op_lock.
          2. (under _op_lock, short) take the fingerprint + file hashes +
             toml rows this push publishes, and freeze them as a copy.
          3. (network, no lock) the CLI pushes the COPY; the post-push probe
             fetches the new manifest. Then (under _op_lock) fold the result
             in. A write that landed during the CLI run is not in the copy:
             the clone fingerprint moved, so `_dirty_since` re-arms and the
             next tick pushes again.

        Returns "done" (push_state says how it went) or "busy" when _op_lock
        could not be had within `lock_wait` (default MANUAL_PUSH_LOCK_WAIT_S),
        or `fence()` answered True at the freeze — nothing changed then, the
        clone stays as dirty as it was."""
        wait = MANUAL_PUSH_LOCK_WAIT_S if lock_wait is None else lock_wait
        prev_state, prev_dirty = self.push_state, self._dirty_since
        cli = fused_cli()
        if cli is None:
            self.push_state = "error"
            self.last_error = "the fused CLI is not available"
            self.error_detail = []
            self.log_event("disk_to_server", "push error", ok=False, detail=self.last_error)
            return "done"
        started = time.time()
        self._push_started_at = started
        self.push_state = "pushing"
        self.log_event("disk_to_server", "push start")

        def deferred(why: str) -> str:
            self._dirty_since = prev_dirty if self._dirty_since is None else self._dirty_since
            self._push_started_at = None
            self.push_state = prev_state
            self.log_event("disk_to_server", "push busy", ok=False, detail=why)
            return "busy"

        # CAS (revision spine): the push carries `--if-match <base_revision>`;
        # a server that moved since answers exit 3 (412). The first conflict
        # merges the server's move (the guard below, forced) and pushes once
        # more with the new revision; a second one leaves the clone pending
        # for the next debounce.
        for attempt in range(2):
            # Guard the wholesale replace: if the remote moved since the last
            # sync point, fold its changes in first (per-file, local wins
            # ties) so the push can't clobber a concurrent workbench edit.
            if self._remote is not None or attempt > 0:
                probe = self._probe_remote()
                if probe is not None:
                    self._note_probe(probe)
                if (
                    probe is not None
                    and (self._remote is None or self._remote_moved(self._remote, probe))
                ):
                    bundle = (self._fetch_bundle(probe)
                              if self._base_files is not None or attempt > 0 else None)
                    if not self._op_lock.acquire(timeout=wait):
                        return deferred("another sync step held the clone")
                    if fence is not None and fence():
                        self._op_lock.release()
                        return deferred("a newer turn started in this folder")
                    try:
                        self._last_merge_files = []
                        if (bundle is not None and self._base_files is not None
                                and self._bundle_matches_sync_point(bundle)):
                            self._rotate_remote(probe)
                            self._save_base()
                            merged = True
                        else:
                            merged = self._merge_remote(
                                probe, bundle, two_way=attempt > 0 and self._base_files is None)
                        files = [f.replace(os.sep, "/") for f in self._last_merge_files]
                        if merged and files:
                            self._last_merge = {"at": time.time(), "files": files}
                            self._journal_add("merged", "", files=files)
                    finally:
                        self._op_lock.release()
                    if merged and files:
                        self.log_event("server_to_disk", "server merged",
                                       detail=", ".join(files[:20]) + "; before a push")
                    if not merged:
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
                        # status endpoint's caller (and _fix_prompt) would send
                        # the session to fix a problem this attempt never even
                        # encountered, one it may have already fixed.
                        self.last_error = None
                        self.error_detail = []
                        self._dirty_since = time.time()
                        self._push_started_at = None
                        self.push_state = "pending"
                        self.log_event("disk_to_server", "push deferred", ok=False,
                                       ms=(time.time() - started) * 1000,
                                       detail="the server changed and its files could not be "
                                              "downloaded to merge first")
                        return "done"
            # Freeze. Baseline BEFORE the push: a save landing while the push
            # runs must re-arm the debounce, not vanish into the pushed
            # snapshot. The file hashes are taken at the same instant — they
            # describe the snapshot this push publishes, which becomes the
            # merge base on success.
            if not self._op_lock.acquire(timeout=wait):
                return deferred("another sync step held the clone")
            if fence is not None and fence():
                self._op_lock.release()
                return deferred("a newer turn started in this folder")
            try:
                served = {os.path.splitext(f)[0] for f in self._base_files or ()
                          if f.endswith(canvas_snapshot.BODY_SUFFIXES)}
                held = self.held_tombstones()
                dropped = self._reconcile_rows(
                    "disk_to_server", self.dir, pending=self._rowless | held,
                    drop=served - self._fileless,
                    backup=lambda rel, data: self._backup_to(self._new_trash_dir(), rel, data))
                self._fingerprint = frozen = self._take_fingerprint()
                self._dirty_since = None
                held_files = canvas_snapshot.node_files(self.dir) if held else {}
                held_out = sorted(held_files[n] for n in held if n in held_files)
                if held_out and held_out != self._held_out_logged:
                    self.log_event("disk_to_server", "held out of the push",
                                   detail=f"{', '.join(held_out)}: the user deleted "
                                          "this node on the canvas; a row in canvas.toml brings it back")
                self._held_out_logged = held_out
                # The CAS token this push carries: the revision its sync point
                # describes (None on an old server or an old CLI).
                self._push_if_match = (
                    self._base_revision
                    if not self._cas_unsupported and "revision" in (self._remote or {"revision": 0})
                    else None)
                if_match = self._push_if_match
                try:
                    copy_dir = self._freeze_copy(exclude=held_out)
                except (OSError, shutil.Error) as exc:
                    copy_dir = None
                    copy_error = str(exc)
                else:
                    dropped += self._reconcile_rows("disk_to_server", copy_dir, publish=True)
                    base_snapshot = self._take_file_hashes(copy_dir)
                    base_toml_snapshot = self._toml_rows(copy_dir)
                    if gone := [n for n in sorted(served.intersection(dropped))
                                if n not in base_toml_snapshot["nodes"]]:
                        self.log_event("disk_to_server", "removed from server (no source file)",
                                       ok=False, detail=", ".join(gone))
            finally:
                self._op_lock.release()
            if copy_dir is None:
                # A file vanished mid-copy (Claude deleting while we froze) or
                # the disk is full: nothing was pushed — retry after the
                # debounce.
                self._dirty_since = time.time()
                self._push_started_at = None
                self.push_state = "pending"
                self.log_event("disk_to_server", "push deferred", ok=False,
                               detail=f"could not freeze the clone for the push: {copy_error}")
                return "done"
            try:
                try:
                    proc = self._run_push_cli(cli, copy_dir)
                except subprocess.TimeoutExpired:
                    return self._push_failed(
                        f"`fused canvas push` timed out after {int(PUSH_TIMEOUT)}s", [],
                        started, frozen)
                except OSError as e:
                    return self._push_failed(f"could not run the fused CLI: {e}", [], started, frozen)
            finally:
                _rmtree_quiet(copy_dir)
            err_text = proc.stderr or ""
            if (proc.returncode not in (0, 3, 4) and if_match is not None
                    and "no such option" in (err_text + (proc.stdout or "")).lower()):
                # A fused CLI from before the revision spine: no CAS. Push
                # unconditionally from now on (the pre-push guard still runs).
                self._cas_unsupported = True
                self._dirty_since = self._dirty_since or time.time()
                self.log_event("disk_to_server", "push without CAS", ok=False,
                               detail="this fused CLI has no `push --if-match`; pushing "
                                      "unconditionally")
                continue
            if proc.returncode == 3:
                detail = _cli_json_detail(err_text)
                self._dirty_since = self._dirty_since or time.time()
                self.log_event("disk_to_server", "push conflict", ok=False,
                               detail=f"the server is at revision {detail.get('revision')}, "
                                      f"this push was based on {if_match}"
                                      + ("; merging and pushing again" if attempt == 0 else
                                         "; retrying after the next quiet period"))
                rev = detail.get("revision")
                if isinstance(rev, int) and not isinstance(rev, bool):
                    self._server_revision = rev
                if attempt == 0:
                    continue
                self._push_started_at = None
                self.push_state = "pending"
                return "done"
            if proc.returncode == 4:
                detail = _cli_json_detail(err_text)
                self._lease_held_by = {"owner": detail.get("owner"),
                                       "lease_until": detail.get("lease_until")}
                self._dirty_since = self._dirty_since or time.time()
                self._push_started_at = None
                self.push_state = "pending"
                self.log_event("disk_to_server", "push held (leased)", ok=False,
                               detail=self._lease_reason())
                return "done"
            break
        if proc.returncode != 0:
            # Everything the CLI printed, one entry per line, capped so a
            # pathological run can't grow the status payload without bound.
            # Includes the summary line last_error already shows — the reader
            # (UI list, fix prompt) wants the verbatim transcript, not a
            # de-duplicated one.
            lines = [ln.strip() for ln in
                     ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines()
                     if ln.strip()]
            return self._push_failed(
                cli_error(proc.stderr or proc.stdout, "fused canvas push failed"),
                lines[:_ERROR_DETAIL_MAX], started, frozen)
        # New sync point: the pushed snapshot is the merge base, and the
        # remote is re-probed so its post-push hashes become the manifest
        # baseline (a workbench edit landing inside this probe window gets
        # absorbed — the documented blind window). Probe failure → None →
        # the next poll adopts a fresh baseline. Probed BEFORE the fold, so the
        # lock is not held across it.
        after = self._probe_remote()
        # The CLI's own word on the revision this push left (`Revision: <n>`,
        # its last stdout line) beats the probe's: a probe can read the
        # canvas from just before the import landed, or just after someone
        # else's write. Either way the sync point is the revision WE made —
        # the next poll then sees any later move and merges or rebaselines it.
        pushed_rev = _pushed_revision(proc.stdout)
        if pushed_rev is not None:
            if after is not None and "revision" in after:
                after = {**after, "revision": pushed_rev}
            self._base_revision = pushed_rev
        # A push changes the account's canvas list (updated_at): drop cached
        # list answers. Module-level, called with no lock held. The disk
        # baseline stays: auto-push fires on every save, and wiping it here
        # would make the catalog block on the control plane every time.
        _cli_cache_clear(keep_listing=True)
        with self._op_lock:
            self.last_push_at = time.time()
            self.last_error = None
            self.error_detail = []
            self._base_files = base_snapshot
            self._base_toml = base_toml_snapshot
            self._rotate_remote(after)
            self._save_base()
            moved = self._take_fingerprint() != frozen
            if moved:
                # Written while the CLI ran: not in the copy, so not pushed.
                self._dirty_since = self._dirty_since or time.time()
            self._push_started_at = None
            self.push_state = "pending" if moved else "idle"
            # Last: `status()` reads without the lock, so a reader that sees
            # the new push_seq must already see the push settled, not
            # push_seq 1 with push_state still "pushing".
            self.push_seq += 1
        self.log_event("disk_to_server", "push end", ms=(time.time() - started) * 1000,
                       detail=f"push_seq {self.push_seq}"
                              + ("; the clone changed during the push, pushing again"
                                 if moved else ""))
        return "done"

    def _push_failed(self, error: str, detail: list[str], started: float,
                     frozen: dict) -> str:
        self.last_error = error
        self.error_detail = detail
        if self._take_fingerprint() != frozen:
            # An edit landed during the failed push — maybe the fix. Re-arm so
            # the debounce tries it (push_state stays "error" until one lands).
            self._dirty_since = self._dirty_since or time.time()
        self._push_started_at = None
        self.push_state = "error"
        self.log_event("disk_to_server", "push error", ok=False,
                       ms=(time.time() - started) * 1000, detail=error)
        return "done"

    def _pull_if_remote_changed(self) -> None:
        """Legacy downstream probe — external-CLI fallback only (no manifest
        shim), CLEAN clones only (the caller gates it). A `pull --dry-run`
        that reports a diff then means the REMOTE moved (local == remote is
        the steady state at a sync point); that marks the canvas
        server-stale, nothing more. pull() applies it on request
        (`_apply_legacy_pull`), whose post-pull `--dry-run` recheck catches a
        local edit that landed while `--force` ran (local wins, goes dirty).
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
                env=_cli_env(cli, self.env),
            )
        except (subprocess.TimeoutExpired, OSError):
            return
        if probe.returncode != 0:
            return  # transient (network, auth) — the next poll retries
        # canvas_pull.py's up-to-date sentinel; anything else is a diff.
        if "already up to date" in (probe.stdout or ""):
            return
        # The remote moved (no shim, so no per-UDF detail): say so; pull()
        # brings it down when asked.
        self._note_server_stale(None)

    def _apply_legacy_pull(self, cli, base: list) -> str | None:
        """pull()'s external-CLI branch (no manifest shim): the wholesale
        `pull --force` that `_pull_if_remote_changed` only detects the need
        for — see `_force_pull`, the shim leg's twin.

        Returns None when the pull LANDED, else the user-facing reason it did
        not, exactly as `_force_pull`. This leg matters more: with no shim
        `_probe_remote()` answers None, so `_push()`'s pre-push merge guard
        never gets a manifest to compare. `server_stale` is the ONLY thing standing
        between a dirty clone and a wholesale push over the remote changes, so
        pull() may clear it only when this returns None."""
        try:
            applied = subprocess.run(
                [*cli.command, *base, "--force"],
                capture_output=True, text=True, timeout=PULL_TIMEOUT,
                encoding="utf-8", errors="replace",
                env=_cli_env(cli, self.env),
            )
        except subprocess.TimeoutExpired:
            return "pulling the canvas from the server timed out; try again"
        except OSError as exc:
            return f"could not run the fused CLI to pull the canvas: {exc}"
        if applied.returncode != 0:
            return cli_error(applied.stderr or applied.stdout, "fused canvas pull failed")
        self.pull_seq += 1
        self.last_pull_at = time.time()
        pulled = self._take_fingerprint()  # see `_force_pull`
        # Did local diverge from remote again during the force pull? If so,
        # local wins — queue a push instead of baselining as clean. Re-seed
        # the Claude helper files only AFTER this recheck (see `_force_pull`,
        # the shim leg's twin, for why: seeding first makes the seeded,
        # never-bundled files look like a permanent diff to the recheck).
        try:
            recheck = subprocess.run(
                [*cli.command, *base, "--dry-run"],
                capture_output=True, text=True, timeout=PULL_TIMEOUT,
                encoding="utf-8", errors="replace",
                env=_cli_env(cli, self.env),
            )
        except (subprocess.TimeoutExpired, OSError):
            recheck = None
        still_diff = recheck is not None and recheck.returncode == 0 and (
            "already up to date" not in (recheck.stdout or "")
        )
        _seed_clone_claude_files(self.dir, self.name)
        still_diff = self._settle_after_force_pull(pulled, still_diff)
        self._dirty_since = time.time() if still_diff else None
        if still_diff and self.push_state != "error":
            self.push_state = "pending"
        elif not still_diff and self.push_state == "pending":
            # As in `_force_pull`: a scan may have read the pull mid-write.
            self.push_state = "idle"
        return None

    def _run(self) -> None:
        while not self.stop_event.wait(SCAN_INTERVAL_S):
            self._tick()

    def _tick(self) -> None:
        """One pass of the watcher: lease heartbeat, fingerprint scan, the
        debounced push, the remote poll (which merges a server move when no
        Claude run is live). Its own method so a test can drive it."""
        if self._paused():
            return
        # Keep the live-run cache warm from the WATCHER thread, on every
        # tick, clean clone or not: sync/state reads the cached answer
        # (the pending push's reason) and never scans itself. A no-op read
        # of the cache except once every AGENT_LIVE_CACHE_S (3 s), so it
        # does not add a scan per tick.
        live = self.agent_run_id()
        if self._idle_check(live):
            return  # stopping: this was the last tick
        self._lease_tick()
        gen = self._fp_gen
        current = self._take_fingerprint()
        self._note_disk(current)
        if self._fp_gen != gen:
            # A pull or push (request thread) re-baselined while this walk
            # ran: the reading predates the new baseline, and comparing
            # them would mark the clone dirty with the pull's own writes.
            return
        if current != self._fingerprint:
            self._fingerprint = current
            self._dirty_since = time.time()
            # "pushing" spans a whole push (a manual one runs on a request
            # thread while this loop keeps scanning): its fold decides
            # what comes next, from the same fingerprint comparison.
            if self.push_state not in ("error", "pushing"):
                self.push_state = "pending"
            return
        if self.server_stale is not None and self._held_for_run and not live:
            # The run a server move was held for has ended: merge it on THIS
            # tick (owner Q8), not PULL_POLL_S later.
            self._last_pull_poll = 0.0
        if self._dirty_since is not None and time.time() - self._dirty_since >= DEBOUNCE_S:
            # Hold the debounced auto-push while a Claude session is live in
            # this clone. The debounce measures file quiet, and a session
            # goes quiet for far longer than DEBOUNCE_S in the middle of a
            # change set — thinking, reading, waiting on a tool — so the
            # watcher would ship a half-done rename (the .py file without
            # its canvas.toml entry) as a validation failure the user sees.
            # The session's turn END publishes instead (the Stop hook,
            # `turn_end`).
            #
            # The watcher stays the BACKSTOP: _dirty_since is left armed, so
            # the moment no run is live a still-dirty clone pushes on the
            # next tick. A turn whose Stop hook never arrives therefore
            # degrades to exactly that — never to a lost change set.
            # And HELD while a server move is not merged yet (`server_stale`):
            # a hosted tab does not save over a server it has not looked
            # at, and neither does this one; or while another owner has the
            # canvas checked out (its lease).
            if not live and self.server_stale is None and not self._leased_elsewhere():
                # Non-blocking: a manual push or a pull in flight already
                # owns the network leg; this tick just skips. The pause
                # count is re-read UNDER the flight lock — pause() raises
                # it before waiting on that lock, so either it sees us
                # and waits, or we see it and stand down.
                if self._flight_lock.acquire(blocking=False):
                    try:
                        if not self._paused():
                            self._push()
                    finally:
                        self._flight_lock.release()
                return
        if time.time() - self._last_pull_poll >= PULL_POLL_S:
            # The downstream leg: probe, and on a real move MERGE it (owner
            # Q8, 2026-11-11) — unless a Claude run is live in the clone, when
            # the move is only noted (`server_stale`, the hook tells the
            # session which files) and merged the moment the run ends.
            # Skipped (retried next tick) while a push or pull holds the
            # flight lock: a push moving the remote would read as a
            # server change, and the push re-baselines the remote anyway.
            if not self._flight_lock.acquire(blocking=False):
                return
            try:
                if self._paused():
                    return
                self._last_pull_poll = time.time()
                cli = fused_cli()
                if cli is not None and _shim_manifest_command(cli) is not None:
                    self._poll_and_merge()
                elif self._dirty_since is None and self.push_state == "idle":
                    # External CLI (no shims): legacy dry-run probe, detect
                    # only, and CLEAN clones only — a `pull --dry-run` cannot
                    # tell a local edit from a remote move, so on a dirty
                    # clone it would read every unpushed edit as "server
                    # changed" and hold the push forever. A network call
                    # that writes nothing: no _op_lock.
                    self._pull_if_remote_changed()
            finally:
                self._flight_lock.release()

    # -- idle stop -------------------------------------------------------------

    def _idle_check(self, live: str) -> bool:
        """Arm, disarm or fire the idle stop (IDLE_STOP_S after the canvas's
        last page socket closed). True when this tick stopped the manager."""
        # No `_bump` on arm/disarm: arming means no socket is listening, and
        # the socket that disarms reads the state as it connects.
        count, at = canvas_sync_state.presence(_sync_key(self.env, self.name))
        if count > 0:
            self._idle_stop_at = None
            self._idle_deferred_for = ""
            self._idle_retries = 0
            self._idle_retry_at = 0.0
            return False
        due = max(at or 0.0, self._started_at, self._idle_retry_at - IDLE_STOP_S) + IDLE_STOP_S
        self._idle_stop_at = due
        if time.time() < due:
            return False
        # Never under a live run (its Stop hook still needs this manager), nor
        # while a Claude session is open on the folder, between turns too
        # (owner 2026-11-12): its next turn's edits need the whole engine. A
        # cached "yes" is enough (one RUNS scan per AGENT_LIVE_CACHE_S while
        # the session sits idle); a "no" is re-read fresh — a run or session
        # that started since would lose its publish with this manager.
        session = ""
        if not live:
            session = self.agent_session_id()
            if not session:
                live = self.agent_run_id(fresh=True)
                if not live:
                    session = self.agent_session_id(fresh=True)
        what = "run is live" if live else "session is open"
        live = live or session
        if live:
            if live != self._idle_deferred_for:
                self._idle_deferred_for = live
                self.log_event("server_to_disk", "idle stop deferred",
                               detail=f"a Claude {what} in this folder ({live})")
            return False
        return self._idle_stop()

    def _idle_stop(self) -> bool:
        """Nobody has had this canvas open for IDLE_STOP_S: merge a held
        server move, publish the folder if it is dirty, then stop and forget
        this manager (the lease is released by stop()). From the watcher
        thread; the join happens on a thread of its own (a thread cannot join
        itself). False (retried next tick) when a sync step holds the clone."""
        if not self._flight_lock.acquire(blocking=False):
            return False
        try:
            if self._paused():
                return False
            done: list[str] = []
            if self.server_stale is not None:
                cli = fused_cli()
                if cli is not None and _shim_manifest_command(cli) is not None:
                    merged = self._merge_now()
                    done.append("held server move merged" if merged.get("ok")
                                else f"held server move not merged ({merged.get('error')})")
            dirty = (self._dirty_since is not None
                     or self._take_fingerprint() != self._fingerprint)
            if dirty:
                if self.server_stale is not None:
                    done.append("push held: " + _stale_reason(self.server_stale))
                elif self._leased_elsewhere():
                    done.append("push held: " + self._lease_reason())
                else:
                    before = self.push_seq
                    self._push()
                    done.append("pushed" if self.push_seq > before
                                else f"push did not land ({self.push_state}"
                                     + (f": {self.last_error}" if self.last_error else "") + ")")
            left = (self.server_stale is not None or self._dirty_since is not None
                    or self._take_fingerprint() != self._fingerprint)
            if left and self._idle_retries < IDLE_STOP_RETRIES:
                # A held move or unpublished edits remain: give the legs
                # another window before dropping them with the manager.
                self._idle_retries += 1
                self._idle_retry_at = time.time() + IDLE_RETRY_S
                self.log_event("server_to_disk", "idle stop deferred",
                               detail=f"try {self._idle_retries} of {IDLE_STOP_RETRIES}: "
                                      + ("; ".join(done) or "a held server move or "
                                         "unpublished edits remain"))
                return False
            self.log_event("server_to_disk", "idle stop",
                           detail=f"no canvas page open for {int(IDLE_STOP_S)}s"
                                  + (f"; {'; '.join(done)}" if done else "; nothing to publish"))
            # Out of the loop now: no tick after this one.
            self.stop_event.set()
        finally:
            self._flight_lock.release()
        threading.Thread(target=_retire_idle_manager, args=(self,), daemon=True,
                         name=f"fused-canvas-idle-stop-{self.name}").start()
        return True

    def _poll_and_merge(self) -> None:
        """The shim poll leg, under `_flight_lock` (not `_op_lock`): probe;
        a move whose bundle is the sync point's bytes rebaselines; a real
        move merges when no Claude run is live and is held (noted, told to
        the session through its hook) while one is."""
        probe = self._probe_remote()
        if probe is None:
            return
        self._note_probe(probe)
        if self._remote is None:
            with self._op_lock:
                if self._poll_remote(probe) != "moved":
                    return
        elif not self._remote_moved(self._remote, probe):
            with self._op_lock:
                self._poll_remote(probe)
            return
        live = self.agent_run_id()
        if ("revision" not in probe and not live
                and self._adopt_converged_deletes(probe)):
            return  # old server: it only caught up with this clone's deletes
        key = self._move_key(probe)
        stale = self.server_stale
        if live and stale is not None and stale.get("key") == key:
            return  # this state is already held and told; nothing new
        if (not live and self._merge_failed_key is not None
                and self._merge_failed_key == (
                    key, tuple(sorted(self._take_file_hashes(self.dir).items())))):
            return  # this state's merge rolled back over THIS local content
        bundle = self._fetch_bundle(probe)
        if bundle is None:
            return  # transient: retried next poll
        with self._op_lock:
            if self._poll_remote(probe, bundle) != "moved":
                return
            if live:
                self._note_server_stale(probe, bundle)
                self._held_for_run = True
                files = [f["path"] for f in (self.server_stale or {}).get("files") or []]
                if files:
                    self._journal_add("server_held", "", files=files)
                self.log_event("server_to_disk", "server move held",
                               detail=f"a Claude run is live in this folder ({live}); "
                                      "merged when it ends")
                return
        self._merge_now(probe, bundle)


    def _note_probe(self, probe: dict) -> None:
        """What every fresh manifest says beyond the move itself: the
        server's revision (sync/state's `server_revision`)."""
        rev = probe.get("revision")
        if isinstance(rev, int) and not isinstance(rev, bool):
            self._server_revision = rev

    # -- the edit lease --------------------------------------------------------

    def _leased_elsewhere(self) -> bool:
        """Another owner holds the canvas's lease and it has not lapsed."""
        held = self._lease_held_by
        if held is None:
            return False
        until = _iso_epoch(held.get("lease_until"))
        if until is not None and until < time.time():
            return False
        return True

    def _run_lease_shim(self, action: str, collection_id: str,
                        timeout: float = LEASE_SHIM_TIMEOUT) -> dict | None:
        cli = fused_cli()
        cmd = _shim_lease_command(cli) if cli is not None else None
        if cmd is None:
            return None
        try:
            proc = subprocess.run(
                [*cmd, action, collection_id, self._lease_owner, str(LEASE_TTL_S)],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace", env=_cli_env(cli, self.env))
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _lease_tick(self) -> None:
        """Acquire/renew the canvas's lease every LEASE_RENEW_S (a network
        call: no lock held). Only on a server with the revision spine (the
        lease endpoints came with it); off for good on a fused-py without
        the lease API (logged once) or an external CLI."""
        if self._lease_disabled or time.time() < self._lease_next_at:
            return
        remote = self._remote
        if remote is None or "revision" not in remote or not remote.get("id"):
            return
        self._lease_next_at = time.time() + LEASE_RENEW_S
        answer = self._run_lease_shim("acquire", str(remote["id"]))
        if answer is None:
            if fused_cli() is None or _shim_lease_command(fused_cli()) is None:
                self._lease_disabled = True
            return  # transient: next heartbeat
        if answer.get("ok"):
            first = self._lease_mine is None
            self._lease_mine = {"owner": answer.get("owner") or self._lease_owner,
                                "lease_until": answer.get("lease_until")}
            if self._lease_held_by is not None:
                self._lease_held_by = None
                self._bump()
            if first:
                self.log_event("watcher", "lease acquired", detail=self._lease_owner)
            return
        if answer.get("code") == "unsupported":
            self._lease_disabled = True
            self.log_event("watcher", "lease unsupported",
                           detail="this fused CLI has no lease API; syncing without one")
            return
        if answer.get("code") == "leased":
            held = {"owner": answer.get("owner"), "lease_until": answer.get("lease_until")}
            self._lease_mine = None
            if held != self._lease_held_by:
                self._lease_held_by = held
                self._bump()
                self.log_event("watcher", "lease held elsewhere", ok=False,
                               detail=f"checked out by {held['owner'] or 'another editor'}")

    def release_lease(self, timeout: float = LEASE_RELEASE_QUIT_S) -> None:
        """Give the lease back (stop, quit). Best-effort, bounded."""
        mine, remote = self._lease_mine, self._remote
        if mine is None or remote is None or not remote.get("id"):
            return
        self._lease_mine = None
        if self._run_lease_shim("release", str(remote["id"]), timeout=timeout) is not None:
            self.log_event("watcher", "lease released", detail=self._lease_owner)

    # -- the turn end ----------------------------------------------------------

    def tool_hook(self, session_id: str) -> None:
        """A turn-start or tool hook (start/pre/post) of `session_id`
        arrived: a turn of it is under way (see `_tool_hooks`)."""
        if session_id:
            self._tool_hooks[session_id] = self._tool_hooks.get(session_id, 0) + 1

    def turn_end(self, session_id: str = "", hooks_at_stop: int | None = None) -> str:
        """A Claude session in this clone ended its turn (the Stop hook): run
        a held server merge, then publish the folder if it is dirty — the
        session's change set is complete NOW, no need to wait for the run to
        end or the debounce. Synchronous; the hook route runs it on a thread,
        passing the session's hook count when the Stop arrived: once it
        moves, that session's next turn is under way, and neither the
        merge nor the push may land in the middle of it (its own Stop
        publishes). Returns what happened: "pushed", "merged", "idle", "busy",
        "held", "superseded"."""
        def superseded() -> bool:
            return (hooks_at_stop is not None
                    and self._tool_hooks.get(session_id, 0) != hooks_at_stop)

        def gave_way() -> str:
            self.log_event("disk_to_server", "turn end", ok=False,
                           detail="a newer turn started in this folder; its own turn end publishes")
            return "superseded"

        self.agent_run_id(fresh=True)
        if not self._flight_lock.acquire(timeout=MANUAL_PUSH_LOCK_WAIT_S):
            self.log_event("disk_to_server", "turn end", ok=False,
                           detail="a sync operation was in flight; the watcher publishes after it")
            return "busy"
        try:
            if self._paused() or self.stop_event.is_set():
                return "busy"
            if superseded():
                return gave_way()
            what = "idle"
            if self.server_stale is not None:
                cli = fused_cli()
                if cli is not None and _shim_manifest_command(cli) is not None:
                    merged = self._merge_now(fence=superseded)
                    if merged.get("code") == "superseded":
                        return gave_way()
                    if merged.get("ok"):
                        what = "merged"
            if self.server_stale is not None or self._leased_elsewhere():
                self.log_event("disk_to_server", "turn end", ok=False,
                               detail="publish held: " + (_stale_reason(self.server_stale)
                                                          if self.server_stale is not None
                                                          else self._lease_reason()))
                return "held"
            dirty = (self._dirty_since is not None
                     or self._take_fingerprint() != self._fingerprint)
            if dirty:
                if superseded():
                    return gave_way()
                self.log_event("disk_to_server", "turn end", detail="publishing the folder")
                if self._push(fence=superseded) == "busy" and superseded():
                    return gave_way()
                return "pushed"
            return what
        finally:
            self._flight_lock.release()

    def _lease_reason(self) -> str:
        owner = (self._lease_held_by or {}).get("owner") or "another editor"
        return f"checked out by {owner}"

    # -- server-stale and the explicit pull -----------------------------------

    def check_remote_now(self) -> None:
        """Ask the loop to probe the server on its next tick (≤ SCAN_INTERVAL_S)
        instead of waiting out PULL_POLL_S — the page calls this when its
        window or tab comes into focus, so a canvas edited elsewhere shows
        its Pull button as the user looks, the way the hosted workbench tab
        checks on focus. Detect-only, like the timed poll: nothing is pulled.
        A plain attribute write; the loop reads it under its own locks."""
        self._last_pull_poll = 0.0
        self.log_event("server_to_disk", "server check", detail="page focused")

    def _toml_rows(self, top: str) -> dict | None:
        """The compact rows of `top`'s canvas.toml, or None."""
        try:
            return canvas_snapshot.rows_of_doc(
                canvas_snapshot._load_toml(os.path.join(top, "canvas.toml")))
        except Exception:  # noqa: BLE001 — no toml, or unparsable: no base rows
            return None

    def _set_sync_point(self) -> None:
        """Disk IS the sync point: file hashes and canvas.toml rows alike."""
        self._base_files = self._take_file_hashes(self.dir)
        self._base_toml = self._toml_rows(self.dir)

    def _merge_toml_bytes(self, local: bytes, remote: bytes) -> bytes | None:
        """canvas.toml merged per node against the sync point's rows, as the
        bytes to write; `local` itself when the merge changes nothing (the
        caller compares identity, so no rewrite); None when a side does not
        parse (local wins, as for any file)."""
        try:
            import rtoml
            local_doc = rtoml.loads(local.decode("utf-8", "replace"))
            remote_doc = rtoml.loads(remote.decode("utf-8", "replace"))
            merged, info = canvas_snapshot.merge_toml(self._base_toml, local_doc, remote_doc)
            self._log_folder_fixes("server_to_disk", info.get("folder_fixes") or [])
            if canvas_snapshot.rows_of_doc(merged) == canvas_snapshot.rows_of_doc(local_doc):
                return local
            return canvas_snapshot._dump_toml(merged).encode("utf-8")
        except Exception:  # noqa: BLE001 — rtoml's own error types
            return None

    def _note_server_stale(self, probe: dict | None,
                           bundle: dict[str, bytes] | None = None) -> None:
        """The remote moved since the sync point and is NOT merged yet (a
        Claude run is live, or the merge rolled back): record what, keep
        `since` from the first sighting. `probe` None is the legacy leg (no
        detail). With the server's `bundle`, `files` lists each file the
        server changed with `identical` (merging it changes nothing here)."""
        udfs: list[dict] = []
        layout = True
        if probe is not None and self._remote is not None:
            before = self._udf_hashes(self._remote.get("udfs"))
            after = self._udf_hashes(probe.get("udfs"))
            for slug in sorted(set(before) | set(after)):
                if slug not in before:
                    udfs.append({"slug": slug, "kind": "added"})
                elif slug not in after:
                    udfs.append({"slug": slug, "kind": "removed"})
                elif before[slug] != after[slug]:
                    udfs.append({"slug": slug, "kind": "changed"})
            layout = probe.get("last_updated") != self._remote.get("last_updated")
        files = self._stale_files(bundle)
        if files:
            layout = any(f["path"] == "canvas.toml" for f in files)
        prev = self.server_stale
        since = (prev or {}).get("since") or time.time()
        self.server_stale = {"since": since, "udfs": udfs, "layout": layout,
                             "files": files,
                             "key": self._move_key(probe) if probe is not None else None}
        if prev is None or prev.get("udfs") != udfs or prev.get("layout") != layout:
            self.log_event("server_to_disk", "probe stale", ok=False,
                           detail=_stale_reason(self.server_stale))

    def pull(self, *, lock_wait: float | None = None, hard: bool = False) -> dict:
        """Bring the remote into the clone, on request (the page's Pull, or a
        session answering the manual push's refusal). `hard` is the sync
        drawer's "Hard pull": no merge on any leg — the clone is replaced
        wholesale by the server's files (trash snapshot first), local edits
        and local-only files included. The caller holds
        `_flight_lock` and NOT `_op_lock` (and has read the pause count under
        the flight lock): the manifest probe and the merge's zip download run
        unlocked, and only the writes take _op_lock — an embed snapshot does
        not wait out the download. The two CLI `pull --force` legs are the
        exception: the CLI writes the clone itself, so they run under
        _op_lock for their length (a snapshot arriving then answers 409 busy
        past SNAPSHOT_LOCK_WAIT_S and the embed re-posts it).

        Shim leg: the watcher's own per-file merge (`_merge_now`; canvas.toml
        per node), clean clone or dirty, even over a live Claude run — never
        a CLI `pull --force` (Hard pull's alone). No shims (external CLI) →
        wholesale `pull --force` either way, trash snapshot first when dirty,
        deferred while a Claude run is live. Clears `server_stale` on success.

        Only the merge KEEPS local edits (a dirty shim clone with no sync point
        merges two-way: local wins every file it holds). The wholesale
        external-CLI leg replaces them with the server's files; the trash
        snapshot is then the only copy. That is deliberate rather than a
        refusal: on that leg
        nothing else ever clears `server_stale` (the legacy detector and the
        sync point are both clean-only, and push 409s while it is set), so a
        refused pull would leave the clone with no exit at all. The banner and
        the push route's 409 say so instead of promising the edits are kept.

        `ok: True` means the pull LANDED. A CLI that times out, cannot start
        or exits non-zero answers `ok: False` with its reason and leaves
        `server_stale` set, so the push stays held over a remote it never
        brought down. `code: "busy"` (with ok False) means _op_lock stayed
        held past `lock_wait`: nothing was written.
        """
        cli = fused_cli()
        if cli is None:
            return {"ok": False, "error": "the fused CLI is not available"}
        wait = MANUAL_PUSH_LOCK_WAIT_S if lock_wait is None else lock_wait
        started = time.time()
        self._pull_started_at = started
        self._pull_in_flight = True
        live = self.agent_run_id(fresh=True) if hard else ""
        self.log_event("server_to_disk", "hard pull start" if hard else "pull start",
                       detail=f"over live Claude run {live}" if live else "")
        try:
            result = self._pull(cli, wait, hard=hard)
        finally:
            self._pull_in_flight = False
            self._pull_started_at = None
        # Even a failed `--force` may have half-written the clone before it
        # timed out, so the embed's disk leg still has to look.
        self._note_disk()
        ms = (time.time() - started) * 1000
        if result.get("ok"):
            self._last_pull_error = None
            if hard:
                # The frame still shows the discarded canvas: drop the view so
                # the next cut is a full state, and fence its snapshots until
                # it has applied that state (see `_hard_fence`).
                self._embed_view = None
                self._hard_fence = {"rev": self.disk_rev,
                                    "until": time.time() + HARD_FENCE_S}
                self._body_fence_rev = self.disk_rev
            if hard and self.push_state == "error":
                # The files that push failed on are gone: the clone is the
                # server's copy now (or a fresh local edit over it, which the
                # debounced push ships). The red banner would otherwise stay
                # up over content it is no longer about.
                self.last_error = None
                self.push_state = "pending" if self._dirty_since is not None else "idle"
            trash = result.get("trash")
            self.log_event("server_to_disk", "hard pull end" if hard else "pull end", ms=ms,
                           detail=f"pull_seq {self.pull_seq}"
                                  + (f"; backup {trash}" if trash else ""))
        elif result.get("code") in ("busy", "agent_live"):
            # Deferred, not failed: nothing was written, and the leg stays
            # "stale" rather than turning into a red pull error.
            self.log_event("server_to_disk",
                           "pull busy" if result["code"] == "busy" else "pull deferred",
                           ok=False, ms=ms, detail=result.get("error") or "")
        else:
            self._last_pull_error = result.get("error")
            self.log_event("server_to_disk", "pull error", ok=False, ms=ms,
                           detail=result.get("error") or "")
        self._bump()
        return result

    def _pull(self, cli, wait: float, hard: bool = False) -> dict:
        busy = {"ok": False, "code": "busy",
                "error": "a sync operation is in flight for this canvas; try again in a moment"}
        self._last_trash = None
        # A wholesale CLI pull rewrites the folder outside anything a Claude
        # session's lock-free writes can coordinate with: a file it writes
        # while `--force` runs can be overwritten before any check sees it.
        # So a wholesale pull waits for the session (the per-file merge,
        # which reads each file under _op_lock right before deciding, does not).
        # A HARD pull is the exception: it is the recovery door out of a
        # broken clone, which a stuck session may be the cause of, so it runs
        # over a live session — behind the page's type-to-confirm step, and
        # only once the whole clone is in the trash (see `_last_trash`).
        agent_live = {"ok": False, "code": "agent_live",
                      "error": "a Claude session is editing this folder; pull when it finishes"}
        dirty = self._dirty_since is not None or self._take_fingerprint() != self._fingerprint
        error: str | None = None
        if _shim_manifest_command(cli) is not None:
            probe = self._probe_remote()
            if probe is None:
                return {"ok": False, "error": "could not read the canvas from the server"}
            if not hard:
                # Clean or dirty, the per-file merge from the sync point (the
                # same one the watcher runs on its own) — never a wholesale
                # `pull --force`: that is Hard pull's alone. A dirty clone
                # with no sync point merges two-way (local wins every file
                # it holds).
                merged = self._merge_now(probe, wait=wait, manual=True)
                if not merged.get("ok"):
                    out = {"ok": False, "error": merged.get("error")}
                    if merged.get("code") == "busy":
                        out["code"] = "busy"
                    return out
            else:
                # A hard pull: the server's copy wholesale, trash snapshot
                # first. `_force_pull` rotates the baseline (clearing
                # `server_stale`) only on the path where the pull landed.
                if not self._op_lock.acquire(timeout=wait):
                    return busy
                try:
                    error = self._force_pull(probe, cli, hard=hard)
                finally:
                    self._op_lock.release()
        else:
            base = ["workbench", "canvas", "pull", self.name, "-o", self.dir]
            if not hard and self.agent_run_id(fresh=True):
                return agent_live
            if not self._op_lock.acquire(timeout=wait):
                return busy
            try:
                if dirty or hard:
                    self._last_trash = self._snapshot_clone()
                    if hard and self._last_trash is None:
                        return {"ok": False, "error": _no_backup_error(self.trash_root())}
                error = self._apply_legacy_pull(cli, base)
                # No manifest here, so no rotation to clear it: clear by hand, and
                # ONLY once the pull landed — this flag is the legacy leg's sole
                # guard against the watcher pushing local over the remote.
                if error is None:
                    self.server_stale = None
            finally:
                self._op_lock.release()
        if error is not None:
            return {"ok": False, "error": error}
        return {"ok": True, "trash": self._last_trash}

    def _embed_enter(self, gen: int, seq: int) -> dict | None:
        """Bookkeeping shared by apply_snapshot / apply_rename, under _op_lock.
        Returns the ack to answer with when nothing is to be written: the one
        already given for this (gen, seq), or a no-op ack for a seq older than
        one already applied in this generation. None means: apply, then record
        the ack with _embed_record."""
        # Dedupe FIRST: a replayed (gen, seq) this host already applied is
        # answered with its ack and touches nothing — not even the generation.
        # Looked up after the generation switch below, an old pair flipped the
        # generation back (view, revs and fences reset) and the snapshot after
        # it ran with no picture of the frame, its rows overwriting Claude's.
        done = self._embed_applied.get((gen, seq))
        if done is not None:
            return done
        if gen != self._embed_gen and seq <= self._gen_applied_max.get(gen, -1):
            # An OLDER generation's pair at or below the highest seq applied
            # in it: applied, or superseded by a later full state of that
            # generation — a replay whose ack was pruned from
            # `_embed_applied`. Answered stale, and the generation stays put:
            # flipped back, the view, revs and (on the next newer post) the
            # hard-pull fences would all reset for a body that is no news.
            return {"ok": True, "gen": gen, "seq": seq, "changed": False, "stale": True,
                    "upserted": [], "deleted": [], "created": [], "push_seq": self.push_seq}
        if gen != self._embed_gen:
            newer = gen > self._embed_gen
            self._embed_gen = gen
            # A new generation: the embed re-hydrated from the SERVER, so what
            # it holds is no longer any disk reading. Its hello brings a full
            # delta that re-seeds the view. The view itself is dropped only
            # once this generation's first post is ACCEPTED
            # (`_embed_commit_gen`): a first snapshot that is BOUNCED (built
            # before a delta it was owed) leaves the frame holding exactly the
            # view it acked, and a full cut against None would re-seat every
            # node — overwriting a drag the frame has not had acked (sim B).
            self._view_reset_pending = not self._view_from_hello
            self._rowless = set()
            if self._fileless:
                self._reconcile_rows("embed_to_disk", self.dir, drop=self._fileless, add=False)
                self._fileless = set()
            # `_embed_applied` and `_gen_applied_max` are NOT cleared: (gen,
            # seq) pairs never repeat (a fresh tab's gen is its clock, a
            # refetch bumps it by one and restarts seq), and keeping them is
            # what answers a replay above.
            self._embed_max_seq = -1
            if newer:
                # A NEWER generation holds what a hard pull just brought down
                # (it refetched), so the fences have nothing left to hold out.
                # An OLDER one (a second frame on this canvas) predates it.
                self._hard_fence = None
                self._body_fence_rev = None
        fence = self._hard_fence
        if fence is not None and time.time() >= fence["until"]:
            self._hard_fence = fence = None
            self.log_event("embed_to_disk", "hard pull fence expired", ok=False,
                           detail=f"no delta ack from the workbench in {HARD_FENCE_S:g}s")
        if fence is not None:
            # This snapshot describes the canvas the hard pull discarded.
            self.log_event("embed_to_disk", "snapshot fenced",
                           detail=f"{gen}/{seq}: predates the hard pull; not applied")
            ack = {"ok": True, "gen": gen, "seq": seq, "changed": False, "stale": True,
                   "upserted": [], "deleted": [], "created": [], "push_seq": self.push_seq}
            return self._embed_record(gen, seq, ack)
        if seq < self._embed_max_seq:
            ack = {"ok": True, "gen": gen, "seq": seq, "changed": False, "stale": True,
                   "upserted": [], "deleted": [], "created": [], "push_seq": self.push_seq}
            self._embed_applied[(gen, seq)] = ack
            return ack
        return None

    def _embed_commit_gen(self) -> None:
        """A new generation's first post was accepted: drop the view now (see
        `_embed_enter`), so this post is measured against nothing known."""
        if self._view_reset_pending:
            self._view_reset_pending = False
            self._embed_view = None

    def _embed_record(self, gen: int, seq: int, ack: dict) -> dict:
        self._embed_max_seq = max(self._embed_max_seq, seq)
        self._embed_applied[(gen, seq)] = ack
        # Per generation, the highest seq applied — what answers a replay
        # once its own ack has been pruned below (`_embed_enter`).
        self._gen_applied_max[gen] = max(self._gen_applied_max.get(gen, -1), seq)
        if len(self._gen_applied_max) > 32:
            for old in sorted(self._gen_applied_max)[:-16]:
                del self._gen_applied_max[old]
        # Bound the dedupe memory: the embed's queue is never deep.
        if len(self._embed_applied) > 64:
            for key in sorted(self._embed_applied)[:-32]:
                del self._embed_applied[key]
        return ack

    # -- the disk → embed leg (deltas) ---------------------------------------

    def _note_disk(self, fingerprint: dict | None = None) -> None:
        """Advance `disk_rev` when the clone's fingerprint or the remote ids
        moved since the last look. Separate from `_fingerprint` (the push
        debounce's baseline, which pulls and merges re-take so their own
        writes do not push): the delta leg wants to see EVERY move, its own
        included."""
        fp = fingerprint if fingerprint is not None else self._take_fingerprint()
        key = (fp, self._remote_ids())
        with self._rev_lock:
            if key == self._rev_key:
                return
            self._rev_key = key
            self.disk_rev += 1
            # When each rev appeared, for sync/state's disk_to_embed age. Only
            # revs the frame has not acked matter; bounded either way.
            self._rev_times[self.disk_rev] = time.time()
            if len(self._rev_times) > 64:
                for old in [r for r in self._rev_times if r <= self._view_rev] or \
                        sorted(self._rev_times)[:-64]:
                    self._rev_times.pop(old, None)
        self._bump()

    # -- the user's canvas changes, for Claude (journal, tombstones, hooks) ---

    def _load_tombstones(self) -> dict[str, dict]:
        try:
            with open(self._tombstones_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}

    def _save_tombstones(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._tombstones_path), exist_ok=True)
            if self._tombstones:
                canvas_snapshot._write_atomic(self._tombstones_path, json.dumps(self._tombstones))
            elif os.path.exists(self._tombstones_path):
                os.unlink(self._tombstones_path)
        except OSError as exc:
            logger.warning("canvas %s: could not save tombstones: %s",
                           _sync_key(self.env, self.name), exc)

    def _journal_add(self, kind: str, name: str, **extra) -> None:
        self._journal_seq += 1
        self._journal.append({"seq": self._journal_seq, "at": time.time(),
                              "kind": kind, "name": name, **extra})
        if len(self._journal) > JOURNAL_MAX:
            del self._journal[:-JOURNAL_MAX]

    def _note_user_deletes(self, deleted: list[str], deleted_files: list[str]) -> None:
        """The frame deleted these nodes (apply_snapshot): tombstone each and
        tell the sessions in the clone."""
        by_stem = {os.path.splitext(f)[0]: f for f in deleted_files}
        now = time.time()
        for name in deleted:
            self._tombstones[name] = {"file": by_stem.get(name), "at": now}
            self._journal_add("deleted", name, file=by_stem.get(name))
        self._save_tombstones()

    def _note_user_changes(self, result: dict, renamed: list | None = None) -> None:
        for name in result.get("added") or ():
            if self._tombstones.pop(name, None) is not None:
                self._save_tombstones()
            self._journal_add("added", name)
        added = set(result.get("added") or ())
        moved = [n for n in result.get("upserted") or () if n not in added]
        if moved:
            # A drag posts a snapshot every 100 ms: consecutive edits fold
            # into ONE entry (re-numbered, so a session that saw the older
            # fold sees the grown one), or they would push a delete out of
            # the ring and out of the note.
            last = self._journal[-1] if self._journal else None
            if last is not None and last["kind"] == "edited":
                self._journal.pop()
                moved = sorted(set(last.get("names") or ()) | set(moved))
            self._journal_add("edited", "", names=sorted(moved))
        for frm, to in renamed or ():
            if self._tombstones.pop(to, None) is not None:
                self._save_tombstones()
            self._journal_add("renamed", frm, to=to)

    def held_tombstones(self) -> set[str]:
        """Tombstoned names whose source file is back on disk with no row —
        kept off the canvas and out of the push. Prunes tombstones a row has
        ended (or that are older than TOMBSTONE_TTL_S). Under _op_lock or
        not: it only reads the clone."""
        if not self._tombstones:
            return set()
        try:
            rows = set(canvas_snapshot.rows_of_doc(canvas_snapshot._load_toml(
                os.path.join(self.dir, "canvas.toml")))["nodes"])
            files = canvas_snapshot.node_files(self.dir)
        except Exception:  # noqa: BLE001 — unreadable mid-write: hold them all
            return set(self._tombstones)
        now = time.time()
        held: set[str] = set()
        # A row ends a tombstone at once; age only one whose file is gone too
        # — expiring one with the file still back and rowless would hand the
        # deleted node a default row on the next push.
        ended = [n for n, t in list(self._tombstones.items())
                 if n in rows or (n not in files
                                  and now - float(t.get("at") or 0) > TOMBSTONE_TTL_S)]
        for name in ended:
            del self._tombstones[name]
        if ended:
            self._save_tombstones()
        for name in list(self._tombstones):
            if name in files:
                held.add(name)
        return held

    def hook_pre(self, session_id: str, tool: str, tool_input: dict) -> dict:
        """PreToolUse for a Claude session in this clone: refuse ONCE a write
        that would recreate the source file of a node the user deleted on the
        canvas. Once: told why, a session that writes it again means it (the
        user asked) — and even then the file stays off the canvas until its
        row is written too (`held_tombstones`)."""
        if tool not in ("Write", "Edit", "MultiEdit", "NotebookEdit") or not self._tombstones:
            return {}
        target = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(target, str) or not target:
            return {}
        target = os.path.abspath(target)
        if os.path.dirname(target) not in {os.path.normpath(self.dir), os.path.realpath(self.dir)}:
            return {}
        base = os.path.basename(target)
        stem = os.path.splitext(base)[0]
        tomb = self._tombstones.get(stem)
        if tomb is None or os.path.exists(target) or (session_id, base) in self._hook_denied:
            return {}
        self._hook_denied.add((session_id, base))
        if len(self._hook_denied) > 500:
            self._hook_denied = set(list(self._hook_denied)[-250:])
        when = time.strftime("%H:%M:%S", time.localtime(float(tomb.get("at") or time.time())))
        return {"deny": (
            f"The user deleted the node `{stem}` on the canvas at {when}, so {base} is gone "
            f"on purpose (a copy is under ../.sync/trash/{self.name}/). Do not recreate it "
            "unless the user asked you to. If they did, write it again and also add its "
            "[[canvas.nodes]] row in canvas.toml: a file without a row stays off the canvas.")}

    def hook_post(self, session_id: str) -> dict:
        """PostToolUse for a Claude session in this clone: what the user did
        in the workbench since this session's previous tool call, as context
        for its next step. Nothing on a session's first call (what happened
        before it started is already on disk when it reads)."""
        if not session_id:
            return {}
        cursor = self._hook_cursors.get(session_id)
        self._hook_cursors[session_id] = self._journal_seq
        if len(self._hook_cursors) > 200:
            for sid in list(self._hook_cursors)[:100]:
                self._hook_cursors.pop(sid, None)
        if cursor is None:
            # The session's first call: what is on disk is what it reads. A
            # server move HELD for a live run is not on disk yet — say so.
            stale = self.server_stale
            held = [f["path"] for f in (stale or {}).get("files") or []]
            if self._held_for_run and held:
                return {"context": f"Server has changes to {_file_list(held)}; they will "
                                   "be merged when your turn ends."}
            return {}
        events = [e for e in self._journal if e["seq"] > cursor]
        if not events:
            return {}
        # What the SERVER did (the revision spine's auto-merge, owner Q8): a
        # merge that landed in this folder, or a move held until the turn
        # ends. Told apart from the user's own workbench edits below.
        server_notes: list[str] = []
        merged = sorted({f for e in events if e["kind"] == "merged" for f in e.get("files") or ()})
        if merged:
            server_notes.append(
                "The canvas changed on the server and was merged into this folder: "
                + _file_list(merged) + ". Re-read them before editing.")
        held_events = [e for e in events if e["kind"] == "server_held"]
        if held_events and self.server_stale is not None:
            held = sorted({f for e in held_events for f in e.get("files") or ()})
            server_notes.append(
                f"Server has changes to {_file_list(held)}; they will be merged "
                "when your turn ends.")
        events = [e for e in events if e["kind"] not in ("merged", "server_held")]
        if not events:
            return {"context": "\n".join(server_notes)} if server_notes else {}
        lines: list[str] = []
        edited: set[str] = set()
        for e in events:
            if e["kind"] == "edited":
                edited.update(e.get("names") or ())
        structural = [e for e in events if e["kind"] != "edited"]
        toml_moved = bool(edited)
        for e in structural[-20:]:
            if e["kind"] == "deleted":
                lines.append(f"- deleted node `{e['name']}`"
                             + (f" ({e['file']} moved to ../.sync/trash/{self.name}/)" if e.get("file") else "")
                             + " — do not recreate it unless the user asks")
            elif e["kind"] == "added":
                lines.append(f"- added node `{e['name']}`")
            elif e["kind"] == "renamed":
                lines.append(f"- renamed node `{e['name']}` to `{e['to']}` (file and row)")
            toml_moved = True
        if edited:
            names = sorted(edited)
            lines.append("- moved or edited: " + ", ".join(names[:12])
                         + (f" (+{len(names) - 12} more)" if len(names) > 12 else ""))
        text = ("While you worked, the user changed this canvas in the workbench "
                "(already on disk in this folder):\n" + "\n".join(lines))
        if toml_moved:
            text += ("\ncanvas.toml was rewritten: Read it again before your next edit "
                     "to it, and edit it with Edit, never by rewriting the whole file.")
        if server_notes:
            text = "\n".join(server_notes) + "\n" + text
        return {"context": text}

    def _force_recut(self) -> None:
        """Advance `disk_rev` with no file moving, so the page cuts a delta
        again: the view changed under the host's own hand (a partly applied
        delta) and the frame is owed the difference."""
        with self._rev_lock:
            self.disk_rev += 1
            self._rev_times[self.disk_rev] = time.time()
        self._bump()

    def _remote_ids(self) -> dict[str, str]:
        """slug → server id from the last remote manifest (the shim emits
        ids; an older shim or the external CLI gives none)."""
        udfs = (self._remote or {}).get("udfs")
        if not isinstance(udfs, dict):
            return {}
        return {slug: info["id"] for slug, info in udfs.items()
                if isinstance(info, dict) and isinstance(info.get("id"), str) and info["id"]}

    def _refresh_view_after_write(self, names, *, edges: list, ids: dict,
                                  bodies: dict, embed_rows: dict | None) -> None:
        """After a snapshot or rename landed on disk: the embed holds disk
        restricted to `names`, the nodes it is known to hold, with its own
        `edges`, `ids` and `bodies` (canvas_snapshot.held_view). `embed_rows`
        are the rows the embed itself sent (what its next snapshot's key
        deletions are measured against). Under _op_lock (the callers are).
        `_view_rev` is set to the current rev in every case (the rev itself
        only moves when the write changed a file), so a delta cut BEFORE a
        write that changed something and acked after it is read as stale:
        the frame applied that delta and then wrote on top of it, and this
        view already describes the result."""
        self._note_disk()
        self._view_rev = self.disk_rev
        self._frame_seen = True
        # The frame's own post describes what it shows now: nothing held over.
        self._skip_hold = {}
        self._view_from_hello = False
        self._bump()
        try:
            disk = canvas_snapshot.compact_state_from_disk(self.dir, self._remote_ids())
        except Exception as exc:  # noqa: BLE001 — a view we cannot read is no view
            logger.warning("canvas %s: could not re-read the clone after a write: %s",
                           _sync_key(self.env, self.name), exc)
            self._embed_view = None
            return
        self._embed_view = canvas_snapshot.held_view(
            disk, names, edges=edges, ids=ids, bodies=bodies, embed_rows=embed_rows)

    def cut_delta(self, full: bool) -> dict:
        """diff(disk, embed_view) for the page to post to the frame. Under
        _op_lock, so a snapshot write never lands between the toml read and
        the file reads. Returns `{rev, base_rev, ops, full}`; `{rev, held}`
        when the clone has no readable canvas.toml (not cloned yet, or
        mid-write): the page asks again shortly. Empty ops
        against a known view mean the embed already holds disk: the view
        moves here and nothing is posted.

        The rev is taken BEFORE the disk read. Claude writes without
        _op_lock, so a write can land between the two; noted first, such a
        write is either in `disk` already or bumps the rev on the next scan
        and a follow-up delta repairs the view. Noted after, the delta would
        carry the pre-write content under the post-write rev and the scan
        would see nothing left to report.

        A cut does NOT mark a frame as seen: the page cuts on every disk_rev
        move and drops the answer when it has no frame (boot, a reload), so
        only a frame's own ack or snapshot is evidence that one is there —
        otherwise disk_to_embed would read "pending" with nobody to ack."""
        self._note_disk()
        rev = self.disk_rev
        try:
            disk = canvas_snapshot.compact_state_from_disk(self.dir, self._remote_ids())
        except FileNotFoundError:
            return self._cut_held(rev, "the clone has no canvas.toml yet")
        except Exception as exc:  # noqa: BLE001 — rtoml's own error types
            return self._cut_held(rev, f"canvas.toml unreadable: {exc}")
        if self._last_cut_held is not None:
            self.log_event("disk_to_embed", "delta unheld", detail=f"rev {rev}")
            self._last_cut_held = None
            self._bump()
        if self._delta_error is not None:
            # A good cut (the frame's hello asks for one) starts the leg over.
            self._delta_error = None
            self._bump()
        view = None if full else self._embed_view
        base_rev = self._view_rev
        if view is not None:
            # Deltas already cut and still unacked: the page posts them in
            # order without waiting for acks, and the frame applies them in
            # order — so by the time it gets THIS one it holds the newest of
            # them, not the acked view. Diffed against the acked view, a node
            # an in-flight delta added and disk has since dropped (a session
            # deleting it) is in neither side of this diff, and no later
            # delta ever deletes it from the canvas (sim seed 127).
            pending = [r for r in self._delta_views if r > self._view_rev]
            if pending:
                base_rev = max(pending)
                acked = view
                view = self._adopt(self._delta_views[base_rev])
                # …except for names the frame SKIPPED (`_skip_hold`): it
                # still shows the acked view's picture of them — node and the
                # edges touching it (as `ack_delta` keeps them), or a cut
                # would find no edge change and never re-send the removal.
                hold = set(self._skip_hold)
                if hold:
                    view = {**view, **{t: dict(view.get(t) or {})
                                       for t in ("nodes", "files", "bodies", "ids")}}
                    view["edges"] = (
                        [list(e) for e in view.get("edges") or []
                         if e[0] not in hold and e[1] not in hold]
                        + [list(e) for e in acked.get("edges") or []
                           if e[0] in hold or e[1] in hold])
                for name in hold:
                    for table in ("nodes", "files", "bodies", "ids"):
                        if name in (acked.get(table) or {}):
                            view[table][name] = acked[table][name]
                        else:
                            view[table].pop(name, None)
        ops = canvas_snapshot.diff_views(disk, view)
        disk["edges"] = canvas_snapshot.held_edges(disk, view["nodes"] if view else ())
        if view is not None and not ops:
            self._embed_view = self._adopt(disk)
            self._view_rev = rev
            self._delta_error = None
            self._bump()
            return {"rev": rev, "base_rev": rev, "ops": [], "full": False, **self._cut_seq()}
        self._delta_views[rev] = disk
        if view is None:
            self._delta_names.setdefault(-rev, set())  # marks rev as a full cut
        self._delta_names[rev] = {n for o in ops for n in (o.get("name"), o.get("from"),
                                                           o.get("to")) if isinstance(n, str)}
        self._cut_rev = max(self._cut_rev, rev)
        for old in sorted(self._delta_views)[:-8]:
            del self._delta_views[old]
        for old in [r for r in self._delta_names
                    if r not in self._delta_views and -r not in self._delta_views]:
            del self._delta_names[old]
        self.log_event("disk_to_embed", "delta cut",
                       detail=f"rev {rev}: {len(ops)} op(s)" + (" (full)" if view is None else ""))
        return {"rev": rev, "base_rev": None if view is None else base_rev,
                "ops": ops, "full": view is None, **self._cut_seq()}

    def _cut_seq(self) -> dict:
        """What a delta says about the snapshot leg (audit 2026-11-10): the
        frame generation and the highest snapshot/rename seq applied in it
        when the delta was cut. The frame skips ops on nodes its own LATER,
        still unacked snapshots added, deleted, renamed or moved — the delta
        was cut without them, and the user's change wins."""
        return {"gen": self._embed_gen or None,
                "seq": self._embed_max_seq if self._embed_max_seq >= 0 else None}

    def _cut_held(self, rev: int, reason: str) -> dict:
        """A delta the clone cannot answer yet. Logged once per distinct
        reason — the page re-asks every few hundred ms while it holds."""
        if reason != self._last_cut_held:
            self._last_cut_held = reason
            self.log_event("disk_to_embed", "delta held", ok=False, detail=f"rev {rev}: {reason}")
        return {"rev": rev, "held": reason}

    def ack_delta(self, rev: int, ok: bool, reason: str | None = None,
                  skipped: Iterable[str] = ()) -> dict:
        """The frame applied (or refused) the delta cut at `rev`. `reason` is
        the page's word for a refusal; one that only means "nobody could take
        it yet" (no frame on the page, a frame not hydrated) resets the view
        like any refusal but is not an error the strip should show — the
        frame's hello brings a full state that settles it. Nor does such a
        refusal mark a frame as seen: until the hello, there is no frame able
        to ack, and disk_to_embed must not read "pending" over a boot."""
        view = self._delta_views.pop(rev, None)
        carried = self._delta_names.pop(rev, set())
        was_full = self._delta_names.pop(-rev, None) is not None
        benign = (not ok and bool(reason)
                  and _BENIGN_DELTA_REFUSAL.search(reason) is not None)
        if not benign:
            self._frame_seen = True
        if not ok:
            # The frame holds something we cannot describe: start over with a
            # full state on its next hello or the next disk move.
            self._embed_view = None
            self._skip_hold = {}
            if not benign:
                self._delta_error = (f"the workbench refused the delta for rev {rev}"
                                     + (f": {reason}" if reason else ""))
                self._bump()
            self.log_event("disk_to_embed", "delta refused", ok=benign,
                           detail=f"rev {rev}" + (f": {reason}" if reason else ""))
            return {"ok": True, "rev": rev, "reset": True}
        if self._body_fence_rev is not None and rev >= self._body_fence_rev:
            # The frame applied the pulled state: its bodies describe it now.
            self._body_fence_rev = None
            self.log_event("embed_to_disk", "body fence lifted", detail=f"rev {rev}")
        if self._hard_fence is not None and rev >= self._hard_fence["rev"]:
            # The frame applied a reading cut after the hard pull: it shows
            # the server's copy now, and its snapshots describe that.
            self._hard_fence = None
            self.log_event("embed_to_disk", "hard pull fence lifted", detail=f"rev {rev}")
        if view is None or rev < self._view_rev:
            # Superseded: a snapshot write re-read disk since this was cut.
            # The frame still APPLIED this delta, so the bodies it carried
            # are bodies the frame holds: fill the ones the current view
            # could not vouch for (a snapshot after a gen change starts with
            # note texts only), or every cut under an autosave stream would
            # re-send all code while each snapshot keeps staling the acks.
            cur = self._embed_view
            if view is not None and cur is not None:
                bodies = dict(cur.get("bodies", {}))
                for name, text in view.get("bodies", {}).items():
                    if name in cur["nodes"] and name not in bodies:
                        bodies[name] = text
                cur["bodies"] = bodies
            self.log_event("disk_to_embed", "delta acked", detail=f"rev {rev} (stale)")
            return {"ok": True, "rev": rev, "stale": True}
        cur = self._embed_view or {}
        skipped = {n for n in skipped if isinstance(n, str)}
        # Earlier skips this delta did not re-send: the frame still shows its
        # own picture of them, so the view keeps it (see `_skip_hold`).
        held_over = {n: at for n, at in self._skip_hold.items()
                     if n not in skipped and not (n in carried and rev > at)}
        self._skip_hold = {**held_over, **{n: self.disk_rev for n in skipped}}
        keep = skipped | set(held_over)
        if keep:
            # The frame kept its own newer change for these (its unacked
            # snapshot touched them after the snapshot this delta was cut
            # behind): the view keeps its previous entry — node and the edges
            # touching it — so the next cut re-sends what the frame did not
            # take, and the frame's next snapshot is not read as removing an
            # edge it never received.
            for table in ("nodes", "files", "bodies", "ids"):
                fresh = dict(view.get(table) or {})
                for name in keep:
                    if name in (cur.get(table) or {}):
                        fresh[name] = cur[table][name]
                    else:
                        fresh.pop(name, None)
                view[table] = fresh
            view["edges"] = (
                [list(e) for e in view.get("edges") or []
                 if e[0] not in keep and e[1] not in keep]
                + [list(e) for e in cur.get("edges") or []
                   if (e[0] in keep or e[1] in keep)])
        self._embed_view = self._adopt(view)
        self._view_reset_pending = False  # the frame's own ack: the view is its truth
        if was_full:
            self._view_from_hello = True
        self._view_rev = rev
        self._delta_error = None
        if skipped:
            # The snapshot that made the frame skip reached this host before
            # this ack (the page relays both through one ordered chain), so a
            # cut now carries its seq and the frame has settled it: the
            # re-sent ops apply. Once per applied seq only — a frame still
            # skipping on the same seq waits for its next snapshot, whose
            # write re-reads disk into the view and cuts on its own.
            key = (self._embed_gen, self._embed_max_seq)
            names = sorted(skipped)
            self.log_event("disk_to_embed", "delta partly kept",
                           detail=f"rev {rev}: the workbench kept its own newer "
                                  f"{', '.join(names[:8])}"
                                  + (f" (+{len(names) - 8})" if len(names) > 8 else ""))
            if key != self._skip_recut_seq:
                self._skip_recut_seq = key
                self._force_recut()
        cut_at = self._rev_times.get(rev)
        self.log_event("disk_to_embed", "delta acked",
                       ms=None if cut_at is None else (time.time() - cut_at) * 1000,
                       detail=f"rev {rev}")
        return {"ok": True, "rev": rev}

    def _adopt(self, disk: dict) -> dict:
        """A disk reading becoming the view keeps what the reading cannot
        carry: the current view's `embed_rows` (a delta changes what the frame
        SHOWS, not what it last SENT, and that baseline is what apply_snapshot
        measures a dropped embed-owned key against) and its `ids` for nodes
        the manifest gives none (the external CLI never does, a shim manifest
        may lag): the frame holds those ids regardless, and apply_snapshot's
        missed-rename detection reads an un-hinted rename with no id as a
        delete."""
        cur = self._embed_view or {}
        out = dict(disk)
        held = [n for n in cur.get("nodes", {}) if n not in disk["nodes"]
                and (n in disk["rowed"] or n in disk["files"])]
        if held:
            out["nodes"] = {**disk["nodes"],
                            **{n: disk["rowed"].get(n, cur["nodes"][n]) for n in held}}
            out["bodies"] = {**disk["bodies"],
                             **{n: cur["bodies"][n] for n in held if n in cur.get("bodies", {})}}
        if cur.get("embed_rows"):
            out["embed_rows"] = cur["embed_rows"]
        held_ids = cur.get("ids") or {}
        if held_ids:
            ids = dict(disk.get("ids", {}))
            for name in disk["nodes"]:
                if name not in ids and name in held_ids:
                    ids[name] = held_ids[name]
            out["ids"] = ids
        return out

    def apply_snapshot(self, gen: int, seq: int, body: dict) -> dict:
        """Fold one embed snapshot into the clone (canvas_snapshot.apply_snapshot)
        under _op_lock, so it never interleaves with a push or a pull writing
        the same files. Returns the ack the page relays to the frame.

        The watcher's fingerprint is deliberately NOT re-taken here: a write
        that changed canvas.toml is a local edit like any other, and the next
        scan arming the debounce is exactly how it reaches the server."""
        stale = self._embed_enter(gen, seq)
        if stale is not None:
            return stale
        new = canvas_snapshot.compact_state(body)
        trash: list[str | None] = [None]

        def backup(rel: str, data: bytes) -> None:
            if trash[0] is None:
                trash[0] = self._new_trash_dir()
            self._backup_to(trash[0], rel, data)

        prev = self._embed_view
        # BOUNCE a stale picture. `body.rev` is the delta rev the frame had
        # applied when it serialized this body (absent from an older
        # workbench, or a rev from before a server restart when it exceeds
        # ours → read as today). One below the newest delta this manager cut
        # with ops was built before the frame saw that delta: applied as the
        # frame's full state it would write the frame's stale rows over
        # Claude's edit (a drag reverting a title, a replayed body deleting a
        # new node — audit R2/R3). So nothing is written; the ack says
        # `rebuild` and names the rev, the frame applies the delta it is owed
        # and posts a fresh snapshot. One round trip, no second merge engine.
        snap_rev = body.get("rev")
        if (isinstance(snap_rev, int) and not isinstance(snap_rev, bool)
                and snap_rev <= self.disk_rev and snap_rev < self._cut_rev):
            self.log_event("embed_to_disk", "snapshot bounced",
                           detail=f"{gen}/{seq} built on rev {snap_rev}, a delta was cut at "
                                  f"rev {self._cut_rev}: the workbench re-sends")
            ack = {"ok": True, "gen": gen, "seq": seq, "changed": False, "stale": True,
                   "rebuild": True, "rev": self._cut_rev, "upserted": [], "deleted": [],
                   "created": [], "push_seq": self.push_seq}
            return self._embed_record(gen, seq, ack)
        self._embed_commit_gen()
        prev = self._embed_view
        result = canvas_snapshot.apply_snapshot(self.dir, new, prev, backup=backup,
                                                rowless_prev=self._rowless)
        self._log_folder_fixes("embed_to_disk", result.get("folder_fixes") or [])
        # The embed now holds what it just sent: disk rows for ITS nodes only
        # (a disk-only node is Claude's and still owed to it as a delta), its
        # edge set and ids, the note texts it sent over the texts it held
        # before. Code bodies it held come from the previous view; with none
        # (a fresh generation, a reset) they are left unknown and the next
        # delta re-sends them once, cheaper than a full state that would also
        # re-seat every node the embed is mid-way through dragging.
        bodies = dict(prev.get("bodies", {})) if prev else {}
        bodies.update(new["bodies"])
        # Nodes the snapshot left out while still listing their UDF (a stale
        # closure on the embed) were NOT deleted (canvas_snapshot's
        # `deferred_deletes`): they stay in the view as known, or the next cut
        # re-adds them as new and the following snapshot can no longer delete
        # them.
        held = list(new["nodes"]) + [n for n in result.get("deferred_deletes") or []
                                     if n not in new["nodes"]]
        self._rowless = set(result.get("rowless_files") or ())
        self._fileless = (self._fileless | set(result["fileless"])) - set(result["deleted"])
        for gone in result.get("deleted") or ():
            self._body_written.pop(gone, None)
        self._refresh_view_after_write(held, edges=new["edges"], ids=new["ids"],
                                       bodies=bodies, embed_rows=new["nodes"])
        view = self._embed_view
        if view is not None:
            # Keys the frame still shows from before a delta it skipped
            # (canvas_snapshot: never written over disk): the view holds the
            # frame's value, so the next delta re-sends disk's.
            for name, keys in (result.get("stale_keys") or {}).items():
                if name in view["nodes"]:
                    view["nodes"][name] = {**view["nodes"][name], **keys}
        if result.get("stale_keys"):
            self.log_event("embed_to_disk", "stale row keys not written",
                           detail=", ".join(f"{n}.{k}" for n, ks in result["stale_keys"].items()
                                            for k in ks)[:300]
                                  + ": the workbench still showed them from before a delta")
        if result.get("ignored"):
            self.log_event("embed_to_disk", "row for a deleted node ignored",
                           detail=", ".join(result["ignored"]) + ": deleted on disk; the next "
                                  "delta deletes it on the canvas")
            if view is not None:
                for name in result["ignored"]:
                    if name not in view["nodes"] and name in new["nodes"]:
                        view["nodes"][name] = new["nodes"][name]
        if result.get("stale_keys") or result.get("ignored"):
            self._force_recut()
        if result.get("deleted"):
            self._note_user_deletes(result["deleted"], result.get("deleted_files") or [])
        self._note_user_changes(result, renamed=result.get("renamed"))
        deleted_files = list(result.get("deleted_files") or ())
        if deleted_files:
            # A node deleted in the canvas takes its file's chats with it
            # (`forget_file_sessions`): off this lock and off the ack —
            # listing a file's transcripts reads their heads.
            threading.Thread(
                target=self._forget_file_sessions, args=(deleted_files, trash[0]),
                name=f"canvas-forget-sessions-{self.name}", daemon=True).start()
        ack = {"ok": True, "gen": gen, "seq": seq, "changed": result["changed"],
               "upserted": result["upserted"], "deleted": result["deleted"],
               "deleted_files": deleted_files,
               "created": result["created"], "push_seq": self.push_seq}
        return self._embed_record(gen, seq, ack)

    def _forget_file_sessions(self, rels: list[str], trash: str | None) -> None:
        """`forget_file_sessions` for this clone's `rels`, backing each
        transcript up into the snapshot's trash batch first (so the recovery
        recipe for a wrongly applied snapshot — `.sync/trash/<canvas>/
        <stamp>/` — still has the chats beside the files)."""
        try:
            if trash is None:
                trash = self._new_trash_dir()
            forget_file_sessions([os.path.join(self.dir, rel) for rel in rels],
                                 backup=lambda rel, data: self._backup_to(trash, rel, data),
                                 log=lambda what: self.log_event("embed_to_disk", what))
        except Exception:  # noqa: BLE001 — a cleanup must never take the sync down
            logger.warning("canvas %s: forgetting node sessions failed", self.name,
                           exc_info=True)

    def apply_body(self, node: str, file: str, base_hash: str, text: str) -> dict:
        """One source text from the workbench's body channel → the clone, by
        compare-and-swap on content (canvas_snapshot.body_hash). Under
        _op_lock (the route takes it), so it never interleaves with a
        snapshot moving the file, a pull or a push.

        The frame names the hash of the text it last agreed on with this
        host (`base_hash`); the write lands only when disk still hashes to
        that. Otherwise — Claude or a pull wrote the file meanwhile — nothing
        is written and the answer carries what disk holds (`stale`,
        `disk_text`, `disk_hash`): the frame rebases its edits onto it and
        posts again. A missing file hashes as the empty text, so a file the
        frame created is written on its first post. Content-gated: a body
        equal to disk is acked without a write (no mtime move, no disk_rev,
        no delta). Returns the ack the page relays to the frame.
        """
        stem, dot, ext = file.rpartition(".")
        if not dot or stem != node or "/" in file or file.startswith(".") \
                or ("." + ext) not in canvas_snapshot.BODY_SUFFIXES:
            raise ValueError(f"{file!r} is not {node!r}'s source file")
        # A node has ONE source. A post naming another suffix than the file
        # already there (a type the frame changed) would plant a second
        # source.
        existing = canvas_snapshot.node_files(self.dir).get(node)
        if existing is not None and existing != file:
            raise ValueError(f"{node!r} is {existing!r} on disk, not {file!r}")
        # A hard pull replaced the clone (see `_body_fence_rev`): until the
        # frame has acked the pulled state, a body describes the canvas the
        # pull discarded — written, it would put that text (or a file the
        # pull removed, hashed here as the empty text) straight back. Not
        # stale (the frame would rebase and repost at once) but `fenced`:
        # the frame holds its text and tries again later, by which time the
        # full delta has re-seeded its base. Bound to the ack, never to a
        # clock: the snapshot fence's deadline does not apply here.
        if self._body_fence_rev is not None:
            self.log_event("embed_to_disk", "body fenced",
                           detail=f"{file}: predates the hard pull (rev "
                                  f"{self._body_fence_rev} not acked); not written")
            return {"ok": False, "code": "fenced", "node": node, "file": file}
        path = os.path.join(self.dir, file)
        text = canvas_snapshot.normalize_newlines(text)
        raw = canvas_snapshot._read_bytes(path)
        current = canvas_snapshot.body_text(raw)
        if current is None and base_hash != canvas_snapshot.body_hash(""):
            # The file was there when the frame last agreed on it and is gone
            # now: Claude (or a pull) deleted or renamed the node. Answered as
            # `stale` with the empty text, the frame rebased its edit onto ""
            # and posted again with the empty base, which reads as a create —
            # the deleted node came back holding a fragment of its code, and
            # the next push gave it a row (audit R5). `gone`: the frame keeps
            # its text and retries later; the delta that deletes or renames
            # the node settles it. Only an empty base (the frame creating a
            # node it just made) may create a file.
            self.log_event("embed_to_disk", "body for a missing file",
                           detail=f"{file}: deleted or renamed on disk; not recreated")
            return {"ok": False, "code": "gone", "node": node, "file": file}
        current_hash = canvas_snapshot.body_hash(current if current is not None else "")
        if current_hash != base_hash:
            self.log_event("embed_to_disk", "body stale",
                           detail=f"{file}: disk moved under the workbench's base; "
                                  "sent back for a rebase")
            return {"ok": False, "code": "stale", "node": node, "file": file,
                    "disk_text": current if current is not None else "",
                    "disk_hash": current_hash}
        new_hash = canvas_snapshot.body_hash(text)
        if current == text:
            self._body_written[node] = new_hash
            return {"ok": True, "node": node, "file": file, "hash": new_hash,
                    "changed": False, "created": False}
        if current is not None and self._body_written.get(node) != base_hash:
            # Not the frame's own previous text: keep a copy, as a note body
            # overwrite does.
            trash = self._new_trash_dir()
            self._backup_to(trash, file, raw)
        # Re-read right before the replace: the trash copy above took time,
        # and Claude writes without _op_lock. A write that landed in between
        # is answered stale like any other, instead of being overwritten
        # (the remaining window is the replace itself, microseconds).
        latest = canvas_snapshot.read_body(path)
        if latest != current:
            latest_text = latest if latest is not None else ""
            self.log_event("embed_to_disk", "body stale",
                           detail=f"{file}: disk moved while the body was being applied")
            return {"ok": False, "code": "stale", "node": node, "file": file,
                    "disk_text": latest_text,
                    "disk_hash": canvas_snapshot.body_hash(latest_text)}
        # An OSError propagates: the route answers busy, the frame keeps the
        # body pending and posts again.
        canvas_snapshot._write_atomic(path, text)
        self._body_written[node] = new_hash
        created = current is None
        if created:
            # The frame's row for a new UDF comes with its snapshot, usually
            # within the same second; until then the file is row-less.
            rows = set()
            try:
                doc = canvas_snapshot._load_toml(os.path.join(self.dir, "canvas.toml"))
                rows = set(canvas_snapshot.rows_of_doc(doc)["nodes"])
            except Exception:  # noqa: BLE001 — no toml yet: the row is owed either way
                pass
            self._fileless.discard(node)
            if node not in rows:
                self._rowless.add(node)
        # The frame holds what it just wrote: the view's body moves with
        # disk, so the next delta carries nothing for this node (its own
        # write never echoes). A node the view does not know (a new file,
        # or no view at all) is owed to the frame by the next cut, which
        # then carries this very text and a hash equal to the frame's base.
        view = self._embed_view
        if view is not None and node in view.get("nodes", {}):
            view.setdefault("bodies", {})[node] = text
        self._note_disk()
        self._view_rev = self.disk_rev
        self._frame_seen = True
        self._bump()
        return {"ok": True, "node": node, "file": file, "hash": new_hash,
                "changed": True, "created": created}

    def apply_rename(self, gen: int, seq: int, frm: str, to: str,
                     file_from: str | None, file_to: str | None) -> dict:
        """The workbench's rename hint (canvas_snapshot.apply_rename), under
        _op_lock like a snapshot, acked the same way. The previous snapshot is
        renamed too, so the snapshot that follows is diffed against the new
        name rather than read as delete + add."""
        stale = self._embed_enter(gen, seq)
        if stale is not None:
            return stale
        self._embed_commit_gen()
        result = canvas_snapshot.apply_rename(self.dir, frm, to, file_from, file_to)
        for held in (self._rowless, self._fileless):
            if frm in held and result["changed"]:
                held.discard(frm)
                held.add(to)
        if frm in self._body_written:
            self._body_written[to] = self._body_written.pop(frm)
        if result["changed"]:
            self._note_user_changes({}, renamed=[(frm, to)])
        renamed = canvas_snapshot.rename_prev(self._embed_view, frm, to, file_to)
        if renamed is None:
            # Nothing known before the rename, nothing known after: the next
            # delta is a full state. `_view_rev` follows the rev as in the
            # refresh.
            self._note_disk()
            self._view_rev = self.disk_rev
            self._frame_seen = True
            self._bump()
        else:
            self._refresh_view_after_write(
                renamed["nodes"], edges=renamed.get("edges", []), ids=renamed.get("ids", {}),
                bodies=renamed.get("bodies", {}), embed_rows=renamed.get("embed_rows"))
        ack = {"ok": True, "gen": gen, "seq": seq, "changed": result["changed"],
               "renamed": result["renamed"], "moved": result["moved"],
               "push_seq": self.push_seq}
        return self._embed_record(gen, seq, ack)

    def status(self) -> dict:
        """The short answer sync/start, sync/push and sync/pull return (the
        live picture is sync/state). Read by: the page (dir; a pull's
        `error`), `_canvas_push.py` (error, error_detail; the routes add `ok`
        and `pull_error`), and the tests (push_state, push_seq)."""
        return {
            "name": self.name,
            "env": self.env,
            "dir": self.dir,
            # "held": dirty, but the remote moved and nothing is pushed until
            # the user pulls. Internally still "pending" so the backstop
            # resumes the moment pull() clears it.
            "push_state": ("held" if (self.server_stale is not None or self._leased_elsewhere())
                           and self.push_state == "pending" else self.push_state),
            "push_seq": self.push_seq,
            "error": self.last_error,
            "error_detail": list(self.error_detail),
        }

    def pending_files(self) -> int:
        """How many files differ from the sync point (edited, added or
        deleted here and not published yet) — a full hash walk, so only on
        request (`sync/state?files=1`, the CLI interception's state line)."""
        base = self._base_files or {}
        disk = self._take_file_hashes(self.dir)
        return sum(1 for rel in set(base) | set(disk) if base.get(rel) != disk.get(rel))

    def sync_state(self, *, files: bool = False) -> dict:
        """GET /api/canvases/sync/state: one entry per leg of the two loops.
        The server knows three; `embed_to_disk` (snapshots the page has
        received and not yet had acked) is the host page's own knowledge and
        is merged in client-side — here it is always idle. `files` adds
        `pending_files` (a hash walk of the clone)."""
        now = time.time()

        def age(t: float | None) -> int | None:
            return None if t is None else max(0, int((now - t) * 1000))

        # disk → embed: the oldest disk_rev the frame has not acked.
        rev, view_rev = self.disk_rev, self._view_rev
        unacked = [t for r, t in list(self._rev_times.items()) if r > view_rev]
        oldest = min(unacked) if unacked else None
        if self._frame_seen and self._delta_error is not None:
            d2e = {"state": "error", "oldest_age_ms": age(oldest), "reason": self._delta_error}
        elif self._frame_seen and rev > view_rev and self._last_cut_held is not None:
            d2e = {"state": "held", "oldest_age_ms": age(oldest), "reason": self._last_cut_held}
        elif self._frame_seen and rev > view_rev:
            d2e = {"state": "pending", "oldest_age_ms": age(oldest), "reason": None}
        else:
            d2e = {"state": "idle", "oldest_age_ms": None, "reason": None}

        # disk → server.
        ps, dirty, stale = self.push_state, self._dirty_since, self.server_stale
        if ps == "pushing":
            d2s = {"state": "pushing", "oldest_age_ms": age(self._push_started_at), "reason": None}
        elif ps == "error":
            d2s = {"state": "error", "oldest_age_ms": age(dirty), "reason": self.last_error}
        elif stale is not None and (ps == "pending" or dirty is not None):
            d2s = {"state": "held", "oldest_age_ms": age(dirty), "reason": _stale_reason(stale)}
        elif self._leased_elsewhere() and (ps == "pending" or dirty is not None):
            d2s = {"state": "held", "oldest_age_ms": age(dirty), "reason": self._lease_reason()}
        elif ps == "pending" or dirty is not None:
            reason = None
            if self._paused():
                reason = "sync is paused"
            elif self._agent_run_id:
                reason = "waiting for the Claude session in this folder to finish"
            d2s = {"state": "pending", "oldest_age_ms": age(dirty), "reason": reason}
        else:
            d2s = {"state": "idle", "oldest_age_ms": None, "reason": None}
        d2s["last_push_at"] = self.last_push_at
        d2s["push_seq"] = self.push_seq

        # server → disk.
        if self._pull_in_flight:
            s2d = {"state": "pulling", "since_ms": age(self._pull_started_at),
                   "changed": _stale_changed(stale), "reason": None}
        elif stale is not None:
            stale_files = stale.get("files") or []
            s2d = {"state": "error" if self._last_pull_error else "stale",
                   "since_ms": age(stale.get("since")), "changed": _stale_changed(stale),
                   "reason": self._last_pull_error or _stale_reason(stale),
                   "files": [dict(f) for f in stale_files],
                   # Every file the server changed is already what this clone
                   # holds: the page's Pull says "Nothing will change".
                   "all_identical": bool(stale_files) and all(f.get("identical")
                                                              for f in stale_files)}
        elif self._leased_elsewhere():
            held = self._lease_held_by or {}
            s2d = {"state": "leased", "since_ms": None, "changed": [],
                   "reason": self._lease_reason(), "owner": held.get("owner"),
                   "lease_until": held.get("lease_until")}
        elif (self._last_merge is not None
              and now - self._last_merge["at"] < MERGED_SHOW_S):
            s2d = {"state": "merged", "since_ms": age(self._last_merge["at"]), "changed": [],
                   "reason": None, "files": list(self._last_merge["files"])}
        else:
            s2d = {"state": "idle", "since_ms": None, "changed": [], "reason": None}

        out = {
            "name": self.name,
            "env": self.env,
            "watching": not self.stop_event.is_set(),
            "legs": {
                "embed_to_disk": dict(_IDLE_EMBED_TO_DISK),
                "disk_to_embed": d2e,
                "disk_to_server": d2s,
                "server_to_disk": s2d,
            },
            "disk_rev": rev,
            "watcher_epoch": self.watcher_epoch,
            "owner": {"pid": os.getpid(), "port": canvas_sync_state.server_port()},
            "other_watchers": _other_watchers(self.env, self.name),
            "error_detail": list(self.error_detail),
            # The Fix-with-Claude button's one input.
            "fix_active": self.active_fix_run_id is not None,
            # Where a pull's backup of the clone lands (the Hard pull's
            # confirm step names it) and whether a Claude run is live in the
            # clone (cached: this is polled). The page warns on hard pull.
            "trash_root": self.trash_root(),
            "agent_live": bool(self.agent_run_id()),
            # The revision spine (None on a server without it): the revision
            # the sync point describes (the push's If-Match) and the latest
            # one a probe saw. `lease`: who holds the canvas's edit lease.
            "base_revision": self._base_revision,
            "server_revision": self._server_revision,
            "lease": self._lease_state(),
            # Epoch seconds when sync stops for want of an open canvas page
            # (IDLE_STOP_S), or None while one is open.
            "idle_stop_at": self._idle_stop_at,
        }
        if files:
            out["pending_files"] = self.pending_files()
        return out

    def _lease_state(self) -> dict | None:
        if self._leased_elsewhere():
            held = self._lease_held_by or {}
            return {"owner": held.get("owner"), "lease_until": held.get("lease_until"),
                    "mine": False}
        if self._lease_mine is not None:
            return {**self._lease_mine, "mine": True}
        return None


_IDLE_EMBED_TO_DISK = {"state": "idle", "oldest_age_ms": None, "reason": None}

# A delta refusal that means "nobody could take it yet", not "the frame
# choked on it": no frame on the page, a frame still hydrating.
_BENIGN_DELTA_REFUSAL = re.compile(r"hydrat|no[ _-]?frame", re.IGNORECASE)


def _stale_changed(stale: dict | None) -> list[dict]:
    """The stale chip's entries: one per UDF the server changed, plus the
    layout. Each carries `identical` when the server's bundle was compared
    (`stale["files"]`): that file is already what this clone holds."""
    if not stale:
        return []
    by_stem: dict[str, bool] = {}
    for f in stale.get("files") or []:
        path = str(f.get("path") or "")
        if "/" not in path:
            by_stem[os.path.splitext(path)[0]] = bool(f.get("identical"))
    changed = []
    for u in stale.get("udfs", []):
        entry = {"slug": u.get("slug"), "kind": u.get("kind")}
        if u.get("slug") in by_stem:
            entry["identical"] = by_stem[u["slug"]]
        changed.append(entry)
    if stale.get("layout"):
        entry = {"slug": None, "kind": "layout"}
        if "canvas" in by_stem:
            entry["identical"] = by_stem["canvas"]
        changed.append(entry)
    return changed


def _stale_reason(stale: dict | None) -> str:
    """"server changed: a (changed), b (added)" — the push route's wording."""
    udfs = (stale or {}).get("udfs") or []
    what = ", ".join(f"{u['slug']} ({u['kind']})" for u in udfs) or "the canvas layout"
    return f"server changed: {what}"


def _sync_key(env: str, name: str) -> str:
    """The canvas's key in canvas_sync_state — the event ring's log label and
    the socket notifier's channel. Env-qualified so a socket on prod's `alpha`
    is never woken (or logged) as unstable's."""
    return f"{env}/{name}"


def _lock_path(env: str, name: str) -> str:
    return os.path.join(canvases_root(env), ".sync", f"{name}.lock")


def _other_watchers(env: str, name: str) -> list[dict]:
    """A live process other than this one holding the clone's writer lock —
    only possible when this process holds no manager for it (the start was
    refused) or the lock was taken over from under us."""
    held = canvas_sync_state.lock_holder(_lock_path(env, name))
    if held is None:
        return []
    return [{"pid": held.get("pid"), "port": held.get("port")}]


def _unwatched_state(env: str, name: str) -> dict:
    """sync/state for a canvas this process is not syncing: every leg idle,
    and the owner if another process holds the lock (the page that got the
    409 shows who)."""
    others = _other_watchers(env, name)
    return {
        "name": name,
        "env": env,
        "watching": False,
        "legs": {
            "embed_to_disk": dict(_IDLE_EMBED_TO_DISK),
            "disk_to_embed": {"state": "idle", "oldest_age_ms": None, "reason": None},
            "disk_to_server": {"state": "idle", "oldest_age_ms": None, "reason": None,
                               "last_push_at": None, "push_seq": 0},
            "server_to_disk": {"state": "idle", "since_ms": None, "changed": [], "reason": None},
        },
        "disk_rev": 0,
        "watcher_epoch": None,
        "owner": others[0] if others else None,
        "other_watchers": others,
        "error_detail": [],
        "fix_active": False,
    }


class _CanvasOwned(Exception):
    """Another live fused-render process holds this clone's writer lock."""

    def __init__(self, owner: dict):
        super().__init__(f"pid {owner.get('pid')} owns this canvas")
        self.owner = owner

class _CanvasDeleting(Exception):
    """api_canvases_delete is running on this clone: no manager starts."""


_SYNC_LOCK = threading.Lock()
# Keyed (env, name): prod's `alpha` and unstable's `alpha` are two clones with
# two managers, live at once. (Keyed by name alone, a manager had to read the
# environment from the preference, which is why changing it once meant
# quiescing every manager first; with the env in the key nothing reads the
# preference and there is nothing to quiesce.)
_syncs: dict[tuple[str, str], _SyncManager] = {}
# Set by the quit rung (app.py `_stop_children`): no new manager — and so no
# new writer lock — once the app is quitting.
_QUITTING = False
# One lock per (env, name), under _SYNC_LOCK: two /clone requests for one
# canvas would run two `pull --force` into one folder. Per canvas, not
# global, so a slow clone of one canvas never holds up another's.
_CLONE_GATES: dict[tuple[str, str], threading.Lock] = {}
# Canvases mid-delete (api_canvases_delete), under _SYNC_LOCK: stopping the
# manager wakes the page's events socket, which reads "no watcher" and asks
# sync/start to heal — while canvas.toml still exists. Without this, a new
# manager (and lock) would start under the delete, and the folder would be
# removed from under a live watcher. api_canvases_rename holds BOTH its keys
# here for the same reason (a watcher on either name mid-move).
_DELETING: set[tuple[str, str]] = set()


class _CanvasHandingOver(Exception):
    """This process is handing the clone over (sync/release in progress)."""


# Clones this process is releasing to another fused-render right now
# (sync/release): its own sync/start is held off for the length of the stop,
# or the owner's page self-heal would re-claim the still-held flock as `ours`
# and start a new watcher under the taker's feet.
_HANDING_OVER: set[tuple[str, str]] = set()


def _clone_gate(env: str, name: str) -> threading.Lock:
    with _SYNC_LOCK:
        return _CLONE_GATES.setdefault((env, name), threading.Lock())


def refuse_new_managers() -> None:
    """The quit rung's spawn latch (armed before any killer runs)."""
    global _QUITTING
    with _SYNC_LOCK:
        _QUITTING = True


def shutdown(*, latch: bool = True) -> None:
    """Quit rung: stop every sync manager — a push CLI in flight, its watcher
    and fs threads, its writer lock — then close any claim left open. The
    packaged Mac app never runs the server's lifespan (app.py
    QUIT_CHILDREN_BUDGET_S), so without this a push CLI outlived the app as
    an orphan and the lock fds were left to the process exit. The server's
    lifespan shutdown (server/app.py) runs it too, with `latch=False`, for
    every exit that IS a lifespan one — the Linux supervisor's child, a pip
    `fused-render` on Windows, where the lock is a pid-probed file that
    nothing but this release ever clears (an unreleased file + pid reuse is a
    canvas no later start can claim).

    `latch` arms `_QUITTING` (no new manager from here on): the quit rung's
    contract. The lifespan caller leaves it off — a test process (or an
    embedding) that builds a second app after the first one shut down must
    still be able to start managers.

    A push is STOPPED, not waited for: its CLI is terminated (SIGTERM, then
    SIGKILL after PUSH_TERMINATE_GRACE_S) so its fold never runs on a half
    state; the push folds as failed and the sync point is not rotated. If the
    server had already taken the push, the next launch's first probe reads it
    as "server changed" and the user pulls their own push back — accepted.
    All stop events are set first so the joins overlap; each join is bounded
    by STOP_JOIN_S, and the kernel drops any flock left at exit."""
    global _QUITTING
    with _SYNC_LOCK:
        if latch:
            _QUITTING = True
        managers = list(_syncs.values())
        _syncs.clear()
    for manager in managers:
        manager.stop_event.set()
    for manager in managers:
        try:
            manager.terminate_push()
        except Exception:  # noqa: BLE001 — the stop below still runs
            logger.warning("canvas %s: stopping the push on quit failed",
                           _sync_key(manager.env, manager.name), exc_info=True)
    # Leases back in parallel, LEASE_RELEASE_QUIT_S for all of them together
    # (best-effort: a release still running past it is abandoned — a daemon
    # thread, its shim child killed by its own timeout — and the lease lapses).
    releasers = [threading.Thread(target=_release_lease_quietly, args=(m,), daemon=True,
                                  name=f"fused-canvas-lease-release-{m.name}")
                 for m in managers if m._lease_mine is not None]
    for t in releasers:
        t.start()
    deadline = time.time() + LEASE_RELEASE_QUIT_S
    for t in releasers:
        t.join(timeout=max(0.0, deadline - time.time()))
    for manager in managers:
        try:
            manager.stop(release_lease=False)
        except Exception:  # noqa: BLE001 — the next manager still stops
            logger.warning("canvas %s: stop on quit failed",
                           _sync_key(manager.env, manager.name), exc_info=True)
    canvas_sync_state.release_all()


def _sync_manager(env: str, name: str, create: bool) -> _SyncManager | None:
    """The live manager for canvas (env, name); with `create`, start one if
    none runs.

    Creating claims the clone's writer lock first (canvas_sync_state's
    docstring): a live foreign pid raises _CanvasOwned and nothing starts; a
    dead one's lock is taken over; this pid's own (a manager this process
    replaced) is simply re-taken."""
    key = (env, name)
    with _SYNC_LOCK:
        manager = _syncs.get(key)
        if manager is not None and manager.stop_event.is_set():
            manager = None
        if manager is None and create and _QUITTING:
            return None  # the quit latch: no new watcher (and no lock)
        if manager is None and create and key in _DELETING:
            raise _CanvasDeleting()  # same critical section as the creation
        if manager is None and create and key in _HANDING_OVER:
            raise _CanvasHandingOver()
        if manager is None and create:
            path = _lock_path(env, name)
            outcome, record = canvas_sync_state.claim_lock(path)
            if outcome == "refused":
                logger.info("canvas %s: lock refused — pid %s (port %s) owns it since %s",
                            _sync_key(env, name), record.get("pid"), record.get("port"),
                            record.get("since"))
                raise _CanvasOwned(record)
            mine = canvas_sync_state.read_lock(path) or {}
            manager = _SyncManager(env, name, lock_since=mine.get("since"))
            _syncs[key] = manager
            if outcome == "taken_over":
                manager.log_event("watcher", "lock taken over",
                                  detail=f"stale lock of pid {record.get('pid')} "
                                         f"(port {record.get('port')}) — that process is gone")
            else:
                manager.log_event("watcher", "lock taken",
                                  detail=f"pid {os.getpid()} port {canvas_sync_state.server_port()}")
        return manager


def _stop_manager(env: str, name: str) -> _SyncManager | None:
    """Stop and forget this process's manager for (env, name), if any: a
    push in flight is terminated (never folded on a half state), the
    watcher and fs threads joined, the writer lock released. The logout
    success path, for one canvas."""
    with _SYNC_LOCK:
        manager = _syncs.pop((env, name), None)
    if manager is None:
        return None
    manager.stop_event.set()
    try:
        manager.terminate_push()
    except Exception:  # noqa: BLE001 — the stop below still runs
        logger.warning("canvas %s: stopping the push before delete failed",
                       _sync_key(env, name), exc_info=True)
    manager.stop()
    return manager


def _clone_leftovers(env: str, name: str) -> list[str]:
    """What a clone leaves under `<root>/.sync` besides the folder itself:
    the merge base, the tombstones of nodes the user deleted, the writer
    lock, the push freeze and the trash. All go
    with the canvas — a later clone of the same name must not inherit a
    stale merge base, and the trash held versions of files that no longer
    have a canvas to go back to."""
    sync = os.path.join(canvases_root(env), ".sync")
    return [
        os.path.join(sync, f"{name}.json"),
        os.path.join(sync, f"{name}.tombstones.json"),
        os.path.join(sync, f"{name}.lock"),
        os.path.join(sync, "push", name),
        os.path.join(sync, "trash", name),
    ]


@router.post("/api/canvases/delete")
def api_canvases_delete(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Delete canvas (env, name) on the server AND on this machine: the
    embedded workbench's "Delete canvas" hands the confirmed delete here
    (`fused-embed-delete`) because the host is the canvas's one writer — a
    frame-side DELETE would race the host's push.

    Order: claim the clone's writer lock for the whole delete (refused while
    another fused-render process owns the clone); stop
    this process's sync (so no push lands on a canvas being deleted) and hold
    sync/start off with `_DELETING` (the stop wakes the page, which would
    otherwise heal a new watcher under the delete); delete
    on the server through the CLI; only then remove the clone, its `.sync`
    leftovers and its desk row (tasks under it archived, as the desk's own
    remove does). A CLI failure leaves the clone untouched — the page's
    self-heal re-arms sync/start — and is the response. A clone that is
    gone already (never cloned, or removed by hand) is fine: the server
    delete alone runs."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    if not _logged_in(env):
        return _error("not signed in to Fused — sign in first", 409)
    root = os.path.realpath(canvases_root(env))
    clone = os.path.realpath(_canvas_dir(env, name))
    # The one folder this may remove is `<root>/<name>` — never the root, never
    # anything a stray separator or link could point the name at.
    if os.path.dirname(clone) != root or os.path.basename(clone) != name:
        return _error("refusing to delete: the clone folder is not under the canvases root", 500)
    # The clone's writer lock is THIS request's for the whole delete: claimed
    # before anything else (a live foreign holder refuses the delete — it is
    # theirs to run), re-stamped over this process's own manager's claim so
    # the manager's stop() below releases nothing, and dropped only after the
    # cleanup. Another fused-render cannot take the lock and sync or push a
    # canvas mid-delete; `_DELETING` holds this process's own sync/start off.
    lock_path = _lock_path(env, name)
    try:
        outcome, record = canvas_sync_state.claim_lock(lock_path)
    except OSError as exc:
        return _error(f"could not take the canvas sync lock: {exc}", 500)
    if outcome == "refused":
        owner = {k: record.get(k) for k in ("pid", "port", "since")}
        return JSONResponse(
            {"error": f"canvas {_sync_key(env, name)!r} is synced by another fused-render "
                      f"(pid {owner['pid']}, port {owner['port']}); delete it from there",
             "code": "owned", "owner": owner}, status_code=409)
    lock_since = (canvas_sync_state.read_lock(lock_path) or {}).get("since")
    key = (env, name)
    with _SYNC_LOCK:
        _DELETING.add(key)
    try:
        manager = _stop_manager(env, name)
        if manager is not None and manager.thread.is_alive():
            # A watcher mid-subprocess past STOP_JOIN_S could still write the
            # clone after the wait; not a folder to delete under. Bounded: no
            # second wait, the user retries.
            return _error(f"canvas {name!r} is still syncing; retry the delete shortly", 409)
        _proc, err = _run_cli(["workbench", "canvas", "delete", name], LIST_TIMEOUT, env)
        _cli_cache_clear()
        if err is not None:
            return err  # the page's self-heal re-arms sync/start once this lifts
        removed_local = False
        if os.path.isdir(clone):
            try:
                shutil.rmtree(clone)
            except OSError as exc:
                return _error(f"canvas {name!r} was deleted on the server, but its folder "
                              f"could not be removed ({clone}): {exc}", 500)
            removed_local = True
        for path in _clone_leftovers(env, name):
            if os.path.isdir(path):
                _rmtree_quiet(path)
            else:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    finally:
        canvas_sync_state.release_lock(lock_path, lock_since)
        with _SYNC_LOCK:
            _DELETING.discard(key)
    try:
        from fused_render import current_apps
        from fused_render.server.routers.current_apps import _archive_folder

        # The CONFIGURED spelling, not the resolved one: the desk row and the
        # tasks' project were recorded as `canvases_dir + "/" + name`,
        # and canonical_fs_path resolves no symlinks.
        desk_dir = _canvas_dir(env, name)
        current_apps.remove(desk_dir)
        _archive_folder(desk_dir)
    except Exception:  # noqa: BLE001 — the desk row is cosmetic next to the delete
        logger.warning("canvas %s: dropping the desk row after delete failed",
                       _sync_key(env, name), exc_info=True)
    logger.info("canvas %s: deleted (clone removed: %s)", _sync_key(env, name), removed_local)
    return {"ok": True, "name": name, "env": env, "removed_local": removed_local}


_TOML_NAME_RE = re.compile(r'^name\s*=\s*"([A-Za-z0-9_]*)"[ \t]*$', re.M)


def _patch_toml_name(clone: str, old: str, new: str) -> None:
    """Best-effort: the clone's canvas.toml carries the canvas name as a
    top-level scalar (`name = "<old>"`, written by the CLI's pull) and every
    push ships it. Rewritten in the preamble only: a `name` under a table
    is a node's, never the canvas's."""
    path = os.path.join(clone, "canvas.toml")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    table = re.search(r"^\[", text, re.M)
    head_end = table.start() if table is not None else len(text)
    match = _TOML_NAME_RE.search(text, 0, head_end)
    if match is None or match.group(1) != old:
        return
    try:
        canvas_snapshot._write_atomic(
            path, text[:match.start()] + f'name = "{new}"' + text[match.end():])
    except OSError:
        logger.warning("canvas rename: could not patch the name in %s", path, exc_info=True)


@router.post("/api/canvases/rename")
def api_canvases_rename(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Rename canvas (env, name) to `new_name` on the server AND on this
    machine. The sidebar's "Rename…" and the embedded workbench's canvas
    title (`fused-embed-canvas-rename`, which only asks the host to open its
    dialog) both land here: the host is the canvas's one writer.

    The hazard every step guards: the CLI's push resolves a canvas BY NAME
    and CREATES one when the name is missing, so anything that pushes under
    the old name after the server rename (this process's watcher, a Claude
    session's hook, another fused-render) brings the old canvas back as a
    duplicate.

    Order (delete's, api_canvases_delete): validate; preflight that nothing
    sits at the new clone path or its `.sync` leftovers and that no canvas
    of the account has the new name; refuse while a Claude session in the
    clone is live or mid-turn — a chat's host process outlives its turn by
    ~30 s (session_host's idle reap), so an idle chat moves, one working or
    just finished does not (its transcript could not move, and a clone has
    no `.fused/meta.json` to retry it later — it must never get one, the
    push would ship it); claim the OLD clone's
    writer lock for the whole call (refused while another fused-render owns
    it); hold sync/start off for BOTH keys with `_DELETING` (the stop wakes
    the page, whose self-heal would otherwise start a watcher on either name
    mid-move); stop this process's sync; rename on the SERVER first — a CLI
    failure changes nothing local; then move the clone and its leftovers,
    patch canvas.toml's name, carry the desk row, stores and Claude sessions
    along. A folder move that fails after the server rename is rolled back
    on the server; a failed rollback keeps the old key held in this process
    and says what to do by hand. The caller navigates to the new route, and
    that page's sync/start starts the watcher under the new name (and
    re-seeds the clone's CLAUDE.md, which names the canvas).

    Accepted: another computer's clone under the old name re-creates the old
    canvas on its next push, and anything calling the canvas by name stops
    resolving — the dialog says both."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    new = body.get("new_name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    if not isinstance(new, str) or not _NAME_RE.fullmatch(new):
        return _error("Canvas names may only use letters, digits, and underscores "
                      "(at most 128)")
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    if new == name:
        return {"ok": True, "env": env, "name": name, "old_name": name}
    if not _logged_in(env):
        return _error("not signed in to Fused — sign in first", 409)
    root = os.path.realpath(canvases_root(env))
    clone = os.path.realpath(_canvas_dir(env, name))
    target = os.path.join(root, new)
    if os.path.dirname(clone) != root or os.path.basename(clone) != name:
        return _error("refusing to rename: the clone folder is not under the canvases root", 500)
    # A case-only rename (foo -> Foo) is the same folder on a case-insensitive
    # disk: `lexists` would refuse it, os.rename handles it.
    case_only = os.path.normcase(target) == os.path.normcase(clone)
    # The new name's lock FILE is not "taken" (a lock file outlives its
    # folder): its writer is decided by the claim below, never by existence.
    taken = [p for p in [target, *_clone_leftovers(env, new)]
             if p != _lock_path(env, new) and os.path.lexists(p) and not case_only]
    if taken:
        return JSONResponse(
            {"error": f"a canvas folder named {new!r} already exists on this computer "
                      f"({taken[0]}); pick another name", "code": "exists"},
            status_code=409)
    # The account's canvases, uncached: nothing shows the server refuses a
    # taken name, and two canvases under one name would let the name-resolving
    # push replace the OTHER canvas's UDF list. Case-insensitive like the
    # workbench's own check; the canvas itself may change case.
    entries, err = _list_entries_uncached(env)
    if err is not None:
        return err
    if any(isinstance(e, dict) and isinstance(e.get("name"), str)
           and e["name"].lower() == new.lower() and e["name"] != name
           for e in entries or ()):
        return JSONResponse(
            {"error": f"a canvas named {new!r} already exists; pick another name",
             "code": "exists"}, status_code=409)
    has_clone = os.path.isdir(clone)
    if has_clone:
        from fused_render import claude_session_move

        held = claude_session_move.held_sessions(_canvas_dir(env, name))
        if held:
            return JSONResponse(
                {"error": "Claude is working in this canvas, or finished moments "
                          "ago. Wait for it to finish (or stop it), then rename.",
                 "code": "sessions_live", "sessions": held}, status_code=409)
    lock_path = _lock_path(env, name)
    try:
        outcome, record = canvas_sync_state.claim_lock(lock_path)
    except OSError as exc:
        return _error(f"could not take the canvas sync lock: {exc}", 500)
    if outcome == "refused":
        owner = {k: record.get(k) for k in ("pid", "port", "since")}
        return JSONResponse(
            {"error": f"canvas {_sync_key(env, name)!r} is synced by another fused-render "
                      f"(pid {owner['pid']}, port {owner['port']}); rename it from there",
             "code": "owned", "owner": owner}, status_code=409)
    lock_since = (canvas_sync_state.read_lock(lock_path) or {}).get("since")
    # The NEW name's writer lock too, for the same span: the folder move makes
    # `canvas.toml` visible under the new path before the merge base,
    # tombstones and trash have followed it, and `_DELETING` holds off only
    # this process — another fused-render's sync/start would claim the new
    # key and seed sync state the leftover move then overwrites.
    # A case-only rename is the same lock FILE on a case-insensitive disk: a
    # second flock on it from this process would refuse itself.
    new_lock_path = _lock_path(env, new)
    try:
        new_outcome, new_record = (("ours", {}) if case_only
                                   else canvas_sync_state.claim_lock(new_lock_path))
    except OSError as exc:
        canvas_sync_state.release_lock(lock_path, lock_since)
        return _error(f"could not take the canvas sync lock: {exc}", 500)
    if new_outcome == "refused":
        canvas_sync_state.release_lock(lock_path, lock_since)
        owner = {k: new_record.get(k) for k in ("pid", "port", "since")}
        return JSONResponse(
            {"error": f"canvas {_sync_key(env, new)!r} is synced by another fused-render "
                      f"(pid {owner['pid']}, port {owner['port']}); pick another name",
             "code": "owned", "owner": owner}, status_code=409)
    new_lock_since = (None if case_only
                      else (canvas_sync_state.read_lock(new_lock_path) or {}).get("since"))
    with _SYNC_LOCK:
        _DELETING.update({(env, name), (env, new)})
    moved_clone = False
    keep_held = False
    # The stopped manager's push/pull locks, held to the end of the local
    # move: stop() joins the watcher thread, but a MANUAL pull or push already
    # inside its network leg runs on a route thread and would go on writing
    # the old clone path mid-move. Same order as pause() (flight, then op).
    flight_locks: list[threading.Lock] = []
    try:
        manager = _stop_manager(env, name)
        if manager is not None and manager.thread.is_alive():
            return _error(f"canvas {name!r} is still syncing; retry the rename shortly", 409)
        if manager is not None:
            for lk in (manager._flight_lock, manager._op_lock):
                if not lk.acquire(timeout=MANUAL_PUSH_LOCK_WAIT_S):
                    return _error(f"canvas {name!r} is still syncing; retry the rename "
                                  "shortly", 409)
                flight_locks.append(lk)
        _proc, err = _run_cli(["workbench", "canvas", "rename", name, new], LIST_TIMEOUT, env)
        _cli_cache_clear()
        if err is not None:
            return err  # nothing local moved; the page's self-heal re-arms sync/start
        if has_clone:
            try:
                os.rename(clone, target)
            except OSError as exc:
                _rb, rb_err = _run_cli(["workbench", "canvas", "rename", new, name],
                                       LIST_TIMEOUT, env)
                _cli_cache_clear()
                if rb_err is None:
                    return _error(f"could not rename the canvas folder ({exc}); "
                                  f"the canvas keeps its name {name!r}", 500)
                # The server says `new`, the clone says `name`: a watcher on the
                # old key would push the old name back as a second canvas.
                # This process keeps sync/start off that key (`_DELETING`) AND
                # keeps the clone's writer lock, so no OTHER fused-render can
                # claim it and start that watcher either — until this process
                # quits (the quit rung's release_all), by when the user has
                # renamed the folder as the error says.
                keep_held = True
                logger.error("canvas %s: rename to %r half done — folder move failed (%s), "
                             "server rollback failed", _sync_key(env, name), new, exc)
                return _error(f"the canvas was renamed to {new!r} on the server, but its "
                              f"folder could not be renamed ({exc}) and the server rename "
                              f"could not be undone. Rename the folder {clone} to {new} "
                              "by hand, then reopen the canvas.", 500)
            moved_clone = True
            for src, dst in zip(_clone_leftovers(env, name), _clone_leftovers(env, new)):
                if src == lock_path or not os.path.lexists(src):
                    continue  # the lock is released below; the new key's is sync/start's
                try:
                    os.rename(src, dst)
                except OSError:
                    # A merge base that did not move means a fresh first sync
                    # under the new name: slower, never wrong.
                    logger.warning("canvas rename: could not move %s -> %s", src, dst,
                                   exc_info=True)
            _patch_toml_name(target, name, new)
    finally:
        for lk in reversed(flight_locks):
            lk.release()
        if not case_only:
            canvas_sync_state.release_lock(new_lock_path, new_lock_since)
        if not keep_held:
            canvas_sync_state.release_lock(lock_path, lock_since)
        if moved_clone:
            try:
                os.unlink(lock_path)
            except OSError:
                pass
        with _SYNC_LOCK:
            _DELETING.discard((env, new))
            if not keep_held:
                _DELETING.discard((env, name))
    if moved_clone:
        try:
            from fused_render import app_state_move, claude_session_move, tasks_watch

            # The CONFIGURED spelling (delete's note): the desk row and the
            # tasks' project were recorded as `canvases_dir + "/" + name`.
            old_dir, new_dir = _canvas_dir(env, name), _canvas_dir(env, new)
            app_state_move.rewrite_stores(old_dir, new_dir)
            carried = claude_session_move.relocate(old_dir, new_dir)
            if carried.get("pending"):
                logger.warning("canvas rename %s -> %s: sessions left behind %s",
                               _sync_key(env, name), new, carried["pending"])
            tasks_watch.notify_all()
        except Exception:  # noqa: BLE001 — the rename itself is done
            logger.warning("canvas %s: carrying stores/sessions after rename failed",
                           _sync_key(env, name), exc_info=True)
    logger.info("canvas %s: renamed to %r (clone moved: %s)", _sync_key(env, name), new,
                moved_clone)
    return {"ok": True, "env": env, "name": new, "old_name": name}


def _sync_start_response(env: str, name: str):
    """The body of sync/start, shared with sync/takeover: claim the writer
    lock and start this process's watcher on the clone, or say who owns it."""
    if not os.path.isfile(os.path.join(_canvas_dir(env, name), "canvas.toml")):
        return _error(f"canvas {name!r} is not cloned yet (no canvas.toml)", 409)
    # The writer lock FIRST: a refused start must not touch a folder another
    # process owns (the seeding below writes into it).
    try:
        manager = _sync_manager(env, name, create=True)
    except _CanvasDeleting:
        return JSONResponse({"error": f"canvas {_sync_key(env, name)!r} is being deleted "
                                      "or renamed",
                             "code": "deleting"}, status_code=409)
    except _CanvasHandingOver:
        # The page retries on `busy`; by then the taker holds the lock and
        # the answer is its `owned`.
        return _busy(f"canvas {_sync_key(env, name)!r} is being handed over to another fused-render")
    except _CanvasOwned as owned:
        # One writer per clone: another fused-render (the desktop app and a
        # dev server, two branches) is already syncing this folder. The page
        # says who and does not start its delta leg (owner decision Q4).
        return _owned_response(env, name, owned.owner)
    except OSError as exc:
        return _error(f"could not take the canvas sync lock: {exc}", 500)
    if manager is None:
        return _busy("Fused Render is quitting; canvas sync is not starting")
    # The page calls this when it OPENS a canvas (and again whenever sync/state
    # or the events socket shows no watcher — `watcher_epoch` null — to heal a
    # dropped one), which makes it the hook that covers a canvas cloned
    # before this feature existed — nearly every real canvas. /clone alone left
    # those sessions with no workbench skills at all. Rate-limited and
    # off-thread; see _kick_workbench_skills.
    _kick_workbench_skills()
    # Re-seed CLAUDE.md for the same reason, and it is the other half of the same
    # bug: seeding otherwise runs at /clone and after a CLI force pull only, so a
    # canvas cloned before a text change keeps the OLD file forever. Concretely,
    # clones from before D360 still name the pre-rename `fused:*` skills — and a
    # user who once ran the `fused claude plugin add` that the older text told
    # them to has that stale plugin installed globally, so the session finds
    # `fused:canvas-toml` in its skill list, uses it, and never touches the
    # current `workbench:*` root we hand it per-run. Nothing warns: stale skills
    # load cleanly. Invisible to the sync — both basenames are excluded from the
    # fingerprint, hash and merge-base walks (_SYNC_IGNORED_BASENAMES) and from
    # every push (.fusedignore) — and shim-gated inside the helper, so the
    # external-CLI leg still cannot churn on it.
    _seed_clone_claude_files(manager.dir, name)
    return manager.status()


def _owned_response(env: str, name: str, owner: dict, *, code: str = "owned",
                    error: str | None = None, **extra) -> JSONResponse:
    """The 409 the page reads as "another process owns this clone"
    (`ownerFromError`): `{error, code, owner: {pid, port, since}}`. `extra`
    fields ride along (takeover's `can_force_quit`)."""
    owner = {k: owner.get(k) for k in ("pid", "port", "since")}
    body = {"error": error or (f"canvas {_sync_key(env, name)!r} is synced by another fused-render "
                               f"(pid {owner['pid']}, port {owner['port']})"),
            "code": code, "owner": owner}
    body.update(extra)
    return JSONResponse(body, status_code=409)


def _is_fused_process(pid: int) -> bool:
    """Is `pid` a live process of THIS app (a fused-render / Fused desktop
    process), by its executable: the very same executable as ours (full path),
    or one named for the app or the package. The gate before `sync/force_quit`
    signals a lock holder — a pid that was reused by something else is never
    ours, whatever the lock file says."""
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False
    if not canvas_sync_state.pid_alive(pid):
        return False
    exe_path = canvas_sync_state.process_executable(pid)
    if not exe_path:
        return False
    # The SAME executable as ours (a dev server's venv python, a pip install's
    # python.exe on Windows): full path, not basename — `Program Files\\x.exe`
    # and `Program Files\\y.exe` share a basename of nothing useful, and a
    # reused pid under another python must not read as ours.
    own = canvas_sync_state.process_executable(os.getpid()) or sys.executable
    try:
        if own and os.path.normcase(os.path.realpath(exe_path)) == os.path.normcase(os.path.realpath(own)):
            return True
    except OSError:
        pass
    # Or an executable NAMED for this app (the packaged app's main binary; an
    # older bundle moved to a hidden sibling path still carries the name).
    exe = os.path.basename(exe_path).lower()
    if "fused_render" in exe or "fused-render" in exe or "fusedrender" in exe:
        return True
    from fused_render import _flavor

    app = _flavor.app_name().lower()
    if app and app in exe:
        return True
    # Or a Python running THIS package by name: the Linux supervisor's child
    # (`python3 -I -m fused_render.cli serve`, under a per-launch AppImage
    # mount, so its path never equals ours after an update) or a pip
    # install's `fused-render serve` from another venv. Tokens are tested as
    # the module name or a script's BASENAME — a checkout path that merely
    # contains `fused-render` (every process started under the repo) is not
    # a match.
    for token in canvas_sync_state.process_argv(pid) or []:
        if token == "fused_render" or token.startswith("fused_render."):
            return True
        # Both separators: the card may be read on any platform, and a
        # Windows token is `C:\\…\\Scripts\\fused-render.exe`.
        if token.replace("\\", "/").rsplit("/", 1)[-1].lower() in _FUSED_SCRIPT_NAMES:
            return True
    return False


#: The package's console scripts (pyproject `[project.scripts]`), as argv[0]
#: basenames on every platform.
_FUSED_SCRIPT_NAMES = frozenset({
    "fused-render", "fused-render.exe", "fused-render-script.py",
    "fused-render-app", "fused-render-app.exe", "fused-render-app-script.py",
})


def _unreachable_owner_response(env: str, name: str, held: dict, *, why: str) -> JSONResponse:
    """The takeover's answer when the lock's holder cannot be asked: `code`
    `owner_unreachable`, plus `can_force_quit` — true when the card's pid is
    a live process of this app (and not us), so the page can offer the
    user a "Force quit" of it. Nothing is signalled here."""
    pid = held.get("pid")
    can_force_quit = isinstance(pid, int) and _is_fused_process(pid)
    hint = (" You can force quit that process from here." if can_force_quit
            else " Close it there.")
    return _owned_response(env, name, held, code="owner_unreachable",
                           error=f"pid {pid} owns this canvas but {why}.{hint}",
                           can_force_quit=can_force_quit)


@router.post("/api/canvases/sync/start")
def api_canvases_sync_start(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    return _sync_start_response(env, name)


# A peer's `sync/release` waits up to RELEASE_PUSH_WAIT_S for a publish in
# flight to end (a clone that was dirty when it opened pushes within seconds
# of starting, exactly when a second page tends to ask for it), then joins
# its watcher for up to STOP_JOIN_S; the hand-over's HTTP wait covers both,
# never a push's full PUSH_TIMEOUT (past the wait the peer answers `busy`).
RELEASE_PUSH_WAIT_S = 20.0
RELEASE_HTTP_TIMEOUT_S = RELEASE_PUSH_WAIT_S + STOP_JOIN_S + 5.0


def _ask_peer_release(port: int, env: str, name: str) -> tuple[int | None, dict]:
    """POST /api/canvases/sync/release on the fused-render at `port` (always
    loopback: the lock's business card names a port on this machine).
    Returns `(status, body)`; `(None, {})` when nothing answered there."""
    import http.client
    import urllib.error
    import urllib.request

    payload = json.dumps({"name": name, "env": env}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/canvases/sync/release",
        data=payload, method="POST",
        headers={"Content-Type": "application/json", "X-Fused": "1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=RELEASE_HTTP_TIMEOUT_S) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except (OSError, http.client.HTTPException):
            raw = b""
        status = exc.code
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        # BadStatusLine / IncompleteRead derive from HTTPException, not
        # OSError: a half-answer from the peer is "did not answer" too.
        return None, {}
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        data = {}
    return status, data if isinstance(data, dict) else {}


@router.post("/api/canvases/sync/release")
def api_canvases_sync_release(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Hand this clone over: stop THIS process's watcher and drop its writer
    lock, so the fused-render that asked (sync/takeover on another port) can
    claim it. `{released: true}` when a watcher ran here and is now stopped;
    `{released: false}` when none did (the lock was never ours — nothing to
    give). A watcher mid-publish or mid-pull is waited for
    (RELEASE_PUSH_WAIT_S), then answers 409 `busy` and keeps the lock: a
    release under a running push CLI would let the taker push over it, the
    two-writer race the lock exists to prevent. Own sync/start is held off
    (`_HANDING_OVER`) until the flock is dropped. The page here notices its
    watcher is gone on its next state read, re-arms through sync/start, gets
    the taker's `owned` and shows the same Take-over button back."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    key = (env, name)
    with _SYNC_LOCK:
        if _syncs.get(key) is None:
            return {"released": False, "name": name, "env": env}
        # Own sync/start held off from here to the end of the stop: the
        # owner's page self-heals through it the moment its watcher goes,
        # and `claim_lock` would hand it the still-held flock as `ours`.
        _HANDING_OVER.add(key)
    try:
        # Quiesce first, then stop — the activity flags alone are not enough:
        # a request thread (the page's Pull, a session's push through the
        # intercept) can be past them and about to write. Wait, bounded, for
        # no push CLI, no pull, and both of the manager's locks idle — a
        # dirty clone pushes seconds after it opens, right when a second page
        # tends to ask for it. Past the wait: `busy`, nothing touched.
        deadline = time.monotonic() + RELEASE_PUSH_WAIT_S
        while True:
            with _SYNC_LOCK:
                manager = _syncs.get(key)
            if manager is None:
                return {"released": False, "name": name, "env": env}
            quiet = (manager._push_proc is None and not manager._pull_in_flight
                     and not manager._flight_lock.locked() and not manager._op_lock.locked())
            if quiet:
                break
            if time.monotonic() >= deadline:
                return _busy(f"canvas {_sync_key(env, name)!r} is publishing; try again when it is done")
            time.sleep(0.25)
        manager.log_event("watcher", "lock handed over",
                          detail=f"released on request of another fused-render (pid {os.getpid()} stops)")
        # stop_event goes up first inside: a push that slipped past the check
        # above aborts at its own stop check before (or right after) its CLI
        # starts, and `terminate_push` covers one already running. The
        # manager's own stop() releases the flock last.
        stopped = _stop_manager(env, name)
        if stopped is not None and stopped.thread.is_alive():
            # Past STOP_JOIN_S mid-subprocess: it may still write the clone.
            # Bounded, like the delete: the taker's page retries.
            return _busy(f"canvas {_sync_key(env, name)!r} is still syncing; try again shortly")
    finally:
        with _SYNC_LOCK:
            _HANDING_OVER.discard(key)
    return {"released": True, "name": name, "env": env}


@router.post("/api/canvases/sync/takeover")
def api_canvases_sync_takeover(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Make THIS process the clone's writer when another fused-render on this
    machine holds the lock: ask that process (the port in the lock's business
    card) to `sync/release`, then start here exactly as sync/start does. The
    answer is sync/start's: the manager's status on success, or the 409
    `owned` the page already understands — with `code` `owner_too_old` when
    the owner runs a build without the release route (close it there, as
    before), `busy` when it is mid-publish, and the owner's own error when it
    refused for another reason. A lock nobody holds any more just starts."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    held = canvas_sync_state.lock_holder(_lock_path(env, name))
    if held is not None:
        port = held.get("port")
        if not isinstance(port, int) or port <= 0:
            return _unreachable_owner_response(env, name, held, why="published no port to ask")
        if canvas_sync_state.card_names_this_server(held):
            # The card names OUR port: the holder is a predecessor that lost
            # the port to us (an update's restart, a quit that never released).
            # Asking it would ask ourselves — `{released: false}`, then the
            # same refusal, and a button that visibly does nothing.
            return _unreachable_owner_response(
                env, name, held, why=f"its port {port} is now this Fused's own, so it cannot be asked")
        status, answer = _ask_peer_release(port, env, name)
        if status == 404:
            return _owned_response(env, name, held, code="owner_too_old", error=(
                f"pid {held.get('pid')} (port {port}) runs an older Fused that cannot hand a "
                f"canvas over; close it there"))
        if status is None:
            return _unreachable_owner_response(
                env, name, held, why=f"nothing answered on its port {port}")
        if status != 200:
            return _owned_response(env, name, held, code=str(answer.get("code") or "owned"),
                                   error=str(answer.get("error") or
                                             f"pid {held.get('pid')} (port {port}) refused to hand "
                                             f"the canvas over (HTTP {status})"))
        logger.info("canvas %s: lock handed over by pid %s (port %s), released=%s",
                    _sync_key(env, name), held.get("pid"), port, answer.get("released"))
    return _sync_start_response(env, name)


FORCE_QUIT_WAIT_S = 6.0


@router.post("/api/canvases/sync/force_quit_owner")
def api_canvases_sync_force_quit_owner(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """USER-PRESSED: send SIGTERM to the process holding this clone's writer
    lock, when it is one of this app's own processes that can no longer be
    asked to hand over (sync/takeover answered `owner_unreachable`). The
    case is an update's restart that left the old instance alive without a
    server, or a quit that never finished: a live flock with nobody behind
    it, which no page could clear before (D1440 rejected killing a LIVE
    owner — one that still answers; this never signals such a one).

    Guards, all re-checked here and not trusted from the page: the lock is
    held right now by a pid other than ours; that pid is a Fused process by
    its command line (`_is_fused_process`); and its port, if any, either is
    ours or does not answer `sync/release` — an owner that answers is asked
    to release instead and never signalled. Then SIGTERM (the ordinary quit
    signal; never SIGKILL from here), and a bounded wait for the lock to
    free. Answers `{ok, freed, pid}`; the page re-arms through sync/start
    once `freed`."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    path = _lock_path(env, name)
    held = canvas_sync_state.lock_holder(path)
    if held is None:
        return {"ok": True, "freed": True, "pid": None, "signalled": False}
    pid = held.get("pid")
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return _error("the lock holder's pid is unknown; close that Fused yourself", 409)
    if not _is_fused_process(pid):
        return _error(f"pid {pid} is not a running Fused process; nothing was signalled", 409)
    port = held.get("port")
    if isinstance(port, int) and port > 0 and not canvas_sync_state.card_names_this_server(held):
        status, answer = _ask_peer_release(port, env, name)
        if status is not None:
            # It answers (a 404 included: an older build without the release
            # route is a LIVE owner): not a leftover, not ours to kill.
            return _owned_response(env, name, held, error=(
                f"pid {pid} (port {port}) is a running Fused that still answers; "
                f"use Take over, or close it there"))
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        return _error(f"could not signal pid {pid}: {exc}", 500)
    logger.warning("canvas %s: SIGTERM sent to lock holder pid %s (port %s) on the user's request",
                   _sync_key(env, name), pid, port)
    deadline = time.monotonic() + FORCE_QUIT_WAIT_S
    freed = False
    while time.monotonic() < deadline:
        now_held = canvas_sync_state.lock_holder(path)
        if now_held is None or now_held.get("pid") != pid:
            freed = True
            break
        time.sleep(0.2)
    return {"ok": True, "freed": freed, "pid": pid, "signalled": True}


@router.post("/api/canvases/snapshot")
def api_canvases_snapshot(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Apply one embedded-workbench snapshot to the clone (embed v3).

    The workspace page relays the frame's `fused-embed-snapshot {gen, seq,
    body}` here and posts the answer back as `-applied` / `-rejected`. The
    embed never writes the server itself under v3 — this is how its canvas
    edits reach disk, and the watcher's push is how they reach the server
    (canvas_snapshot.py's docstring has the model).

    Refuses with 409 `busy` (same shape as sync/push) when another sync leg
    holds _op_lock for longer than SNAPSHOT_LOCK_WAIT_S; the embed keeps
    the snapshot queued and re-posts on its next change or reload. A
    malformed snapshot or a clone with no readable canvas.toml is a 400
    `rejected` with the reason — the embed drops that one and the user sees
    the reason on the chip.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    gen, seq, snapshot = body.get("gen"), body.get("seq"), body.get("body")
    rename = body.get("rename")
    if not isinstance(gen, int) or not isinstance(seq, int):
        return _error("'gen' and 'seq' must be integers")
    if isinstance(rename, dict):
        frm, to = rename.get("from"), rename.get("to")
        if not isinstance(frm, str) or not isinstance(to, str) or not frm or not to:
            return _error("'rename' needs 'from' and 'to'")
        for key in ("file_from", "file_to"):
            val = rename.get(key)
            if val is not None and (not isinstance(val, str) or "/" in val):
                return _error(f"'rename.{key}' must be a basename")
    elif not isinstance(snapshot, dict):
        return _error("'body' must be an object (or 'rename' a hint)")
    manager = _sync_manager(env, name, create=False)
    if manager is None:
        return JSONResponse(
            {"error": f"canvas {_sync_key(env, name)!r} is not being synced (no watcher is running)",
             "code": "no_watcher"}, status_code=409)
    # A short wait, not sync/push's 5 s: the page retries a 409 on its own
    # (SNAPSHOT_BUSY_WINDOW_MS), and holding the request open for the length of
    # a pull would only delay the ack past the chip's "reconnecting" line. With
    # _op_lock never held across a network call this should essentially never
    # fire — the one remaining long holder is a CLI `pull --force` — so when it
    # does, it is logged.
    what = f"rename {rename.get('from')} → {rename.get('to')}" if isinstance(rename, dict) \
        else "snapshot"
    started = time.time()
    if not manager._op_lock.acquire(timeout=SNAPSHOT_LOCK_WAIT_S):
        manager.log_event("embed_to_disk", "snapshot busy", ok=False,
                          ms=(time.time() - started) * 1000,
                          detail=f"{what} {gen}/{seq}: the clone stayed locked past "
                                 f"{SNAPSHOT_LOCK_WAIT_S:g}s")
        return _busy(
            "a sync operation is in flight for this canvas; try again in a moment")
    try:
        with manager.pause_lock:
            if manager.pause_count > 0:
                manager.log_event("embed_to_disk", "snapshot busy", ok=False,
                                  detail=f"{what} {gen}/{seq}: sync is paused")
                return _busy(f"syncing for {_sync_key(env, name)!r} is paused; try again in a moment")
        try:
            if isinstance(rename, dict):
                ack = manager.apply_rename(
                    gen, seq, rename["from"], rename["to"],
                    rename.get("file_from"), rename.get("file_to"))
            else:
                ack = manager.apply_snapshot(gen, seq, snapshot)
        except FileNotFoundError:
            # Busy, not rejected: a canvas.toml that is missing for a moment
            # (Claude replacing it, a pull mid-write) must not cost the user
            # the edit — a 400 makes the frame drop this snapshot and refetch
            # the server's copy. The page retries a 409 for two minutes and
            # only then reports it.
            manager.log_event("embed_to_disk", "snapshot busy", ok=False,
                              detail=f"{what} {gen}/{seq}: the clone has no canvas.toml right now")
            return _busy("the clone has no canvas.toml right now; retrying")
        except canvas_snapshot.DiskUnreadable as exc:
            # Claude's edit between two steps, or a script writing it in
            # pieces: retried until it parses (audit R4).
            manager.log_event("embed_to_disk", "snapshot busy", ok=False,
                              detail=f"{what} {gen}/{seq}: {exc}")
            return _busy(str(exc))
        except canvas_snapshot.DiskMoved as exc:
            # Claude wrote canvas.toml while this was being applied: nothing
            # of Claude's is replaced; the retry is measured against it.
            manager.log_event("embed_to_disk", "snapshot busy", ok=False,
                              detail=f"{what} {gen}/{seq}: {exc}")
            return _busy(str(exc))
        except OSError as exc:
            # A file write failed (disk full, a vanished folder): a 409 keeps
            # the snapshot queued on the embed, where a 400 would drop it.
            manager.log_event("embed_to_disk", "snapshot busy", ok=False,
                              detail=f"{what} {gen}/{seq}: write failed: {exc}")
            return _busy(f"could not write the snapshot: {exc}")
        except Exception as exc:  # noqa: BLE001 — the reason goes to the chip
            manager.log_event("embed_to_disk", "snapshot rejected", ok=False,
                              detail=f"{what} {gen}/{seq}: {type(exc).__name__}: {exc}")
            return JSONResponse(
                {"ok": False, "gen": gen, "seq": seq, "code": "rejected",
                 "error": f"{type(exc).__name__}: {exc}"}, status_code=400)
        manager.log_event("embed_to_disk", "snapshot applied",
                          ms=(time.time() - started) * 1000,
                          detail=_snapshot_ack_detail(what, gen, seq, ack))
        return JSONResponse(ack)
    finally:
        manager._op_lock.release()


@router.post("/api/canvases/body")
def api_canvases_body(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """One source text from the embedded workbench → the clone, by
    compare-and-swap on content (embed v3 body channel,
    SyncManager.apply_body). The workspace page relays the frame's
    `fused-embed-body {seq, name, file, base_hash, text}` here and posts the
    answer back as `-body-applied` / `-body-rejected`.

    200 `{ok: true, hash}` when written (or already on disk); 200 `{ok:
    false, code: "stale", disk_text, disk_hash}` when disk no longer hashes
    to `base_hash` — the frame rebases and posts again, so this is not an
    error; 200 `{ok: false, code: "fenced"}` while a hard pull's fence
    holds (the frame retries later). 409 `busy` as the snapshot route; 400
    `rejected` for a malformed post (the frame drops it and logs the reason).
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    node, file = body.get("node"), body.get("file")
    base_hash, text = body.get("base_hash"), body.get("text")
    if not isinstance(node, str) or not node or not isinstance(file, str) or not file:
        return _error("'node' and 'file' must be non-empty strings")
    if not isinstance(base_hash, str) or not isinstance(text, str):
        return _error("'base_hash' and 'text' must be strings")
    manager = _sync_manager(env, name, create=False)
    if manager is None:
        return JSONResponse(
            {"error": f"canvas {_sync_key(env, name)!r} is not being synced (no watcher is running)",
             "code": "no_watcher"}, status_code=409)
    started = time.time()
    if not manager._op_lock.acquire(timeout=SNAPSHOT_LOCK_WAIT_S):
        manager.log_event("embed_to_disk", "body busy", ok=False,
                          ms=(time.time() - started) * 1000,
                          detail=f"{file}: the clone stayed locked past {SNAPSHOT_LOCK_WAIT_S:g}s")
        return _busy("a sync operation is in flight for this canvas; try again in a moment")
    try:
        with manager.pause_lock:
            if manager.pause_count > 0:
                return _busy(f"syncing for {_sync_key(env, name)!r} is paused; try again in a moment")
        try:
            ack = manager.apply_body(node, file, base_hash, text)
        except OSError as exc:
            manager.log_event("embed_to_disk", "body busy", ok=False,
                              detail=f"{file}: write failed: {exc}")
            return _busy(f"could not write {file}: {exc}")
        except Exception as exc:  # noqa: BLE001 — the reason goes back to the frame
            manager.log_event("embed_to_disk", "body rejected", ok=False,
                              detail=f"{file}: {type(exc).__name__}: {exc}")
            return JSONResponse(
                {"ok": False, "code": "rejected", "error": f"{type(exc).__name__}: {exc}"},
                status_code=400)
        if ack.get("ok") and ack.get("changed"):
            manager.log_event("embed_to_disk", "body written",
                              ms=(time.time() - started) * 1000,
                              detail=f"{file}: {len(text)} chars"
                                     + (" (new file)" if ack.get("created") else ""))
        return JSONResponse(ack)
    finally:
        manager._op_lock.release()


def _snapshot_ack_detail(what: str, gen: int, seq: int, ack: dict) -> str:
    if ack.get("stale"):
        return f"{what} {gen}/{seq}: superseded by a later seq, not written"
    parts = [f"{what} {gen}/{seq}"]
    if not ack.get("changed"):
        parts.append("no change")
    for key in ("upserted", "deleted", "created"):
        names = ack.get(key) or []
        if names:
            parts.append(f"{key} {', '.join(map(str, names[:5]))}"
                         + (f" (+{len(names) - 5})" if len(names) > 5 else ""))
    if ack.get("renamed"):
        parts.append("renamed")
    return ": ".join(parts[:1]) + (": " + "; ".join(parts[1:]) if len(parts) > 1 else "")


@router.get("/api/canvases/delta")
def api_canvases_delta(name: str = "", env: str = "", full: str = ""):
    """The delta that takes the embed from what it holds to what disk holds
    (embed v3, canvas_snapshot.py's delta section). The workspace page asks
    when the events socket (or its sync/state fallback poll) shows `disk_rev`
    advanced, and with `full=1` when
    the frame says hello after hydrating; it posts the answer to the frame as
    `fused-embed-delta` and relays the ack to /api/canvases/delta/ack."""
    if not _NAME_RE.fullmatch(name or ""):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(env)
    if bad is not None:
        return bad
    manager = _sync_manager(env, name, create=False)
    if manager is None:
        return JSONResponse(
            {"error": f"canvas {_sync_key(env, name)!r} is not being synced (no watcher is running)",
             "code": "no_watcher"}, status_code=409)
    if not manager._op_lock.acquire(timeout=SNAPSHOT_LOCK_WAIT_S):
        manager.log_event("disk_to_embed", "delta busy", ok=False,
                          detail=f"the clone stayed locked past {SNAPSHOT_LOCK_WAIT_S:g}s")
        return _busy("a sync operation is in flight for this canvas; try again in a moment")
    try:
        return JSONResponse(manager.cut_delta(full == "1"))
    finally:
        manager._op_lock.release()


@router.post("/api/canvases/delta/ack")
def api_canvases_delta_ack(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name, rev, ok = body.get("name"), body.get("rev"), body.get("ok", True)
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    if not isinstance(rev, int):
        return _error("'rev' must be an integer")
    manager = _sync_manager(env, name, create=False)
    if manager is None:
        return JSONResponse({"ok": False, "code": "no_watcher"}, status_code=409)
    reason = body.get("reason")
    with manager._op_lock:
        skipped = body.get("skipped")
        return JSONResponse(manager.ack_delta(
            rev, bool(ok), reason=reason if isinstance(reason, str) else None,
            skipped=[n for n in skipped if isinstance(n, str)] if isinstance(skipped, list) else ()))


@router.post("/api/canvases/agent/hook")
def api_canvases_agent_hook(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Claude Code hooks for a session working in a canvas clone
    (claude_agent/canvas_hook.py posts here; agent.py attaches it to every
    session whose target is in a clone). `event` "pre" (PreToolUse) may
    answer `{deny}` — a write recreating a node the user deleted, refused
    once; "post" (PostToolUse) may answer `{context}` — what the user changed
    in the workbench since the session's previous tool call, and what the
    server merged into (or holds for) the folder; "stop" (the turn ended)
    starts `turn_end` — held merge, then publish — on a thread and answers
    `{}` at once (a push outlives the hook's timeout). `{}` otherwise, and
    always `{}` for a path in no synced clone: a hook must never get in the
    way of a session the sync has nothing to say to."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    event = body.get("event")
    session_id = str(body.get("session_id") or "")
    tool = str(body.get("tool_name") or "")
    tool_input = body.get("tool_input") if isinstance(body.get("tool_input"), dict) else {}
    where = tool_input.get("file_path") or tool_input.get("notebook_path") or body.get("cwd")
    found = clone_dir_for(where) if isinstance(where, str) else None
    if found is None and isinstance(body.get("cwd"), str):
        found = clone_dir_for(body["cwd"])
    if found is None:
        return {}
    env, name, _dir = found
    manager = _sync_manager(env, name, create=False)
    if manager is None and event in ("start", "pre", "post", "stop"):
        # No canvas page is open (never opened, or the idle stop ran): a
        # session at work in the clone starts the watcher itself, and
        # `agent_session_id` keeps it running for as long as the session is up.
        manager = _revive_for_session(env, name, f"{event} hook")
    if manager is None:
        return {}
    try:
        if event in ("start", "pre", "post"):
            manager.tool_hook(session_id)
        if event == "pre":
            return manager.hook_pre(session_id, tool, tool_input)
        if event == "post":
            return manager.hook_post(session_id)
        if event == "stop":
            hooks = manager._tool_hooks.get(session_id, 0) if session_id else None
            threading.Thread(target=_turn_end_quietly, args=(manager, session_id, hooks),
                             daemon=True, name=f"fused-canvas-turn-end-{name}").start()
            return {}
    except Exception:  # noqa: BLE001 — a hook answer is advice, never a failure
        logger.warning("canvas %s: agent hook failed", _sync_key(env, name), exc_info=True)
    return {}


def _revive_for_session(env: str, name: str, event: str) -> "_SyncManager | None":
    """Start the watcher of a cloned canvas for a Claude session's hook, as
    sync/start does for a page — or None: not a clone any more, owned by
    another fused-render, being deleted or handed over, quitting. Never an
    error: a hook's answer is advice."""
    if not os.path.isfile(os.path.join(_canvas_dir(env, name), "canvas.toml")):
        return None
    try:
        manager = _sync_manager(env, name, create=True)
    except (_CanvasDeleting, _CanvasHandingOver, _CanvasOwned, OSError):
        return None
    if manager is not None:
        manager.log_event("watcher", "started for an agent session",
                          detail=f"no canvas page open ({event})")
    return manager


def session_turn_start(path: str, backend: str = "") -> bool:
    """A turn of an agent session whose target is `path` is beginning, said
    by the server itself (agent.py `_start`/`_send`) for the backends with no
    hook that could say it: Codex, Gemini and OpenCode have neither Claude's
    CLI hooks nor a host that posts them (Copilot's). Starts the clone's
    watcher when none runs, exactly as the "start" hook does. That is all
    such a session needs: the watcher holds its push while the run is live
    and publishes the dirty folder once it ends, and `agent_session_id`
    keeps it running between turns. True when a watcher runs for the clone
    now. Never raises."""
    try:
        found = clone_dir_for(path)
        if found is None:
            return False
        env, name, _dir = found
        if _sync_manager(env, name, create=False) is not None:
            return True
        return _revive_for_session(env, name, f"{backend or 'agent'} turn start") is not None
    except Exception:  # noqa: BLE001 — a turn must start whatever the sync says
        logger.warning("canvas sync: could not start a watcher for %s", path, exc_info=True)
        return False


def _file_list(files: list[str], cap: int = 12) -> str:
    return ", ".join(files[:cap]) + (f" (+{len(files) - cap} more)" if len(files) > cap else "")


def _retire_idle_manager(manager: "_SyncManager") -> None:
    """The idle stop's second half, off the watcher thread: forget the
    manager (only if it is still the registered one — a reopen may already
    have started its successor) and stop it: threads joined, lease and
    writer lock released."""
    key = (manager.env, manager.name)
    with _SYNC_LOCK:
        if _syncs.get(key) is manager:
            del _syncs[key]
    try:
        manager.stop()
    except Exception:  # noqa: BLE001 — a reopen starts a fresh manager anyway
        logger.warning("canvas %s: idle stop failed", _sync_key(*key), exc_info=True)


def _release_lease_quietly(manager: "_SyncManager") -> None:
    try:
        manager.release_lease(timeout=LEASE_RELEASE_QUIT_S)
    except Exception:  # noqa: BLE001 — the lease lapses on its own
        logger.debug("canvas %s: lease release on quit failed",
                     _sync_key(manager.env, manager.name), exc_info=True)


def _turn_end_quietly(manager: "_SyncManager", session_id: str = "",
                      hooks_at_stop: int | None = None) -> None:
    try:
        manager.turn_end(session_id, hooks_at_stop)
    except Exception:  # noqa: BLE001 — the debounce backstop still publishes
        logger.warning("canvas %s: turn-end publish failed",
                       _sync_key(manager.env, manager.name), exc_info=True)


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
      * it moves the remote behind the watcher's back. The next poll cannot
        tell that from a workbench edit: it flags the canvas server-changed
        (`server_stale`), which holds every push until someone pulls the
        agent's own push back down.

    Running the real `_push` under the real `_flight_lock` keeps the module's
    "pushes are serialized per canvas" invariant true and makes the sync point
    move WITH the push, so the poll that follows has nothing to react to.

    Refuses rather than queues (409) when the watcher is paused: the caller is an
    agent that can read the answer and retry, and a silent no-op would leave it
    believing it had published.

    Returns the status with `ok`, `error_detail` included, because the transcript
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
    # `env` is the environment segment of the clone's path, sent by the CLI
    # interception (_canvas_push.py). It ROUTES the push: the manager for
    # (env, name) pushes with its own FUSED_ENV, so a session in
    # `~/Fused/canvas/unstable/<name>` publishes to unstable even while
    # prod's `<name>` syncs beside it. (It used to be refused as `other_env`
    # when it differed from the one environment the server targeted.)
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    manager = _sync_manager(env, name, create=False)
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
        others = _other_watchers(env, name)
        if others:
            # Not "nobody is watching": ANOTHER fused-render owns this clone
            # (the writer lock). Telling the session to "open this canvas in
            # fused-render" would send it to a start that 409s `owned`.
            owner = others[0]
            return JSONResponse(
                {"error": f"canvas {_sync_key(env, name)!r} is synced by another fused-render "
                          f"(pid {owner['pid']}, port {owner['port']})",
                 "code": "owned", "owner": owner}, status_code=409)
        return JSONResponse(
            {"error": f"canvas {_sync_key(env, name)!r} is not being synced (no watcher is running)",
             "code": "no_watcher"}, status_code=409)
    # The three refusals below are all "someone else has this folder right
    # now", which is BENIGN — nothing is wrong with the canvas or its files.
    # They carry code "busy" so neither the caller nor this manager mistakes
    # them for a failed push: none of them touches push_state, last_error or
    # error_detail, so the clone stays dirty, the watcher's backstop still
    # publishes it, and the Fix-with-Claude button (gated on
    # push_state == "error") never lights up for a race there is nothing to fix.
    # The flight lock, not _op_lock: _push takes _op_lock itself, only for its
    # short freeze and fold phases, and never across the CLI.
    before = manager.push_seq
    with manager._flight_lock:
        # Re-checked under the lock: pause() sets the count and then waits on
        # this same lock, so holding it is what makes the answer stable.
        with manager.pause_lock:
            if manager.pause_count > 0 or manager.stop_event.is_set():
                return _busy(f"syncing for {_sync_key(env, name)!r} is paused; try again in a moment")
        if manager.server_stale is not None:
            # A held server move (a run was live when it landed): merge it now,
            # the same per-file merge the watcher runs, then publish.
            cli = fused_cli()
            if cli is not None and _shim_manifest_command(cli) is not None:
                manager._merge_now()
        if manager.server_stale is not None:
            # The merge could not land (download failed, or the mix failed
            # validation and rolled back): refuse, like a hosted tab's save
            # over a server it could not reconcile. Carries the list.
            changed = ", ".join(
                f"{u['slug']} ({u['kind']})" for u in manager.server_stale.get("udfs", [])
            ) or "the canvas layout"
            # Promise "kept" only where pull() merges: a manifest shim. The
            # external-CLI leg pulls wholesale (see pull()), and a session told
            # its edits survive would pull and lose them — so name the trash
            # copy instead.
            cli = fused_cli()
            trash = os.path.join(manager.root, ".sync", "trash", name)
            if cli is None or _shim_manifest_command(cli) is None:
                kept = ("this fused CLI cannot merge, so the pull REPLACES your local "
                        f"edits with the server's files; a copy is kept under {trash}")
            elif manager._base_files is None:
                kept = ("there is no sync point, so every file you changed is kept "
                        "as it is and only what the server added comes in")
            else:
                kept = "your edits are merged with them and kept"
            # A Pull that was pressed and failed is the one thing a session
            # told "pull first" cannot see from here: the refusal reads the
            # same before and after it. Say so, verbatim, or the session
            # reports a landed pull and a "stale guard" (the HLS_Tiff loop).
            pull_error = manager._last_pull_error
            failed = (f" The last Pull did NOT land: {pull_error}" if pull_error else "")
            return JSONResponse(
                {"error": "the canvas changed on the server since the last sync "
                          f"({changed}); pull first — the Pull button in the canvas view "
                          f"brings those changes into this folder ({kept}), "
                          "then push again." + failed,
                 "code": "server_changed", "server_stale": manager.server_stale,
                 "pull_error": pull_error},
                status_code=409)
        if manager.push_seq == before or manager._dirty_since is not None \
                or manager._take_fingerprint() != manager._fingerprint:
            before = manager.push_seq
            if manager._push() == "busy":
                return _busy(
                    "a sync operation is in flight for this canvas; try again in a moment")
    status = manager.status()
    # "The counter moved and no error" is success: the push LANDED. Not
    # `push_state == "idle"` — a write during the CLI run (not in the frozen
    # copy) leaves the fold at "pending" for a follow-up push although this
    # one published; and an abort (remote moved, could not be reconciled)
    # never moves the counter, so it still does not read as published.
    status["ok"] = manager.push_seq > before and manager.last_error is None
    return status


def _fix_prompt(name: str, detail: list[str], error: str | None) -> str:
    """The first message of a fix session: the verbatim CLI output (never
    reworded — rewording makes an error unsearchable, D328) plus what the
    session must do.

    Since the revision spine (2026-11-11, Q7) the session has no sync verbs:
    the folder publishes itself when its turn ends (the Stop hook), so the
    session's job is the files and `validate` — it is told not to push or
    pull (both are refused in the clone anyway)."""
    report = "\n".join(detail) or (error or "the push failed")
    return (
        f"The automatic canvas publish for {name!r} (this folder) is failing. "
        f"The CLI reported:\n\n"
        f"{report}\n\n"
        "Fix these problems in this folder's files. Check your work with "
        "`fused workbench canvas validate .` until it passes. This folder "
        "publishes itself when your turn ends, so do not run `fused workbench "
        "canvas push` or `pull` here (both are refused). Do not change the "
        "canvas name."
    )


@router.post("/api/canvases/sync/pull")
def api_canvases_sync_pull(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Pull the server's canvas into the clone NOW — the canvas page's
    "Server changes · Pull" (the hosted workbench's pull, for this tab).
    Clean clone: force pull. Dirty: the three-way merge, canvas.toml per
    node, local edits kept; the held push then ships the merged folder —
    except with no shim, where the pull is wholesale and the trash holds the
    overwritten edits (see _SyncManager.pull). Returns the
    status with `ok`, false (plus `pull_error`) when the pull did not land."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    # `hard`: the sync drawer's Hard pull — discard the clone's local files
    # and take the server's copy wholesale (trash snapshot first, no merge).
    # Unlike a plain pull it is NOT deferred for a live Claude session: it is
    # the recovery door, and the page makes the user type "pull" first.
    hard = body.get("hard") is True
    manager = _sync_manager(env, name, create=False)
    if manager is None:
        return JSONResponse(
            {"error": f"canvas {_sync_key(env, name)!r} is not being synced (no watcher is running)",
             "code": "no_watcher"}, status_code=409)
    if manager.push_state == "pushing":
        return _busy("a push is running for this canvas; try again in a moment")
    if not manager._flight_lock.acquire(timeout=MANUAL_PUSH_LOCK_WAIT_S):
        return _busy(
            "a sync operation is in flight for this canvas; try again in a moment")
    try:
        with manager.pause_lock:
            if manager.pause_count > 0:
                return _busy(f"syncing for {_sync_key(env, name)!r} is paused; try again in a moment")
        result = manager.pull(hard=hard)
    finally:
        manager._flight_lock.release()
    if result.get("code") == "busy":
        return _busy(result.get("error") or "a sync operation is in flight for this canvas")
    status = manager.status()
    status["ok"] = bool(result.get("ok"))
    # The snapshot folder the pull copied the clone into before writing
    # (None when nothing was snapshotted): the page shows it after a Hard pull.
    status["trash"] = result.get("trash")
    if not result.get("ok"):
        # Its own field: `error` is the PUSH's last_error, and the page keeps
        # this answer as its sync state, so a pull failure written there
        # would wear the push-failed banner until the next poll. An
        # `agent_live` deferral arrives here too: the page shows its text.
        status["pull_error"] = result.get("error")
        if result.get("code"):
            status["code"] = result["code"]
    return status


@router.post("/api/canvases/sync/check")
def api_canvases_sync_check(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Probe the server for canvas changes NOW rather than on the next timed
    poll — the canvas page's window/tab just came into focus. Detect-only:
    the result is the ordinary `server_to_disk` leg on sync/state and the
    events socket (stale → the Pull button). Answers ok even with no watcher
    (nothing to hurry; sync/start is the page's own concern)."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    manager = _sync_manager(env, name, create=False)
    if manager is None:
        return {"ok": True, "watching": False}
    manager.check_remote_now()
    return {"ok": True, "watching": True}


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
    env, bad = _require_env(body.get("env"))
    if bad is not None:
        return bad
    manager = _sync_manager(env, name, create=False)
    if manager is None or manager.push_state != "error":
        # Nothing to fix: the button only renders on an error, so reaching
        # this means the push recovered between the click and the POST.
        return _error("this canvas has no failing push to fix", 409)
    with manager.fix_lock:
        if manager.active_fix_run_id is not None:
            return _error("a fix is already running for this canvas", 409)
        prompt = _fix_prompt(name, manager.error_detail, manager.last_error)
        try:
            # CLAUDE, NAMED: a canvas fix is a Claude feature (like the app
            # builder), so it never follows the Preferences default agent.
            res = claude_spawn.spawn_helper(manager.dir, prompt, "auto",
                                            backend="claude",
                                            isolate_mcp=True)
        except Exception as exc:  # noqa: BLE001 — spawn failure is the answer
            return _error(f"failed to start Claude session: {exc}", 500)
        if res.get("reason") == "backend_disabled":
            # Claude switched off in Preferences (D17): the refusal whole, as
            # a 400 the page can act on — not a "failed to start" 500.
            return JSONResponse(res, status_code=400)
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


# -- sync observability: state, log, events ------------------------------------

# The events socket's heartbeat, and how often it re-sends the state while a
# leg is not idle (so the page's ages tick and an over-budget leg turns amber
# without waiting for the next change).
SYNC_EVENTS_PING_S = 15.0
SYNC_EVENTS_TICK_S = 1.0


def _state_for(env: str, name: str, files: bool = False) -> dict:
    manager = _sync_manager(env, name, create=False)
    return _unwatched_state(env, name) if manager is None else manager.sync_state(files=files)


@router.get("/api/canvases/sync/state")
def api_canvases_sync_state(name: str = "", env: str = "", files: str = ""):
    """Every leg of the two sync loops for one canvas (the build contract's
    shape). A canvas this process is not syncing answers all-idle with
    `watching: false` — never a 409: the page polls this as the socket's
    fallback, before and after a watcher exists. `files=1` adds
    `pending_files` (the CLI interception's state line)."""
    if not _NAME_RE.fullmatch(name or ""):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(env)
    if bad is not None:
        return bad
    return _state_for(env, name, files=files in ("1", "true"))


@router.get("/api/canvases/sync/log")
def api_canvases_sync_log(name: str = "", env: str = "", after: int = 0):
    """The canvas's sync event ring (200 newest), events with seq > `after`.
    `next` is the `after` for the following call. Seqs restart with a new
    watcher (the state's `watcher_epoch`); an `after` past this ring's end is
    read as 0."""
    if not _NAME_RE.fullmatch(name or ""):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(env)
    if bad is not None:
        return bad
    manager = _sync_manager(env, name, create=False)
    if manager is None:
        return {"events": [], "next": 0}
    return manager._log.since(after)


def _legs_busy(state: dict) -> bool:
    return any(leg.get("state") != "idle" for leg in state.get("legs", {}).values())


def _socket_read(env: str, name: str, epoch,
                 log_after: int) -> tuple[dict, list[dict], object, int]:
    """One socket wake's reads, in a worker thread: `_sync_manager` takes the
    threading `_SYNC_LOCK` and the state reads the lock file — neither
    belongs on the event loop. Returns (state, new log events, epoch,
    log_after). A new watcher (or none) restarts the log cursor at its
    ring's current end: only events from here on are "new" (the drawer reads
    history off sync/log)."""
    manager = _sync_manager(env, name, create=False)
    state = _unwatched_state(env, name) if manager is None else manager.sync_state()
    if state.get("watcher_epoch") != epoch:
        return state, [], state.get("watcher_epoch"), \
            (manager._log.last_seq if manager is not None else 0)
    if manager is None:
        return state, [], epoch, log_after
    page = manager._log.since(log_after)
    return state, page["events"], epoch, page["next"]


@router.websocket("/api/canvases/sync/events")
async def api_canvases_sync_events(ws: WebSocket):
    """Push feed of `sync/state` for one canvas: `{"type": "state", "state"}`
    on every change (disk_rev, push/pull state, stale detect, a log event) and
    once a second while any leg is busy; `{"type": "log", "event"}` per new
    ring event; `{"type": "ping"}` every SYNC_EVENTS_PING_S while idle.

    Wakes come from canvas_sync_state's per-canvas notifier (keyed
    `_sync_key(env, name)`) (the sync threads
    call `loop.call_soon_threadsafe`), so a stop/start that replaces the
    manager is followed without re-subscribing, and every read runs in a
    worker thread — the event loop never waits on the clone or a threading
    lock. The wake event is cleared only right before a read, never after a
    timed-out wait, so a change that lands while a ping is being sent is not
    lost. Origin-checked like every other socket (no CORS preflight on a
    WebSocket)."""
    if not canvas_sync_state.ws_origin_ok(ws):
        await ws.close(code=1008)
        return
    name = ws.query_params.get("name") or ""
    if not _NAME_RE.fullmatch(name):
        await ws.close(code=1008)
        return
    # A bad env and a pinned-away one both close 1008: a socket has no body
    # to carry the 400/423 reason, and the page's sync/state fallback poll
    # gets the same answer with its reason.
    env, bad = _require_env(ws.query_params.get("env"))
    if bad is not None:
        await ws.close(code=1008)
        return
    key = _sync_key(env, name)
    await ws.accept()
    token = canvas_sync_state.subscribe(key)
    wake = token[1]
    closed = asyncio.Event()

    async def drain() -> None:
        # Read only to learn of the disconnect; the page sends nothing.
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            closed.set()
            wake.set()

    drainer = asyncio.create_task(drain())
    loop = asyncio.get_running_loop()
    try:
        epoch: object = object()
        log_after = 0
        while not closed.is_set():
            wake.clear()
            state, events, epoch, log_after = await asyncio.to_thread(
                _socket_read, env, name, epoch, log_after)
            if closed.is_set():
                break
            await ws.send_text(json.dumps({"type": "state", "state": state}))
            for event in events:
                await ws.send_text(json.dumps({"type": "log", "event": event}))
            busy = _legs_busy(state)
            ping_at = loop.time() + SYNC_EVENTS_PING_S
            woke = False
            while not closed.is_set():
                timeout = SYNC_EVENTS_TICK_S if busy else max(0.0, ping_at - loop.time())
                try:
                    await asyncio.wait_for(wake.wait(), timeout=timeout)
                    woke = True
                    break
                except asyncio.TimeoutError:
                    if busy:
                        break  # re-send so the ages tick
                    await ws.send_text(json.dumps({"type": "ping"}))
                    ping_at = loop.time() + SYNC_EVENTS_PING_S
            if woke and not closed.is_set():
                # Coalesce a burst (a snapshot write bumps rev, then logs,
                # then the fs thread notices) into one state message.
                await asyncio.sleep(0.02)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        canvas_sync_state.unsubscribe(key, token)
        drainer.cancel()
