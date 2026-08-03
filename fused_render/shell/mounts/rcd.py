"""The rclone rcd (remote-control daemon) process: spawn-or-reuse lifecycle,
the {port, pid, auth} registry in rcd.json, and the rc JSON-RPC client
(_rc/_rc_cancellable) every other module speaks to rcd through."""

import base64
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fused_render.shell import storage

logger = logging.getLogger(__name__)


def _rcd_state_path() -> str:
    return os.path.join(storage.home_dir(), "rcd.json")


_RCD_RC_USER = "fused-render"


def _rcd_child_env(auth: tuple[str, str]) -> dict:
    """The rcd child's environment: ours inherited, but with the whole
    RCLONE_RC_* namespace REPLACED rather than merged.

    rclone configures every flag from an env var named after it, so an
    inherited RCLONE_RC_* can reconfigure the very interface we are trying to
    lock down — and setting our own two keys on top of os.environ does not
    displace the others. Verified against rclone v1.74.4: an inherited
    RCLONE_RC_ALLOW_ORIGIN=* makes the daemon answer with
    `Access-Control-Allow-Origin: *` AND `Access-Control-Allow-Headers:
    Authorization`, which hands a foreign page the ability to READ replies —
    removing the read-blindness the loopback boundary otherwise leaves intact.
    RCLONE_RC_NO_AUTH=true and RCLONE_RC_USER_FROM_HEADER happened not to beat
    an explicit user/pass in that version, but that is version-dependent luck,
    not a property to build on.

    Nothing in this repo sets RCLONE_RC_*; the rc interface is entirely ours to
    configure, so the safe rule is that none of it comes from ambient env. The
    rest of RCLONE_* (RCLONE_CONFIG, RCLONE_CONFIG_PASS, ...) is legitimate
    user configuration for the remotes themselves and is inherited untouched.
    RCLONE_RC_NO_AUTH is pinned to "false" explicitly rather than merely
    dropped, so the intent survives a future default flip."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("RCLONE_RC_")}
    env["RCLONE_RC_USER"], env["RCLONE_RC_PASS"] = auth
    env["RCLONE_RC_NO_AUTH"] = "false"
    # The macOS nfsmount's NFS handle cache. MUST be set here in the environment
    # rather than on argv: --nfs-cache-type/--nfs-cache-dir are registered on the
    # `serve nfs`/`nfsmount` COMMAND flag sets, so `rcd` rejects them outright
    # ("unknown flag") and refuses to start. They are also a registered global
    # option block ("nfs"), and rclone derives an env var for every option, so
    # RCLONE_NFS_* reaches the daemon — verified against rclone v1.74.4 via
    # `rc options/get` reporting {"HandleCache":"disk", "HandleCacheDir": ...}.
    #
    # Why "disk" and not the "memory" default: go-nfs's in-memory CachingHandler
    # (helpers/cachinghandler.go, v0.0.4 — the version rclone v1.74.4 pins) scans
    # EVERY cached handle on every handle resolution, i.e. on essentially every
    # NFS RPC:
    #     if f, ok := c.activeHandles.Get(id); ok {
    #         for _, k := range c.activeHandles.Keys() {   // O(N)
    #             candidate, _ := c.activeHandles.Peek(k)
    #             if hasPrefix(f.p, candidate.p) { ... }
    # ToHandle mints one handle per directory entry and READDIRPLUS calls it per
    # entry, so any recursive walk of a mount (a global search across the home
    # dir, ripgrep, Spotlight, a flat million-key S3 prefix) drives N toward
    # --nfs-cache-handle-limit, default 1_000_000. The LRU has no TTL and only
    # shrinks by eviction at the limit, so the mount NEVER recovers — the
    # long-standing "one global search kills the mount, only a force-unmount
    # heals it" failure. Measured on a LOCAL 50k-file tree (so no S3 latency
    # involved), per-stat cost under "memory" grew linearly with handles minted:
    # 0.12ms at 0 -> 3.58ms at 50k (29x), and re-listing one unchanged 500-entry
    # directory went 80ms -> 1801ms. Under "disk" both stayed FLAT (0.12ms,
    # ~63ms) — rclone's own diskHandler.FromHandle is a sha256 of the path plus
    # one small file read, with no scan. Extrapolated to the 1M-handle limit,
    # "memory" reaches ~70ms per stat.
    #
    # Second benefit: disk handles are derived from the path, so they are stable
    # across a daemon restart. "memory" hands out random UUIDs, so restart_rcd
    # previously gave every still-connected NFS client stale handles.
    env["RCLONE_NFS_CACHE_TYPE"] = "disk"
    env["RCLONE_NFS_CACHE_DIR"] = _reset_nfs_handle_cache()
    return env


def _rcd_auth(port: int) -> tuple[str, str] | None:
    """The (user, pass) recorded for the daemon on `port`, or None when it has
    none on record (pre-auth daemon, or state not written yet).

    Checked against rcd.json first, then the central registry — the reaper
    probes daemons belonging to OTHER homes, whose rcd.json we cannot read.

    Deliberately NOT memoized. rcd is shared per-home and outlives us, so the
    daemon on a given port can be replaced — with a new secret — by another
    process at any time, and a cache keyed on the port alone would pin this
    process to the dead secret: every call 401s, _live_rcd_port reads that as
    "no daemon", and _ensure_rcd_locked spawns a second rcd that nothing owns
    (precisely the orphan the registry and reaper exist to clean up). Keying
    on (port, pid) like _live_port_cache would not help either — the pid comes
    out of the same file the credential does, so a correct lookup has to read
    it regardless.

    The read is free in practice: _live_rcd_port already reads this exact file
    unconditionally on every rc-routed call (its memo skips the ~3s core/pid
    probe, not the file), so the state file is page-cached and this adds no
    round trip. The file is the single source of truth for the daemon's
    identity AND its credential, which keeps the two from drifting apart."""
    state = storage.read_json(_rcd_state_path())
    if isinstance(state, dict) and state.get("port") == port and state.get("rc_pass"):
        return (state.get("rc_user") or _RCD_RC_USER, state["rc_pass"])
    reg = storage.read_json(_rcd_registry_path())
    if isinstance(reg, list):
        for e in reg:
            if isinstance(e, dict) and e.get("port") == port and e.get("rc_pass"):
                return (e.get("rc_user") or _RCD_RC_USER, e["rc_pass"])
    return None


def _rcd_registry_path() -> str:
    """Path to the central registry of every rcd this machine has spawned,
    one entry per home (state) dir.

    Unlike rcd.json — which lives INSIDE home_dir() and so vanishes with the
    dir when a pytest temp home is rmtree'd or a git worktree is deleted, taking
    the only record of that daemon's pid with it — the registry lives at the
    BASELINE home (never branch-nested). One registry is shared by the baseline
    run and every per-branch/worktree run, so reap_stale_rcd() on any run can
    still see, and reap, a daemon whose own (now-deleted) home would otherwise
    leave no trace. This is the ONLY place we learn the pid of a daemon whose
    home dir is already gone."""
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(base, "rcd-registry.json")


def _register_rcd(pid: int, port: int, auth: tuple[str, str] | None = None) -> None:
    """Record a freshly spawned daemon in the central registry, keyed by its
    home dir (a new daemon for the same home replaces the old record). Purely
    additive breadcrumb for reap_stale_rcd — a failure here must never fail a
    mount, so it's swallowed."""
    try:
        home = storage.home_dir()
        reg = storage.read_json(_rcd_registry_path())
        entries = [e for e in reg if isinstance(e, dict)] if isinstance(reg, list) else []
        entries = [e for e in entries if e.get("dir") != home]  # dedupe by home
        entry = {"pid": pid, "port": port, "dir": home}
        if auth:
            # So a later run can still authenticate core/pid against this
            # daemon after its home (and rcd.json) is gone — see _rcd_auth.
            entry["rc_user"], entry["rc_pass"] = auth
        entries.append(entry)
        storage.write_json(_rcd_registry_path(), entries)
    except OSError:
        logger.warning("rcd registry write failed", exc_info=True)


