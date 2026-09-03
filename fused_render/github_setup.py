"""Whether the GitHub CLI is usable on this machine — asked BEFORE the first
"Publish to GitHub" click needs it.

Ported from claude_health.py's shape, deliberately: that module exists so a
Claude-dependent surface is never a dead link, and "Publish to GitHub" is
about to be exactly that kind of surface. The facts a first run can be told
up front are the same two questions:

  * is the `gh` binary there, and where did we find it
  * is it signed in, and as whom

**Nothing here raises.** A report that 500s is worth less than one that says
"I could not tell". Every probe degrades to None/False and the caller still
gets a whole answer.

Two real differences from `claude auth status` shape this module's parser:

  * `gh --version` prints a bare "gh version X.Y.Z (date)" line rather than a
    dotted string on its own, but the same "find the first dotted run" regex
    still reads it.
  * `gh auth status` prints human-readable TEXT to stderr, not JSON, and its
    EXIT CODE is authoritative: 0 means signed in, non-zero means signed out.
    `claude auth status` needed a tri-state (True/False/None) because a CLI
    too old for the subcommand exits non-zero with no way to tell "signed
    out" from "could not ask" apart — `gh auth status` carries no such
    ambiguity, so `signed_in` here is a plain bool, never None.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from typing import Optional

from fused_render.shell import storage

# The same defaults every subprocess in claude_health.py is spawned with, and
# for the same reason (see claude_health.SUBPROCESS_KWARGS for the full
# writeup): close_fds=False forces posix_spawn instead of fork()+exec (a fork
# with libproj resident in the server dies SIGSEGV before exec), and pinning
# UTF-8 keeps a GUI-launched server — which inherits no LANG, so ASCII — from
# UnicodeDecodeError-ing on the first non-ASCII byte a child prints.
SUBPROCESS_KWARGS = {
    "close_fds": False,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
}

# An explicit override beats every probe below. Named on the same pattern as
# claude_health.BIN_ENV, for the same reason: it is the escape hatch a stale
# or shadowed resolution can be worked around with.
BIN_ENV = "FUSED_RENDER_GH_BIN"

# Where `gh` gets installed, for when it isn't on the PATH this process
# inherited — a Finder/Dock-launched .app gets the supervisor's PATH, not a
# shell's, so it misses ~/.local/bin and Homebrew (see claude_health.py's
# WINDOWS_CANDIDATES/POSIX_CANDIDATES docstring for the full argument).
#
# `~/.fused-render/bin` is this app's OWN install directory — nothing writes
# into it yet, but Task 2 installs `gh` there, and a candidate list that did
# not already know to look is a candidate list a Dock-launched app would
# never see it through.
WINDOWS_CANDIDATES = (
    r"%USERPROFILE%\.fused-render\bin\gh.exe",
    r"%ProgramFiles%\GitHub CLI\gh.exe",
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\gh.exe",
)
POSIX_CANDIDATES = (
    "~/.fused-render/bin/gh",
    "/opt/homebrew/bin/gh",
    "/usr/local/bin/gh",
    "/usr/bin/gh",
    "~/.local/bin/gh",
)


def candidates() -> tuple:
    """The platform's known install locations, unexpanded."""
    return WINDOWS_CANDIDATES if os.name == "nt" else POSIX_CANDIDATES


# `gh --version` prints e.g. "gh version 2.63.0 (2024-10-30)". Take the first
# dotted run and ignore the rest, so a change in the surrounding text can't
# break the parse — same idiom as claude_health._VERSION_RE.
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")

# How long a probe may take. Both are local execs against credential state on
# disk, not a round trip to the API, so both are cheap — bounded anyway so a
# wedged CLI cannot pin the probe.
_VERSION_TIMEOUT_S = 10
_AUTH_TIMEOUT_S = 10


def parse_version(text: str) -> Optional[str]:
    """The dotted version in `text`, or None. Pure, so it is testable without
    a `gh` on the machine."""
    if not text:
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def executable(path: str) -> bool:
    """Whether `path` is a file we could actually exec. Same discipline as
    claude_health.executable: isfile AND the exec bit off Windows, because the
    exec bit means nothing there (os.access(X_OK) is true for any existing
    file)."""
    if not os.path.isfile(path):
        return False
    return os.name == "nt" or os.access(path, os.X_OK)


