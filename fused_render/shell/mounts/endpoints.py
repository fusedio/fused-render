"""The /api/mounts/* FastAPI surface: CRUD, mount/unmount/reconnect,
restart, remote-credential detection, and the OAuth sign-in endpoints
(Google Drive, Dropbox, Box)."""

import collections
import dataclasses
import json
import logging
import os
import re
import subprocess
import threading
import time

from fastapi.responses import JSONResponse
from fastapi import APIRouter, Body, Header

from .config import _require_fused
from .credentials import (
    CloudUrlError,
    _detected_credential_error,
    _rclone_state,
    resolve_cloud_url,
)
from .lifecycle import PROBE_TIMEOUT, attach_mount, mount_view, sync_serves
from .signing import _invalidate_upstream_caches
from .store import _ismount, add_mount, get_mount, mountpoint, remove_mount

logger = logging.getLogger(__name__)


router = APIRouter()


@router.get("/api/mounts/resolve")
def resolve_url_endpoint(url: str = ""):
    """Path bar support: turn a bucket URL into the local path under the mount
    that covers it. Read-only and side-effect free, so no X-Fused guard (same
    as GET /api/mounts)."""
    try:
        return {"path": resolve_cloud_url(url)}
    except CloudUrlError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)


@router.get("/api/mounts")
def get_mounts():
    from fused_render.shell.mounts import (
        _effective_serve_read_only,
        _mount_credential_status,
        _mount_upload_status,
        _upload_unknown,
        list_mounts,
        mount_state,
        mounted_paths,
        rclone_bin,
    )
    live = mounted_paths()
    mounts = list_mounts()
    bin_ = rclone_bin()
    # Probe states — AND, for a broken mount, its credential status — concurrently:
    # each disconnected/wedged mount blocks its state probe for the full
    # PROBE_TIMEOUT, and the credential `rclone lsd` can take ~10s more. Serially
    # (states threaded but the credential probe left to the mount_view loop) a
    # few broken aws mounts would stall the polled Mounts page for tens of
    # seconds — exactly when the user is polling to recover. So do BOTH in the
    # per-mount worker and hand the results to mount_view, which never probes.
    states: list[str | None] = [None] * len(mounts)
    cred_statuses: list[str] = ["n/a"] * len(mounts)
    uploads: list[dict | None] = [None] * len(mounts)
    threads = []
    for i, m in enumerate(mounts):
        def probe(i=i, m=m):
            try:
                st = mount_state(m, live)
                states[i] = st
                # Credentials only matter for a broken mount; a healthy/unmounted
                # one never pays the lsd probe.
                if st in ("disconnected", "stale"):
                    cred_statuses[i] = _mount_credential_status(m, bin_)
                elif st == "mounted" and not _effective_serve_read_only(m):
                    # The async upload queue (D207): only a live, writable mount
                    # can have one. Deliberately in THIS worker rather than a
                    # second poll loop — and mutually exclusive with the
                    # credential probe above, so the join budget is unchanged.
                    #
                    # The gate is what the LIVE mount baked, not the record: the
                    # record's read_only can drift ahead of it (detection flipped
                    # it, or a remount was deferred — see
                    # _effective_serve_read_only), and a mount recorded read-only
                    # but actually mounted read-write can still be holding
                    # queued writes we must not stay silent about.
                    uploads[i] = _mount_upload_status(m)
            except Exception:
                # A worker that dies leaves this mount's slots unset, which reads
                # as "disconnected, nothing queued" — a false all-clear on the
                # very mount that just misbehaved. Report the unknown honestly.
                logger.exception("mount %r: status probe failed", m.get("name"))
                if states[i] == "mounted" and uploads[i] is None:
                    uploads[i] = _upload_unknown("the mount could not be probed")
        t = threading.Thread(target=probe, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        # Allow for the state probe PLUS the credential lsd (30s cap) so a slow
        # probe still lands in this listing rather than defaulting to unknown.
        t.join(PROBE_TIMEOUT + 31)
    return {
        "rclone": _rclone_state(),
        "mounts": [
            mount_view(m, live, state=s or "disconnected", cred_status=cs, uploads=up)
            for m, s, cs, up in zip(mounts, states, cred_statuses, uploads)
        ],
    }


@router.post("/api/mounts")
def create_mount(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        # An explicit read_only in the body wins over attach-time detection —
        # the caller knows their credentials better than the probe does.
        # add_mount validates it (strict bool or absent).
        m = add_mount(body.get("name") or "", body.get("remote") or "",
                      read_only=body.get("read_only"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    err = attach_mount(m)
    if err:
        # Create implies mount; a mount that never mounted is not kept.
        remove_mount(m["id"])
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.post("/api/mounts/{cid}/mount")
def mount_endpoint(cid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    err = attach_mount(m)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.post("/api/mounts/{cid}/reconnect")
def reconnect_endpoint(cid: str, x_fused: str | None = Header(default=None)):
    """Repair a disconnected mount: force-clear the dead mountpoint, remount."""
    from fused_render.shell.mounts import reconnect_mount
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    # Belt-and-braces like restart_endpoint below: reconnect_mount contracts to
    # return an error string, but it drives kernel unmounts and stat()s on a
    # mountpoint that is by definition broken — a surprise from down there
    # should read as a 502 on the Mounts page, never a raw 500 traceback.
    try:
        err = reconnect_mount(m)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.post("/api/mounts/restart")
def restart_endpoint(x_fused: str | None = Header(default=None, alias="X-Fused")):
    """Global recovery: restart the rcd daemon and re-mount everything. The one
    tool that fixes a stale-credential daemon (a fresh daemon re-reads refreshed
    keys) and applies changed mount params — see restart_rcd. Sync def so the
    multi-second unmount+kill+spawn+remount runs in the threadpool, never the
    event loop. Returns the same payload as GET /api/mounts."""
    from fused_render.shell.mounts import restart_rcd
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        restart_rcd()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return get_mounts()


@router.post("/api/mounts/{cid}/unmount")
def unmount_endpoint(cid: str, force: str = "0",
                     x_fused: str | None = Header(default=None)):
    from fused_render.shell.mounts import detach_mount
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    err = detach_mount(m, force=force == "1")
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.delete("/api/mounts/{cid}")
def delete_mount(cid: str, x_fused: str | None = Header(default=None)):
    from fused_render.shell.mounts import detach_mount
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    if m.get("builtin"):
        # BUGBOT: nothing stopped this — a deleted builtin record only
        # reappears at the next full SERVER restart (ensure_learn_mount runs
        # once, from run_automount at startup), while the already-open
        # Sidebar's learnMountReady state never rechecks once true, leaving
        # a dead Learn link for the rest of the session. Bundled read-only
        # content isn't something a user action should be able to
        # permanently remove out from under a running session anyway —
        # unmounting (POST .../unmount) still works to free the mountpoint.
        return JSONResponse(
            {"error": "this is a bundled default mount and can't be deleted"},
            status_code=400,
        )
    err = detach_mount(m)
    mp = mountpoint(m)
    if err and _ismount(mp):
        # Deleting the record while the filesystem is still mounted would
        # strand a live mount (and let a re-added name silently reuse it).
        return JSONResponse({"error": f"not deleted — {err}"}, status_code=502)
    if os.path.isdir(mp) and not _ismount(mp) and not os.listdir(mp):
        os.rmdir(mp)
    remove_mount(cid)
    sync_serves()  # stop the deleted mount's HTTP serve, drop its map entry
    _invalidate_upstream_caches()  # the gone remote's memoized facts mustn't linger
    return {"ok": True}


@router.post("/api/mounts/remotes")
def create_remote(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Create an S3-compatible rclone remote non-interactively from keys.
    OAuth backends have no keys to paste and go through the browser sign-in
    instead (POST /api/mounts/remotes/oauth). Credentials go straight into
    rclone's own config, never through the store."""
    from fused_render.shell.mounts import _rc, ensure_rcd, rclone_bin
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    # Validate the request body (a 400 client error) BEFORE probing for rclone
    # (a 502 environment error): a bad name is bad on every platform, and where
    # rclone is absent (e.g. a macOS host with no bundled/PATH binary) the
    # availability check would otherwise mask the real 400 with a 502.
    name = (body.get("name") or "").strip()
    if not name or ":" in name or "/" in name:
        return JSONResponse({"error": "invalid remote name"}, status_code=400)
    bin_ = rclone_bin()
    if not bin_:
        return JSONResponse({"error": "rclone is not installed"}, status_code=502)
    p = body.get("params") or {}
    parameters = {
        "provider": p.get("provider") or "Other",
        "access_key_id": p.get("access_key_id") or "",
        "secret_access_key": p.get("secret_access_key") or "",
        "env_auth": "false",
    }
    if p.get("endpoint"):
        parameters["endpoint"] = p["endpoint"]
    if p.get("region"):
        parameters["region"] = p["region"]
    # Created through the rc daemon (JSON over loopback HTTP) rather than
    # `rclone config create` on argv: the latter would put the plaintext
    # secret_access_key in the process's command line, visible to any other
    # local user via `ps` (same rationale as the rcd auth secret in
    # _rcd_child_env).
    try:
        port = ensure_rcd()
        _rc(port, "config/create",
            {"name": name, "type": "s3", "parameters": parameters}, timeout=30)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)[-500:]}, status_code=502)
    _invalidate_upstream_caches()  # new/changed keys must be picked up without restart
    return {"ok": True, "name": name + ":"}


# -- OAuth sign-in: Google Drive, Dropbox, Box (D205, D209) ----------------------
#
# `rclone authorize "<backend>"` runs its OWN loopback callback server on
# 127.0.0.1:53682, opens the system browser, and prints the OAuth token JSON on
# stdout when the user approves. We spawn it as one tracked child, poll it, and
# on a clean exit create the remote through the rc daemon.
#
# Verified against rclone v1.74.4: the token is the ONLY thing on stdout —
# every NOTICE ("Please go to the following link", "Waiting for code...") goes
# to stderr — and the child waits indefinitely for the callback, which is why
# the OAUTH_TIMEOUT backstop below is ours to enforce, not rclone's.
#
# The whole child lifecycle — dataclass record, stdout/stderr pump threads, an
# exit watcher, SIGTERM->SIGKILL escalation, poll-don't-push completion — is
# lifted from account.py's `fused cloud login` flow deliberately: that pattern is
# already proven here, and a second concurrency shape for the same problem would
# be a second thing to get wrong. The differences are only the ones the task
# forces: there is no URL to capture (rclone opens the browser itself), so the
# request returns as soon as the child is spawned and everything interesting
# happens on the watcher thread; and a second sign-in is REJECTED rather than
# joined, since rclone's callback port can only be bound once and a joiner would
# be waiting on a remote with a different name.

# rclone's callback server waits indefinitely, so the backstop is ours. Five
# minutes is the same order as the CLI login's own ceiling and is generous for a
# consent screen the user is actively looking at; past it the child is killed so
# an abandoned tab can't hold 53682 (and the in-flight flag) forever.
OAUTH_TIMEOUT = 300.0
# Grace for a terminated authorize child before it is SIGKILLed.
OAUTH_KILL_GRACE = 3.0
OAUTH_RC_TIMEOUT = 30.0


# Every browser-consent backend we offer, in ONE table — the whole flow below
# (spawn, poll, cancel, create, error wording) reads its per-provider facts
# from here rather than hardcoding "drive" the way it originally did.
#
#   backend      the name `rclone authorize <backend>` takes, which is also the
#                remote's `type` for all three (they coincide; kept as its own
#                key so a future provider where they diverge has somewhere to go)
#   label        what the USER calls it, for every error string
#   needs_client whether the user must supply their own OAuth client (below)
#   params       extra config/create parameters beyond the token
#
# Only Drive sets `needs_client`. Google is retiring rclone's built-in shared
# client ID — rclone was notified that Google will begin charging for API
# requests made with it later in 2026, after 90 days' notice, and rclone's plan
# is warn → disable → remove — so a Drive remote that does not carry the user's
# own client ID is on a countdown. Dropbox and Box are NOT affected: rclone's
# config prompts still say "Leave blank normally" for both, and asking for a
# client there would invent setup work that does not exist (explicit decision;
# do not "make it consistent").
_OAUTH_PROVIDERS: dict[str, dict] = {
    "drive": {
        "backend": "drive",
        "label": "Google Drive",
        "needs_client": True,
        # Full read-write `drive`: read-write is the requirement, and the only
        # non-restricted alternative (drive.file) can only ever see files this
        # app itself created, which is useless behind a mount. skip_gdocs
        # because Docs/Sheets have no byte representation — without it they
        # surface as ordinary-looking files whose saves silently fail to
        # round-trip.
        "params": {"scope": "drive", "skip_gdocs": "true"},
    },
    "dropbox": {
        "backend": "dropbox",
        "label": "Dropbox",
        "needs_client": False,
        "params": {},
    },
    "box": {
        "backend": "box",
        "label": "Box",
        "needs_client": False,
        "params": {},
    },
}

# The 400 a Drive sign-in without a client gets. Deliberately not a bare
# validation message: the user has real work to do in the Google Cloud console
# before a retry can succeed, so the reason has to travel with the refusal.
_CLIENT_REQUIRED_MSG = (
    "Google Drive needs your own Google API client ID and secret. rclone's "
    "built-in shared client ID is being retired — Google will start charging "
    "for API requests made with it later in 2026 — so every user must now "
    "supply their own. Create an OAuth client of type \"Desktop app\" in the "
    "Google Cloud console, then paste its client ID and secret."
)

# The success frame rclone wraps the token in (fs/config/authorize.go, verified
# against v1.74.4). Matched when present, but not depended on — the fallback
# scans for a bare JSON object, so a reworded frame degrades to still working.
_TOKEN_BEGIN = "Paste the following into your remote machine --->"
_TOKEN_END = "<---End paste"


@dataclasses.dataclass
class _ActiveAuthorize:
    """The single in-flight `rclone authorize <backend>` child.

    `done` is set exactly once, by the watcher thread, AFTER `ok`/`error` are
    written — status readers only act on `done`, so that ordering is the whole
    synchronization (plain attribute writes are atomic under the GIL, the same
    discipline account.py's _SetupJob uses).

    Two separate output sinks, and that split is load-bearing: `out` is stdout,
    which CARRIES THE TOKEN, and `tail` is stderr, which is what error messages
    are built from. Keeping them apart is what stops a failed sign-in from
    pasting an OAuth credential into a UI banner, a log line, or a bug report.
    deque.append is atomic under the GIL, so the pumps need no lock.

    `out` is CLEARED the moment the token is parsed out of it (see
    _authorize_outcome). This record outlives the attempt on purpose — the
    status endpoint reports the last outcome from it — so the token's lifetime
    has to be bounded explicitly rather than by the record's. Nothing may read
    `out` after that point.
    """

    name: str
    provider: str  # key into _OAUTH_PROVIDERS — the label and params come from it
    backend: str
    proc: subprocess.Popen
    # Held only until the remote is created, then CLEARED by _watch_authorize —
    # the same bounded-lifetime discipline as the token in `out`, and for the
    # same reason: this record deliberately outlives the attempt.
    client_id: str = ""
    client_secret: str = ""
    # The caller explicitly asked to overwrite an EXISTING remote. Recorded at
    # spawn time because it decides whether a failed create may be rolled back:
    # deleting is only safe for a remote we ourselves brought into existence.
    replacing: bool = False
    done: threading.Event = dataclasses.field(default_factory=threading.Event)
    out: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=200))
    tail: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=40))
    ok: bool = False
    error: str | None = None
    canceled: bool = False
    timed_out: bool = False