def _pid_alive(pid: int) -> bool:
    """True if `pid` names a running process."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) (OpenProcess) keeps succeeding for an exited process
        # whose handle is still held (e.g. the supervisor Job Object), so read
        # the exit code instead: 259 == STILL_ACTIVE means running.
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_PS_TIMEOUT_S = 3.0


def _pid_looks_like_rcd(pid: int) -> bool:
    """True only when pid's command line is recognisably an `rclone ... rcd`.

    A conservative identity guard: the reaper must NEVER signal a process that
    merely inherited a pid we once recorded (pids are recycled). Best-effort —
    any ps failure is treated as 'not confirmed', so we fail closed (don't
    kill on doubt)."""
    if not pid or pid <= 0:
        return False
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=_PS_TIMEOUT_S,
        ).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return "rclone" in out and "rcd" in out


_CONFIRM_RC_TIMEOUT_S = 3.0


def _confirmed_our_rcd(entry: dict) -> bool:
    """Proof that entry's pid IS the rclone rcd we recorded, gating a kill.
    Two independent checks, either suffices: (1) the recorded rc port still
    answers core/pid with the recorded pid; (2) the pid's command line is an
    rclone rcd. Both fail closed."""
    from fused_render.shell.mounts import _pid_looks_like_rcd, _rc
    pid = entry.get("pid") or 0
    port = entry.get("port")
    if port:
        try:
            if _rc(int(port), "core/pid",
                   timeout=_CONFIRM_RC_TIMEOUT_S).get("pid") == pid:
                return True
        except (RuntimeError, ValueError, TypeError):
            pass
    return _pid_looks_like_rcd(pid)


def reap_stale_rcd() -> None:
    """Kill rcd daemons that outlived the home/worktree that spawned them, and
    prune dead entries from the registry. Best-effort and deliberately
    CONSERVATIVE — the rcd is spawned detached and 'outlives the server on
    purpose', so nothing else ever reaps it; days-old orphans from finished
    pytest runs and deleted worktrees are the observed failure mode.

    An entry is only ever killed when BOTH hold:
      * its recorded home (state) dir no longer exists  -> orphaned, AND
      * the pid is still alive AND provably our rclone rcd (_confirmed_our_rcd).
    Then it gets a SIGTERM (rcd unmounts cleanly on SIGTERM) and its registry
    entry is dropped.

    Everything else is left as safe as possible:
      * home dir still present            -> assumed in use, untouched (this is
                                             also the daemon we're about to
                                             reuse/spawn);
      * pid already dead                  -> just drop the stale registry entry;
      * orphaned but NOT provably ours    -> left in the registry, never
                                             blind-killed, for a later run to
                                             reconsider.

    Wired into the (rare) spawn path of _ensure_rcd_locked, not a timer."""
    from fused_render.shell.mounts import _confirmed_our_rcd, _pid_alive
    reg = storage.read_json(_rcd_registry_path())
    if not isinstance(reg, list):
        return
    kept: list = []
    changed = False
    for e in reg:
        if not isinstance(e, dict):
            changed = True
            continue
        pid = e.get("pid") or 0
        home = e.get("dir")
        home_present = isinstance(home, str) and os.path.isdir(home)
        if home_present:
            kept.append(e)  # dir present -> in use, leave alone
            continue
        # home dir is gone -> candidate orphan
        if not _pid_alive(pid):
            changed = True  # already dead: drop the stale record
            continue
        if _confirmed_our_rcd(e):
            try:
                os.kill(pid, signal.SIGTERM)
                logger.info("reaped orphaned rcd pid=%s (home %s gone)", pid, home)
            except OSError:
                logger.warning("failed to signal orphaned rcd pid=%s", pid, exc_info=True)
            changed = True  # drop after signalling
        else:
            kept.append(e)  # alive but unidentifiable -> never blind-kill
    if changed:
        try:
            storage.write_json(_rcd_registry_path(), kept)
        except OSError:
            logger.warning("rcd registry prune failed", exc_info=True)


def _rcd_log_path() -> str:
    return os.path.join(storage.home_dir(), "rcd.log")


RCD_LOG_MAX_BYTES = 10 * 1024 * 1024


def _nfs_handle_cache_dir() -> str:
    return os.path.join(storage.home_dir(), "nfs-handle-cache")


def _reset_nfs_handle_cache() -> str:
    """Clear and return the directory backing rclone's on-disk NFS handle cache.

    The disk handler never evicts (one small file per path ever handled), so a
    long-lived home would otherwise accumulate them without bound. A fresh
    daemon can't honour handles it didn't mint anyway — the paths behind them
    are only meaningful to the VFS instance that is now gone — so the spawn is
    the right moment to reset it. Best-effort: rclone recreates the dir, and a
    stale cache is a disk-space nuisance, never a correctness problem."""
    d = _nfs_handle_cache_dir()
    try:
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    except OSError:
        logger.warning("could not reset the NFS handle cache dir %s", d, exc_info=True)
    return d


def _rotate_rcd_log() -> str:
    """Before spawning rcd, roll the log if it has grown past the cap:
    rcd.log -> rcd.log.1 (overwriting any previous .1). One generation is
    enough — this is diagnostic breadcrumbs, not an audit trail. Returns the
    (current) log path to hand to --log-file.

    INCIDENT 2026-07-16: rcd ran with NO --log-file, so when a read-only mount
    wedged under load there was zero rclone-side evidence to diagnose with (no
    record of the 403 PutObject loop). Best-effort: a stat/rename failure just
    means we append to whatever is there."""
    log = _rcd_log_path()
    try:
        if os.path.getsize(log) > RCD_LOG_MAX_BYTES:
            os.replace(log, log + ".1")
    except OSError:
        pass
    return log


def _copytruncate_rcd_log() -> None:
    """Enforce the log cap against a LIVE daemon (server startup path).

    _rotate_rcd_log's os.replace only rolls the file when THIS process spawns a
    new rcd, but the daemon is detached and outlives server restarts — so a
    long-lived rcd's log grows unbounded, its cap never re-checked. os.replace
    can't rotate under it either: rclone holds the inode open in append mode and
    would keep writing to the renamed file. Copytruncate instead — copy the
    current contents to rcd.log.1, then truncate the live file in place (its fd
    keeps appending past offset 0, which is safe for O_APPEND writers). Fully
    best-effort: any failure (missing file, permissions) must never block
    startup, so it's swallowed."""
    log = _rcd_log_path()
    try:
        if os.path.getsize(log) <= RCD_LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        with open(log, "r+b") as f:
            data = f.read()
            with open(log + ".1", "wb") as backup:
                backup.write(data)
            f.seek(0)
            f.truncate(0)
    except OSError:
        logger.warning("rcd log copytruncate failed", exc_info=True)


