"""Whether Claude Code is usable on this machine — asked BEFORE anything needs it.

Every Claude failure in this app used to be discovered the same way: the user
did something, it failed, and a card explained the failure afterwards (SPEC §42
handles that part well). This module is the other half — the facts a first run
can be told *before* a prompt has been typed:

  * is the `claude` binary there, and where did we find it
  * is it new enough for the flag set `server/ai.py:_ai_cmd` spawns it with
  * is it signed in

`/api/config` already establishes the pattern: it publishes `learn_mount_ready`
so the sidebar's Learn entry renders only when it works, "so it's never a dead
link". Claude Code had no equivalent, so every Claude-dependent surface rendered
as available and found out otherwise on click.

**This module is the one place in the package that names an install directory.**
`server/ai.py` imports its candidate tuples from here rather than keeping a
second copy — that divergence is what let a CLI in `~/.bun/bin` produce a
working Claude-config tab and an `ai_unavailable` from `fused.ai()` on the same
machine, in the same second. `templates/claude/agent.py` keeps its own copy on
purpose (a template is standalone user-forkable code and may not import the app,
D166); `tests/test_claude_health.py` pins the two lists together so they cannot
drift silently.

**Nothing here raises.** Health is a report about the machine, and a report that
500s is worth less than one that says "I could not tell". Every probe degrades
to None/False and the caller still gets a whole answer.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Optional

from fused_render.shell import storage

# The same two defaults every subprocess in claude_config/ is spawned with, and
# for the same two reasons — see claude_config/lib.py:SUBPROCESS_KWARGS for the
# full writeup. Short version: close_fds=False forces posix_spawn instead of
# fork()+exec (a fork with libproj resident in the server dies SIGSEGV before
# exec, rc -11, no output), and pinning UTF-8 keeps a GUI-launched server — which
# inherits no LANG, so ASCII — from UnicodeDecodeError-ing on the first non-ASCII
# byte a child prints.
SUBPROCESS_KWARGS = {
    "close_fds": False,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
}

# An explicit override beats every probe below. Named identically to
# FUSED_RENDER_RCLONE_BIN, and it is what both the `notfound` error text and the
# troubleshooting guide tell users to set — so it has to be honoured by
# everything that resolves the CLI, not merely by most things.
BIN_ENV = "FUSED_RENDER_CLAUDE_BIN"

# Where Claude Code installs `claude`, for when it isn't on the PATH this process
# inherited — a Finder/Dock-launched .app gets the supervisor's PATH, not a
# shell's, so it misses ~/.local/bin and Homebrew. On Windows it is worse: a GUI
# launch inherits the PATH of its login session, so an install that appended to
# the *user* PATH afterwards stays invisible until the next sign-in.
#
# This is the UNION of the four lists that used to exist independently across
# server/ai.py, claude_config/lib.py, core_apps/learn/check_env.py and
# core_apps/sessions/analyze.py. A directory only one of them knew about was a
# directory where the app disagreed with itself.
#
# Ordered most-canonical first, `.exe` ahead of any `.cmd` shim: a shim has to be
# run through cmd.exe, which re-parses the command line (server/ai.py:_popen_cmd).
WINDOWS_CANDIDATES = (
    # native installer (irm https://claude.ai/install.ps1 | iex) — recommended
    r"%USERPROFILE%\.local\bin\claude.exe",
    # winget install Anthropic.ClaudeCode, via winget's own shim dir
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe",
    # npm install -g @anthropic-ai/claude-code, in npm's global prefix
    r"%APPDATA%\npm\claude.exe",
    r"%APPDATA%\npm\claude.cmd",
    # legacy local npm install, written by older Claude Code versions
    r"%USERPROFILE%\.claude\local\claude.exe",
)
POSIX_CANDIDATES = (
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    # legacy local npm install, written by older Claude Code versions
    "~/.claude/local/claude",
    "~/.bun/bin/claude",
    "~/Library/pnpm/claude",
    "~/.npm-global/bin/claude",
)


def candidates() -> tuple:
    """The platform's known install locations, unexpanded."""
    return WINDOWS_CANDIDATES if os.name == "nt" else POSIX_CANDIDATES


# The lowest `claude` this app's spawn line is known to work with.
#
# DELIBERATELY CONSERVATIVE, and the conservatism is the point. `_ai_cmd` passes
# --system-prompt-file, --tools=, --setting-sources=, --no-session-persistence
# and --include-partial-messages; the chat template adds --effort, --plugin-dir
# and --permission-prompt-tool. That set is verified against 2.1.220 (see the
# --tools= comment in server/ai.py), and a 1.x CLI certainly predates it — but
# nobody has bisected which 2.x minor introduced each flag. A floor of 2.0.0
# therefore catches the genuinely ancient install without ever telling someone
# whose 2.x CLI works fine that it is too old, which would be the same
# wrong-advice failure the two-tier matching in trouble.ts exists to prevent.
#
# Raise this when a newly adopted flag needs more, and say which flag in the
# commit message — this constant is the only written record of what the spawn
# line requires.
MIN_VERSION = "2.0.0"