_AUTH_LOCK = threading.Lock()
# The MOST RECENT attempt, live or finished — the status endpoint reports the
# last outcome from the same record, so a client that polls one tick late still
# learns whether the sign-in worked.
_authorize: _ActiveAuthorize | None = None


def _authorize_argv(bin_: str, backend: str) -> list[str]:
    """The authorize command line. Nothing sensitive on argv: the backend name
    is a constant, the OAuth client id/secret travel in the ENVIRONMENT (see
    _client_credential_env), and the token only ever comes back on stdout (and
    then goes out over the rc daemon, never as an argument — see
    create_remote)."""
    return [bin_, "authorize", backend]


def _client_credential_env(backend: str, client_id: str,
                           client_secret: str) -> dict[str, str]:
    """The RCLONE_<BACKEND>_CLIENT_ID / _SECRET overlay for an authorize child,
    or {} when the provider supplies no client of its own.

    ENVIRONMENT, NOT ARGV — and that is a security decision, not a style one.
    `rclone authorize <backend> <client_id> <client_secret>` accepts them only
    POSITIONALLY, which would put the secret on the command line, and
    /proc/<pid>/cmdline is mode -r--r--r-- — readable by every other local user,
    the exact invariant _authorize_argv exists to hold (and the same one the S3
    secret key and the rcd auth secret are kept off argv for).
    /proc/<pid>/environ is mode -r-------- (owner only), so the environment
    preserves the property.

    Verified empirically against rclone v1.74.4: with
    RCLONE_DRIVE_CLIENT_ID=TESTID12345 exported, the OAuth redirect carried
    client_id=TESTID12345; with nothing exported it fell back to rclone's shared
    202264815644.apps.googleusercontent.com — the client ID being retired. Do
    not "simplify" this back onto argv."""
    if not client_id and not client_secret:
        return {}
    prefix = f"RCLONE_{backend.upper()}"
    return {f"{prefix}_CLIENT_ID": client_id,
            f"{prefix}_CLIENT_SECRET": client_secret}