def write_rcd_state(port: int, pid: int, log_path: str | None = None,
                    auth: tuple[str, str] | None = None) -> None:
    # Record the log path alongside port/pid so tooling (and a human tailing
    # the daemon) can find it without reconstructing home_dir() (INCIDENT).
    # spawner_pid records WHO spawned the daemon: rcd is shared per-home, so a
    # later process reusing it (e.g. the macOS app alongside a CLI server) must
    # be able to tell on quit whether the daemon is its own to stop — see
    # stop_local_rcd's ownership gate.
    # rc_user/rc_pass: the daemon's basic-auth secret, so any later process (or
    # a restarted server) reusing this shared daemon can still call it.
    from fused_render.shell.mounts import _register_rcd
    state = {
        "port": port,
        "pid": pid,
        "log": log_path or _rcd_log_path(),
        "spawner_pid": os.getpid(),
    }
    if auth:
        state["rc_user"], state["rc_pass"] = auth
    storage.write_json(_rcd_state_path(), state)
    # Also record in the central registry so a future run can reap this daemon
    # even after its home dir (and this rcd.json) is deleted (INCIDENT: leaked
    # rcd daemons outliving pytest runs / deleted worktrees for days).
    _register_rcd(pid, port, auth)


def _rc(port: int, method: str, params: dict | None = None, timeout: float = 30,
        auth: tuple[str, str] | None = None):
    """One rc call. Returns the decoded JSON on 200; raises RuntimeError with
    rclone's error message on any failure.

    `auth` is the daemon's basic-auth credential; when omitted it is looked up
    from the recorded state (_rcd_auth). The spawn path passes it explicitly
    because it calls core/pid before the state is written."""
    raw = json.dumps(params or {}).encode()
    headers = {"Content-Type": "application/json"}
    creds = auth or _rcd_auth(port)
    if creds:
        token = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{method}",
        data=raw,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("error", "")
        except ValueError:
            detail = ""
        raise RuntimeError(detail or f"rclone rc {method}: HTTP {e.code}") from e
    except OSError as e:
        raise RuntimeError(f"rclone rc {method}: {e}") from e


