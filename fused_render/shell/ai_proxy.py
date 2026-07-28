"""Bundled AI proxy — supervises a local CLIProxyAPI instance for fused.ai().

Follow-on to fused.ai() (SPEC RH-11), which relays to an OpenAI-compatible
proxy the user was expected to install and run themselves. This module ships
that proxy inside the app instead: it resolves the binary, spawns it lazily
on first use, and tears it down — the exact shape of shell/mounts.py's rcd
half (`rclone_bin()` / `ensure_rcd()` / `stop_local_rcd()`), because
CLIProxyAPI is, like rclone, a single static Go binary we bundle and drive
over a local HTTP API. See docs/AI_PROXY_BUNDLING.md for the design and
docs/AI_PROXY_MANAGEMENT_API.md for the wire contract this was verified
against (CLIProxyAPI v7.2.90 — pin hard, degrade gracefully on version skew).

Sync vs. async: every function here is a plain blocking call, not a
coroutine — the same choice mounts.py made for `ensure_rcd()`/`_rc()`, and
the one this codebase already has a house idiom for (shell/prefetch.py:
"Blocking — run via asyncio.to_thread"; server.py calls sync shell helpers
from async routes via `await asyncio.to_thread(...)` throughout, e.g.
shell_mounts.bearer_upstream_for). Spawning a process and health-polling it
for up to ~15s is exactly the kind of blocking work that pattern exists for;
making this module's functions `async def` would just push the same
event-loop-blocking problem into the network calls it makes, so the caller
in server.py (owned by another worker) is expected to hop this off the loop
with `asyncio.to_thread` rather than this module reinventing an async
transport on top of plain HTTP.

Security posture, mirroring mounts.py's rcd auth block: the proxy is bound
to 127.0.0.1 only (never the upstream default of all interfaces) and every
call — chat completions and management alike — requires a random per-launch
secret we mint ourselves, because loopback is not a boundary against the
browser: any page the user has open could otherwise drive an unauthenticated
local API. Two secrets, two blast radii: `api_key` gates the OpenAI-compatible
surface fused.ai() relays through; `management_key` gates account
management (listing/deleting credentials, OAuth login) — a strictly more
sensitive surface, kept on its own key so the relay's routine traffic never
carries a credential that can delete someone's login.
"""
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from fused_render.shell import prefs, storage

logger = logging.getLogger(__name__)

BIN_NAME = "cli-proxy-api.exe" if sys.platform == "win32" else "cli-proxy-api"

# How long ensure_ai_proxy() waits for a freshly spawned instance to answer
# an authenticated /v1/models before giving up — generous relative to
# rclone's 10s in ensure_rcd(), since this is a bigger Go binary doing OAuth
# client setup on top of an HTTP server, and it only pays this cost once per
# app session (lazy spawn, then reused for every later fused.ai() call).
_STARTUP_TIMEOUT_S = 15.0

# rclone's rcd has no built-in log rotation and neither does this binary in
# our config (logging-to-file: false — see _write_config); cap our own
# stdout/stderr capture the same way _rotate_rcd_log does.
LOG_MAX_BYTES = 10 * 1024 * 1024

# Spawn-or-reuse must be serialized: fused.ai() is called from async request
# handlers, and two concurrent calls racing "no live instance" would each
# spawn their own proxy, with the loser's orphaned and its state clobbered —
# the same race _rcd_lock exists to close for rclone's rcd.
_lock = threading.Lock()

# The Popen handle for an instance THIS process spawned, kept solely so the
# child can be reaped after we signal it (see _reap_child). None when the live
# instance was spawned by some earlier process — that case needs no reaping,
# since a child is only ever a zombie to its own parent.
_child: subprocess.Popen | None = None


def _state_dir() -> str:
    return os.path.join(storage.home_dir(), "ai-proxy")


def _config_path() -> str:
    return os.path.join(_state_dir(), "config.yaml")


def _auth_dir() -> str:
    return os.path.join(_state_dir(), "auths")


def _log_path() -> str:
    return os.path.join(_state_dir(), "proxy.log")


def _state_path() -> str:
    return os.path.join(storage.home_dir(), "ai_proxy.json")


