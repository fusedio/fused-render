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
# `claude auth status` measured at 0.6-1.5s (it reads local credential state;
# it is not a round trip to the API). Bounded like everything else here so a
# wedged CLI cannot pin the probe.
_AUTH_TIMEOUT_S = 10


def _probe_cmd(path: str, *args: str):
    """How to spawn `path` with `args` for a probe: an argv list, or — behind a
    Windows .cmd/.bat shim — one command STRING for the cmd.exe hop.

    npm installs claude as a .cmd shim, which CreateProcess cannot run directly;
    only cmd.exe can. Without this both probes below would raise OSError on
    every npm-installed Windows CLI and report version and sign-in as "unknown"
    — safe, since unknown never produces advice, but useless to the one platform
    where the PATH problem this module exists for is worst.

    server/ai.py:_popen_cmd solves the same problem for the completion spawn and
    is not reused here: it cannot be (ai.py imports THIS module, so the
    dependency only runs one way), and it does not need to be. Its difficulty is
    quoting values that came from a caller; every argument here is a static
    literal of ours plus a path we resolved, so the simple form is correct. A
    path containing a quote is refused rather than mis-quoted — Windows paths
    cannot contain one, so this is an assertion, not a fallback.
    """
    if not (os.name == "nt" and path.lower().endswith((".cmd", ".bat"))):
        return [path, *args]
    if '"' in path:
        raise ValueError(f"path may not contain a double quote: {path!r}")
    return " ".join(f'"{part}"' for part in (path, *args))


def _run_probe(path: str, *args: str, timeout: float):
    """Run a bounded probe, returning the completed process (or raising).

    `shell=True` ONLY on the .cmd path, where `_probe_cmd` returned a string:
    that is the cmd.exe hop, not a shell-injection surface — the payload is
    ours, fully quoted, and carries no user text.
    """
    cmd = _probe_cmd(path, *args)
    return subprocess.run(
        cmd, capture_output=True, timeout=timeout, shell=isinstance(cmd, str),
        env={**os.environ, "PATH": augmented_path()}, **SUBPROCESS_KWARGS,
    )


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