_RC_JOB_POLL_S = 0.05


def _rc_cancellable(port: int, method: str, params: dict | None = None,
                    timeout: float = 30):
    """Like _rc, but runs `method` as a CANCELLABLE rclone job so a timed-out
    call actually stops rclone's server-side work instead of orphaning it.

    Why this exists (the 14h-runaway INCIDENT): operations/list and
    operations/stat make rclone run an UNBOUNDED ListObjectsV2 over the whole
    prefix (see rc_list_dir / _rc_stat_item). A plain urlopen socket timeout
    only abandons the CLIENT socket — rclone KEEPS enumerating, and repeated
    timed-out calls pile up orphaned walks that pinned a CPU for 14h. Submitting
    with `_async=true` returns a {"jobid": N} immediately; we poll job/status
    until the job finishes or the deadline passes, and on timeout call job/stop
    so rclone's context cancellation propagates into the S3 lister and the walk
    STOPS.

    Returns the job's `output` dict — the SAME shape the synchronous _rc call
    returns for this method (operations/list -> {"list": [...]}, operations/stat
    -> {"item": ...}). Raises RuntimeError exactly where _rc would, so callers
    keep their existing except handling:
      - deadline exceeded -> raised FROM a TimeoutError, so _rc_timed_out()
        recognizes it (rc_list_dir maps it to RcListTimeout; _rc_stat_item to
        the indeterminate sentinel);
      - a failed job      -> raised with rclone's own error message and NO
        timeout cause (rc_list_dir maps it to RcListError, same as the sync
        HTTPError path)."""
    from fused_render.shell.mounts import _rc
    deadline = time.monotonic() + timeout
    p = dict(params or {})
    p["_async"] = True
    # Submitting a job returns at once (only the enumeration is slow), so cap the
    # submit round trip modestly rather than granting it the whole budget.
    submit = _rc(port, method, p, timeout=min(timeout, 10))
    jobid = submit.get("jobid") if isinstance(submit, dict) else None
    if jobid is None:
        # No jobid handed back. If the peer IGNORED _async and ran the command
        # synchronously, `submit` already holds the full payload (operations/list
        # -> {"list": [...]}, operations/stat -> {"item": ...}) — return it rather
        # than re-issuing the same unbounded enumeration a second time. Only a
        # truly empty/absent ack falls back to a fresh sync call on the remaining
        # budget, so behavior still degrades to the old path when there's nothing
        # to reuse.
        if isinstance(submit, dict) and submit:
            return submit
        remaining = deadline - time.monotonic()
        return _rc(port, method, params, timeout=max(remaining, 0.1))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            status = _rc(port, "job/status", {"jobid": jobid},
                         timeout=min(remaining, 10))
        except RuntimeError:
            # Polling itself failed — common near the deadline when the budget
            # (min(remaining, 10)) is tiny, or when a busy rcd is slow to answer.
            # Do NOT let it propagate uncancelled: break out and stop the job
            # below so the in-flight enumeration can't outlive us (the INCIDENT).
            logger.warning("rc job/status failed for job %s; cancelling",
                           jobid, exc_info=True)
            break
        if isinstance(status, dict) and status.get("finished"):
            if status.get("error"):
                raise RuntimeError(status["error"])  # sync error path equivalent
            out = status.get("output")
            return out if isinstance(out, dict) else {}
        time.sleep(min(_RC_JOB_POLL_S, max(deadline - time.monotonic(), 0)))
    # Deadline passed (or polling failed) with the job still running: cancel it
    # server-side so rclone stops enumerating (the whole point), then raise a
    # timeout the callers recognize. job/stop failing is non-fatal — we still raise.
    try:
        _rc(port, "job/stop", {"jobid": jobid}, timeout=3)
    except RuntimeError:
        logger.warning("rc job/stop failed for job %s", jobid, exc_info=True)
    raise RuntimeError(
        f"rc {method} timed out after {timeout:g}s (job {jobid} cancelled)"
    ) from TimeoutError()