def resolve() -> tuple:
    """`(path, source)` for the `gh` to run, or `(None, None)`.

    Order: explicit override, PATH, known install dirs. No login-shell probe
    (unlike claude_health.resolve): `gh` has no volta/fnm/nvm-style ecosystem
    of relocated installs to hunt for, so the two cheap steps plus the
    fixed-path fallback list cover the machines this targets — a Dock-launched
    app missing ~/.local/bin/Homebrew, and this app's own bin dir once Task 2
    populates it.
    """
    forced = os.environ.get(BIN_ENV)
    if forced:
        # A value the USER (or FUSED_RENDER_GH_BIN) set. Reported even when it
        # does not exist: a stale override is a real finding, and silently
        # falling through to a working install would leave the app resolving
        # a different binary than the override implies.
        return forced, "override"
    found = shutil.which("gh")
    if found:
        return found, "path"
    for candidate in candidates():
        path = os.path.expanduser(os.path.expandvars(candidate))
        if executable(path):
            return path, "candidate"
    return None, None


def _run_probe(path: str, *args: str, timeout: float):
    """Run a bounded probe, returning the completed process (or raising).

    Absolute path, close_fds=False, no cwd= — the subprocess discipline
    test_git_posix_spawn.py pins for every git spawn in the package, and the
    same reasoning applies to any spawn in this server: a fork with libproj
    resident in the process dies SIGSEGV before exec. `path` is already
    resolved to an absolute file by `resolve()`/the caller, so no `-C` or cwd
    is ever needed here.
    """
    return subprocess.run(
        [path, *args], capture_output=True, timeout=timeout, **SUBPROCESS_KWARGS,
    )


