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
server-vs-server). If the remote moved since the last sync point the watcher
does what a hosted workbench tab does when it notices the server changed: it
STOPS WRITING THE SERVER (`server_stale` is set, the auto-push is held, the
manual push refuses with the list of what changed) and the canvas page shows
"Server changes · Pull". Nothing is pulled or merged on its own. Local edits
— embed snapshots, Claude's files — keep landing on disk meanwhile.

PULL (`_SyncManager.pull`, the page's button / POST sync/pull) brings the
remote in: clean clone → CLI `pull --force` (clone snapshot to trash first);
dirty clone → MERGE: download the zip (_fused_canvas_zip.py shim) and apply
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
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
import zipfile

from fastapi import APIRouter, Body, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from fused_render import canvas_snapshot, canvas_sync_state
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
WHOAMI_TIMEOUT = 20.0
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
LOGIN_CHILD_TIMEOUT = 600.0
# Quiet period after the last observed file change before a push fires.
DEBOUNCE_S = 1.5
# Folder fingerprint poll cadence.
SCAN_INTERVAL_S = 1.0
# Remote-change poll cadence: the manifest probe (a network call, so much
# slower than the local fingerprint walk), clean or dirty; with an external CLI
# the legacy `pull --dry-run` probe instead, clean clones only. Detects only.
PULL_POLL_S = 10.0
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

# How long the manual push/pull endpoints wait for the manager's _flight_lock
# (and a push or pull for each short _op_lock phase) before giving up with a
# 409. Short on purpose: the caller is a Claude session that just asked to
# publish, and "a sync operation is in flight, try again" is a better answer
# than a request that hangs for the whole PUSH_TIMEOUT.
MANUAL_PUSH_LOCK_WAIT_S = 5.0
# How long /api/canvases/snapshot waits on _op_lock before answering 409.
SNAPSHOT_LOCK_WAIT_S = 1.0
# stop()'s bound on joining the watcher thread. A push still inside its CLI
# can hold that thread for up to PUSH_TIMEOUT; the quit rung terminates the
# CLI first (terminate_push), and no caller should hang behind a network call.
STOP_JOIN_S = 5.0
# SIGTERM → SIGKILL grace for a push CLI the quit rung stops.
PUSH_TERMINATE_GRACE_S = 2.0
# How long a file the embed created WITHOUT its node (a snapshot's
# `new_files` slug missing from `nodes`: the stale-closure race) waits for the
# workbench's own row before the host invents one. Synthesizing at once put a
# made-up position into the frame by delta and the new node jumped. Within the
# grace the file is left out of deltas and the push is held; past it the row
# is synthesized as for any orphan.
ROWLESS_GRACE_S = 5.0

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
    the custom environment to follow. Without one a custom run is just prod
    under another clone folder, so the pickers offer it disabled."""
    return any(os.path.isfile(os.path.expanduser(raw)) for raw in _FUSED_SETTINGS_FILES)


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


def _apply_embed_proxy() -> None:
    """Start, retarget or stop the proxy to match the preference and the
    settings file's current origin. Idempotent; called from status."""
    from fused_render.canvas_embed_proxy import controller

    if _embed_proxy_wanted():
        controller.apply(workbench_base_url(CUSTOM_WORKBENCH_ENV), _custom_token)
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


def _cli_cache_clear() -> None:
    global _cli_cache_gen
    with _CLI_CACHE_LOCK:
        _cli_cache.clear()
        _cli_cache_gen += 1


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
        # Preferences row. `available` is false only for custom without a
        # settings file: the pickers disable it.
        "envs": {
            env: {
                "base_url": workbench_base_url(env),
                # Where the workspace's iframe loads from and posts to: the
                # workbench origin itself, or — custom env, embed proxy on —
                # the loopback proxy standing in front of it.
                "embed_url": embed_base_url(env),
                "canvases_dir": canvases_root(env),
                "cloned_count": len(_cloned_names(env)),
                "available": env != CUSTOM_WORKBENCH_ENV or fused_settings_present(),
            }
            for env in WORKBENCH_ENV_CHOICES
        },
    }


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
            if cand == base or not cand.startswith(base + os.sep):
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

    result = _cached_cli("whoami", env, compute)
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
fused-render: a watcher pushes every quiet change set upstream, and edits made
in the hosted workbench come down when the user pulls, merged per file.

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

## How the sync works (know this before running sync commands yourself)

A watcher in the fused-render app syncs this folder continuously:

- **Auto-push**: after ~1.5s of file quiet it pushes this folder, which
  REPLACES the remote UDF set (deletes propagate). Before pushing it probes
  the remote and merges concurrent workbench edits in, per file. It is
  HELD while you are working — see "Publishing your work" below.
- **Server changes**: every ~10s it checks the remote. If someone edited
  the canvas in the hosted workbench meanwhile, the sync STOPS pushing (like
  a workbench tab that notices the server changed) and the canvas view shows
  "Server changes · Pull". A push you request is refused with the list of
  what changed until the user pulls. The pull merges: a file only you changed
  keeps your version, only they changed gets theirs, and canvas.toml merges
  per node so their new nodes arrive next to your moves.

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
  .fusedignore (the watcher re-seeds them on its own pulls). Do not use it to
  answer "server changes": the Pull in the canvas view merges instead.
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
- **Folders** are `[[canvas.nodes]]` rows with `type = "udf-folder"`. Give a
  folder row `udfName` equal to its `folderName`. Membership lives ONLY in
  the folder's `childUdfOrder` (the child rows' `udfName`s; one folder per
  child, never a folder inside a folder). A child's `x`/`y` are RELATIVE to
  its folder's `x`/`y`, so moving a node into or out of a folder means
  converting its coordinates too. Deleting or renaming a node means
  removing or renaming it in its folder's `childUdfOrder` as well: a stale
  name there is tolerated, but it is a bug to fix. A node in a folder is
  still a root-level file in this folder, never a subdirectory.

## Files can change between change sets — trust the filesystem, not your memory