# Timeout of the core/pid probe _live_rcd_port makes. Named because it is not
# only a latency knob: it is a term in RCD_REAP_WORST_CASE_S below, since the kill
# poll can enter one probe just under its deadline.
_LIVE_PORT_PROBE_TIMEOUT_S = 3.0


_LIVE_PORT_TTL_S = 1.0


_DEAD_PORT_TTL_S = 5.0


_live_port_lock = threading.Lock()


_live_port_cache: tuple | None = None  # ((port, pid), port|None, monotonic expiry)


def _live_rcd_port(*, trust_dead_cache: bool = True) -> int | None:
    """The recorded daemon's port iff it answers core/pid; never spawns.
    Memoized per recorded (port, pid) — _LIVE_PORT_TTL_S for a live answer,
    _DEAD_PORT_TTL_S for a failed probe — so neither a walk over many
    directories nor a UI polling status re-probes core/pid per call.
    trust_dead_cache=False skips the cached miss and re-probes: the spawn path
    must not start a duplicate daemon over a live one that merely had a probe
    time out."""
    from fused_render.shell import mounts as _mounts_pkg
    from fused_render.shell.mounts import _rc
    state = storage.read_json(_rcd_state_path())
    if not isinstance(state, dict) or not state.get("port"):
        return None
    key = (state.get("port"), state.get("pid"))
    now = time.monotonic()
    with _live_port_lock:
        c = _mounts_pkg._live_port_cache
        if c is not None and c[0] == key and c[2] > now:
            if c[1] is not None or trust_dead_cache:
                return c[1]
    try:
        _rc(state["port"], "core/pid", timeout=_LIVE_PORT_PROBE_TIMEOUT_S)
    except RuntimeError:
        with _live_port_lock:
            c = _mounts_pkg._live_port_cache
            # A concurrent probe may have cached a live hit while ours timed
            # out — trust it rather than blacking out a healthy daemon for
            # the whole dead TTL.
            if (c is not None and c[0] == key and c[1] is not None
                    and c[2] > time.monotonic()):
                return c[1]
            _mounts_pkg._live_port_cache = (key, None, now + _DEAD_PORT_TTL_S)
        return None
    with _live_port_lock:
        _mounts_pkg._live_port_cache = (key, state["port"], now + _LIVE_PORT_TTL_S)
    return state["port"]


def rclone_bin() -> str | None:
    """Path to the rclone binary to run.

    Resolution order:
    1. An explicit FUSED_RENDER_RCLONE_BIN pointing at a real file. The
       supervisor's child_environment sets this in packaged builds (Windows
       installer, Linux AppImage) to the rclone bundled in the payload — env
       beats path-guessing per platform, so mounts work with zero user setup.
       A stale/wrong override (not a file) is ignored so it can't shadow a real
       rclone in a dev checkout.
    2. The packaged macOS app bundle (py2app sets sys.frozen = "macosx_app",
       same check as deploy.py's _setup_cli_hint): rclone at
       Contents/Resources/bin/rclone (D103, build_dmg.sh).
    3. The system rclone on PATH (dev checkout, or a host that installed it)."""
    override = os.environ.get("FUSED_RENDER_RCLONE_BIN")
    if override and os.path.isfile(override):
        return override
    if getattr(sys, "frozen", None) == "macosx_app":
        contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        bundled = os.path.join(contents, "Resources", "bin", "rclone")
        if os.path.isfile(bundled):
            return bundled
    return shutil.which("rclone")