# `claude --version` prints a bare dotted version, sometimes with a trailing
# product name ("2.1.220 (Claude Code)"). Take the first dotted run and ignore
# the rest, so a change in the surrounding text can't break the parse.
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")

# How long a probe may take. A version probe is a local exec (fast, but Node's
# startup is not free); a login-shell probe sources the user's whole profile,
# which on a heavily-configured machine genuinely takes seconds.
_VERSION_TIMEOUT_S = 10
_SHELL_TIMEOUT_S = 8


def parse_version(text: str) -> Optional[tuple]:
    """The dotted version in `text` as a tuple of ints, or None.

    Pure, so the parse is testable without a `claude` on the machine."""
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:  # pragma: no cover - the regex admits only digits
        return None


def is_outdated(found: Optional[str], floor: str = MIN_VERSION) -> bool:
    """Whether `found` is below `floor`.

    UNPARSEABLE IS NOT OUTDATED. A version string we cannot read says nothing
    about how old the CLI is, and answering True would put "your Claude Code is
    too old" in front of someone whose install is fine — the one outcome this
    check must never produce. Same for a missing version.
    """
    got, want = parse_version(found or ""), parse_version(floor)
    if got is None or want is None:
        return False
    # Zero-pad the shorter side so ("2", "2.0.0") compares equal rather than low.
    width = max(len(got), len(want))
    return got + (0,) * (width - len(got)) < want + (0,) * (width - len(want))


def augmented_path() -> str:
    """PATH with the known install dirs appended (deduped, order preserved).

    Passed through to the resolved `claude` as well as used to find it: the CLI
    is a Node program that has to locate its own interpreter, and a stripped
    GUI PATH is as short of `node` as it is of `claude`."""
    seen, parts = set(), []
    dirs = [os.path.dirname(os.path.expanduser(os.path.expandvars(c)))
            for c in candidates()]
    for part in (os.environ.get("PATH", "") or "").split(os.pathsep) + dirs:
        if part and part not in seen:
            seen.add(part)
            parts.append(part)
    return os.pathsep.join(parts)