def ai_proxy_bin() -> str | None:
    """Path to the CLIProxyAPI binary to run.

    Resolution order, exactly rclone_bin()'s shape:
    1. An explicit FUSED_RENDER_AI_PROXY_BIN pointing at a real file. Set by
       the supervisor in packaged Windows/Linux builds to the binary staged
       in the payload. A stale/wrong override (not a file) is ignored so it
       can't shadow a working binary in a dev checkout.
    2. The packaged macOS app bundle (py2app sets sys.frozen = "macosx_app"):
       Contents/Resources/bin/cli-proxy-api, staged the same way rclone is.
    3. The system binary on PATH — keeps a dev checkout against a homebrew
       install working, and is what a user's own separately-installed proxy
       would already satisfy (though see is_supervised(): that case is
       normally reached via an explicit ai_base_url instead, not this tier).
    """
    override = os.environ.get("FUSED_RENDER_AI_PROXY_BIN")
    if override and os.path.isfile(override):
        return override
    if getattr(sys, "frozen", None) == "macosx_app":
        contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        bundled = os.path.join(contents, "Resources", "bin", BIN_NAME)
        if os.path.isfile(bundled):
            return bundled
    return shutil.which(BIN_NAME)


def is_supervised() -> bool:
    """False when the user has explicitly pointed ai_base_url somewhere of
    their own choosing — in which case we supervise nothing and the relay
    behaves exactly as it does today (talks straight to that base URL, no
    generated credentials involved). True (the default) means no explicit
    override exists, so the relay should route through ensure_ai_proxy().

    Deliberately keyed on *explicit* configuration (the env var, or the pref
    key being present with a non-empty value) rather than on ai_base_url()'s
    return value equaling some particular string: prefs.DEFAULT_AI_BASE_URL
    (127.0.0.1:8317) is just what an UNSET pref happens to resolve to — a
    user-run CLIProxyAPI's conventional default port — not a signal that the
    user asked for anything. Our bundled instance runs on an unrelated
    ephemeral port picked at spawn time, so "the pref is unset" must still
    mean "supervise it ourselves," not "the user chose 8317."
    """
    if os.environ.get("FUSED_RENDER_AI_BASE_URL"):
        return False
    value = prefs.read_prefs().get("ai_base_url")
    return not (isinstance(value, str) and value)


def _should_persist() -> bool:
    """Whether a freshly spawned proxy should detach (setsid) and outlive
    this process, or stay a normal child that dies with it.

    Own env var (FUSED_RENDER_AI_PROXY_PERSIST), not rclone's — this is a
    separate daemon with a separate lifecycle, and coupling the two would
    mean a dev-mode rclone convenience silently also changed how the AI
    proxy tears down. Same dev-iteration rationale as mounts.py's
    _rclone_should_persist: OFF by default (production: quitting the app
    kills it), ON only when a dev script opts in (skip the respawn +
    re-login cost across watchfiles restarts)."""
    return os.environ.get("FUSED_RENDER_AI_PROXY_PERSIST") not in (None, "", "0")