def probe_version(path: str) -> Optional[str]:
    """What `path --version` says, or None if it would not tell us.

    None covers every way this can fail — not executable any more, hung,
    exited non-zero, printed nothing parseable — because they are one fact to
    the caller: the version is unknown.
    """
    try:
        res = _run_probe(path, "--version", timeout=_VERSION_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    text = (res.stdout or "") + "\n" + (res.stderr or "")
    return parse_version(text)


# `gh auth status` prints, per logged-in host:
#
#   github.com
#     ✓ Logged in to github.com account octocat (keyring)
#     - Active account: true
#     ...
#
# and on sign-out exits non-zero with a message like "You are not logged
# into any GitHub hosts." — no JSON, unlike `claude auth status`. This reads
# the account name that follows "Logged in to github.com account" and
# ignores everything else, including a second host block for a GHE
# instance: `gh` supports being signed into github.com and an Enterprise
# host at once, and this feature targets github.com only, so any other
# host's line is simply never matched.
_GITHUB_COM_ACCOUNT_RE = re.compile(
    r"Logged in to github\.com account (\S+)")


def parse_auth_status(text: str, returncode: int) -> dict:
    """`gh auth status`'s answer, as `{signed_in, account}`. Pure, so it is
    testable without a CLI.

    THE EXIT CODE IS AUTHORITATIVE, unlike claude_health.signed_in's tri-state:
    `gh auth status` has no "too old for this subcommand" ambiguity to guard
    against, so a non-zero exit is always signed_in=False and a zero exit is
    always signed_in=True — the text is consulted only to name WHO, never to
    decide whether. A future output format this parser does not recognise
    still degrades correctly: signed in with no account, or signed out.
    """
    if returncode != 0:
        return {"signed_in": False, "account": None}
    match = _GITHUB_COM_ACCOUNT_RE.search(text or "")
    return {"signed_in": True, "account": match.group(1) if match else None}


def _auth_status(path: str) -> Optional[dict]:
    """`gh auth status`'s parsed answer, or None when the probe itself could
    not run (hung, missing, raised) — as opposed to a normal signed-out exit,
    which `parse_auth_status` already reports as a real `{signed_in: False}`
    answer, not a None.
    """
    try:
        res = _run_probe(path, "auth", "status", timeout=_AUTH_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    # gh prints its report to stderr; stdout is checked too since a future
    # version could move it, the same "either stream, whichever has it"
    # tolerance as claude_health.probe_version.
    text = (res.stderr or "") + "\n" + (res.stdout or "")
    return parse_auth_status(text, res.returncode)


# --- the cached snapshot ------------------------------------------------------
#
# Same reasoning as claude_health.py's cache: resolution and both probes are
# process spawns, so neither may run per request. The snapshot lives on disk
# under the shell home so it survives a restart.

_CACHE_NAME = "github-setup.json"
_LOCK = threading.Lock()

#: Bumped whenever the MEANING of a field changes, so a snapshot written by an
#: older build is discarded rather than served — same doctrine as
#: claude_health._CACHE_VERSION.
_CACHE_VERSION = 1

#: How long a snapshot may be served before it is re-measured. Signing out of
#: `gh` changes no path and touches no binary, so — same as
#: claude_health._MAX_AGE_S — only the age check ever notices it; a
#: fingerprint-only cache would serve a stale "signed in" forever.
_MAX_AGE_S = 60.0


def _cache_path() -> str:
    return os.path.join(storage.home_dir(), _CACHE_NAME)


def _fingerprint(path: Optional[str]) -> str:
    """What must be unchanged for a cached snapshot to still be true — the
    override, PATH, resolved path, and the binary's own mtime (an in-place
    upgrade rewrites the file, so this invalidates exactly when it should)."""
    stamp = ""
    if path:
        try:
            stamp = str(os.stat(path).st_mtime_ns)
        except OSError:
            stamp = "missing"
    return "\x1f".join([str(_CACHE_VERSION), os.environ.get(BIN_ENV, ""),
                        os.environ.get("PATH", ""), path or "", stamp])


def _measure() -> dict:
    """Run every probe and build a fresh snapshot. Never raises."""
    path, source = resolve()
    usable = bool(path) and executable(path)
    version = probe_version(path) if usable else None
    # ONE `gh auth status` spawn answers both "signed in?" and "as whom?".
    # Only asked when the binary is actually runnable: asking one that isn't
    # there wastes a spawn on a certain failure.
    auth = _auth_status(path) if usable else None
    signed_in = auth["signed_in"] if auth is not None else False
    account = auth["account"] if auth is not None else None

    return {
        "found": usable,
        "path": path,
        "source": source,
        "version": version,
        "signed_in": signed_in,
        "account": account,
        "checked_at": time.time(),
        "fingerprint": _fingerprint(path),
    }


def _too_old(cached: dict) -> bool:
    """Whether `cached` has aged past the point of being trustworthy. Same
    NaN-safe range check as claude_health._too_old, and for the same reasons:
    an unreadable/missing checked_at, or one from the future (a clock
    correction, a suspended laptop), both count as too old rather than
    permanently fresh."""
    taken = cached.get("checked_at")
    if not isinstance(taken, (int, float)) or isinstance(taken, bool):
        return True
    return not 0 <= time.time() - taken <= _MAX_AGE_S


def _read_cache() -> Optional[dict]:
    """The cached snapshot, or None when there isn't a usable one. A corrupt
    file is disposable and entirely re-derivable, so it is treated the same
    as a missing one (see claude_health._read_cache)."""
    data = storage.read_json(_cache_path())
    return data if isinstance(data, dict) else None


def snapshot(refresh: bool = False) -> dict:
    """The health snapshot: cached when still valid, re-measured when not.

    `refresh=True` re-measures unconditionally — what "Check again" means
    after the user has gone and installed or signed into `gh`.
    """
    with _LOCK:
        if not refresh:
            cached = _read_cache()
            if (cached
                    and cached.get("fingerprint") == _fingerprint(cached.get("path"))
                    and not _too_old(cached)):
                return dict(cached)
        fresh = _measure()
        try:
            storage.write_json(_cache_path(), fresh)
        except OSError:
            pass  # an unwritable home is not a reason to withhold the answer
        return fresh


def _public(data: dict) -> dict:
    """A snapshot without its cache bookkeeping — the endpoint's payload
    shape. `fingerprint` carries the machine's whole PATH, which has no
    business in a browser."""
    return {k: v for k, v in data.items() if k != "fingerprint"}


def summary() -> dict:
    """The cached snapshot, as the endpoint answers it."""
    return _public(snapshot())


def summary_refreshed() -> dict:
    """`summary()` after a forced re-measure. Returns the measurement it just
    took rather than re-reading through `summary()` — the same fix
    claude_health.summary_refreshed applies, and for the same reason: an
    unwritable home is tolerated by design, so re-reading through the cache
    could serve the stale file "Check again" was pressed to get past."""
    return _public(snapshot(refresh=True))
