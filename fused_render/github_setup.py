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

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from typing import Optional

from fused_render import jobs
from fused_render.shell import storage

logger = logging.getLogger(__name__)

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


# --- installing `gh` without a package manager -------------------------------
#
# apt/dnf need sudo a desktop app cannot supply, and Homebrew is not a given
# either — so instead of shelling out to the platform's package manager (three
# code paths, one of them a request for a password this app cannot make), this
# downloads GitHub's own official `gh` release binary straight into
# `~/.fused-render/bin`, the same app-private dir every POSIX/WINDOWS_CANDIDATES
# entry above already knows to look in. One code path, no sudo, on all three
# platforms — the same "downloads one binary" shape as claude_install.py's
# native installer, except there the vendor supplies a shell one-liner that
# does the download and unpack itself; `gh` supplies only the archive, so this
# module is the unpacker.
#
# THE VERSION HAS TO BE ASKED FOR, NOT GUESSED. GitHub documents a stable
# `/releases/latest/download/<asset-name>` redirect for exactly this situation
# — a fixed URL that always serves whatever is current — but it only resolves
# when the REQUESTED filename already exists among the latest release's
# assets, and every `gh` asset name embeds its version
# (`gh_2.100.0_linux_amd64.tar.gz`). A filename guessed without knowing the
# version 404s outright rather than redirecting to the right file (checked
# live against the real repo: v2.100.0 was current, and a `gh_1.0.0_...` name
# against `/latest/download/` came back a plain 404). So `_fetch_latest_version`
# asks the release API which tag is current, and the download URL is built
# from that tag — one extra round trip, but one that always lands on a real
# file instead of a guess.


class InstallError(RuntimeError):
    """A refusal the caller should turn into a 4xx with this text in it."""


#: The job id in the shared registry — a `sys:` id because the server runs
#: this work, same convention as claude_install.JOB_ID.
JOB_ID = "sys:github-install"

_LATEST_RELEASE_API = "https://api.github.com/repos/cli/cli/releases/latest"

# Both bounded for the same reason claude_install.TIMEOUT_S is: a network
# that is gone should fail loudly in well under a minute of user-visible
# waiting, not hang the single-flight slot for the life of the process.
_API_TIMEOUT_S = 15
_DOWNLOAD_TIMEOUT_S = 300

# gh's release-asset OS spelling per platform.machine()'s normalized arch —
# see `_target_arch`. Kept to the two architectures this app ships for; `gh`
# itself also publishes 386 and armv6 builds, but this app does not run on
# those, so a machine reporting one is a real refusal, not a silent guess.
_ARCH_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def _target_os() -> str:
    """gh's release-asset OS name for this machine: `macOS`, `linux`, or
    `windows` — the exact three spellings its asset filenames use."""
    if sys.platform == "darwin":
        return "macOS"
    if os.name == "nt":
        return "windows"
    return "linux"


def _target_arch(machine: str) -> str:
    """gh's release-asset arch name for `platform.machine()`'s raw string.

    Pure — takes the string rather than calling `platform.machine()` itself —
    so it is testable without pretending to be a different CPU."""
    normalized = _ARCH_ALIASES.get(machine.lower())
    if normalized is None:
        raise InstallError(
            f"there is no GitHub CLI build published for this machine's "
            f"architecture ({machine!r})")
    return normalized


def _asset_name(os_kind: str, arch: str, version: str) -> str:
    """The release asset filename `gh` publishes for `(os_kind, arch,
    version)` — e.g. `gh_2.100.0_linux_amd64.tar.gz`.

    Pure: no network, no filesystem. All three platforms' names (and the
    zip-vs-tar.gz split — macOS and Windows ship zip, Linux ships tar.gz) are
    checked without a `gh` release or a network connection in play.
    """
    ext = "tar.gz" if os_kind == "linux" else "zip"
    return f"gh_{version}_{os_kind}_{arch}.{ext}"


def _release_url(version: str, asset_name: str) -> str:
    """Where `asset_name` lives for a known release `version` — a real,
    resolvable URL because `version` names an actual tag, not a guess (see
    the module docstring above for why this is not the `/latest/download/`
    shortcut)."""
    return f"https://github.com/cli/cli/releases/download/v{version}/{asset_name}"


def _member_for(os_kind: str, arch: str, version: str) -> str:
    """The path INSIDE the archive that is the `gh` binary itself — e.g.
    `gh_2.100.0_linux_amd64/bin/gh`. Every `gh` archive unpacks into one
    top-level dir named after the asset (minus its extension) containing a
    `bin/` with the binary, a LICENSE, and man pages/shell completions this
    app has no use for."""
    root = f"gh_{version}_{os_kind}_{arch}"
    name = "gh.exe" if os_kind == "windows" else "gh"
    return f"{root}/bin/{name}"