def _reap_child(pid: int) -> None:
    """Collect the exit status of a child THIS process spawned, so a killed
    instance stops being a zombie.

    Without this, os.kill(pid, 0) keeps succeeding for a dead-but-unreaped
    child — a zombie is still a process table entry — so _pid_alive reports
    it alive forever and _kill_current_ai_proxy escalates SIGTERM to SIGKILL
    and then raises "did not exit", for a process that in fact died on the
    first signal. Measured: a 10s stall plus a bogus error on every quit.

    Only the spawning process can reap, hence the pid guard: a stale handle
    from a different instance must not be waited on. Non-blocking-ish by
    design (the child is already signalled, so the wait returns at once) with
    a bounded timeout so a wedged child can never hang app teardown."""
    global _child
    proc = _child
    if proc is None or proc.pid != pid:
        return
    try:
        proc.wait(timeout=_KILL_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return  # genuinely still running, or already reaped elsewhere
    _child = None


def _pid_alive(pid: int) -> bool:
    """True if `pid` names a running process.

    Adapted from mounts._pid_alive rather than imported (this module stays
    decoupled from rclone's daemon internals, and the check is generic OS
    plumbing). One deliberate difference: on POSIX a zombie — a child that
    has exited but whose status nobody collected — still answers
    os.kill(pid, 0), so this would call a dead process alive. Callers that
    signal a child of ours reap it first via _reap_child; see the note there
    for the 10s-stall-and-false-error this closes."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_looks_like_our_proxy(pid: int, config_path: str) -> bool:
    """True only when pid's command line names OUR config file.

    Unlike mounts._pid_looks_like_rcd (which matches on "rclone"+"rcd",
    since any such process IS the daemon we care about), a generic
    "cli-proxy-api" substring match isn't a strong enough identity proof
    here: a user could independently be running their own CLIProxyAPI
    instance on the same machine, and we must never signal that one. The
    exact -config path we generated is unique to this instance, so matching
    on it is the actual ownership proof — the same role _confirmed_our_rcd's
    core/pid-vs-recorded-pid check plays for rclone, adapted to a binary
    whose HTTP API has no equivalent "give me my own pid" call.
    Best-effort and fails closed: any `ps` failure (including "no `ps` on
    this platform") reads as unconfirmed, never as confirmed."""
    if not pid or pid <= 0 or not config_path:
        return False
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return config_path in out


def _probe_models(port: int, api_key: str, timeout: float = 2.0) -> bool:
    """True iff the proxy on `port` answers GET /v1/models with `api_key`.

    Used both for the post-spawn health-poll and to decide whether a
    recorded instance is still the one to reuse. Never raises — a probe
    failure just means "not up yet" or "not this one any more"; callers
    treat that as "go spawn a fresh one," not as an error in its own right."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False
    except urllib.error.HTTPError:
        return False


def _confirmed_ours(entry: dict) -> bool:
    """Proof entry's pid IS the ai-proxy we spawned, gating a kill — the
    same two-of-either-suffices shape as mounts._confirmed_our_rcd: (1) the
    recorded port still answers /v1/models with the recorded api key, or (2)
    the pid's command line names our own config file. Both fail closed."""
    pid = entry.get("pid") or 0
    port = entry.get("port")
    api_key = entry.get("api_key")
    if port and api_key and _probe_models(int(port), api_key):
        return True
    return _pid_looks_like_our_proxy(pid, entry.get("config") or "")


def _yaml_str(value: str) -> str:
    """Escape a value for a double-quoted YAML scalar.

    No yaml dependency: pyyaml is not in pyproject.toml's core deps, the
    config shape we emit is fixed and entirely our own construction, and the
    only variable inputs are our own state-dir paths and secrets.token_urlsafe
    output (already backslash/quote-free) — so this only needs to handle
    backslashes and double quotes, not the general YAML grammar, and adding
    a real YAML writer for that would be all cost, no benefit."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _preserved_api_key_blocks() -> str:
    """The provider API-key sections of the EXISTING config, verbatim.

    _write_config regenerates config.yaml from scratch on every spawn, but
    provider API keys (claude-api-key / codex-api-key) live in that same file —
    the proxy itself writes them there when the management API adds one. So a
    naive regenerate DESTROYS every key the user has added: verified by adding a
    key, changing the routing strategy (which restarts), and finding the key
    gone. Unlike OAuth credentials, which live as separate files under auth-dir
    and are therefore untouched by this, keys have no home outside the config.

    Rather than model these blocks (entries carry base-url/proxy-url/models and
    a server-assigned auth-index, and the shapes differ per provider), the old
    text is carried across unparsed: this function owns nothing about their
    meaning, it just refuses to lose them. Everything from a top-level
    `<provider>-api-key:` line up to the next top-level key is kept.

    Best-effort by design — a missing or unreadable config simply means there
    is nothing to preserve, which is the correct answer for a first spawn.
    """
    try:
        with open(_config_path(), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    kept: list[str] = []
    keeping = False
    for line in lines:
        if line[:1] not in (" ", "\t", "", "#"):
            # A new top-level key ends any block we were copying, and starts a
            # new one only if it is a provider api-key section.
            keeping = line.split(":", 1)[0] in _API_KEY_ROUTE.values()
        if keeping:
            kept.append(line)
    return ("\n".join(kept) + "\n") if kept else ""


def _write_config(port: int, api_key: str, management_key: str,
                   routing_strategy: str) -> str:
    """Write the proxy's YAML config and return its path.

    host is pinned to 127.0.0.1 (never the upstream default of all
    interfaces — see the module docstring's security note); api-keys holds
    our one generated key; remote-management is locked to loopback-only with
    our second generated key and disable-control-panel set, since we drive
    the API ourselves and don't want it fetching a web panel from GitHub.
    logging-to-file is off because we capture stdout/stderr to our own
    rotated log instead (see _rotate_log) — there is no --log-file flag on
    this binary, so config-side logging and our redirect would otherwise be
    two independent, un-rotated log destinations.

    routing.strategy is written out EXPLICITLY rather than left to omission:
    the upstream default ("round-robin" — pool every credential for a
    provider and fail over between them) is otherwise invisible in this
    file, and prefs.ai_routing_strategy() already resolves an unset pref to
    that same default, so a caller reading this config can't tell "chose
    round-robin" from "never asked." Written from the pref at spawn time
    because the binary only reads its config at startup (see
    restart_ai_proxy()) — there is no hot-reload to short-circuit that with.

    Written 0600 immediately after creation: the file holds both secrets in
    plaintext, and CLIProxyAPI hashes a plaintext secret-key on startup, so
    this is the only place either key is ever readable at rest."""
    os.makedirs(_state_dir(), exist_ok=True)
    try:
        os.chmod(_state_dir(), 0o700)
    except OSError:
        pass
    os.makedirs(_auth_dir(), exist_ok=True)
    # Read the outgoing config's provider key blocks BEFORE the write below
    # clobbers it — see _preserved_api_key_blocks for why losing them is data
    # loss and not just a reset.
    preserved = _preserved_api_key_blocks()
    config = (
        'host: "127.0.0.1"\n'
        f'port: {port}\n'
        'api-keys:\n'
        f'  - "{_yaml_str(api_key)}"\n'
        f'auth-dir: "{_yaml_str(_auth_dir())}"\n'
        'logging-to-file: false\n'
        'routing:\n'
        f'  strategy: "{_yaml_str(routing_strategy)}"\n'
        'remote-management:\n'
        '  allow-remote: false\n'
        f'  secret-key: "{_yaml_str(management_key)}"\n'
        '  disable-control-panel: true\n'
        + preserved
    )
    path = _config_path()
    with open(path, "w", encoding="utf-8") as f:
        f.write(config)
    os.chmod(path, 0o600)
    return path


def _rotate_log() -> str:
    """Before spawning, roll proxy.log -> proxy.log.1 if it's grown past the
    cap, and return the (current) log path — the same one-generation
    rotation as mounts._rotate_rcd_log, for the same reason: this binary has
    no rotation of its own, and stdout/stderr redirected into it otherwise
    grows unbounded across the life of a long-running app. Best-effort: a
    stat/rename failure just means we append to whatever is there."""
    log = _log_path()
    try:
        if os.path.getsize(log) > LOG_MAX_BYTES:
            os.replace(log, log + ".1")
    except OSError:
        pass
    return log


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _write_state(port: int, pid: int, api_key: str, management_key: str,
                  config_path: str, log_path: str) -> None:
    """Persist the live instance's identity and secrets to the state file.

    spawner_pid records WHO spawned it, so a later process that reuses this
    instance (or the same process restarted) can tell on teardown whether it
    is theirs to stop — see stop_local_ai_proxy's ownership gate, mirroring
    write_rcd_state's spawner_pid field.

    Chmod 0600 after write: storage.write_json has no notion of permissions,
    and this file carries the same two live secrets the config does."""
    state = {
        "port": port,
        "pid": pid,
        "spawner_pid": os.getpid(),
        "api_key": api_key,
        "management_key": management_key,
        "config": config_path,
        "log": log_path,
    }
    path = _state_path()
    storage.write_json(path, state)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def ensure_ai_proxy() -> tuple[str, str, str]:
    """Ensure a supervised proxy instance is up; return (base_url, api_key,
    management_key). Spawns lazily — this is meant to be called right before
    the first request that needs the proxy, not at app launch, so an idle AI
    proxy process isn't rent every session pays (see AI_PROXY_BUNDLING.md).

    Reuses a live instance when the recorded state answers a health probe;
    otherwise spawns a fresh one under _lock, so two racing callers (two
    concurrent fused.ai() calls hitting "no live instance" at once) can't
    both spawn.

    Raises RuntimeError naming what went wrong: no binary resolved, the
    binary exited immediately, or it never became healthy within
    _STARTUP_TIMEOUT_S. Callers (the /api/ai relay) are expected to map that
    into the house {ok, error} contract, same as any other proxy-unreachable
    case.

    Does NOT check is_supervised() itself — that's the caller's decision
    (skip calling this entirely when the user pointed ai_base_url somewhere
    of their own), so this function has exactly one job: get a bundled
    instance running and hand back how to reach it."""
    with _lock:
        return _ensure_locked()


def _terminate_unhealthy_child(proc: subprocess.Popen) -> None:
    """Kill a just-spawned instance that never became healthy, and reap it.

    Only ever called with a Popen WE created moments ago, so ownership needs no
    proving — unlike _kill_current_ai_proxy, whose pid comes from a state file
    and could have been recycled. SIGTERM then SIGKILL, then wait() so the dead
    child doesn't linger as a zombie (see _reap_child). Best-effort throughout:
    this runs on a path that is already failing, and must not replace the
    caller's "never became healthy" error with a teardown one."""
    global _child
    try:
        proc.terminate()
        try:
            proc.wait(timeout=_KILL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_KILL_TIMEOUT_S)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        logger.warning("ai-proxy: could not terminate an unhealthy spawn",
                       exc_info=True)
    if _child is proc:
        _child = None


def _ensure_locked() -> tuple[str, str, str]:
    state = storage.read_json(_state_path())
    if isinstance(state, dict) and state.get("port") and state.get("api_key"):
        if _probe_models(int(state["port"]), state["api_key"]):
            return (_base_url(state["port"]), state["api_key"],
                    state.get("management_key", ""))
        # Recorded but not answering. If that pid is still alive it is a HUNG
        # instance, not a dead one — and we are about to overwrite the config
        # and state file it is described by, which would leave it running,
        # unreachable, and unprovable as ours (so never reapable). Kill it
        # first, while the state file that identifies it still exists.
        # Best-effort: an unconfirmable pid raises here and we carry on to the
        # spawn, since refusing to start a proxy because an unrelated process
        # inherited a recycled pid would be worse than the leak.
        if _pid_alive(state.get("pid") or 0):
            try:
                _kill_current_ai_proxy()
            except RuntimeError:
                logger.warning(
                    "ai-proxy: recorded instance is unreachable but its pid "
                    "could not be confirmed as ours; leaving it alone")

    bin_ = ai_proxy_bin()
    if not bin_:
        raise RuntimeError(
            "the AI proxy (cli-proxy-api) is not installed or bundled; "
            "set FUSED_RENDER_AI_PROXY_BIN to a real binary, or point "
            "ai_base_url at a proxy you run yourself")

    # Pick the port ourselves (parsing the binary's own startup log for a
    # bound port is brittle) — same bind-0-then-close trick as
    # _ensure_rcd_locked.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    api_key = secrets.token_urlsafe(32)
    management_key = secrets.token_urlsafe(32)
    routing_strategy = prefs.ai_routing_strategy()
    config_path = _write_config(port, api_key, management_key, routing_strategy)
    log_path = _rotate_log()

    with open(log_path, "ab") as log_f:
        proc = subprocess.Popen(
            [bin_, "-config", config_path],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            # Dev (FUSED_RENDER_AI_PROXY_PERSIST set): setsid so the instance
            # outlives server restarts, same convenience as rclone's rcd.
            # Production (unset): a normal child, reaped with the app.
            start_new_session=_should_persist(),
        )
    # Keep the handle: a Popen whose child we later signal must be WAITED on,
    # or the dead child lingers as a zombie — and a zombie still answers
    # os.kill(pid, 0), so _pid_alive would keep reporting it alive and the
    # teardown poll below would spin its full timeout and then wrongly report
    # "did not exit after SIGKILL" for a process that died instantly. Storing
    # it is what lets _reap_child() actually collect the exit status.
    global _child
    _child = proc

    deadline = time.time() + _STARTUP_TIMEOUT_S
    while time.time() < deadline:
        if _probe_models(port, api_key):
            _write_state(port, proc.pid, api_key, management_key,
                         config_path, log_path)
            return (_base_url(port), api_key, management_key)
        if proc.poll() is not None:
            _child = None  # already dead; nothing to reap but don't keep the handle
            raise RuntimeError(
                f"ai-proxy exited immediately (code {proc.returncode}); "
                f"see {log_path}")
        time.sleep(0.2)
    # Unhealthy but ALIVE — kill it before giving up. Without this the process
    # leaks permanently: no state file was written, so nothing afterwards knows
    # its pid, _confirmed_ours can never prove it is ours, and every later
    # retry spawns another one beside it. Worse under
    # FUSED_RENDER_AI_PROXY_PERSIST, where start_new_session has already
    # detached it from this process group, so app teardown won't collect it
    # either. Signal directly rather than via _kill_current_ai_proxy: that
    # reads the state file, which deliberately does not exist yet.
    _terminate_unhealthy_child(proc)
    raise RuntimeError(
        f"ai-proxy did not become healthy within {_STARTUP_TIMEOUT_S:g}s; "
        f"see {log_path}")


# How long _kill_current_ai_proxy waits for a signalled instance to actually
# exit before escalating / giving up — mirrors mounts._KILL_TIMEOUT_S.
_KILL_TIMEOUT_S = 5.0


def _kill_current_ai_proxy() -> None:
    """Terminate the recorded ai-proxy instance, if there is one to
    terminate. Only ever signals a pid PROVEN to be ours (_confirmed_ours) —
    the single most important constraint here, reused verbatim from
    mounts._kill_current_rcd's rationale: pids get recycled, so an alive but
    unconfirmed pid must raise rather than risk killing an unrelated
    process. No recorded instance / an already-dead pid is a clean no-op."""
    entry = storage.read_json(_state_path())
    if not isinstance(entry, dict):
        return
    pid = entry.get("pid") or 0
    if not _pid_alive(pid):
        return
    if not _confirmed_ours(entry):
        raise RuntimeError(
            f"refusing to kill pid {pid}: not confirmed to be our ai-proxy")
    sigs = (signal.SIGTERM,) if sys.platform == "win32" else (signal.SIGTERM, signal.SIGKILL)
    for sig in sigs:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return  # raced us and exited between the check and the signal
        except OSError as e:
            if not _pid_alive(pid):
                return
            raise RuntimeError(f"failed to signal ai-proxy pid {pid}: {e}") from e
        # Reap before polling: if this is our own child it is now a zombie,
        # which _pid_alive cannot distinguish from a live process, so without
        # this the loop below would run its full timeout on an already-dead
        # instance and then raise a false "did not exit".
        _reap_child(pid)
        deadline = time.time() + _KILL_TIMEOUT_S
        while time.time() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.1)
    raise RuntimeError(f"ai-proxy pid {pid} did not exit after {sigs[-1].name}")


def restart_ai_proxy() -> bool:
    """Force-kill the current supervised instance so the NEXT ensure_ai_proxy()
    call spawns a fresh one, picking up a config change that only takes
    effect at process startup — routing.strategy today (see _write_config
    and prefs.ai_routing_strategy()). Returns True if a live instance was
    actually killed, False if there was nothing running to kill (nothing
    spawned yet, or it belongs to a process we can't confirm — see
    _confirmed_ours).

    Deliberately does NOT respawn here: that would make a routing-strategy
    change block its caller (an accounts-page PUT) on the same ~15s startup
    poll ensure_ai_proxy() pays once per session, for a change that doesn't
    need the proxy back up immediately. The next call that actually needs
    the proxy (an accounts refresh, or the next fused.ai()) respawns it
    lazily, same discipline as every other ensure_ai_proxy() call site.

    Unlike stop_local_ai_proxy() (the app-quit path), this ignores
    _should_persist(): a dev convenience for surviving watchfiles restarts
    across app-process restarts is not a reason to keep serving a stale
    routing strategy after the user explicitly changed it from the
    accounts page."""
    with _lock:
        entry = storage.read_json(_state_path())
        if not isinstance(entry, dict):
            return False
        pid = entry.get("pid") or 0
        if not _pid_alive(pid):
            return False
        try:
            _kill_current_ai_proxy()
        except RuntimeError:
            # Not confirmed ours (e.g. pid recycled to an unrelated process,
            # or owned by something else entirely) — nothing safe to kill;
            # the next ensure call still spawns fresh with the new config.
            return False
        return True


def stop_local_ai_proxy() -> None:
    """Best-effort teardown of the proxy we spawned, for the app's quit path
    — stop_local_rcd's exact contract, applied to this daemon.

    Gated on NOT persisting (FUSED_RENDER_AI_PROXY_PERSIST unset): a
    detached dev instance is meant to outlive this process. Ownership gate:
    when the recorded spawner_pid is a DIFFERENT, still-alive process, this
    instance is shared and someone else is using it — leave it alone (a
    missing spawner_pid, from a state file written before the field existed,
    preserves the old kill-it behavior). Swallows every error — a reap
    failure must never block app quit."""
    if _should_persist():
        return
    with _lock:
        entry = storage.read_json(_state_path())
        if isinstance(entry, dict):
            spawner_pid = entry.get("spawner_pid") or 0
            if spawner_pid and spawner_pid != os.getpid() and _pid_alive(spawner_pid):
                logger.info(
                    "stop_local_ai_proxy: instance was spawned by pid %s "
                    "which is still alive; leaving it to its owner",
                    spawner_pid,
                )
                return
        try:
            _kill_current_ai_proxy()
        except Exception:
            logger.warning("stop_local_ai_proxy: teardown failed", exc_info=True)


def status() -> dict:
    """Cheap, non-spawning snapshot for the accounts UI: {"supervised":
    bool, "running": bool}. Never spawns and never raises — a page merely
    checking proxy status shouldn't pay the spawn cost or fail loudly; that
    only happens on an actual management call or fused.ai() request."""
    if not is_supervised():
        return {"supervised": False, "running": False}
    state = storage.read_json(_state_path())
    if isinstance(state, dict) and state.get("port") and state.get("api_key"):
        return {"supervised": True,
                "running": _probe_models(int(state["port"]), state["api_key"])}
    return {"supervised": True, "running": False}


def management_request(method: str, path: str, body: dict | list | None = None,
                        *, timeout: float = 10.0) -> dict:
    """One authenticated call to the proxy's /v0/management API.

    `path` is the suffix after /v0/management (e.g. "/auth-files",
    "/auth-files?name=foo.json", "/anthropic-auth-url", "/oauth-callback")
    — the routes layer builds the exact route; this is transport only, with
    no FastAPI/route awareness. `body` is a dict for every route except the
    API-key PUTs, which the doc is explicit take a bare JSON array (see
    replace_api_keys) — json.dumps handles either with no branching needed
    here. Ensures a live proxy first (same lazy-spawn
    discipline as ensure_ai_proxy(), since a management call to an instance
    that isn't running is meaningless) and authenticates with
    `management_key` — the account-management surface's own secret,
    distinct from the api_key fused.ai() traffic uses (see module docstring).

    Raises RuntimeError. A 404 is ambiguous on this API and the two cases
    are told apart by the BODY, not the status code alone (verified against
    the real binary): an unrouted path (a route this build genuinely lacks)
    is Gin's bare NoRoute 404 with an EMPTY body, whereas a business-logic
    404 on a route that exists (e.g. auth-files DELETE for an unknown name)
    carries a normal `{"error": "..."}` JSON body. Only the empty-body case
    is reported as "this proxy build doesn't support account management from
    the app" per the doc's version-skew note; a 404 WITH an error body is
    just that error, raised like any other non-2xx — conflating the two
    would misreport an ordinary "no such credential" as a version mismatch."""
    base_url, _api_key, management_key = ensure_ai_proxy()
    url = base_url.rstrip("/") + "/v0/management" + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {management_key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        try:
            detail = json.loads(raw).get("error", "") if raw else ""
        except (ValueError, AttributeError):
            detail = ""
        if e.code == 404 and not detail:
            raise RuntimeError(
                "this proxy build does not support account management from "
                "the app (404 with no error body on a management route) — "
                "it may be a different version than this app was built "
                "against"
            ) from e
        raise RuntimeError(
            detail or f"ai-proxy management {method} {path}: HTTP {e.code}"
        ) from e
    except OSError as e:
        raise RuntimeError(f"ai-proxy management {method} {path}: {e}") from e


# --- Login-flow and credential helpers ---------------------------------------
#
# Thin, named wrappers over management_request for the exact routes the
# accounts UI needs (docs/AI_PROXY_MANAGEMENT_API.md, re-verified 2026-07-28
# after an earlier draft of that doc wrongly claimed there was no login-status
# channel). Each one just fixes the path/method/body shape for its route —
# still no FastAPI/route awareness, still just transport, but named so the
# routes layer isn't hand-assembling query strings and reading raw dicts.
#
# NOT wrapped here, deliberately: binding the fixed OAuth callback ports
# (54545 / 1455) and the ?is_webui=1 control-panel shortcut (rejected — binds
# all interfaces, forwards into the control panel we disable). Both are the
# routes worker's job.


def start_login(provider: str) -> dict:
    """Begin OAuth for `provider` (e.g. "anthropic", "codex"). Returns the
    proxy's own `{state, status, url}` — `state` must be handed back
    unchanged to submit_login_code/poll_login_status/cancel_login, and `url`
    is what the frontend opens in the user's browser."""
    return management_request("GET", f"/{quote(provider)}-auth-url")


def submit_login_code(provider: str, state: str, code: str) -> dict:
    """Hand the code captured off the provider's callback redirect to the
    proxy. A 200 here means "code recorded for exchange," NOT "logged in" —
    the exchange happens asynchronously; poll_login_status is what actually
    confirms success or failure. Replaying a state that already completed is
    a 409, surfaced as a RuntimeError like any other non-2xx."""
    return management_request(
        "POST", "/oauth-callback",
        {"provider": provider, "state": state, "code": code})


def poll_login_status(state: str) -> dict:
    """The real result channel for a login: {"status": "wait"|"ok"|"error",
    "error"?: str}. Callers should poll this — not auth-files — since
    oauth-callback's own 200 proves nothing about the exchange outcome."""
    return management_request("GET", f"/get-auth-status?state={quote(state)}")


def cancel_login(state: str) -> dict:
    """Cancel a pending login (e.g. the user closed the browser tab, or the
    UI is releasing a callback port it bound for the duration of one login).
    A no-op past the state's TTL/completion is fine — the proxy's own
    {status, cancelled} reply carries whether there was anything to cancel."""
    return management_request("DELETE", f"/oauth-session?state={quote(state)}")


def list_credentials() -> list:
    """Every connected account, proxy-native shape (see the doc's Credential
    listing shape) — filtering down to {provider, email, disabled, expired}
    and restricting to claude/codex is the routes layer's job, not this
    module's; this just returns `files` unmodified. Never returns token
    material itself (auth-files doesn't include it)."""
    result = management_request("GET", "/auth-files")
    files = result.get("files")
    return files if isinstance(files, list) else []


def delete_credential(name: str) -> dict:
    """Remove one credential by its `name` (the handle auth-files lists it
    under). An unknown/missing name is the proxy's own 400, surfaced as a
    RuntimeError like any other management error."""
    return management_request("DELETE", f"/auth-files?name={quote(name)}")


def set_credential_disabled(name: str, disabled: bool,
                             auth_index: str | None = None) -> dict:
    """Enable/disable a credential without deleting it. `auth_index`
    disambiguates when a provider has multiple accounts sharing routing
    priority (per-entry field in the auth-files listing) — omit it to target
    by name alone."""
    body: dict = {"name": name, "disabled": disabled}
    if auth_index is not None:
        body["auth_index"] = auth_index
    return management_request("PATCH", "/auth-files/status", body)


# -- API keys (config-level credentials) --------------------------------------
#
# Separate surface from the auth-files/OAuth wrappers above: an API key is a
# config entry, not a file in auth-dir, so it never shows up in list_credentials
# (docs/AI_PROXY_MANAGEMENT_API.md's "API keys" section). Our provider names
# ("claude"/"codex") already match the route names 1:1 here — unlike the OAuth
# routes, there is no anthropic/claude naming seam to bridge.
#
# This layer stays exactly as thin as the wrappers above: no read-modify-write
# policy, no key masking, no base-url defaulting. Those are the routes layer's
# job (ai_accounts.py) precisely because they need to reason about what a
# client is allowed to see and about the codex-base-url trap, neither of
# which belongs in a transport-only module.

_API_KEY_ROUTE = {"claude": "claude-api-key", "codex": "codex-api-key"}


def list_api_keys(provider: str) -> list:
    """Every API-key entry configured for `provider`, proxy-native shape:
    {api-key, base-url, proxy-url, models, auth-index}. Returns the raw
    entries — including the plaintext api-key field — unfiltered; masking a
    key down to a display hint before it can reach a client is the routes
    layer's job, same division of labor as list_credentials()/
    api_ai_accounts(). Callers holding this return value must treat it with
    the same care as any other live credential material: never log it,
    never let it further than the one response that needed it."""
    route = _API_KEY_ROUTE[provider]
    result = management_request("GET", f"/{route}")
    entries = result.get(route)
    return entries if isinstance(entries, list) else []


def replace_api_keys(provider: str, entries: list) -> dict:
    """Overwrite `provider`'s ENTIRE api-key list with `entries`. There is no
    POST-to-append on this API (confirmed 404 in the doc), so every add or
    remove is a read-modify-write of the whole array, and this wrapper is
    deliberately dumb about that — it PUTs exactly the list it's given, no
    more and no less.

    `entries` MUST be a bare list (not a dict wrapping it under the route
    name) — the proxy's 400 on the object shape is the exact trap the doc
    calls out, since GET returns that wrapper and it's an easy shape to
    carry over by mistake. Defaulting Codex's base-url (the doc's silent-
    drop trap) and reading back to confirm a write landed are both the
    caller's job; this function does not know which provider it's talking
    to beyond picking the route."""
    route = _API_KEY_ROUTE[provider]
    return management_request("PUT", f"/{route}", entries)