The user may be editing this same canvas in the hosted workbench while you
work. Their edits do NOT land here on their own: the sync only notices them
("Server changes") and waits for the user to pull. So this folder is stable
while you work and can change when they pull. Consequences:

- Re-read a file (Read tool) before editing it if you have pushed since you
  last looked, or if you are not sure. Your memory can be stale across a
  push, and an Edit whose old text no longer matches means the file changed
  underneath: re-read and re-apply, don't force it.
- Never reconstruct or rewrite a whole file from memory (Write over it) —
  that silently discards remote edits a pull merged in. Prefer targeted
  Edits against freshly read content.
- The sync's rules on concurrent changes: a file only you touched keeps
  your version; a file only the workbench touched gets theirs; both →
  yours wins. Because their edits arrive only when the user pulls, a
  same-file collision surfaces then rather than mid-edit; `canvas.toml`
  merges per node, but where you both changed the same node your version
  wins — mention it if you notice.
- A file that unexpectedly disappeared or reverted was likely changed
  remotely. Before recreating it, check the state on disk and say what you
  found; overwritten/deleted versions are recoverable from
  `../.sync/trash/<canvas>/<timestamp>/` (newest last).
- Group related multi-file changes (e.g. a rename: the `.py` file AND its
  `canvas.toml` entry) into one change set and push it whole — a half-done
  rename that gets pushed or merged mid-way is exactly how invalid states
  happen.