def _binary_name() -> str:
    """What the installed binary is called once it is out of the archive —
    the same name every probe in this module already looks for."""
    return "gh.exe" if os.name == "nt" else "gh"


def install_dir() -> str:
    """Where this app installs `gh` — `~/.fused-render/bin`, already the
    first entry `resolve()`'s candidate list checks on both platforms."""
    return os.path.join(storage.home_dir(), "bin")


def _fetch_latest_version() -> str:
    """Ask GitHub's release API which `gh` version is current, and return it
    without the leading `v` its tags carry. The network boundary for version
    lookup — tests monkeypatch this directly rather than the HTTP call
    underneath it, since nothing about the state machine cares HOW the
    version was learned.
    """
    request = urllib.request.Request(
        _LATEST_RELEASE_API, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=_API_TIMEOUT_S) as resp:
        data = json.load(resp)
    tag = data.get("tag_name") or ""
    return tag[1:] if tag.startswith("v") else tag


def _download(url: str, dest_path: str) -> None:
    """Fetch `url` to `dest_path`, streamed rather than buffered whole in
    memory (a `gh` archive is a few tens of MB, not worth holding twice over).

    THE NETWORK BOUNDARY. This is the one function in the module that ever
    makes an HTTP request for the archive itself — tests monkeypatch exactly
    this name, so nothing else here needs a mock server to be exercised.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "fused-render"})
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_S) as resp:
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(resp, f)


def _unpack(archive_path: str, member: str, dest_path: str) -> None:
    """Extract exactly the `gh` binary named `member` out of `archive_path`
    (zip or tar.gz, told apart by the archive's own extension) to
    `dest_path`, then chmod +x on POSIX.

    Extracts ONLY the binary — not the whole archive — because everything
    else in it (LICENSE, man pages, shell completions) is the vendor's own
    install script's job to place, and this app runs the binary, not that
    script.
    """
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            with zf.open(member) as src, open(dest_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive_path, "r:gz") as tf:
            src = tf.extractfile(member)
            if src is None:
                raise InstallError(
                    f"{member!r} was not found in the downloaded archive — "
                    "gh may have changed how it packages this release")
            with src:
                with open(dest_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    if os.name != "nt":
        st = os.stat(dest_path)
        os.chmod(dest_path, st.st_mode | 0o111)


# --- the single-flight install record -----------------------------------------
#
# One record, ported from claude_install.py's `_state`/`_lock`/`_run`/`start`
# shape (see that module's docstring for the full "why a state machine, not a
# call" argument — it applies here unchanged: this is minutes long, replaces a
# binary the rest of the app spawns, and a generic "install failed" would
# throw away a 403 vs. a proxy eating the TLS handshake, which are different
# problems with different fixes).
#
# No subprocess watchdog here, unlike claude_install._run: there is no child
# process to kill if the network stalls, because this downloads over
# `urllib.request` directly rather than shelling out to a vendor installer.
# `_DOWNLOAD_TIMEOUT_S`/`_API_TIMEOUT_S` passed straight to `urlopen` are the
# equivalent bound — a stalled read raises `socket.timeout` on its own,
# without a separate timer thread standing over it.

_install_lock = threading.Lock()
_install_state: dict = {"state": "idle", "detail": "", "error": None,
                        "started_at": None, "finished_at": None}


def _report_install(snapshot: dict) -> None:
    """Mirror one record into the job registry. Never raises — same
    discipline as claude_install._report, and for the same reason: reporting
    must never be what breaks the install."""
    try:
        state = snapshot["state"]
        jobs.upsert({
            "id": JOB_ID,
            "title": "Installing the GitHub CLI",
            "kind": "task",
            "state": (jobs.RUNNING if state == "running"
                      else "error" if state == "error" else "done"),
            "detail": snapshot["detail"],
            "message": snapshot["error"] or "",
        }, server=True)
    except Exception:  # noqa: BLE001 - reporting must never break the install
        logger.debug("could not report the gh install job")


def _publish_install(**fields) -> None:
    """Update the record and mirror it into the job registry. Never raises."""
    with _install_lock:
        _install_state.update(fields)
        snapshot = dict(_install_state)
    _report_install(snapshot)


def install_status() -> dict:
    """The record as the endpoint answers it. `error` is the download's own
    words where one is available — a 403 and a stalled TLS handshake are
    different documented problems with different fixes, and a generic message
    would leave the user exactly where they were."""
    with _install_lock:
        return dict(_install_state)


def install_running() -> bool:
    with _install_lock:
        return _install_state["state"] == "running"


def _run_install() -> None:
    """The worker body: resolve the version, download, unpack, re-probe.
    Never raises out of the thread — every exit is a `_publish_install` call,
    the same discipline claude_install._run follows for the same reason: an
    escape that skipped publishing a terminal state would wedge the record
    `running` forever.
    """
    try:
        os_kind = _target_os()
        arch = _target_arch(platform.machine())
        _publish_install(detail="Checking the latest release…")
        version = _fetch_latest_version()
        asset = _asset_name(os_kind, arch, version)
        url = _release_url(version, asset)
        member = _member_for(os_kind, arch, version)
        dest_path = os.path.join(install_dir(), _binary_name())

        _publish_install(detail=f"Downloading {asset}…")
        with tempfile.TemporaryDirectory(prefix="gh-install-") as tmp:
            archive_path = os.path.join(tmp, asset)
            _download(url, archive_path)
            _publish_install(detail="Unpacking…")
            _unpack(archive_path, member, dest_path)
    except InstallError as exc:
        _publish_install(state="error", finished_at=time.time(), error=str(exc))
        return
    except (OSError, urllib.error.URLError, zipfile.BadZipFile,
            tarfile.TarError, KeyError) as exc:
        # VERBATIM, same reasoning as claude_install's own catch: "the
        # download failed" throws away exactly the part ("403", "Errno 110:
        # Connection timed out") that tells the user what to try next.
        _publish_install(state="error", finished_at=time.time(),
                         error=f"the download failed: {exc}")
        return

    # THE RE-PROBE IS THE POINT — same argument as claude_install._run: a
    # forced re-measure finds the freshly installed binary (its directory is
    # already first in POSIX_CANDIDATES/WINDOWS_CANDIDATES, so no PATH change
    # or restart is needed) and the snapshot flips to found without the user
    # doing anything else.
    fresh = None
    try:
        fresh = summary_refreshed()
    except Exception:  # noqa: BLE001 - a probe that failed is not a failed install
        logger.warning("gh install finished but the re-probe failed")

    # A download that unpacked cleanly and left nothing runnable behind is a
    # FAILURE however happy it looked a moment ago — reporting success and
    # leaving the "Publish to GitHub" surface still unable to find `gh` would
    # be the app telling the user two contradictory things in the same
    # breath.
    if fresh is not None and not fresh.get("found"):
        _publish_install(
            state="error", finished_at=time.time(),
            error="the download finished, but the GitHub CLI still cannot "
                  "be found on this machine")
        return
    _publish_install(state="done", finished_at=time.time(),
                     detail=("gh " + (fresh or {}).get("version", "") or "").strip()
                            if fresh else "Finished")


def install_start() -> dict:
    """Kick off the `gh` install and return the opening record.

    Refuses rather than queues — two installs writing the same file at once
    is not a race worth surviving, and the honest answer to "install again
    while an install is running" is that one already is.
    """
    with _install_lock:
        if _install_state["state"] == "running":
            raise InstallError("a GitHub CLI install is already running")

    # THE ACTUAL GUARD, and it has to be one operation — same reasoning as
    # claude_install.start: FastAPI runs POSTs on a threadpool, so two
    # concurrent requests could both pass the early check above before either
    # claims the slot. Re-testing state in the SAME critical section that
    # claims it is what makes "one at a time" true rather than likely.
    with _install_lock:
        if _install_state["state"] == "running":
            raise InstallError("a GitHub CLI install is already running")
        _install_state.update(state="running", detail="Starting…", error=None,
                              started_at=time.time(), finished_at=None)
        claimed = dict(_install_state)
    _report_install(claimed)  # mirrored to the job registry outside the lock

    # A CATCH-ALL AROUND THE WHOLE BODY. The slot is claimed now, so any
    # escape from `_run_install` that did not publish a terminal state would
    # leave the record `running` forever and refuse every later install.
    def _guarded() -> None:
        try:
            _run_install()
        except BaseException as exc:  # noqa: BLE001 - a stuck record is worse
            logger.exception("the gh install worker died")
            _publish_install(state="error", finished_at=time.time(),
                             error=f"the install stopped unexpectedly: {exc}")

    threading.Thread(target=_guarded, daemon=True, name="gh-install").start()
    return claimed


def install_reset() -> None:
    """Test seam — the record is module state and suites share a module."""
    with _install_lock:
        _install_state.update({"state": "idle", "detail": "", "error": None,
                               "started_at": None, "finished_at": None})