def executable(path: str) -> bool:
    """Whether `path` is a file we could actually exec.

    isfile AND the exec bit, because the resolvers this replaces disagreed:
    some checked only isfile (so a non-executable file shadows a real install
    further down the list, and then fails to spawn) and some only access.

    PUBLIC, and `server/ai.py:_claude_bin` calls it rather than testing isfile
    itself. The two now walk the same candidate list, so a check that differed
    between them would put the health report and the spawn on different
    binaries — health calling the install ready while the session dies on the
    dud, which is precisely the contradiction this module exists to end.

    The exec bit is only consulted off Windows. There it means nothing —
    `os.access(X_OK)` is true for any existing file, so the test would be
    isfile twice — and what actually decides runnability is the extension,
    which the candidate list spells out (`.exe` ahead of `.cmd`). Skipping it
    explicitly rather than relying on that no-op also keeps this honest under a
    test that simulates Windows on a POSIX filesystem.
    """
    if not os.path.isfile(path):
        return False
    return os.name == "nt" or os.access(path, os.X_OK)


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
    if not executable(shell):
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
            if cand and executable(cand):
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
    if forced and forced != _ADOPTED:
        # A value the USER set. Reported even when it does not exist: a stale
        # override is a REAL finding — it is why the app cannot start a session
        # — and silently falling through to a working install would leave them
        # with an app that works today and breaks whenever the override is
        # consulted by one of the paths that trusts it blindly.
        return forced, "override"
    if forced:
        # One WE published (see `adopt`), which is a convenience and must never
        # become a trap. Two things follow, and both are the opposite of the
        # branch above:
        #
        #   * It is VERIFIED rather than trusted. If the path has since gone —
        #     the CLI was upgraded, volta switched versions, it was uninstalled
        #     — the value is dropped and resolution falls through to the full
        #     chain, including a fresh shell probe. Otherwise the process would
        #     be stuck reporting a dead override until it restarted, and "Check
        #     again" could never recover.
        #   * It reports "shell", not "override". That is where it came from,
        #     and it keeps `override` meaning "the user set this" — without
        #     which a vanished adoption renders a card blaming them for an
        #     environment variable they never set.
        if executable(forced):
            return forced, "shell"
        _forget()
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
        if executable(path):
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
        res = _run_probe(path, "--version", timeout=_VERSION_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
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


def _auth_status(path: str) -> Optional[bool]:
    """`claude auth status`'s own answer, or None when it did not give one.

    THE CLI IS THE ONLY THING THAT ACTUALLY KNOWS. It prints JSON:

        {"loggedIn": false, "authMethod": "none", "apiProvider": "firstParty"}

    Parsed rather than trusted by exit code, and the parse is the compatibility
    check too: a CLI too old for the subcommand exits 1 with
    `error: unknown command 'status'` on stderr and NOTHING on stdout, so
    "stdout is JSON carrying a boolean loggedIn" cleanly separates a real answer
    from every way of not getting one. Any other shape is None, not False —
    guessing "signed out" from output we could not read is how a signed-in user
    gets told to go and sign in.
    """
    try:
        res = _run_probe(path, "auth", "status", timeout=_AUTH_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    try:
        data = json.loads(res.stdout or "")
    except ValueError:
        return None
    value = data.get("loggedIn") if isinstance(data, dict) else None
    return value if isinstance(value, bool) else None


def signed_in(path: Optional[str] = None) -> Optional[bool]:
    """Whether Claude Code has a credential — True, False, or None for unknown.

    TRI-STATE, and the None carries real weight: `False` is what puts "sign in"
    in front of the user, so it may only ever come from an authoritative answer.

    Order, and the ordering is the correctness argument:

    1. **Ask the CLI** (`claude auth status`). It is the only party that knows,
       and it knows on every platform.
    2. **Positive evidence only** — an env token, or `.credentials.json` in the
       config dir. Enough to say True when the CLI could not be asked (it is
       missing, or predates the subcommand).
    3. **Otherwise None.**

    STEP 2 CAN NEVER ANSWER FALSE, and that is a correction rather than caution.
    This used to conclude "no credential file on Linux/Windows, therefore signed
    out", reasoning from supervisor/paths.py's note that macOS keeps its
    credential in the login Keychain while the others keep it in the config dir.
    That note is true and the inference from it was still wrong: a credential
    can also arrive on an inherited file descriptor
    (CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR), through a managed provider, or in
    any other place a future release chooses. Measured on a container that is
    demonstrably logged in (`claude auth status` → `"loggedIn": true`) with no
    credentials file and no token in the environment, the old rule answered
    False — the wrong-advice failure this module is arranged to avoid, produced
    by the module itself.

    So absence of a file is not evidence of being signed out, on any platform,
    and the platform split is gone with it: what separates a real False from
    "cannot tell" is whether the CLI answered, not which OS this is.
    """
    if path:
        answer = _auth_status(path)
        if answer is not None:
            return answer
    # A token in the environment is a credential on every platform, and it is
    # what a headless/CI run uses.
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        if (os.environ.get(name) or "").strip():
            return True
    if os.path.isfile(os.path.join(config_dir(), ".credentials.json")):
        return True
    return None


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


#: The path THIS PROCESS published into the override (see `adopt`), so it can be
#: told apart from one the user set. The distinction decides both whether a dead
#: value is dropped and who gets blamed for it — see `resolve`.
_ADOPTED: Optional[str] = None


def _forget() -> None:
    """Drop an adoption that no longer resolves, override and record together.

    Both halves matter: leaving the env var would keep every spawn path pointed
    at a dead file, and leaving `_ADOPTED` set would make the NEXT adoption of
    the same path look like it was already published.
    """
    global _ADOPTED
    if _ADOPTED is not None and os.environ.get(BIN_ENV) == _ADOPTED:
        os.environ.pop(BIN_ENV, None)
    _ADOPTED = None


def adopt(path: str) -> bool:
    """Publish `path` as the override, so every spawn path finds it too.

    THE SHELL PROBE'S ANSWER IS ONLY USEFUL IF SOMETHING ACTS ON IT. It is the
    one resolver that can find a volta/fnm/asdf/nvm install, and it runs HERE —
    but the two paths that actually start Claude Code (`server/ai.py:_claude_bin`
    and the chat template's own copy) both go override → PATH → candidates, and
    neither shells out. So without this, a shell-only install left the health
    report saying "found" while every session still failed to start, and the
    only way out was telling the user to go and set an environment variable we
    were already holding the value for.

    Setting the override is what closes that, and it is the right lever rather
    than a clever one: it is the documented escape hatch, it is what the
    `notfound` error text tells users to set, and BOTH spawn paths already
    honour it — including the chat template, which cannot import this module
    (D166) but does inherit the server's environment. One assignment reaches
    code that is not allowed to know this module exists.

    A USER'S OWN SETTING IS NEVER OVERWRITTEN. Someone who set the override
    deliberately has said which binary to run, and a probe silently replacing it
    would be the app overruling an explicit instruction — the same reason
    `resolve` reports a stale override rather than falling through to something
    that works.

    Process-scoped on purpose: nothing is written to the user's config or shell
    profile. The probe re-runs at the next start and re-adopts, so this can
    never leave a stale path behind on a machine where the CLI has moved.
    """
    global _ADOPTED
    if os.environ.get(BIN_ENV):
        return False
    os.environ[BIN_ENV] = path
    _ADOPTED = path
    return True


def _measure(allow_shell: bool = True) -> dict:
    """Run every probe and build a fresh snapshot. Never raises."""
    path, source = resolve(allow_shell=allow_shell)
    # Before anything else reads it: a binary only the login shell could find is
    # one the spawn paths cannot find at all until it is published (see `adopt`).
    if source == "shell" and path:
        adopt(path)
    version = probe_version(path) if path else None
    # `found` is "we can run it", not "we resolved a string": a stale override
    # resolves to a path that is reported (see `resolve`) and still is not a
    # usable install, and saying found=True for it would make the strip claim
    # Claude Code is ready moments before a session fails to start.
    #
    # Only the override needs checking here. Every other source verified the file
    # during resolution — `which` tests access(X_OK) itself, and the direct probe
    # is `executable` — while an override is taken entirely on faith, which is
    # exactly why it is the one that can be wrong.
    usable = bool(path) and (source != "override" or executable(path))
    return {
        "found": usable,
        "path": path,
        "source": source,
        "version": version,
        "min_version": MIN_VERSION,
        # Only ever True on a version we actually read AND that is below the
        # floor — see is_outdated.
        "outdated": is_outdated(version),
        # The resolved path only when it is RUNNABLE: asking a binary that
        # isn't there for its auth state wastes a spawn on a certain failure,
        # and the fallback below still answers True on positive evidence.
        "signed_in": signed_in(path if usable else None),
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


def _public(data: dict) -> dict:
    """A snapshot without its cache bookkeeping — the endpoint's payload shape.

    `fingerprint` is dropped: it is how this module decides whether to re-probe
    and means nothing to a caller, and it carries the machine's whole PATH,
    which has no business in a browser.
    """
    return {k: v for k, v in data.items() if k != "fingerprint"}


def summary() -> dict:
    """The cached snapshot, as the endpoint answers it."""
    return _public(snapshot())


def summary_refreshed() -> dict:
    """`summary()` after a forced re-measure.

    Returns the measurement it just took, rather than re-reading through
    `summary()`. Two reasons, and the first is a bug: a cache write that failed
    (an unwritable home, which this module deliberately tolerates) would leave
    `summary()` re-reading the OLD file, so "Check again" would answer with the
    snapshot the user pressed the button to get past. The second is simply that
    re-reading costs a second full probe whenever there is no cache to read.
    """
    return _public(snapshot(refresh=True))


def as_json(value: Any) -> str:  # pragma: no cover - debugging aid
    """Pretty snapshot, for `python -m` style poking at a live machine."""
    return json.dumps(value, indent=2, sort_keys=True)