def _spawn_authorize(bin_: str, backend: str,
                     env_extra: dict[str, str] | None = None) -> subprocess.Popen:
    """Start the authorize child. The one seam the tests replace — everything
    above it (pumps, watcher, kill escalation, parsing, the rc call) is
    exercised for real against the substituted child.

    `env_extra` OVERLAYS os.environ rather than replacing it: the child still
    needs PATH/HOME (and on macOS the browser-opening plumbing) to run at all."""
    return subprocess.Popen(
        _authorize_argv(bin_, backend),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **(env_extra or {})},
    )


def _pump_authorize(stream, sink: collections.deque) -> None:
    for raw in stream:
        line = raw.rstrip("\n")
        if line.strip():
            sink.append(line)
    stream.close()


def _ensure_dead_child(proc: subprocess.Popen) -> None:
    """Escalate a terminated child to SIGKILL if it ignores SIGTERM (account.py's
    _ensure_dead). A merely-SIGTERM'd authorize child could survive holding its
    loopback callback server, complete a late Google round-trip, and race a
    retried sign-in."""
    try:
        proc.wait(OAUTH_KILL_GRACE)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(OAUTH_KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass  # unkillable child; status still reports it in flight


def _parse_authorize_token(stdout: str) -> str | None:
    """The OAuth token JSON from an authorize child's stdout, or None when it
    produced none (the abandoned-tab / denied-consent case).

    Returned verbatim rather than re-serialized: rclone reads this string back
    as the remote's `token` value, so the bytes it printed are the bytes it
    should get. Validated as a JSON object carrying an access_token, so a
    progress line that merely looks brace-ish can't be mistaken for one."""
    start = stdout.find(_TOKEN_BEGIN)
    if start != -1:
        rest = stdout[start + len(_TOKEN_BEGIN):]
        end = rest.find(_TOKEN_END)
        candidates = [(rest[:end] if end != -1 else rest).strip()]
    else:
        candidates = [line.strip() for line in stdout.splitlines()]
    for blob in candidates:
        if not blob.startswith("{"):
            continue
        try:
            parsed = json.loads(blob)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("access_token"):
            return blob
    return None


def _authorize_failure_detail(job: _ActiveAuthorize) -> str:
    """rclone's own last words, for the error message. stderr only — stdout is
    where the token lives and must never reach a user-visible string."""
    return "\n".join(job.tail)[-400:]


def _create_oauth_remote(job: _ActiveAuthorize, token: str) -> None:
    """Create the remote for a finished sign-in, from a fresh token. Raises
    RuntimeError.

    Through the rc daemon (JSON over loopback HTTP), NOT `rclone config create`:
    an OAuth token on argv is visible to any other local user via `ps` — the
    identical reasoning as the S3 secret key in create_remote above, and it is
    why the client secret rides in the environment rather than on argv too (see
    _client_credential_env).

    The provider's extra params (Drive's scope/skip_gdocs; nothing for
    Dropbox/Box) come from _OAUTH_PROVIDERS. A user-supplied client id/secret is
    PERSISTED into the remote's config, which is not optional bookkeeping:
    rclone refreshes the access token with the same client that minted it, so a
    remote carrying only a token would work until the first refresh and then
    stop. It also makes the remote itself a legitimate place to read the
    client back from on a later sign-in.

    Deliberately NOT probed afterwards, unlike create_detected_remote's `lsd`.
    That probe exists because a detected credential was found sitting in a
    dotfile and may be of any age; this token was minted seconds ago by a
    consent the user just completed, so there is nothing for the probe to catch
    — while an `lsd` against a large Drive would add up to 30s to the spinner
    the user is watching. Recorded as a choice, not an oversight: if signed-in
    but broken Drive remotes ever turn up, this is where the probe goes."""
    from fused_render.shell.mounts import _rc, ensure_rcd
    spec = _OAUTH_PROVIDERS[job.provider]
    parameters = {"token": token, **spec["params"]}
    if job.client_id:
        parameters["client_id"] = job.client_id
    if job.client_secret:
        parameters["client_secret"] = job.client_secret
    port = ensure_rcd()
    _rc(port, "config/create",
        {"name": job.name, "type": spec["backend"], "parameters": parameters},
        timeout=OAUTH_RC_TIMEOUT)


def _delete_remote(name: str) -> bool:
    """Best-effort `config/delete` for a remote we just failed to finish
    creating. True when rclone confirmed the delete. Over the rc daemon like
    the create, so it works on the same connection and needs no argv."""
    from fused_render.shell.mounts import _rc, ensure_rcd
    try:
        _rc(ensure_rcd(), "config/delete", {"name": name}, timeout=OAUTH_RC_TIMEOUT)
        return True
    except (RuntimeError, ValueError):
        return False


def _authorize_outcome(job: _ActiveAuthorize) -> str | None:
    """Finish the attempt: the error message, or None when the remote was
    created. Runs on the watcher thread — the endpoint is long gone.

    Every user-visible string names the PROVIDER's label (D209): these read
    "the Google sign-in …" for all three backends before the registry, which
    was simply wrong for a Dropbox or Box user."""
    label = _OAUTH_PROVIDERS[job.provider]["label"]
    if job.canceled:
        return f"the {label} sign-in was canceled"
    if job.timed_out:
        return (f"the {label} sign-in was not completed within "
                f"{int(OAUTH_TIMEOUT // 60)} minutes — try again")
    if job.proc.returncode != 0:
        detail = _authorize_failure_detail(job)
        return (f"`rclone authorize {job.backend}` failed"
                + (f": {detail}" if detail else f" (exit {job.proc.returncode})"))
    token = _parse_authorize_token("\n".join(job.out))
    # Consumed. `_authorize` is a module global that deliberately outlives the
    # attempt (the status endpoint reports the last outcome from it), so without
    # this the raw access/refresh token would stay reachable for the life of the
    # process — the thing that turns a later crash dump or an added diagnostic
    # into a real credential leak. Nothing reads `out` past this point: every
    # failure branch below builds its message from `tail` (stderr) instead.
    job.out.clear()
    if token is None:
        # Exit 0 with nothing to show for it: the browser tab was closed, or
        # consent was never granted. Retryable, and it must SAY so — this is the
        # state the client sees as in_flight dropping without success.
        detail = _authorize_failure_detail(job)
        return (f"the {label} sign-in did not complete — no account was connected "
                "(the browser tab was closed, or approval was not granted). "
                "Try again." + (f" Last output: {detail}" if detail else ""))
    try:
        _create_oauth_remote(job, token)
    except RuntimeError as e:
        # Whatever happened, the config may have CHANGED before it failed —
        # drop the memoized view either way, or a later read could serve a
        # half-written remote's config from cache.
        _invalidate_upstream_caches()
        msg = f"signed in, but the remote could not be created: {str(e)[-400:]}"
        # "could not be created" may be a LIE: _rc raises the same RuntimeError
        # for an HTTP error and for a socket timeout, and a timeout against a
        # daemon that nonetheless finished config/create leaves a remote that
        # exists while the user is told it does not. Roll it back so a
        # half-written remote can't linger under Remotes inviting a doomed
        # mount — the posture create_detected_remote already takes for the same
        # class of failure.
        if job.replacing:
            # We did not bring this name into existence; the user had a working
            # remote here. Deleting it on top of a failed sign-in would be
            # strictly worse than leaving whatever survived.
            return msg + (" — the remote you asked to replace may or may not have "
                          "been overwritten; check it before mounting")
        if not _delete_remote(job.name):
            # The delete failed too, so the remote may still be there. Say so
            # rather than returning the bare error as if cleanup succeeded.
            return msg + (" (the half-created remote could not be removed "
                          "automatically — delete it manually before retrying)")
        return msg
    # A new remote's config must be picked up without a server restart.
    _invalidate_upstream_caches()
    return None


def _watch_authorize(job: _ActiveAuthorize, pumps: list[threading.Thread]) -> None:
    try:
        job.proc.wait(OAUTH_TIMEOUT)
    except subprocess.TimeoutExpired:
        job.timed_out = True
        job.proc.terminate()
        _ensure_dead_child(job.proc)
    for pump in pumps:
        pump.join(2.0)  # both pipes at EOF, so the output is complete
    label = _OAUTH_PROVIDERS[job.provider]["label"]
    try:
        job.error = _authorize_outcome(job)
    except Exception as e:  # never strand the client polling a stuck in_flight
        logger.exception("%s sign-in failed unexpectedly", label)
        job.error = f"the {label} sign-in failed unexpectedly: {e}"
    finally:
        # Consumed. Bounded here rather than by the record's lifetime, exactly
        # like the token in `out` — `_authorize` outlives the attempt so the
        # status endpoint can report the last outcome, and a client secret left
        # reachable for the life of the process is what turns a later crash
        # dump or an added diagnostic into a real credential leak. In `finally`
        # because every branch above must clear it, including the failures.
        job.client_id = job.client_secret = ""
    job.ok = job.error is None
    job.done.set()


def _start_authorize(bin_: str, name: str, provider: str,
                     replacing: bool = False, client_id: str = "",
                     client_secret: str = "") -> _ActiveAuthorize:
    """Spawn the authorize child and start its pumps + exit watcher.

    Raises OSError when the command can't start (caller maps it to a 502)."""
    backend = _OAUTH_PROVIDERS[provider]["backend"]
    env_extra = _client_credential_env(backend, client_id, client_secret)
    proc = _spawn_authorize(bin_, backend, env_extra)
    job = _ActiveAuthorize(name=name, provider=provider, backend=backend,
                           proc=proc, replacing=replacing,
                           client_id=client_id, client_secret=client_secret)
    pumps = [
        threading.Thread(target=_pump_authorize, args=(proc.stdout, job.out), daemon=True),
        threading.Thread(target=_pump_authorize, args=(proc.stderr, job.tail), daemon=True),
    ]
    for pump in pumps:
        pump.start()
    threading.Thread(target=_watch_authorize, args=(job, pumps), daemon=True).start()
    return job


def _cancel_active_authorize() -> bool:
    """Terminate the in-flight authorize child, if any; True when one was live.
    The watcher observes the death and records the cancellation, so the status
    endpoint reports it like any other outcome."""
    with _AUTH_LOCK:
        job = _authorize
    if job is None or job.done.is_set() or job.proc.poll() is not None:
        return False
    job.canceled = True
    job.proc.terminate()
    # Confirm the kill off the request path — the grace period is not the
    # client's to wait out, but the escalation must still happen.
    threading.Thread(target=_ensure_dead_child, args=(job.proc,), daemon=True).start()
    return True


@router.post("/api/mounts/remotes/oauth")
def start_remote_oauth(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Begin a browser sign-in: spawn `rclone authorize "<backend>"`, which opens
    the system browser. Returns as soon as the child is running; the client polls
    GET .../oauth/status for the outcome.

    `provider` selects the backend from _OAUTH_PROVIDERS and defaults to "drive"
    for backwards compatibility with the pre-registry client. `client_id` /
    `client_secret` are the user's own OAuth client — REQUIRED for Drive, and
    accepted-but-unnecessary for Dropbox/Box."""
    from fused_render.shell.mounts import rclone_bin
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    # Body validation (a 400) BEFORE probing for rclone (a 502) — the same
    # deliberate ordering as create_remote above: a bad name is bad on every
    # platform, and a host with no rclone would otherwise mask the real 400.
    name = (body.get("name") or "").strip()
    if not name or ":" in name or "/" in name:
        return JSONResponse({"error": "invalid remote name"}, status_code=400)
    provider = body.get("provider") or "drive"
    if not isinstance(provider, str) or provider not in _OAUTH_PROVIDERS:
        return JSONResponse(
            {"error": f"unknown storage provider {provider!r} — expected one of "
                      f"{', '.join(sorted(_OAUTH_PROVIDERS))}"},
            status_code=400)
    spec = _OAUTH_PROVIDERS[provider]
    client_id = body.get("client_id") or ""
    client_secret = body.get("client_secret") or ""
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        return JSONResponse(
            {"error": "'client_id' and 'client_secret' must be strings"},
            status_code=400)
    client_id, client_secret = client_id.strip(), client_secret.strip()
    # Drive without a client is not a request we can fulfil at all: rclone's
    # shared client ID is being retired, so refuse HERE rather than open a
    # browser onto a flow that is on a countdown. The message carries the
    # reason because the fix is work in the Google Cloud console, not a retry.
    if spec["needs_client"] and not (client_id and client_secret):
        return JSONResponse({"error": _CLIENT_REQUIRED_MSG}, status_code=400)
    # Strict bool, like add_mount's read_only: a truthy string from a sloppy
    # caller must not be able to authorize destroying an existing remote.
    replace = body.get("replace", False)
    if not isinstance(replace, bool):
        return JSONResponse({"error": "'replace' must be true or false"}, status_code=400)
    bin_ = rclone_bin()
    if not bin_:
        return JSONResponse({"error": "rclone is not installed"}, status_code=502)
    # config/create OVERWRITES a same-named remote, and re-signing in under the
    # name you already use is the natural thing to do — so the collision has to
    # be caught HERE. The page's own check is a snapshot from when the dialog
    # opened, which is stale for the whole sign-in window and absent entirely
    # for any non-UI caller. Refused before the browser opens, not after the
    # user has consented to something we then throw away.
    if not replace and any(r["name"] == f"{name}:"
                           for r in _rclone_state().get("remotes", [])):
        return JSONResponse(
            {"error": f"a remote named '{name}' already exists — pick another name, "
                      f"or confirm replacing it"},
            status_code=409)

    global _authorize
    with _AUTH_LOCK:
        if _authorize is not None and not _authorize.done.is_set():
            # Not joinable like account.py's login: rclone's callback server
            # binds 127.0.0.1:53682, so a second child could not even start, and
            # the caller is asking for a differently-named remote anyway.
            return JSONResponse(
                {"error": "a sign-in is already in progress — finish or cancel it first"},
                status_code=409)
        try:
            _authorize = _start_authorize(bin_, name, provider, replacing=replace,
                                          client_id=client_id,
                                          client_secret=client_secret)
        except OSError as e:
            return JSONResponse({"error": f"could not run rclone ({bin_}): {e}"},
                                status_code=502)
    return {"ok": True, "name": name, "provider": provider, "in_flight": True}


@router.get("/api/mounts/remotes/oauth/status")
def remote_oauth_status():
    """Progress of the last/current sign-in. A pure in-memory read with no side
    effects, so it stays an open GET like GET /api/mounts.

    `in_flight` false with `ok` false is the failure the client must surface —
    including the child that exited having produced no token at all (abandoned
    tab, denied consent, timeout), which is retryable and says so."""
    with _AUTH_LOCK:
        job = _authorize
    if job is None:
        return {"in_flight": False, "name": None, "provider": None,
                "backend": None, "ok": None, "error": None}
    in_flight = not job.done.is_set()
    return {
        "in_flight": in_flight,
        "name": job.name,
        "provider": job.provider,
        "backend": job.backend,
        "ok": None if in_flight else job.ok,
        "error": None if in_flight else job.error,
    }


@router.post("/api/mounts/remotes/oauth/cancel")
def cancel_remote_oauth(x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return {"ok": True, "canceled": _cancel_active_authorize()}


@router.post("/api/mounts/remotes/detect")
def create_detected_remote(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Materialize a keyless rclone remote from an auto-detected credential
    source (see _credential_suggestions). The spec comes from the server's own
    detection keyed by `id` — never from client-supplied rclone params — and
    env_auth=true means no keys are written. Idempotent: an already-created
    remote is returned as-is — but a detected (env_auth) one is re-probed
    first, since its creds may have expired since creation."""
    from fused_render.shell.mounts import _credential_suggestions, rclone_bin
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bin_ = rclone_bin()
    if not bin_:
        return JSONResponse({"error": "rclone is not installed"}, status_code=502)
    sid = (body.get("id") or "").strip()
    sugg = next((s for s in _credential_suggestions() if s["id"] == sid), None)
    if sugg is None:
        return JSONResponse({"error": f"unknown credential source {sid!r}"}, status_code=404)
    name = sugg["remote_name"]
    # Public (anonymous) remotes carry no credentials to go stale; only the
    # detected, env_auth-backed ones get the validity probe (an rclone `lsd`).
    detected = sugg.get("kind", "detected") == "detected"
    # remotes are {name,label} objects now — match on the bare rclone spec.
    if any(r["name"] == f"{name}:" for r in _rclone_state().get("remotes", [])):
        # Idempotent re-entry: the remote already exists. Don't report it
        # healthy on faith — a detected remote's creds may have expired since
        # it was created, and returning {"ok": True} here would invite a doomed
        # mount just as surely as a freshly created stale one. Re-probe (one
        # `lsd`) so an expired detected remote is never reported ok; anonymous
        # remotes carry nothing that expires and return quickly.
        if detected:
            cred_err = _detected_credential_error(bin_, name)
            if cred_err:
                return JSONResponse({"error": cred_err}, status_code=502)
        return {"ok": True, "name": name + ":"}
    cmd = [bin_, "config", "create", name, sugg["backend"]]
    for k, v in sugg["params"].items():
        cmd += [k, v]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "rclone config create timed out (30s)"}, status_code=502)
    if r.returncode != 0:
        return JSONResponse({"error": (r.stderr or r.stdout or "").strip()[-500:]}, status_code=502)
    # A detected remote whose creds turn out expired is rolled back so the
    # broken thing doesn't linger under Remotes inviting doomed mounts.
    if detected:
        cred_err = _detected_credential_error(bin_, name)
        if cred_err:
            # Roll back the just-created remote. If the delete itself fails
            # (non-zero exit or an OSError/timeout) the remote may still exist,
            # so say so rather than returning the bare cred error as if cleanup
            # succeeded — a silently-lingering remote would be reported ok on
            # the next detect and re-invite the doomed mount.
            try:
                d = subprocess.run([bin_, "config", "delete", name],
                                   capture_output=True, text=True, timeout=30)
                removed = d.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                removed = False
            if not removed:
                cred_err += (" (the half-created remote could not be removed "
                             "automatically — delete it manually before "
                             "retrying)")
            return JSONResponse({"error": cred_err}, status_code=502)
    _invalidate_upstream_caches()  # new/changed keys must be picked up without restart
    return {"ok": True, "name": name + ":"}
