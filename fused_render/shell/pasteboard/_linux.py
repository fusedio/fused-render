"""Linux pasteboard backend — wl-clipboard on Wayland, xclip on X11.

There is no OS-owned clipboard on X11 or Wayland: a live process must own the
selection and answer conversion requests for it. We are a web app with a
Python backend, not a toolkit client, so we shell out to whichever helper is
installed — `wl-copy`/`wl-paste` first (Wayland, and it works under XWayland
too), then `xclip`. With neither present the bridge reports unsupported and
the app keeps its existing in-app-only clipboard.

The two file managers disagree on the format:

  GNOME / Nautilus  x-special/gnome-copied-files   "copy\\nfile:///a\\nfile:///b"
  KDE / Dolphin     text/uri-list                  "file:///a\\r\\nfile:///b"

`xclip`/`wl-copy` can only publish ONE of those per invocation — both take a
single `--type`, and a second call replaces the first rather than adding to
it — so on their own neither format can also carry `text/plain`, and a paste
into a plain text field comes back empty. `write_files` therefore tries a
richer path first: `_linux_owner.py`, a small script run under a *separate*
Python interpreter (one with PyGObject, which the app's own venv does not
have), that uses GTK4 to own the selection directly and can offer all four
targets — both file-manager formats plus `text/plain` and
`text/plain;charset=utf-8` — at once. Only when that path is unavailable (no
interpreter has `gi`, no display, a spawn error, no readiness signal in time)
does it fall through to the single-target `xclip`/`wl-copy` write below,
picking the family the session reports via `XDG_CURRENT_DESKTOP`. That guess,
and never offering `text/plain`, is the one real limitation of the fallback,
not of this backend as a whole.

Everything here is driven through `shutil.which` and `subprocess.run`, both
looked up on the module at call time, so the tests fake them and run on any
platform. `fcntl` is the one exception — it's Unix-only, so it's imported
inside the two functions that need it (non-blocking pipe I/O to the owner
process) rather than at module scope, keeping this module importable on
Windows even though those two functions never actually run there.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import time
from urllib.parse import quote, unquote

GNOME_TARGET = "x-special/gnome-copied-files"
URI_LIST_TARGET = "text/uri-list"

# Read order: the GNOME target first because it's the only one carrying the
# copy/cut verb, so a desktop that offers both tells us more via that one.
READ_TARGETS = (GNOME_TARGET, URI_LIST_TARGET)

# A short timeout everywhere: these are local, instant tools, and a hung
# wl-paste (no compositor, no display) must not stall a focus-time read.
_TIMEOUT_S = 3


class NoClipboardTool(RuntimeError):
    """Neither wl-clipboard nor xclip is installed."""


# --------------------------------------------------------------- URI helpers

def path_to_uri(path: str) -> str:
    """Absolute path -> a file:// URI with per-segment percent-encoding.

    `safe="/"` keeps the separators literal — encoding them would collapse the
    whole path into one URI segment, which no file manager accepts.
    """
    return "file://" + quote(path, safe="/")


def uri_to_path(uri: str) -> str | None:
    """file:// URI -> absolute path, or None for anything else (http URLs get
    dropped on the floor rather than pasted as a nonsense path).

    The authority between `file://` and the path is normally empty, but
    `file://localhost/home/x` is equally legal and some toolkits emit it.
    Slicing a fixed 7 characters would turn that into the relative path
    `localhost/home/x` — the contract's absolute-path filter would drop it,
    correctly but silently, so the user would just see a file quietly missing
    from their paste. A non-empty, non-localhost authority is a remote host
    we have no local path for, and is refused rather than guessed at.
    """
    if not uri.startswith("file://"):
        return None
    rest = uri[len("file://"):]
    if not rest.startswith("/"):
        # Everything up to the first "/" is the authority.
        authority, sep, tail = rest.partition("/")
        if not sep or authority.lower() != "localhost":
            return None
        rest = "/" + tail
    return unquote(rest)


# ------------------------------------------------------------ tool selection

def _is_wayland() -> bool:
    """Is this a Wayland session (including XWayland, where wl-* still work)?

    `WAYLAND_DISPLAY` is the socket the wl-clipboard tools actually connect to,
    so it is the direct evidence and comes first; `XDG_SESSION_TYPE` is the
    logind-provided fallback for the case where a compositor is running but the
    server's environment never inherited the socket name.
    """
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


def _tool() -> tuple[list[str], list[str]]:
    """(read argv prefix, write argv prefix) for the best available helper.

    Preference follows the SESSION, not merely what is installed. Both tool
    families are commonly present at once (wl-clipboard is a dependency of
    plenty of unrelated packages), and preferring wl-clipboard on that basis
    alone broke every X11 machine that happened to have it: with no compositor
    to talk to, `wl-copy`/`wl-paste` fail outright, and because the failure
    happened INSIDE the chosen tool the contract reported `supported: false`
    while a perfectly working xclip sat one branch away, never tried.

    Availability still filters the preference — a Wayland session with only
    xclip installed (XWayland) uses xclip rather than failing, and vice versa.
    wl-clipboard additionally counts as present only if BOTH halves are: a half
    install that can copy but not paste is worse than falling through to xclip,
    which does both.
    """
    wl = bool(shutil.which("wl-copy") and shutil.which("wl-paste"))
    xc = bool(shutil.which("xclip"))
    order = ("wl", "xclip") if _is_wayland() else ("xclip", "wl")
    for choice in order:
        if choice == "wl" and wl:
            return (["wl-paste", "--no-newline", "--type"], ["wl-copy", "--type"])
        if choice == "xclip" and xc:
            return (["xclip", "-selection", "clipboard", "-o", "-t"],
                    ["xclip", "-selection", "clipboard", "-i", "-t"])
    raise NoClipboardTool(
        "no clipboard helper found — install wl-clipboard or xclip")


def _is_kde() -> bool:
    """Does the session report itself as KDE/Plasma?

    XDG_CURRENT_DESKTOP is a colon-separated list ("ubuntu:GNOME",
    "KDE:plasma"), so this is a substring test on the lowercased value rather
    than an equality check.

    Only the fallback below still consults this. The owner offers both file
    formats simultaneously, so there's nothing for a desktop guess to decide
    once it's in play.
    """
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    return "kde" in desktop or "plasma" in desktop


# --------------------------------------------------- multi-target GTK4 owner

# Sentinel distinct from None, which is the legitimate "no candidate
# interpreter has gi" answer we want to cache rather than re-probe on every
# write — this shells out per candidate, and a write is on the hot path for
# every Explorer copy.
_INTERPRETER_UNPROBED = object()
_gi_interpreter: object = _INTERPRETER_UNPROBED

# A short bound on how long we wait for the owner to print its readiness
# line. It's a local process reading a JSON blob off stdin and calling into
# GTK — if it hasn't answered in this long, something (no display, no
# compositor, a broken install) is wrong, and the fallback below is a better
# bet than blocking the write any further.
_OWNER_READY_TIMEOUT_S = 3

# Must match `_linux_owner.OWNER_READY_TOKEN` exactly (that one a str, this
# one the bytes it encodes to). The two modules can't import each other —
# `_linux_owner` runs under a separate, possibly gi-less interpreter, per its
# own module docstring — so the token is duplicated here rather than shared,
# the same way `path_to_uri` is duplicated there instead of imported. Keep
# the two literals in step by hand if either changes.
_OWNER_READY_TOKEN = b"ready"

# `_wait_for_owner_ready` tolerates this many non-token lines before the
# token, in addition to the overall timeout below — see that function's
# docstring for why tolerating a stray line is worth the bound at all, and
# why the bound has to apply on this axis too, not just the deadline.
_OWNER_READY_MAX_STRAY_LINES = 5


def _reset_gi_interpreter_cache() -> None:
    """Undo the caching in `_probe_gi_interpreter`. Exists for tests, which
    need to re-probe under a different faked environment than whatever
    settled the cache first."""
    global _gi_interpreter
    _gi_interpreter = _INTERPRETER_UNPROBED


def _probe_gi_interpreter() -> str | None:
    """The first of a short fixed list of interpreters that can `import gi`.

    `sys.executable` is tried first only because it's free to check, not
    because it's expected to work — PyGObject isn't, and shouldn't become, a
    dependency of the app's own venv. `/usr/bin/python3` is the one that
    reliably has it: GTK's Python bindings are normally packaged against the
    distro's system Python, not any particular venv. `shutil.which("python3")`
    is the last, weakest guess, for a layout where neither of the first two
    is the right one.

    Cached for the life of the process; `_reset_gi_interpreter_cache` is the
    escape hatch for tests that need a fresh probe.
    """
    global _gi_interpreter
    if _gi_interpreter is not _INTERPRETER_UNPROBED:
        return _gi_interpreter  # type: ignore[return-value]

    seen: set[str] = set()
    candidates = []
    for c in (sys.executable, "/usr/bin/python3", shutil.which("python3")):
        if c and c not in seen:
            seen.add(c)
            candidates.append(c)

    found = None
    for c in candidates:
        try:
            proc = subprocess.run(
                [c, "-c", "import gi"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            found = c
            break

    _gi_interpreter = found
    return found


def _owner_script_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "_linux_owner.py")


def _wait_for_owner_ready(stream) -> bool:
    """Block for at most `_OWNER_READY_TIMEOUT_S` for the owner's readiness
    TOKEN, not merely "a line arrived".

    Comparing against the literal `_OWNER_READY_TOKEN`, rather than
    `bool(stream.readline())`, is the whole point of this function. On the
    SUCCESS path the owner never closes its stdout (it dup2's fd 1 onto
    /dev/null right after printing, deliberately staying resident), so a
    plain blocking read only ever returns early on a failure path — but
    "returns a line" and "returns the RIGHT line" are different things, and
    treating them as the same is exactly the bug this replaced: any stdout
    line the child emits before reaching its token — a sitecustomize
    warning, a `-W` deprecation routed to stdout, a `gi` compatibility shim
    printing a diagnostic, a partial crash message — used to satisfy the
    handshake and make `_write_via_owner` report success for a selection
    that was never actually set. That is the one failure mode that skipped
    the fallback which would otherwise have worked, so it's worth getting
    exactly right rather than merely "probably fine".

    A single stray line is tolerated rather than instant failure — common
    enough on a stock distro Python that failing the whole owner path over
    a cosmetic warning would give up a working machine for nothing. But
    tolerance is bounded on BOTH axes, or a chatty/wedged child turns this
    into the unbounded read this function exists to prevent:
    `_OWNER_READY_MAX_STRAY_LINES` caps the line count independently of
    `_OWNER_READY_TIMEOUT_S`, and whichever bound is hit first ends the wait
    as failure. `select` still gates every wait for more bytes, the same
    discipline `_TIMEOUT_S` already applies to the fallback tools.

    Deliberately NOT `stream.readline()` in a loop with `select` in between
    calls, tempting as that reads: `stream` is a buffered file object, and
    its `readline()` pulls a whole chunk off the underlying fd per call, not
    just the one line it returns — any extra bytes (e.g. the token, already
    sent right behind a stray line) sit in ITS OWN internal buffer, which
    `select` on the raw fd has no visibility into. A second `select` call
    then sees a fd with nothing new to read and times out even though the
    token already arrived, moments after the stray line, well within the
    deadline. So this reads raw bytes off the fd itself (non-blocking, via
    `os.read`) into a buffer this function owns outright, and only `select`
    decides whether to wait for the next chunk.
    """
    import fcntl

    fd = stream.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    deadline = time.monotonic() + _OWNER_READY_TIMEOUT_S
    buf = b""
    for _ in range(_OWNER_READY_MAX_STRAY_LINES + 1):
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            ready, _, _ = select.select([stream], [], [], remaining)
            if not ready:
                return False
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                # EOF: the child exited (crashed, or a broken pipe) before
                # ever printing the token.
                return False
            buf += chunk
        line, _, buf = buf.partition(b"\n")
        if line.strip() == _OWNER_READY_TOKEN:
            return True
        # Anything else is a stray line — loop, bounded by the `for` above
        # and by whatever's left of `deadline`.
    return False


def _write_stdin_bounded(stream, data: bytes) -> bool:
    """Write `data` to the owner's stdin without risking an unbounded block.

    The pipe backing `stream` has a limited buffer (64 KiB is typical on
    Linux); a copy of on the order of a thousand paths serializes past that.
    The owner drains stdin before touching GTK, so under ordinary conditions
    the write completes in a syscall or two — but "ordinary conditions" is
    exactly what the parent can't assume: a wedged or already-dead child
    never drains, and a plain blocking `write()` then hangs this (long-lived
    server) process with no timeout at all, which is worse than every
    failure mode this module otherwise guards against. Fixed with the same
    discipline `_wait_for_owner_ready` uses — put the fd in non-blocking
    mode and bound the write loop with `select`, against its own
    `_OWNER_READY_TIMEOUT_S` budget (separate from the one the readiness
    wait spends afterwards, since the two can't overlap: nothing is read
    before the whole request has been written). Returns False, never
    raises `BlockingIOError`, on running out of that budget — the caller
    treats it exactly like a readiness timeout: kill the child, fall back.
    """
    import fcntl

    fd = stream.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    deadline = time.monotonic() + _OWNER_READY_TIMEOUT_S
    view = memoryview(data)
    while view:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _, writable, _ = select.select([], [stream], [], remaining)
        if not writable:
            return False
        try:
            n = stream.write(view)
        except BlockingIOError:
            continue
        if n:
            view = view[n:]
    return True


def _kill_owner(proc: subprocess.Popen) -> None:
    """Kill and reap an owner process we're abandoning without a confirmed
    readiness signal.

    Every not-ready outcome (the write didn't complete, the handshake never
    arrived) leaves the child in one of two states, and both are bad to
    leave running: it may be genuinely wedged — the interpreter-probe only
    checked `import gi`, which succeeds with PyGObject installed regardless
    of whether `DISPLAY`/`WAYLAND_DISPLAY` points at a reachable compositor,
    and `Gtk.init()` blocks on that connect rather than failing fast, so a
    misconfigured box would otherwise leak one resident GTK process per
    Explorer copy for the life of the machine (it's `start_new_session=True`
    and outlives us). Or it may just be slow: if it finishes moments after
    we give up, its `set_content()` call takes the selection back from the
    `wl-copy`/`xclip` write `write_files` is about to fall through to,
    displacing a write the caller was already told succeeded. Killing it
    removes both risks at once rather than trying to distinguish them.

    `kill()` is safe and targeted here specifically because of
    `start_new_session=True`: the child is the sole member of its own
    session, so there's no sibling process sharing that session for a
    SIGKILL to orphan.
    """
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _write_via_owner(paths: list[str]) -> bool:
    """Best-effort: hand `paths` to the multi-target GTK4 owner.

    Returns False on ANY failure — no capable interpreter, a spawn error, an
    unbounded/failed stdin write, no confirmed readiness token in time — and
    never raises. Every one of those is a normal, expected outcome on a
    machine without PyGObject or without a display, and `write_files` treats
    False as "try the single-target fallback next", not as an error. On
    every not-ready outcome the child is killed rather than left running —
    see `_kill_owner`.
    """
    interpreter = _probe_gi_interpreter()
    if interpreter is None:
        return False

    try:
        proc = subprocess.Popen(
            [interpreter, _owner_script_path()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False

    try:
        payload = json.dumps({"paths": paths}).encode("utf-8")
        try:
            wrote = _write_stdin_bounded(proc.stdin, payload)
        except OSError:
            wrote = False
        finally:
            # Closed here regardless of outcome — on the old success-only
            # close, a `write()`/`close()` OSError left this handle open
            # until GC, on a process that's meant to run indefinitely.
            try:
                proc.stdin.close()
            except OSError:
                pass

        if not wrote:
            _kill_owner(proc)
            return False

        ready = _wait_for_owner_ready(proc.stdout)
        if not ready:
            _kill_owner(proc)
        return ready
    finally:
        # Our own read end of the owner's stdout is only ever needed for the
        # readiness token — whatever happened above, it must be closed
        # here. The owner itself has already moved fd 1 onto /dev/null by
        # the time we could see this fd (on the ready path), so closing our
        # end doesn't touch it; it just stops this (long-lived server)
        # process from accumulating one open pipe per copy for the rest of
        # the session.
        try:
            proc.stdout.close()
        except OSError:
            pass


# --------------------------------------------------------------------- read

def read_files() -> list[str]:
    read_argv, _ = _tool()
    for target in READ_TARGETS:
        proc = subprocess.run(
            read_argv + [target],
            capture_output=True, timeout=_TIMEOUT_S)
        if proc.returncode != 0 or not proc.stdout:
            # Non-zero here just means "the owner doesn't offer this target",
            # which is the normal answer for one of the two formats.
            continue
        paths = _parse(proc.stdout.decode("utf-8", "replace"))
        if paths:
            return paths
    return []


def _parse(text: str) -> list[str]:
    """Pull file paths out of either clipboard format.

    Both are line-based; the GNOME one prefixes a "copy"/"cut" verb line.
    Ignoring the verb is deliberate — cut is out of scope, and honouring it
    would mean deleting the user's source files on a guess.
    """
    paths = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or line in ("copy", "cut"):
            continue
        p = uri_to_path(line)
        if p:
            paths.append(p)
    return paths


# -------------------------------------------------------------------- write

def write_files(paths: list[str]) -> None:
    """Publish `paths` as file references, multi-target where possible.

    Tries the GTK4 owner first (see the module docstring); `_write_via_owner`
    never raises, so `False` is the only signal it gives, and that's the cue
    to fall through to the single-target `xclip`/`wl-copy` write that has
    always lived here.
    """
    if _write_via_owner(paths):
        return
    _write_via_fallback(paths)


def _write_via_fallback(paths: list[str]) -> None:
    _, write_argv = _tool()
    uris = [path_to_uri(p) for p in paths]
    if _is_kde():
        target = URI_LIST_TARGET
        # CRLF, per RFC 2483 — text/uri-list is a line-based format whose
        # terminator is specified, not incidental, and this module's own
        # docstring has always described the KDE flavor that way. Joining with
        # bare "\n" happened to work for a single URI (no separator appears)
        # and left a multi-file paste into Dolphin to a lenient parser.
        # Reading is unaffected: `_parse` normalizes CRLF before splitting.
        payload = "\r\n".join(uris)
    else:
        target = GNOME_TARGET
        payload = "copy\n" + "\n".join(uris)

    # stdout/stderr MUST NOT be pipes here, and this is not a tidy-up to
    # "fix" later. Per this module's opening premise, a live process has to
    # own the selection, so both `xclip -i` and `wl-copy` fork a RESIDENT
    # daemon and exit. That daemon inherits our pipes and holds them open for
    # as long as it owns the clipboard — which is indefinitely — so
    # subprocess.run, which waits for EOF on both, blocks for the entire
    # timeout and then raises TimeoutExpired. The clipboard is genuinely set
    # by then, but the contract sees the exception, reports the write as
    # unsupported, and never records the token, so the app believes its own
    # copy failed and re-adopts its own paths on the next focus. Pointing
    # them at DEVNULL leaves nothing to hold: the forking parent exits at
    # once and returncode is still ours to check.
    #
    # The read path deliberately keeps capture_output=True — `wl-paste` and
    # `xclip -o` print and exit without daemonizing, and we need their stdout.
    proc = subprocess.run(
        write_argv + [target],
        input=payload.encode("utf-8"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=_TIMEOUT_S)
    if proc.returncode != 0:
        # No stderr text to quote — capturing it is what caused the hang. The
        # exit status is the whole diagnosis available here, and the contract
        # turns this into `supported: False` either way.
        raise OSError(f"{write_argv[0]} exited with status {proc.returncode}")