"""


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


def _list_entries(env: str):
    """`_list_entries_uncached` behind the CLI result cache (see `_cached_cli`):
    `(entries, error)`, errors never cached."""

    def compute():
        entries, err = _list_entries_uncached(env)
        if err is not None:
            return _err_of(err)
        return ("ok", entries)

    result = _cached_cli("list", env, compute)
    if result[0] == "err":
        return None, _error(result[1], result[2])
    return result[1], None


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
                     "updated_at": None}
                )
            elif isinstance(entry, dict):
                name = entry.get("name") or entry.get("slug")
                if isinstance(name, str) and name:
                    entries.append(
                        {"name": name, "id": entry.get("id"), "preview_url": None,
                         "preview_pending": False, "n_code_udfs": None,
                         "updated_at": None}
                    )
    return entries, None


@router.get("/api/canvases/list")
def api_canvases_list(env: str = "", x_fused: str | None = Header(default=None)):
    # EXECUTES a control-plane call — guarded like whoami. `env` picks the
    # account listed AND the folder `cloned` is judged against: a prod clone
    # of `alpha` says nothing about unstable's `alpha`.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    env, bad = _require_env(env)
    if bad is not None:
        return bad
    if not _logged_in(env):
        return _error("not signed in to Fused — sign in first", 409)
    canvases, err = _list_entries(env)
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
    return {"canvases": canvases}


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
    return {"ok": True, "name": name}


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
        self._log = canvas_sync_state.SyncLog(_sync_key(env, name), self._bump)
        self._push_state = "idle"
        # SERVER_STALE: the remote moved since the sync point and nothing has
        # been pulled yet — {since, udfs: [{slug, kind}], layout}. While set
        # the auto-push is held and the manual push refuses; cleared by pull().
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
        # ROWLESS: slug → when an embed snapshot created its file with no node
        # (ROWLESS_GRACE_S). Written under _op_lock by apply_snapshot, expired
        # by the watcher tick: its own small lock.
        self._rowless: dict[str, float] = {}
        self._rowless_lock = threading.Lock()
        self._rowless_hold_logged: frozenset = frozenset()
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
        # The compact rows of canvas.toml at the sync point (merge_toml's base).
        self._base_toml: dict | None = None
        self.merge_rollback_seq = 0
        # Cached answer to "is a Claude session live in this clone?" — see
        # AGENT_LIVE_CACHE_S. Written by whichever of the watcher thread and a
        # status request asks first; a lost race just means one extra scan, so
        # the plain-attribute rule the class docstring states still holds.
        self._agent_run_id = ""
        self._agent_checked_at = 0.0
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
        self._embed_applied: dict[tuple[int, int], dict] = {}
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

    def stop(self) -> None:
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
        base_toml = data.get("base_toml")
        if isinstance(base_toml, dict) and isinstance(base_toml.get("nodes"), dict):
            self._base_toml = base_toml

    def _save_base(self) -> None:
        payload = json.dumps(
            {
                "files": self._base_files,
                "remote": self._remote,
                "base_toml": self._base_toml,
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
        self.server_stale = None
        self._last_pull_error = None
        # New ids reach the frame through the delta leg, which only wakes on
        # a disk_rev move: count a manifest rotation as one.
        self._note_disk()

    def _new_trash_dir(self) -> str | None:
        """A fresh timestamped folder under .sync/trash/<name>/ for the files
        the current pull/merge is about to overwrite or delete; prunes the
        oldest snapshots beyond _TRASH_MAX. None if it can't be created."""
        root = os.path.join(self.root, ".sync", "trash", self.name)
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
                env=_cli_env(cli, self.env),
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
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
        """Did the server's canvas change between two manifests? The
        collection stamp (layout/dashboard saves) and each UDF's hash and
        stamp — NOT its `id`. The id is identity, not an edit: it is absent
        from a baseline saved by a build before the shim emitted ids and
        from a UDF the push has only just created, and read as a move it
        would hold every push behind a "server changed" chip that lists
        nothing (_note_server_stale compares hashes and finds no change)."""
        if prev.get("last_updated") != cur.get("last_updated"):
            return True

        def edits(udfs) -> dict:
            if not isinstance(udfs, dict):
                return {}
            return {
                slug: ((info.get("hash"), info.get("last_updated"))
                       if isinstance(info, dict) else info)
                for slug, info in udfs.items()
            }

        return edits(prev.get("udfs")) != edits(cur.get("udfs"))

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
                bundle[os.path.join(*rel.split("/"))] = zf.read(info.filename)
        return bundle

    def _merge_remote(self, probe: dict, bundle: dict[str, bytes] | None) -> bool:
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
        download) — the caller must NOT push over an unreconciled remote."""
        base = self._base_files
        if base is None:
            self._rotate_remote(probe)
            self._save_base()
            return True
        if bundle is None:
            return False  # transient — retried with the same probe
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
            self.log_event("server_to_disk", "pull rollback", ok=False,
                           detail="merged clone failed `fused canvas validate`; restored "
                                  + ", ".join(r for r, _b, _p in rollback[:5]))
            rollback = []
            rolled_back = True
        else:
            rolled_back = False
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

    def _poll_remote(self, probe: dict) -> None:
        """Shim-backed poll leg: adopt a baseline on the first look; after
        that only DETECT a remote move (`server_stale`). Read-only."""
        if self._remote is None:
            # First look (fresh manager, or right after clone/push dropped
            # it): adopt as baseline. A remote edit made before this first
            # look is indistinguishable from the sync point — same blind
            # spot the pre-merge sync had; converges on the next remote save.
            self._remote = probe
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
            return
        if not self._remote_moved(self._remote, probe):
            self.server_stale = None
            return
        # The remote moved: do what a hosted tab does — stop writing the
        # server and say so. Nothing is pulled or merged here; pull() is the
        # user's (or the session's) explicit act.
        self._note_server_stale(probe)

    def _force_pull(self, probe: dict, cli) -> str | None:
        """pull()'s shim-backed wholesale branch (clean clone, or dirty with no
        merge base), after it has committed to writing: CLI force pull,
        post-pull divergence recheck, re-baseline.

        Returns None when the pull LANDED, else the user-facing reason it did
        not. pull() must not report success on a pull that never ran: the
        remote baseline only rotates here, so a failed `--force` leaves
        `server_stale` set (the push stays held, correctly) and the Pull
        button has to say why instead of answering ok with the banner still up.
        Under _op_lock for its whole length: the CLI writes the clone itself.
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
        # refuses a wholesale pull while one is live.)
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

    def _freeze_copy(self) -> str:
        """Copy the clone to `.sync/push/<name>/` (fresh each push) for the
        CLI to read while _op_lock is free. Dot-DIRS are left out (the
        fingerprint and every sync walk skip them too); dot-files — the
        clone's `.fusedignore` above all, which the CLI reads from the folder
        it pushes — are copied. Under _op_lock, so no snapshot write lands
        half-way through the copy. Raises OSError/shutil.Error on failure."""
        import fnmatch

        dest = self._push_copy_dir()
        _rmtree_quiet(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # What the clone's .fusedignore lists is never pushed (the CLI skips
        # it), so it is not worth copying either — but only where this can be
        # SURE: the copy is what gets published, and a file wrongly left out
        # here is deleted server-side, while one wrongly copied is still
        # dropped by the CLI's own (full gitignore) filter. So a deliberately
        # small subset: a pattern without a "/" matches a FILE by basename at
        # the clone root only and a DIRECTORY by name at any depth (a trailing
        # "/" = directories only), last match wins with `!` re-including. Any
        # rule with a "/" in it (`/x.py`, `src/*.py`, where fnmatch's `*`
        # would cross "/") turns the subset off: everything is copied and the
        # CLI decides. `.fusedignore` itself always travels (the CLI reads it).
        rules: list[tuple[str, bool, bool]] = []
        try:
            with open(os.path.join(self.dir, ".fusedignore"), encoding="utf-8",
                      errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    negated = line.startswith("!")
                    line = line[1:] if negated else line
                    dir_only = line.endswith("/")
                    line = line.rstrip("/")
                    if not line:
                        continue
                    if "/" in line:
                        rules = []
                        break
                    rules.append((line, negated, dir_only))
        except OSError:
            rules = []

        def ignored(name: str, is_dir: bool, at_root: bool) -> bool:
            if not is_dir and not at_root:
                return False
            out = False
            for pattern, negated, dir_only in rules:
                if dir_only and not is_dir:
                    continue
                if fnmatch.fnmatchcase(name, pattern):
                    out = not negated
            return out

        root = os.path.normpath(self.dir)

        def ignore(folder: str, names: list[str]) -> list[str]:
            out = []
            at_root = os.path.normpath(folder) == root
            for n in names:
                is_dir = os.path.isdir(os.path.join(folder, n))
                if n.startswith(".") and is_dir:
                    out.append(n)
                    continue
                if n == ".fusedignore" or not rules:
                    continue
                if ignored(n, is_dir, at_root):
                    out.append(n)
            return out

        shutil.copytree(self.dir, dest, ignore=ignore, ignore_dangling_symlinks=True)
        return dest

    def _push_argv(self, cli, copy_dir: str) -> list[str]:
        return [*cli.command, "workbench", "canvas", "push", copy_dir, "--canvas", self.name]

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
                       detail="fused-render is quitting")
        return True

    def _push(self, *, lock_wait: float | None = None) -> str:
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
        could not be had within `lock_wait` (default MANUAL_PUSH_LOCK_WAIT_S)
        — nothing changed then, the clone stays as dirty as it was."""
        wait = MANUAL_PUSH_LOCK_WAIT_S if lock_wait is None else lock_wait
        # A file the embed created without its node (ROWLESS_GRACE_S): its row
        # is either on its way from the workbench or about to be synthesized.
        # The CLI only WARNS on a file with no row, so pushing now would
        # publish the UDF node-less (invariant 2). Hold — nothing changes, the
        # clone stays dirty, the next tick (or the session's retry) pushes.
        rowless = self._rowless_in_grace()
        if rowless:
            key = frozenset(rowless)
            if key != self._rowless_hold_logged:
                self._rowless_hold_logged = key
                self.log_event("disk_to_server", "push held", ok=False,
                               detail=f"waiting for the workbench's row for "
                                      f"{', '.join(sorted(rowless)[:5])}")
            return "busy"
        self._rowless_hold_logged = frozenset()
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

        # Guard the wholesale replace: if the remote moved since the last
        # sync point, fold its changes in first (per-file, local wins ties)
        # so the push can't clobber a concurrent workbench edit.
        if self._remote is not None:
            probe = self._probe_remote()
            if (
                probe is not None
                and self._remote_moved(self._remote, probe)
            ):
                bundle = self._fetch_bundle(probe) if self._base_files is not None else None
                if not self._op_lock.acquire(timeout=wait):
                    return deferred("another sync step held the clone")
                try:
                    merged = self._merge_remote(probe, bundle)
                finally:
                    self._op_lock.release()
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
                    # status endpoint's caller (and _fix_prompt, and the CLI
                    # interception's error_detail passthrough) would send the
                    # session to fix a problem this attempt never even
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
        # Freeze. Baseline BEFORE the push: a save landing while the push runs
        # must re-arm the debounce, not vanish into the pushed snapshot. The
        # file hashes are taken at the same instant — they describe the
        # snapshot this push publishes, which becomes the merge base on success.
        if not self._op_lock.acquire(timeout=wait):
            return deferred("another sync step held the clone")
        try:
            # A rowless file that arrived while the probe ran: hold, as above.
            if self._rowless_peek():
                return deferred("a new node's row is still on its way from the workbench")
            # Rows for orphan source files FIRST, so the fingerprint, hashes
            # and copy below all include them.
            self._synthesize_orphans()
            frozen = self._take_fingerprint()
            self._fingerprint = frozen
            self._dirty_since = None
            base_snapshot = self._take_file_hashes()
            base_toml_snapshot = self._toml_rows()
            try:
                copy_dir = self._freeze_copy()
            except (OSError, shutil.Error) as exc:
                copy_dir = None
                copy_error = str(exc)
        finally:
            self._op_lock.release()
        if copy_dir is None:
            # A file vanished mid-copy (Claude deleting while we froze) or the
            # disk is full: nothing was pushed — retry after the debounce.
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
        # A push changes the account's canvas list (updated_at): drop cached
        # list answers. Module-level, called with no lock held.
        _cli_cache_clear()
        with self._op_lock:
            self.push_seq += 1
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
            if self._paused():
                continue
            # Keep the live-run cache warm from the WATCHER thread, on every
            # tick, clean clone or not: sync/state reads the cached answer
            # (the pending push's reason) and never scans itself. A no-op read
            # of the cache except once every AGENT_LIVE_CACHE_S (3 s), so it
            # does not add a scan per tick.
            self.agent_run_id()
            # Expire rowless files past their grace: the forced disk_rev bump
            # makes the page cut again, and that cut synthesizes their rows.
            self._rowless_in_grace()
            gen = self._fp_gen
            current = self._take_fingerprint()
            self._note_disk(current)
            if self._fp_gen != gen:
                # A pull or push (request thread) re-baselined while this walk
                # ran: the reading predates the new baseline, and comparing
                # them would mark the clone dirty with the pull's own writes.
                continue
            if current != self._fingerprint:
                self._fingerprint = current
                self._dirty_since = time.time()
                # "pushing" spans a whole push (a manual one runs on a request
                # thread while this loop keeps scanning): its fold decides
                # what comes next, from the same fingerprint comparison.
                if self.push_state not in ("error", "pushing"):
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
                # And HELD while the remote moved under us (`server_stale`):
                # a hosted tab does not save over a server it has not looked
                # at, and neither does this one. Pull() clears it.
                if not self.agent_run_id() and self.server_stale is None:
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
                    continue
            if time.time() - self._last_pull_poll >= PULL_POLL_S:
                # The downstream leg only DETECTS now (server_stale); it writes
                # nothing, so it runs through a live Claude session too — the
                # old hold existed because this leg pulled files from under a
                # session mid-change-set. pull() is explicit and its own act.
                # Skipped (retried next tick) while a push or pull holds the
                # flight lock: a push moving the remote would read as a
                # server change, and the push re-baselines the remote anyway.
                if not self._flight_lock.acquire(blocking=False):
                    continue
                try:
                    if self._paused():
                        continue
                    self._last_pull_poll = time.time()
                    cli = fused_cli()
                    if cli is not None and _shim_manifest_command(cli) is not None:
                        # Manifest probe: runs clean OR dirty, read-only, and a
                        # network call — so outside _op_lock; only adopting
                        # the answer takes it.
                        probe = self._probe_remote()
                        if probe is not None:
                            with self._op_lock:
                                self._poll_remote(probe)
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

    def _toml_rows(self) -> dict | None:
        """The compact rows of the clone's canvas.toml, or None."""
        try:
            return canvas_snapshot.rows_of_doc(
                canvas_snapshot._load_toml(os.path.join(self.dir, "canvas.toml")))
        except Exception:  # noqa: BLE001 — no toml, or unparsable: no base rows
            return None

    def _set_sync_point(self) -> None:
        """Disk IS the sync point: file hashes and canvas.toml rows alike."""
        self._base_files = self._take_file_hashes()
        self._base_toml = self._toml_rows()

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

    def _note_server_stale(self, probe: dict | None) -> None:
        """The remote moved since the sync point: record what, keep `since`
        from the first sighting. `probe` None is the legacy leg (no detail)."""
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
        prev = self.server_stale
        since = (prev or {}).get("since") or time.time()
        self.server_stale = {"since": since, "udfs": udfs, "layout": layout}
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

        Clean clone → CLI `pull --force` (trash snapshot first). Dirty clone →
        the three-way merge (per file; canvas.toml per node); the merged
        folder stays dirty so the held push ships it. No shims (external CLI)
        → wholesale `pull --force` either way, trash snapshot first when
        dirty. Clears `server_stale` on success.

        Only the merge KEEPS local edits. The two wholesale legs — external
        CLI, and a dirty shim clone with no sync point to merge against —
        replace them with the server's files; the trash snapshot is then the
        only copy. That is deliberate rather than a refusal: on those legs
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
        self.log_event("server_to_disk", "hard pull start" if hard else "pull start")
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
            if hard and self.push_state == "error":
                # The files that push failed on are gone: the clone is the
                # server's copy now (or a fresh local edit over it, which the
                # debounced push ships). The red banner would otherwise stay
                # up over content it is no longer about.
                self.last_error = None
                self.push_state = "pending" if self._dirty_since is not None else "idle"
            self.log_event("server_to_disk", "hard pull end" if hard else "pull end", ms=ms,
                           detail=f"pull_seq {self.pull_seq}")
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
        # A wholesale CLI pull rewrites the folder outside anything a Claude
        # session's lock-free writes can coordinate with: a file it writes
        # while `--force` runs can be overwritten before any check sees it.
        # So a wholesale pull waits for the session (the per-file merge,
        # which reads each file under _op_lock right before deciding, does not).
        agent_live = {"ok": False, "code": "agent_live",
                      "error": "a Claude session is editing this folder; pull when it finishes"}
        dirty = self._dirty_since is not None or self._take_fingerprint() != self._fingerprint
        error: str | None = None
        if _shim_manifest_command(cli) is not None:
            probe = self._probe_remote()
            if probe is None:
                return {"ok": False, "error": "could not read the canvas from the server"}
            if dirty and self._base_files is not None and not hard:
                bundle = self._fetch_bundle(probe)
                if bundle is None:
                    return {"ok": False,
                            "error": "could not download the canvas from the server; try again"}
                if not self._op_lock.acquire(timeout=wait):
                    return busy
                try:
                    rollbacks_before = self.merge_rollback_seq
                    if not self._merge_remote(probe, bundle):
                        return {"ok": False,
                                "error": "could not download the canvas from the server; "
                                         "try again"}
                    if self.merge_rollback_seq != rollbacks_before:
                        return {"ok": False,
                                "error": "merging the server's changes would break the canvas "
                                         "(fused canvas validate failed); nothing was changed. "
                                         "Publish or revert your local edits, then pull again"}
                    self.pull_seq += 1
                    self.last_pull_at = time.time()
                    _seed_clone_claude_files(self.dir, self.name)
                    # The merged folder carries this clone's unpushed edits: it
                    # stays dirty and the (no longer held) push ships it.
                    self._fingerprint = self._take_fingerprint()
                    self._dirty_since = time.time()
                    if self.push_state != "error":
                        self.push_state = "pending"
                finally:
                    self._op_lock.release()
            else:
                # Clean — or dirty with no sync point to merge against, where
                # the server wins and the trash keeps what it overwrote.
                # `_force_pull` rotates the baseline (clearing `server_stale`)
                # only on the path where the pull landed.
                if self.agent_run_id(fresh=True):
                    return agent_live
                if not self._op_lock.acquire(timeout=wait):
                    return busy
                try:
                    error = self._force_pull(probe, cli)
                finally:
                    self._op_lock.release()
        else:
            base = ["workbench", "canvas", "pull", self.name, "-o", self.dir]
            if self.agent_run_id(fresh=True):
                return agent_live
            if not self._op_lock.acquire(timeout=wait):
                return busy
            try:
                if dirty or hard:
                    self._snapshot_clone()
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
        return {"ok": True}

    def _embed_enter(self, gen: int, seq: int) -> dict | None:
        """Bookkeeping shared by apply_snapshot / apply_rename, under _op_lock.
        Returns the ack to answer with when nothing is to be written: the one
        already given for this (gen, seq), or a no-op ack for a seq older than
        one already applied in this generation. None means: apply, then record
        the ack with _embed_record."""
        if gen != self._embed_gen:
            self._embed_gen = gen
            # A new generation: the embed re-hydrated from the SERVER, so what
            # it holds is no longer any disk reading. Its hello brings a full
            # delta that re-seeds the view. It also holds what a hard pull
            # just brought down, so the fence has nothing left to hold out.
            self._embed_view = None
            self._embed_applied = {}
            self._embed_max_seq = -1
            self._hard_fence = None
        done = self._embed_applied.get((gen, seq))
        if done is not None:
            return done
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

    def _embed_record(self, gen: int, seq: int, ack: dict) -> dict:
        self._embed_max_seq = max(self._embed_max_seq, seq)
        self._embed_applied[(gen, seq)] = ack
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
        when the clone is mid-write (a row whose source file is not there
        yet, an unparsable toml) — the page asks again shortly. Empty ops
        against a known view mean the embed already holds disk: the view
        moves here and nothing is posted.

        The rev is taken BEFORE the disk read. Claude writes without
        _op_lock, so a write can land between the two; noted first, such a
        write is either in `disk` already or bumps the rev on the next scan
        and a follow-up delta repairs the view. Noted after, the delta would
        carry the pre-write content under the post-write rev and the scan
        would see nothing left to report.

        A source file with no row (Claude wrote `foo.py` and no
        `[[canvas.nodes]]` entry) arrives in `disk` as a SYNTHESIZED row
        (canvas_snapshot, invariant 2); it is written into canvas.toml here,
        under the same lock, so the next push ships the node with its file.

        A cut does NOT mark a frame as seen: the page cuts on every disk_rev
        move and drops the answer when it has no frame (boot, a reload), so
        only a frame's own ack or snapshot is evidence that one is there —
        otherwise disk_to_embed would read "pending" with nobody to ack.

        Files the embed just created without their node are left out while in
        their ROWLESS_GRACE_S (expiring them first may itself bump the rev, so
        it runs before the rev is taken)."""
        exclude = self._rowless_in_grace()
        self._note_disk()
        rev = self.disk_rev
        try:
            disk = self._disk_reading(exclude)
        except FileNotFoundError:
            return self._cut_held(rev, "the clone has no canvas.toml yet")
        except Exception as exc:  # noqa: BLE001 — rtoml's own error types
            return self._cut_held(rev, f"canvas.toml unreadable: {exc}")
        if disk["missing"]:
            return self._cut_held(rev, "source file missing for " + ", ".join(disk["missing"][:3]))
        if self._last_cut_held is not None:
            self.log_event("disk_to_embed", "delta unheld", detail=f"rev {rev}")
            self._last_cut_held = None
            self._bump()
        if self._delta_error is not None:
            # A good cut (the frame's hello asks for one) starts the leg over.
            self._delta_error = None
            self._bump()
        synthesized = list(disk.get("synthesized") or [])
        if synthesized:
            self._persist_synthesized(synthesized)
        view = None if full else self._embed_view
        ops = canvas_snapshot.diff_views(disk, view)
        if view is not None and not ops:
            self._embed_view = self._adopt(disk)
            self._view_rev = rev
            self._delta_error = None
            self._bump()
            return {"rev": rev, "base_rev": rev, "ops": [], "full": False}
        self._delta_views[rev] = disk
        for old in sorted(self._delta_views)[:-8]:
            del self._delta_views[old]
        self.log_event("disk_to_embed", "delta cut",
                       detail=f"rev {rev}: {len(ops)} op(s)" + (" (full)" if view is None else ""))
        return {"rev": rev, "base_rev": None if view is None else self._view_rev,
                "ops": ops, "full": view is None}

    def _cut_held(self, rev: int, reason: str) -> dict:
        """A delta the clone cannot answer yet. Logged once per distinct
        reason — the page re-asks every few hundred ms while it holds."""
        if reason != self._last_cut_held:
            self._last_cut_held = reason
            self.log_event("disk_to_embed", "delta held", ok=False, detail=f"rev {rev}: {reason}")
        return {"rev": rev, "held": reason}

    # -- rowless files: the embed's new file, its node still in flight ------------

    def _note_rowless(self, nodes, rowless, *, deleted=()) -> None:
        """After an embed snapshot: `nodes` (the names it sent) settle any
        rowless entry — the workbench's own row arrived — and so does a slug
        the snapshot `deleted` (dropped before its row ever came); `rowless`
        (apply_snapshot's `rowless_files`) start a grace each. Under _op_lock
        (apply_snapshot)."""
        names = set(nodes)
        gone = set(deleted)
        now = time.time()
        started: list[str] = []
        with self._rowless_lock:
            for slug in [s for s in self._rowless if s in names or s in gone]:
                del self._rowless[slug]
            for slug in rowless or []:
                if isinstance(slug, str) and slug not in names and slug not in self._rowless:
                    self._rowless[slug] = now
                    started.append(slug)
        if started:
            self.log_event("embed_to_disk", "row deferred",
                           detail=f"{', '.join(started[:5])}: file written, its node not "
                                  f"in the snapshot; waiting up to {ROWLESS_GRACE_S:g}s for "
                                  "the workbench's row before synthesizing one")

    def _rowless_peek(self) -> set[str]:
        now = time.time()
        with self._rowless_lock:
            return {s for s, t in self._rowless.items() if now - t < ROWLESS_GRACE_S}

    def _rowless_in_grace(self) -> set[str]:
        """Slugs still inside ROWLESS_GRACE_S. Expires the rest; an expiry
        forces a disk_rev bump (nothing on disk moved, so no scan would) so the
        page cuts again and that cut synthesizes their rows."""
        now = time.time()
        with self._rowless_lock:
            pending = bool(self._rowless)
        # A row that reached disk some other way (Claude wrote it, a rename)
        # settles the entry just as a snapshot naming the node does.
        rows = (self._toml_rows() or {}).get("nodes", {}) if pending else {}
        with self._rowless_lock:
            for slug in [s for s in self._rowless if s in rows]:
                del self._rowless[slug]
            aged = [s for s, t in self._rowless.items() if now - t >= ROWLESS_GRACE_S]
            for slug in aged:
                del self._rowless[slug]
            live = set(self._rowless)
        if aged:
            self.log_event("embed_to_disk", "row grace elapsed",
                           detail=f"{', '.join(sorted(aged)[:5])}: no row from the workbench "
                                  f"in {ROWLESS_GRACE_S:g}s; synthesizing")
            self._force_rev()
        return live

    def _force_rev(self) -> None:
        """Advance disk_rev although no file moved (a decision changed, not
        the disk), so the page asks for a delta."""
        with self._rev_lock:
            self._rev_key = None
        self._note_disk()

    def _disk_reading(self, exclude: set[str]) -> dict:
        """compact_state_from_disk, leaving `exclude` (rowless files in their
        grace) out: no synthesized row, no delta op, no persist."""
        return canvas_snapshot.compact_state_from_disk(
            self.dir, self._remote_ids(), exclude_orphans=exclude)

    def _synthesize_orphans(self) -> None:
        """Invariant 2 on the PUSH path: a root-level source file with no
        `[[canvas.nodes]]` row gets its synthesized row written into
        canvas.toml before the push freezes the clone — whether or not any
        frame ever cut a delta — so a node is never published without its
        file's row. Under _op_lock (the push's freeze phase)."""
        rows = self._toml_rows()
        if rows is None:
            return  # no or unparsable canvas.toml: the push's validation says so
        exclude = self._rowless_in_grace()
        try:
            orphans = canvas_snapshot.orphan_sources(
                self.dir, rows["nodes"].keys(), exclude=exclude)
        except Exception as exc:  # noqa: BLE001 — never block a push on this
            logger.warning("canvas %s: orphan scan failed: %s",
                           _sync_key(self.env, self.name), exc)
            return
        if orphans:
            self._persist_synthesized([slug for slug, _file in orphans], leg="disk_to_server")

    def _persist_synthesized(self, names: list[str], *, leg: str = "disk_to_embed") -> None:
        """canvas_snapshot.persist_synthesized, logged on the ring; a cut
        itself already carries the rows either way. Under _op_lock (cut_delta's
        caller and the push's freeze phase hold it)."""
        try:
            written = canvas_snapshot.persist_synthesized(self.dir, names)
        except Exception as exc:  # noqa: BLE001 — the row reaches the frame anyway
            self.log_event(leg, "synthesize failed", ok=False,
                           detail=f"{', '.join(names[:5])}: {exc}")
            return
        # canvas.toml moved: note it now rather than on the next scan/fs event.
        # The rows are byte-identical to the reading the cut carries, so the
        # follow-up cut diffs to nothing.
        self._note_disk()
        if not written:
            return  # idempotent: already in canvas.toml, nothing to log
        self.log_event(leg, "rows synthesized",
                       detail="canvas.toml rows written for " + ", ".join(written[:5])
                              + (f" (+{len(written) - 5})" if len(written) > 5 else ""))

    def ack_delta(self, rev: int, ok: bool, reason: str | None = None) -> dict:
        """The frame applied (or refused) the delta cut at `rev`. `reason` is
        the page's word for a refusal; one that only means "nobody could take
        it yet" (no frame on the page, a frame not hydrated) resets the view
        like any refusal but is not an error the strip should show — the
        frame's hello brings a full state that settles it. Nor does such a
        refusal mark a frame as seen: until the hello, there is no frame able
        to ack, and disk_to_embed must not read "pending" over a boot."""
        view = self._delta_views.pop(rev, None)
        benign = (not ok and bool(reason)
                  and _BENIGN_DELTA_REFUSAL.search(reason) is not None)
        if not benign:
            self._frame_seen = True
        if not ok:
            # The frame holds something we cannot describe: start over with a
            # full state on its next hello or the next disk move.
            self._embed_view = None
            if not benign:
                self._delta_error = (f"the workbench refused the delta for rev {rev}"
                                     + (f": {reason}" if reason else ""))
                self._bump()
            self.log_event("disk_to_embed", "delta refused", ok=benign,
                           detail=f"rev {rev}" + (f": {reason}" if reason else ""))
            return {"ok": True, "rev": rev, "reset": True}
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
        self._embed_view = self._adopt(view)
        self._view_rev = rev
        self._delta_error = None
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
        # `rowless_prev`: a rowless file the embed has since dropped (UDF gone,
        # no node) is a delete, not an orphan to synthesize later.
        result = canvas_snapshot.apply_snapshot(self.dir, new, prev, backup=backup,
                                                rowless_prev=self._rowless_peek())
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
        # A file this snapshot created with no node: grace before any row is
        # invented for it; a node it names settles an earlier one.
        self._note_rowless(new["nodes"], result.get("rowless_files"),
                           deleted=result.get("deleted") or ())
        self._refresh_view_after_write(held, edges=new["edges"], ids=new["ids"],
                                       bodies=bodies, embed_rows=new["nodes"])
        ack = {"ok": True, "gen": gen, "seq": seq, "changed": result["changed"],
               "upserted": result["upserted"], "deleted": result["deleted"],
               "created": result["created"], "push_seq": self.push_seq}
        # Rows apply_snapshot wrote for files the snapshot sent with no node
        # (canvas_snapshot, invariant 2) — the frame learns of them by delta.
        if result.get("synthesized"):
            ack["synthesized"] = list(result["synthesized"])
        return self._embed_record(gen, seq, ack)

    def apply_rename(self, gen: int, seq: int, frm: str, to: str,
                     file_from: str | None, file_to: str | None) -> dict:
        """The workbench's rename hint (canvas_snapshot.apply_rename), under
        _op_lock like a snapshot, acked the same way. The previous snapshot is
        renamed too, so the snapshot that follows is diffed against the new
        name rather than read as delete + add."""
        stale = self._embed_enter(gen, seq)
        if stale is not None:
            return stale
        result = canvas_snapshot.apply_rename(self.dir, frm, to, file_from, file_to)
        # A file still in its row grace keeps the grace under its new name
        # (same start): otherwise the renamed file is an orphan at once and
        # the next cut invents the very row the grace exists to wait for.
        with self._rowless_lock:
            if frm in self._rowless and to not in self._rowless:
                self._rowless[to] = self._rowless.pop(frm)
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
            "push_state": ("held" if self.server_stale is not None and self.push_state == "pending"
                           else self.push_state),
            "push_seq": self.push_seq,
            "error": self.last_error,
            "error_detail": list(self.error_detail),
        }

    def sync_state(self) -> dict:
        """GET /api/canvases/sync/state: one entry per leg of the two loops.
        The server knows three; `embed_to_disk` (snapshots the page has
        received and not yet had acked) is the host page's own knowledge and
        is merged in client-side — here it is always idle."""
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
        elif ps == "pending" or dirty is not None:
            reason = None
            if self._paused():
                reason = "sync is paused"
            elif self._agent_run_id:
                reason = "waiting for the Claude session in this folder to finish"
            elif self._rowless_peek():
                reason = ("waiting for the workbench's row for "
                          + ", ".join(sorted(self._rowless_peek())[:3]))
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
            s2d = {"state": "error" if self._last_pull_error else "stale",
                   "since_ms": age(stale.get("since")), "changed": _stale_changed(stale),
                   "reason": self._last_pull_error or _stale_reason(stale)}
        else:
            s2d = {"state": "idle", "since_ms": None, "changed": [], "reason": None}

        return {
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
        }


_IDLE_EMBED_TO_DISK = {"state": "idle", "oldest_age_ms": None, "reason": None}

# A delta refusal that means "nobody could take it yet", not "the frame
# choked on it": no frame on the page, a frame still hydrating.
_BENIGN_DELTA_REFUSAL = re.compile(r"hydrat|no[ _-]?frame", re.IGNORECASE)


def _stale_changed(stale: dict | None) -> list[dict]:
    if not stale:
        return []
    changed = [{"slug": u.get("slug"), "kind": u.get("kind")} for u in stale.get("udfs", [])]
    if stale.get("layout"):
        changed.append({"slug": None, "kind": "layout"})
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


def _clone_gate(env: str, name: str) -> threading.Lock:
    with _SYNC_LOCK:
        return _CLONE_GATES.setdefault((env, name), threading.Lock())


def refuse_new_managers() -> None:
    """The quit rung's spawn latch (armed before any killer runs)."""
    global _QUITTING
    with _SYNC_LOCK:
        _QUITTING = True


def shutdown() -> None:
    """Quit rung: stop every sync manager — a push CLI in flight, its watcher
    and fs threads, its writer lock — then close any claim left open. The
    packaged app never runs the server's lifespan (app.py
    QUIT_CHILDREN_BUDGET_S), so without this a push CLI outlived the app as
    an orphan and the lock fds were left to the process exit.

    A push is STOPPED, not waited for: its CLI is terminated (SIGTERM, then
    SIGKILL after PUSH_TERMINATE_GRACE_S) so its fold never runs on a half
    state; the push folds as failed and the sync point is not rotated. If the
    server had already taken the push, the next launch's first probe reads it
    as "server changed" and the user pulls their own push back — accepted.
    All stop events are set first so the joins overlap; each join is bounded
    by STOP_JOIN_S, and the kernel drops any flock left at exit."""
    global _QUITTING
    with _SYNC_LOCK:
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
    for manager in managers:
        try:
            manager.stop()
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
    if not os.path.isfile(os.path.join(_canvas_dir(env, name), "canvas.toml")):
        return _error(f"canvas {name!r} is not cloned yet (no canvas.toml)", 409)
    # The writer lock FIRST: a refused start must not touch a folder another
    # process owns (the seeding below writes into it).
    try:
        manager = _sync_manager(env, name, create=True)
    except _CanvasOwned as owned:
        # One writer per clone: another fused-render (the desktop app and a
        # dev server, two branches) is already syncing this folder. The page
        # says who and does not start its delta leg (owner decision Q4).
        owner = {k: owned.owner.get(k) for k in ("pid", "port", "since")}
        return JSONResponse(
            {"error": f"canvas {_sync_key(env, name)!r} is synced by another fused-render "
                      f"(pid {owner['pid']}, port {owner['port']})",
             "code": "owned", "owner": owner}, status_code=409)
    except OSError as exc:
        return _error(f"could not take the canvas sync lock: {exc}", 500)
    if manager is None:
        return _busy("fused-render is quitting; canvas sync is not starting")
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
            manager.log_event("embed_to_disk", "snapshot rejected", ok=False,
                              detail=f"{what} {gen}/{seq}: the clone has no canvas.toml yet")
            return JSONResponse(
                {"ok": False, "gen": gen, "seq": seq, "code": "rejected",
                 "error": "the clone has no canvas.toml yet"}, status_code=400)
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


def _snapshot_ack_detail(what: str, gen: int, seq: int, ack: dict) -> str:
    if ack.get("stale"):
        return f"{what} {gen}/{seq}: superseded by a later seq, not written"
    parts = [f"{what} {gen}/{seq}"]
    if not ack.get("changed"):
        parts.append("no change")
    for key in ("upserted", "deleted", "created", "synthesized"):
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
        return JSONResponse(manager.ack_delta(
            rev, bool(ok), reason=reason if isinstance(reason, str) else None))


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

    Refuses rather than queues (409) when the watcher is paused, when a push is
    already running, or when another sync leg holds the lock: the caller is an
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
    if manager.push_state == "pushing":
        return _busy("a push is already running for this canvas")
    if manager.server_stale is not None:
        # What a hosted tab's save does while the server moved: refuse until
        # the user pulls. Carries the list so a session can say what changed.
        changed = ", ".join(
            f"{u['slug']} ({u['kind']})" for u in manager.server_stale.get("udfs", [])
        ) or "the canvas layout"
        # Promise "kept" only where pull() merges: a manifest shim AND a sync
        # point. The external-CLI leg and a dirty clone with no sync point pull
        # wholesale (see pull()), and a session told its edits survive would
        # pull and lose them — so name the trash copy instead.
        cli = fused_cli()
        trash = os.path.join(manager.root, ".sync", "trash", name)
        if cli is None or _shim_manifest_command(cli) is None:
            kept = ("this fused CLI cannot merge, so the pull REPLACES your local "
                    f"edits with the server's files; a copy is kept under {trash}")
        elif manager._base_files is None:
            kept = ("there is no sync point to merge against, so the pull REPLACES "
                    f"your local edits with the server's files; a copy is kept under {trash}")
        else:
            kept = "your edits are merged with them and kept"
        return JSONResponse(
            {"error": "the canvas changed on the server since the last sync "
                      f"({changed}); pull first — the Pull button in the canvas view "
                      f"brings those changes into this folder ({kept}), "
                      "then push again",
             "code": "server_changed", "server_stale": manager.server_stale},
            status_code=409)
    # The flight lock, not _op_lock: _push takes _op_lock itself, only for its
    # short freeze and fold phases, and never across the CLI.
    if not manager._flight_lock.acquire(timeout=MANUAL_PUSH_LOCK_WAIT_S):
        return _busy(
            "a sync operation is in flight for this canvas; try again in a moment")
    try:
        # Re-checked under the lock: pause() sets the count and then waits on
        # this same lock, so holding it is what makes the answer stable.
        with manager.pause_lock:
            if manager.pause_count > 0:
                return _busy(f"syncing for {_sync_key(env, name)!r} is paused; try again in a moment")
        before = manager.push_seq
        if manager._push() == "busy":
            return _busy(
                "a sync operation is in flight for this canvas; try again in a moment")
    finally:
        manager._flight_lock.release()
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


@router.post("/api/canvases/sync/pull")
def api_canvases_sync_pull(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Pull the server's canvas into the clone NOW — the canvas page's
    "Server changes · Pull" (the hosted workbench's pull, for this tab).
    Clean clone: force pull. Dirty: the three-way merge, canvas.toml per
    node, local edits kept; the held push then ships the merged folder —
    except with no shim or no sync point, where the pull is wholesale and the
    trash holds the overwritten edits (see _SyncManager.pull). Returns the
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


def _state_for(env: str, name: str) -> dict:
    manager = _sync_manager(env, name, create=False)
    return _unwatched_state(env, name) if manager is None else manager.sync_state()


@router.get("/api/canvases/sync/state")
def api_canvases_sync_state(name: str = "", env: str = ""):
    """Every leg of the two sync loops for one canvas (the build contract's
    shape). A canvas this process is not syncing answers all-idle with
    `watching: false` — never a 409: the page polls this as the socket's
    fallback, before and after a watcher exists."""
    if not _NAME_RE.fullmatch(name or ""):
        return _error("'name' must be a canvas name (letters, digits, underscore)")
    env, bad = _require_env(env)
    if bad is not None:
        return bad
    return _state_for(env, name)


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