def _executable(path: str) -> bool:
    """Whether `path` is a file we could actually exec.

    isfile AND access(X_OK), because the resolvers this replaces disagreed:
    some checked only isfile (a non-executable file shadows a real install
    further down the list) and some only access (which answers False for a
    directory, but also for a file whose bit is merely unset by a broken
    install — worth skipping past, not worth stopping on)."""
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _shell_probe() -> Optional[str]:
    """Ask the user's LOGIN SHELL where `claude` is.

    THE ONLY PROBE THAT ACTUALLY SOLVES THE PROBLEM ALL THE OTHERS EXIST FOR.
    A static candidate list can only know the install layouts somebody thought
    to write down; volta, fnm, asdf, nvm and a relocated npm prefix all put the
    binary somewhere no list will ever guess, and all of them work by editing
    the user's shell profile. Asking that profile is what finds them.

    Ported from core_apps/learn/check_env.py, which has been doing this
    correctly and alone. Its two hard-won details are kept:

      * PYTHONHOME/PYTHONPATH are scrubbed. The packaged app runs a bundled
        interpreter and exports both; a child that inherits them and is not
        that interpreter dies with "No module named 'encodings'".
      * three flag spellings are tried. `-lic` is what picks up an interactive
        rc file (where nvm/volta shims are usually set up), but not every shell
        accepts the combined form, and a shell that rejects its flags prints
        nothing rather than failing loudly.

    LAST RESORT, never first: it costs seconds and spawns the user's whole
    profile. Only reached when the override, PATH and every candidate missed,
    and its answer is cached like everything else here.
    """
    if os.name == "nt":
        # cmd/PowerShell have no login-shell profile in this sense, and the
        # `where` equivalent adds nothing over PATH + the candidate list.
        return None
    shell = os.environ.get("SHELL") or "/bin/bash"
    if not _executable(shell):
        return None
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH")}
    for flags in (["-lic"], ["-l", "-c"], ["-ic"]):
        try:
            out = subprocess.run(
                [shell, *flags, "command -v claude"],
                capture_output=True, timeout=_SHELL_TIMEOUT_S, env=env,
                **SUBPROCESS_KWARGS,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in (out or "").splitlines():
            # `command -v` may answer with a shell function or alias rather
            # than a path; only an executable file is something we can spawn.
            cand = line.strip()
            if cand and _executable(cand):
                return cand
    return None


def resolve(allow_shell: bool = True) -> tuple:
    """`(path, source)` for the `claude` to run, or `(None, None)`.

    `source` is reported all the way out to the UI, because WHERE we found it
    is itself actionable: a binary only the login shell can see means the app's
    own PATH is the problem, and the fix is FUSED_RENDER_CLAUDE_BIN rather than
    another install. A resolver that returned only the path threw that away.

    Order: explicit override, PATH, known install dirs, login shell.
    """
    forced = os.environ.get(BIN_ENV)
    if forced:
        # Reported even when it does not exist. A stale override is a REAL
        # finding — it is why the app cannot start a session — and silently
        # falling through to a working install would leave the user with an app
        # that works today and breaks whenever the override is consulted by one
        # of the paths that trusts it blindly.
        return forced, "override"
    # THE INHERITED PATH, not the augmented one, and the difference is the whole
    # value of `source`. `augmented_path()` has the candidate dirs appended, so
    # asking it here would resolve a ~/.bun/bin install and call it "path" —
    # making "path" mean nothing and "candidate" unreachable. Probing the PATH we
    # were actually launched with is what lets "path" mean *the app can see it
    # unaided* and "candidate" mean *only because we knew where to look*, which
    # is the fact the UI needs to decide whether to mention the override at all.
    found = shutil.which("claude")
    if found:
        return found, "path"
    for candidate in candidates():
        path = os.path.expanduser(os.path.expandvars(candidate))
        if _executable(path):
            return path, "candidate"
    # One more pass over the candidate dirs through `which`, which is not
    # redundant on Windows: PATHEXT resolution is how a `claude.cmd` beside a
    # missing `claude.exe` gets found, and spelling every extension into the
    # candidate list by hand is what that list used to get wrong.
    augmented = shutil.which("claude", path=augmented_path())
    if augmented:
        return augmented, "candidate"
    if allow_shell:
        via_shell = _shell_probe()
        if via_shell:
            return via_shell, "shell"
    return None, None


def probe_version(path: str) -> Optional[str]:
    """What `path --version` says, or None if it would not tell us.

    None covers every way this can fail — not on PATH any more, not executable,
    hung, exited non-zero, printed nothing parseable — because they are one fact
    to the caller: the version is unknown, so no version claim may be made.
    """
    try:
        res = subprocess.run(
            [path, "--version"], capture_output=True, timeout=_VERSION_TIMEOUT_S,
            env={**os.environ, "PATH": augmented_path()}, **SUBPROCESS_KWARGS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    # stdout normally, stderr as a fallback: some builds greet on stderr, and a
    # version on the wrong stream is still a version.
    text = (res.stdout or "") + "\n" + (res.stderr or "")
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def config_dir() -> str:
    """Claude Code's config dir.

    CLAUDE_CONFIG_DIR is Claude Code's OWN variable and wins. CLAUDE_DIR is
    read second only because claude_config/lib.py has always keyed off it, so a
    dev or test that sets it expects this module to agree; neither is invented
    here.
    """
    return (os.environ.get("CLAUDE_CONFIG_DIR")
            or os.environ.get("CLAUDE_DIR")
            or os.path.expanduser("~/.claude"))


def signed_in() -> Optional[bool]:
    """Whether Claude Code has a credential — True, False, or None for unknown.

    TRI-STATE, and the None is the whole reason this is careful rather than a
    boolean. Where the credential lives is platform-specific, and
    supervisor/paths.py already had to learn this the hard way (see its
    CLAUDE_CONFIG_DIR note — relocating the config dir logged Linux and Windows
    users out of a CLI they were signed into, while macOS hid the bug):

      * Linux/Windows — the credential is `.credentials.json` in the config
        dir. We can see it, so its absence is REAL: False.
      * macOS — the credential is in the login Keychain. We cannot read that
        without risking an access prompt, and prompting the user for their
        Keychain to draw a status line is not a trade worth making. Absence of
        a file therefore proves nothing: None.

    False means "signed out, and I checked". None means "I cannot tell from
    here". The UI must only ever offer a sign-in fix on False — claiming a
    signed-in macOS user is signed out is exactly the wrong-advice failure the
    reactive path's `_account_error` was written to avoid.
    """
    # A token in the environment is a credential on every platform, and it is
    # what a headless/CI run uses.
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        if (os.environ.get(name) or "").strip():
            return True
    creds = os.path.join(config_dir(), ".credentials.json")
    if os.path.isfile(creds):
        return True
    return None if sys.platform == "darwin" else False


# --- the cached snapshot ------------------------------------------------------
#
# Resolution can cost seconds (the login-shell probe sources a whole profile)
# and the version probe is a process spawn, so neither may run per request. The
# snapshot lives on disk under the shell home so it survives a restart — the
# facts it holds change when the user installs or signs into something, not when
# a process starts.

_CACHE_NAME = "claude-health.json"
_LOCK = threading.Lock()


def _cache_path() -> str:
    return os.path.join(storage.home_dir(), _CACHE_NAME)


def _fingerprint(path: Optional[str]) -> str:
    """What must be unchanged for a cached snapshot to still be true.

    The override and PATH decide WHICH binary we resolve; the binary's own mtime
    is how an in-place upgrade announces itself, and is what makes the version
    in the cache trustworthy rather than merely recent. `claude update` rewrites
    the file, so this invalidates exactly when it should.
    """
    stamp = ""
    if path:
        try:
            stamp = str(os.stat(path).st_mtime_ns)
        except OSError:
            stamp = "missing"
    return "\x1f".join([os.environ.get(BIN_ENV, ""),
                        os.environ.get("PATH", ""), path or "", stamp])


def _measure(allow_shell: bool = True) -> dict:
    """Run every probe and build a fresh snapshot. Never raises."""
    path, source = resolve(allow_shell=allow_shell)
    version = probe_version(path) if path else None
    # `found` is "we can run it", not "we resolved a string": a stale override
    # resolves to a path that is reported (see `resolve`) and still is not a
    # usable install, and saying found=True for it would make the strip claim
    # Claude Code is ready moments before a session fails to start.
    #
    # Only the override needs checking here. Every other source verified the file
    # during resolution — `which` tests access(X_OK) itself, and the direct probe
    # is `_executable` — while an override is taken entirely on faith, which is
    # exactly why it is the one that can be wrong.
    usable = bool(path) and (source != "override" or _executable(path))
    return {
        "found": usable,
        "path": path,
        "source": source,
        "version": version,
        "min_version": MIN_VERSION,
        # Only ever True on a version we actually read AND that is below the
        # floor — see is_outdated.
        "outdated": is_outdated(version),
        "signed_in": signed_in(),
        "config_dir": config_dir(),
        "checked_at": time.time(),
        "fingerprint": _fingerprint(path),
    }


def _read_cache() -> Optional[dict]:
    """The cached snapshot, or None when there isn't a usable one.

    Read through `storage.read_json`, which answers None for a corrupt file as
    well as a missing one. THAT IS CORRECT HERE and is not the silent-swallow
    this module would otherwise object to: a cache is disposable and entirely
    re-derivable, so a damaged one has nothing to recover and nothing to tell
    the user about. Do not "fix" this to raise — user DATA is where corruption
    has to surface, and none of it lives in this file.
    """
    data = storage.read_json(_cache_path())
    return data if isinstance(data, dict) else None


def snapshot(refresh: bool = False) -> dict:
    """The health snapshot: cached when still valid, re-measured when not.

    `refresh=True` re-measures unconditionally — what a "Check again" button
    means after the user has gone and installed or signed into something.
    """
    with _LOCK:
        if not refresh:
            cached = _read_cache()
            # Re-fingerprint against the cached PATH so an upgrade of the same
            # binary, a new override, or a PATH change all invalidate; anything
            # else is answered from disk for free.
            if cached and cached.get("fingerprint") == _fingerprint(cached.get("path")):
                return dict(cached)
        fresh = _measure()
        try:
            storage.write_json(_cache_path(), fresh)
        except OSError:
            pass  # an unwritable home is not a reason to withhold the answer
        return fresh


def warm_in_background() -> None:
    """Fill the cache off the request path, so the first GET is a disk read.

    Called from the server entry points, never from create_app — the same rule
    community.refresh_in_background follows, and for the same reason: importing
    the server in a test must not spawn the user's login shell.

    Failures are swallowed. A cold cache costs the first request its probe; a
    thread that raised would cost the log a traceback and change nothing else.
    """
    def _run() -> None:
        try:
            snapshot()
        except Exception:  # noqa: BLE001 - a warm-up must never be the failure
            pass

    threading.Thread(target=_run, daemon=True, name="claude-health").start()


def summary() -> dict:
    """The snapshot without its cache bookkeeping — the endpoint's payload.

    `fingerprint` is dropped: it is how this module decides whether to re-probe
    and means nothing to a caller, and it carries the machine's whole PATH,
    which has no business in a browser.
    """
    data = snapshot()
    return {k: v for k, v in data.items() if k != "fingerprint"}


def summary_refreshed() -> dict:
    """`summary()` after a forced re-measure."""
    snapshot(refresh=True)
    return summary()


def as_json(value: Any) -> str:  # pragma: no cover - debugging aid
    """Pretty snapshot, for `python -m` style poking at a live machine."""
    return json.dumps(value, indent=2, sort_keys=True)