WINFSP_DOWNLOAD_URL = "https://winfsp.dev/rel/"


def _winfsp_missing_error() -> str:
    """The friendly 'install WinFsp' message shared by every mount path that
    can't proceed without the driver (attach's creation path, reconnect)."""
    return ("Windows mounts require WinFsp, which isn't installed. Install "
            f"it from {WINFSP_DOWNLOAD_URL} and try again. (The FusedRender "
            "installer offers it during setup — see DECISIONS.md.)")


def _winfsp_available() -> bool:
    """Whether WinFsp — rclone's Windows mount backend — is installed.

    WinFsp is a kernel-mode driver the Windows installer chain-installs via its
    bundled MSI (D133), but the user can decline that elevation step (or run an
    older install), so mounts must still detect and explain a missing driver.
    Non-win32 platforms don't use WinFsp, so this is vacuously True there; on
    Windows we look for the system DLL WinFsp installs under
    %ProgramFiles(x86)%\\WinFsp\\bin — winfsp-x64.dll on x64, winfsp-a64.dll on
    ARM64 — falling back to a loader lookup for either. Best-effort: a False
    here only downgrades the mount attempt into a friendly install prompt,
    never a crash."""
    if sys.platform != "win32":
        return True
    dll_names = ("winfsp-x64.dll", "winfsp-a64.dll")  # x64 and ARM64 builds
    for env in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        base = os.environ.get(env)
        if not base:
            continue
        if any(os.path.isfile(os.path.join(base, "WinFsp", "bin", dll))
               for dll in dll_names):
            return True
    import ctypes.util
    return any(ctypes.util.find_library(n) is not None
               for n in ("winfsp-x64", "winfsp-a64"))


def _rclone_should_persist() -> bool:
    return os.environ.get("FUSED_RENDER_RCLONE_PERSIST") not in (None, "", "0")


_rcd_lock = threading.Lock()


def ensure_rcd() -> int:
    """Port of a live rcd daemon, spawning one (detached) if none answers.
    Raises RuntimeError when rclone is not installed or the daemon won't come
    up."""
    from fused_render.shell.mounts import _ensure_rcd_locked
    with _rcd_lock:
        return _ensure_rcd_locked()


def _ensure_rcd_locked() -> int:
    from fused_render.shell.mounts import (
        _live_rcd_port,
        _rc,
        _rclone_should_persist,
        _rotate_rcd_log,
        rclone_bin,
        reap_stale_rcd,
        write_rcd_state,
    )
    port = _live_rcd_port(trust_dead_cache=False)
    if port is not None:
        return port
    # About to spawn a fresh daemon — a natural, rare moment to opportunistically
    # reap any rcd that outlived a deleted home/worktree (best-effort, never
    # blocks the spawn; NOT on a timer). The hot reuse path above skips this.
    try:
        reap_stale_rcd()
    except Exception:
        logger.warning("reap_stale_rcd failed", exc_info=True)
    bin_ = rclone_bin()
    if not bin_:
        raise RuntimeError("rclone is not installed")
    # Pick the port ourselves (parsing rcd's stderr for a :0 bind is brittle).
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # --use-server-modtime: take an object's mtime from the store's LIST
    # response (S3 LastModified etc.) instead of rclone's default, which HEADs
    # every object to read its precise metadata mtime. That per-object HEAD is
    # what makes directory listings — hence recursive search — crawl over a
    # mount: measured ~300ms/object, turning a 264-file sentinel-cogs subtree
    # into a ~78s walk vs ~1.5s with the LIST mtime. We don't need upload-time
    # precision to browse, so trade it for the 50x faster listing.
    # --log-file/--log-level: give the detached daemon a durable log so a mount
    # that wedges under load leaves rclone-side evidence (INCIDENT 2026-07-16 —
    # the daemon had none, so the read-only PutObject 403 loop was invisible).
    # Rotate first since rclone won't cap the file itself. stdout/stderr stay
    # DEVNULL: --log-file captures everything, and a detached daemon has no
    # console to write to anyway.
    log_path = _rotate_rcd_log()
    # The rc API requires basic auth (see the _rcd_auth block above): a random
    # per-daemon secret, handed over in the ENVIRONMENT rather than on argv so
    # it is not visible in `ps` to other local users. Without it the daemon is
    # an unauthenticated filesystem API that any page in the user's browser can
    # drive blind with a no-preflight cross-origin POST. _rcd_child_env also
    # clears the inherited RCLONE_RC_* namespace, which could otherwise
    # reconfigure the interface out from under us.
    auth = (_RCD_RC_USER, secrets.token_urlsafe(32))
    subprocess.Popen(
        [bin_, "rcd", "--use-server-modtime",
         f"--rc-addr=127.0.0.1:{port}",
         f"--log-file={log_path}", "--log-level", "INFO"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_rcd_child_env(auth),
        # Dev (FUSED_RENDER_RCLONE_PERSIST set): setsid into its own session so
        # the daemon outlives watchfiles server restarts. Production (unset):
        # stay a normal child so app teardown reaps it (on Linux via the
        # server's process-group killpg; on Windows it stays in the supervisor's
        # Job either way; on macOS app.py SIGTERMs it explicitly on quit).
        start_new_session=_rclone_should_persist(),
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            pid = _rc(port, "core/pid", timeout=2, auth=auth).get("pid", 0)
            write_rcd_state(port, pid, log_path, auth)
            return port
        except RuntimeError:
            time.sleep(0.2)
    raise RuntimeError("rclone rcd did not come up within 10s")


_KILL_TIMEOUT_S = 5.0


# Worst-case wall time of one _kill_current_rcd, DERIVED from the bounds it is
# built out of: the identity proof (an rc core/pid that times out, then a `ps`
# that times out — _confirmed_our_rcd tries both before it will signal anything)
# plus each exit poll AND the one _live_rcd_port probe that poll can overrun by.
# That last term is easy to miss and was: the loops test the clock BEFORE an
# iteration, so an iteration entered just under the deadline still runs a full
# probe timeout past it. Counting it per phase is the true upper bound (the
# probe's dead-answer cache, _DEAD_PORT_TTL_S, is longer than a phase, so a phase
# cannot pay for more than one).
#
# Exported because the caller that has to outlast this — app.py's quit deadline —
# must not restate the arithmetic: tightening any constant above has to move that
# deadline with it. Excludes the wait for _rcd_lock, which a concurrent spawn can
# hold for its own 10s; a quit racing a mount attach is not a case worth padding
# every deadline for, and the hard deadline terminates anyway.
RCD_REAP_WORST_CASE_S = (
    _CONFIRM_RC_TIMEOUT_S + _PS_TIMEOUT_S
    + 2 * (_KILL_TIMEOUT_S + _LIVE_PORT_PROBE_TIMEOUT_S))


def _kill_current_rcd() -> None:
    """Terminate the recorded rcd daemon, if there is one to terminate.

    Safety invariant (the single most important constraint here): only ever
    signal a pid we can PROVE is our rclone rcd. Reuses the exact gates
    reap_stale_rcd trusts — _pid_alive + _confirmed_our_rcd (which itself folds
    in the rc core/pid check and _pid_looks_like_rcd). A recorded pid that is
    alive but NOT confirmed ours raises rather than risk killing an unrelated
    process that inherited a recycled pid.

    No recorded daemon / a dead pid is a clean no-op: the caller's fresh spawn
    just starts one. SIGTERM first, escalating to SIGKILL only if it won't exit
    within _KILL_TIMEOUT_S; we poll until the daemon's port stops answering AND
    the pid is gone.

    This does NOT unmount anything, and must not be trusted to: rclone's own
    SIGTERM handler issues a PLAIN `umount`, which a busy macOS nfsmount rejects
    (the state _force_unmount exists for), and the SIGKILL escalation skips even
    that. Killing rcd while a kernel mount it serves is still attached is what
    produced the "server connection interrupted / disks not ejected properly"
    alerts on quit (INCIDENT 2026-07-29). Callers that own the mounts must run
    the unmount ladder FIRST — see lifecycle.unmount_all_for_quit."""
    from fused_render.shell.mounts import _confirmed_our_rcd, _live_rcd_port, _pid_alive
    entry = storage.read_json(_rcd_state_path())
    if not isinstance(entry, dict):
        return  # no daemon on record — nothing to kill
    pid = entry.get("pid") or 0
    if not _pid_alive(pid):
        return  # already gone; the stale rcd.json is harmless (spawn overwrites)
    if not _confirmed_our_rcd(entry):
        # Alive but unprovable: pids get recycled, so this could be anything.
        # Fail loud instead of blind-killing (the critical safety invariant).
        raise RuntimeError(
            f"refusing to kill pid {pid}: not confirmed to be our rclone rcd"
        )
    # SIGKILL is absent on Windows; SIGTERM there maps to TerminateProcess.
    sigs = (signal.SIGTERM,) if sys.platform == "win32" else (signal.SIGTERM, signal.SIGKILL)
    for sig in sigs:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return  # raced us and exited between the check and the signal
        except OSError as e:
            # Windows signals the same race as a bare OSError, not ProcessLookupError.
            if not _pid_alive(pid):
                return
            raise RuntimeError(f"failed to signal rcd pid {pid}: {e}") from e
        deadline = time.time() + _KILL_TIMEOUT_S
        while time.time() < deadline:
            # _pid_alive FIRST, and the short-circuit is the point: it is a free
            # syscall and the authoritative "our daemon is gone", while
            # _live_rcd_port makes a rc probe with its own multi-second timeout.
            # Probing first ran that probe throughout the wait (dead-cached, but
            # re-probed every _DEAD_PORT_TTL_S and able to overrun this deadline),
            # which both slowed every reap and inflated the quit deadline that has
            # to outlast it. The conjunction is unchanged — the port must also
            # stop answering before we call the daemon gone.
            if not _pid_alive(pid) and _live_rcd_port() is None:
                return  # daemon gone
            time.sleep(0.1)
    raise RuntimeError(f"rcd pid {pid} did not exit after {sigs[-1].name}")


def _rcd_is_ours_to_reap() -> bool:
    """Whether THIS process may tear the recorded rcd down on quit — and, by
    extension, the mounts it serves (unmount_all_for_quit asks the same
    question: unmounting a daemon's mounts is as much a teardown of it as
    signalling it, so the two rungs must never disagree about ownership).

    Two gates, both "leave it alone":

      persistence — FUSED_RENDER_RCLONE_PERSIST (dev, set by dev.sh) means the
      detached daemon is DELIBERATELY meant to outlive this process, mounts and
      warm VFS cache included, so a fresh server re-adopts them.

      ownership — rcd is shared per-home, so the daemon on record may have been
      spawned by ANOTHER process still using it (e.g. the app quitting while a
      CLI `fused-render` server keeps serving mounts). A recorded spawner_pid
      that is not us and is still alive is the spawner's to reap. A missing
      spawner_pid (an rcd.json written before the field existed) preserves the
      old behavior and reaps.

    Does NOT take _rcd_lock — callers under the lock (stop_local_rcd) would
    deadlock on a re-entrant acquire; this only reads rcd.json."""
    from fused_render.shell.mounts import _pid_alive, _rclone_should_persist
    if _rclone_should_persist():
        return False
    entry = storage.read_json(_rcd_state_path())
    if isinstance(entry, dict):
        spawner_pid = entry.get("spawner_pid") or 0
        if spawner_pid and spawner_pid != os.getpid() and _pid_alive(spawner_pid):
            logger.info(
                "rcd was spawned by pid %s which is still alive; leaving the "
                "shared daemon (and its mounts) to its owner",
                spawner_pid,
            )
            return False
    return True


def stop_local_rcd() -> None:
    """Best-effort teardown of the rcd we spawned, for the app's quit path.

    Only needed where nothing else reaps rcd on quit — notably macOS, which has
    no supervisor tree-kill (the server runs in-process; app.py, a rumps app).
    On Linux/Windows the process-group killpg / Job Object already collect a
    non-detached rcd, so this is redundant there but harmless.

    Gated on _rcd_is_ours_to_reap (persistence + shared-daemon ownership).
    Reuses _kill_current_rcd's safety gates (only ever signals a pid PROVEN to
    be our rclone rcd) and swallows every error — a reap failure must never
    block app quit.

    Reaps ONLY: it does not unmount, and rcd's own SIGTERM unmount cannot be
    relied on (see _kill_current_rcd). The caller must have run
    unmount_all_for_quit first, or the kernel mounts outlive their NFS server
    and macOS raises the "disks not ejected properly" alerts."""
    from fused_render.shell.mounts import _kill_current_rcd, _rcd_is_ours_to_reap
    with _rcd_lock:
        if not _rcd_is_ours_to_reap():
            return
        try:
            _kill_current_rcd()
        except Exception:
            logger.warning("stop_local_rcd: rcd teardown failed", exc_info=True)


def rcd_mount_map() -> dict:
    """{mountpoint: remote fs} for every mount rcd currently serves (empty
    when no daemon is live). Read-only: never spawns a daemon just to answer
    a status question."""
    from fused_render.shell.mounts import _live_rcd_port, _rc
    port = _live_rcd_port()
    if port is None:
        return {}
    try:
        listed = _rc(port, "mount/listmounts").get("mountPoints", [])
    except RuntimeError:
        return {}
    # normpath the keys so membership tests against mountpoint() compare like
    # with like whatever separator form rclone reports.
    return {os.path.normpath(m["MountPoint"]): m.get("Fs")
            for m in listed if isinstance(m, dict) and m.get("MountPoint")}


def mounted_paths() -> set:
    return set(rcd_mount_map())
