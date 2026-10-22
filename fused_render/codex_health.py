"""Whether OpenAI Codex CLI is usable on this machine — asked BEFORE anything
needs it. The Codex twin of `copilot_health.py` (docs/CODEX_BACKEND.md D10),
with the same public surface and the same snapshot keys, so the strip that
renders one can render the other without learning a third shape.

What is shared and what is copied follows the line `copilot_health` draws:
the GENERIC pieces (subprocess kwargs, version parsing, `executable`, the
shell-rc / `path_fix` machinery, the cache-age rule) are imported from
`claude_health`; the pieces that spell "codex" in a string literal (override
name, candidate list, login-shell probe, `resolve` and its adoption record,
cache file, fingerprint) are COPIED with the name changed.

The probes differ where the CLIs differ. `codex login status` exists, but it
answers in prose ("Logged in using ChatGPT" / "Not logged in"), so the
signed-in question goes to the CLI's own app-server instead, which answers in
JSON (verified 2026-10-21 against 0.163.0): `initialize` → the `initialized`
notification → `account/read {refreshToken: false}`. The reply is
`{"account": null, "requiresOpenaiAuth": true}` signed out, and
`{"account": {"type": "chatgpt", "email": …, "planType": "go"}, …}` (or
`{"type": "apiKey"}`) signed in. No model is spawned and no prompt is sent:
the probe is free. There is a `codex doctor` (0.163.0), but the contract does
not use it yet, so `doctor` is always None and the install method is a path
guess.

**One probe, no second credential.** Unlike Copilot there is no fallback
token to try: the probe runs with the user's own environment, CODEX_HOME
exactly as found (it is where `codex login` keeps the credential), and the
snapshot's `account.method` is the CLI's own account type, mapped.

**Nothing here raises.** Same doctrine as the Claude module: a health report
that 500s is worth less than one that says "I could not tell".
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
from typing import Optional

from fused_render import claude_health
from fused_render.claude_health import (
    SUBPROCESS_KWARGS,
    _too_old,
    executable,
    parse_version,
    path_fix,
)
from fused_render.shell import storage

# An explicit override beats every probe below. Same role as
# FUSED_RENDER_CLAUDE_BIN: `adopt` publishes a shell-only find into it so the
# spawn paths (codex_host via agent.py) resolve the same binary health did.
BIN_ENV = "FUSED_RENDER_CODEX_BIN"

# Where the installers put `codex`, for when it isn't on the PATH this process
# inherited (D10, in this order). `npm install -g` lands in whichever prefix
# npm is configured for — ~/.npm-global is the documented no-sudo one —
# Homebrew links into /opt/homebrew/bin (Apple Silicon) or /usr/local/bin
# (Intel, and the default npm prefix there too); the rest are user-local
# prefixes and the bun/pnpm global bins.
POSIX_CANDIDATES = (
    "~/.npm-global/bin/codex",
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    "~/.local/bin/codex",
    "~/.bun/bin/codex",
    "~/Library/pnpm/codex",
)


def candidates() -> tuple:
    """The known install locations, unexpanded."""
    return POSIX_CANDIDATES


# The lowest `codex` the host's protocol is known to work with.
#
# DELIBERATELY CONSERVATIVE, same argument as copilot_health.MIN_VERSION. The
# host speaks app-server protocol v2 (thread/start, turn/start with per-turn
# policy, the v2 approval requests, account/read); all verified on 0.163.0
# only. Nobody has bisected which release introduced each, so the floor sits a
# few releases under the verified one rather than AT it (an assumption, D10).
# Raise it when a newly adopted method needs more, and say which in the commit.
MIN_VERSION = "0.160.0"


def is_outdated(found: Optional[str], floor: str = MIN_VERSION) -> bool:
    """claude_health.is_outdated against THIS floor — a bare re-export would
    default to Claude's 2.0.0 and call every Codex release too old."""
    return claude_health.is_outdated(found, floor)


_VERSION_TIMEOUT_S = 10
_SHELL_TIMEOUT_S = 8
#: The app-server sign-in probe: a Node start, the native binary's start, and
#: two local RPCs. Measured at ~0.3s signed out and ~1.1s signed in (the
#: signed-in read touches the token store); bounded so a wedged CLI cannot pin
#: a health read, and the timeout reads as "unknown", never as "signed out".
_RPC_TIMEOUT_S = 15
#: How long each reap step waits before escalating (see `_reap`).
_REAP_GRACE_S = 2.0

#: `codex --version` prints `codex-cli 0.163.0` on stdout. Matched by name
#: first: the npm wrapper can put a WARNING on stderr whose temp path carries
#: digits (`/tmp/claude-0/…`), which a bare first-dotted-run parse would read
#: as the version.
_BANNER_RE = re.compile(r"codex-cli\s+v?(\d+(?:\.\d+)+)")


def augmented_path() -> str:
    """PATH with the known install dirs appended (deduped, order preserved).

    Built on top of claude_health's, not instead of it: an npm-installed Codex
    is a Node script (`bin/codex.js`) that needs `node`, and the Node prefixes
    a GUI-launched app is missing are exactly the ones the Claude list knows.
    """
    seen, parts = set(), []
    dirs = [os.path.dirname(os.path.expanduser(os.path.expandvars(c)))
            for c in candidates()]
    for part in claude_health.augmented_path().split(os.pathsep) + dirs:
        if part and part not in seen:
            seen.add(part)
            parts.append(part)
    return os.pathsep.join(parts)


def _child_env() -> dict:
    """The environment every probe and child here runs with: the user's own,
    PYTHONHOME/PYTHONPATH scrubbed (the packaged app's bundled interpreter
    exports both), PATH augmented so the CLI finds `node`. CODEX_HOME is
    passed through exactly as found — nothing here sets, moves or clears it."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH")}
    env["PATH"] = augmented_path()
    return env


def _run_probe(path: str, *args: str, timeout: float):
    """Run a bounded probe, returning the completed process (or raising)."""
    return subprocess.run(
        [path, *args], capture_output=True, timeout=timeout,
        env=_child_env(), **SUBPROCESS_KWARGS,
    )


def _shell_probe() -> Optional[str]:
    """Ask the user's LOGIN SHELL where `codex` is.

    A copy of claude_health._shell_probe with the binary name changed — see
    that docstring for why it exists (volta/fnm/nvm installs no list can
    guess), why PYTHONHOME/PYTHONPATH are scrubbed, and why three flag
    spellings are tried. Last resort, and cached like everything else here.
    """
    shell = os.environ.get("SHELL") or "/bin/bash"
    if not executable(shell):
        return None
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH")}
    for flags in (["-lic"], ["-l", "-c"], ["-ic"]):
        try:
            out = subprocess.run(
                [shell, *flags, "command -v codex"],
                capture_output=True, timeout=_SHELL_TIMEOUT_S, env=env,
                **SUBPROCESS_KWARGS,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in (out or "").splitlines():
            cand = line.strip()
            if cand and executable(cand):
                return cand
    return None


#: The path THIS PROCESS published into the override (see `adopt`), so it can
#: be told apart from one the user set — see claude_health for the argument.
_ADOPTED: Optional[str] = None


def _forget() -> None:
    """Drop an adoption that no longer resolves, override and record together."""
    global _ADOPTED
    if _ADOPTED is not None and os.environ.get(BIN_ENV) == _ADOPTED:
        os.environ.pop(BIN_ENV, None)
    _ADOPTED = None


def adopt(path: str) -> bool:
    """Publish `path` as the override, so every spawn path finds it too.

    Process-scoped, never overwrites a user's own setting — the same contract
    as claude_health.adopt. The host (claude_agent/codex_host.py, via
    agent.py) inherits the server's environment, which is how a binary only
    the login shell could find reaches code that may not import this module.
    """
    global _ADOPTED
    if os.environ.get(BIN_ENV):
        return False
    os.environ[BIN_ENV] = path
    _ADOPTED = path
    return True


def resolve(allow_shell: bool = True) -> tuple:
    """`(path, source)` for the `codex` to run, or `(None, None)`.

    A copy of claude_health.resolve against this module's override and
    candidate list; the source vocabulary (override / path / candidate /
    shell) and every rule about it are that function's.
    """
    forced = os.environ.get(BIN_ENV)
    if forced and forced != _ADOPTED:
        return forced, "override"
    if forced:
        if executable(forced):
            return forced, "shell"
        _forget()
    found = shutil.which("codex")
    if found:
        return found, "path"
    for candidate in candidates():
        path = os.path.expanduser(os.path.expandvars(candidate))
        if executable(path):
            return path, "candidate"
    augmented = shutil.which("codex", path=augmented_path())
    if augmented:
        return augmented, "candidate"
    if allow_shell:
        via_shell = _shell_probe()
        if via_shell:
            return via_shell, "shell"
    return None, None


def probe_version(path: str) -> Optional[str]:
    """What `path --version` says, or None if it would not tell us.

    0.163.0 prints `codex-cli 0.163.0`. The named banner is looked for in
    both streams; failing that, the first dotted run of STDOUT only — stderr
    is where the npm wrapper's warnings go (see `_BANNER_RE`).
    """
    try:
        res = _run_probe(path, "--version", timeout=_VERSION_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    banner = _BANNER_RE.search((res.stdout or "") + "\n" + (res.stderr or ""))
    got = parse_version(banner.group(1) if banner else (res.stdout or ""))
    return ".".join(str(p) for p in got) if got else None


def config_dir() -> str:
    """Codex's home: `CODEX_HOME` is the CLI's own override (the app-server's
    `initialize` reply echoes it as `codexHome`), `~/.codex` otherwise."""
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


# --- the credential, and whose it is ------------------------------------------


def _classify(result: Optional[dict]) -> Optional[bool]:
    """An `account/read` result → True / False / None-unknown.

    Only `account: null` WITH `requiresOpenaiAuth: true` is False: that is
    the CLI saying "nobody is signed in and somebody must be". A null account
    with `requiresOpenaiAuth: false` is a configured model provider that needs
    no OpenAI credential, so nothing is missing (assumed from the schema; not
    exercised live). Any other shape is None, because False is what puts
    "sign in" in front of the user and a sign-in cannot fix a reply we could
    not read.
    """
    if not isinstance(result, dict):
        return None
    acct = result.get("account")
    if isinstance(acct, dict):
        return True
    if acct is not None:
        return None
    needs = result.get("requiresOpenaiAuth")
    if needs is True:
        return False
    if needs is False:
        return True
    return None


def _reap(proc: subprocess.Popen) -> None:
    """Close stdin, wait, then SIGTERM, wait, then SIGKILL — in that order.

    The app-server exits on stdin EOF (verified), so the first step is
    normally the last. TERM BEFORE KILL because an npm install's `codex` is a
    Node wrapper (`bin/codex.js`) that spawns the native binary and forwards
    SIGINT/SIGTERM/SIGHUP to it; a SIGKILL to the wrapper cannot be forwarded
    and would orphan the real server, once per health read.

    PLAIN signals to the child, NOT A PROCESS GROUP: `start_new_session=True`
    forces CPython off posix_spawn onto fork()+exec, and this runs in the
    server, where a fork with libproj resident dies SIGSEGV before exec
    (claude_spawn.py's history). The wrapper's forwarding is what reaches the
    grandchild.
    """
    try:
        proc.stdin.close()
    except (OSError, ValueError):
        pass
    for step, grace in ((None, _REAP_GRACE_S), (proc.terminate, _REAP_GRACE_S),
                        (proc.kill, 5)):
        try:
            if step is not None:
                step()
            proc.wait(grace)
            return
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return


def _app_server_probe(path: str) -> Optional[dict]:
    """Ask the CLI's app-server who is signed in: the `account/read` result,
    or None when it would not say.

    `initialize` → (its reply) → the `initialized` notification and
    `account/read {refreshToken: false}` — the notification is required
    before any other request, so the second write waits for the first reply.
    `refreshToken: false` keeps the read local: it reports the stored
    credential and never refreshes it over the network. Timeout, a dead
    child, an error reply, output we could not parse — all None, because
    "could not tell" must never render as "sign in".

    No `cwd=` (the fork argument in `_reap`) and no tempdir: `account/read`
    needs no workspace, and nothing here creates a thread for a cwd to name.
    """
    proc = None
    answer: list = []

    def _send(*msgs: dict) -> None:
        for msg in msgs:
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def _reader(stream) -> None:
        # Runs until EOF or the answer; the deadline is kept by the join below.
        try:
            for line in stream:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                # A server→client REQUEST also carries an `id` (and a
                # `method`); only a reply to one of ours is read here.
                if not isinstance(msg, dict) or "method" in msg:
                    continue
                if msg.get("id") == 1:
                    if not isinstance(msg.get("result"), dict):
                        return  # initialize refused: unknown
                    _send({"method": "initialized"},
                          {"id": 2, "method": "account/read",
                           "params": {"refreshToken": False}})
                elif msg.get("id") == 2:
                    answer.append(msg)
                    return
        except (OSError, ValueError):
            pass

    try:
        proc = subprocess.Popen(
            [path, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=_child_env(),
            **SUBPROCESS_KWARGS,
        )
        _send({"id": 1, "method": "initialize",
               "params": {"clientInfo": {"name": "fused-render", "version": "1"}}})
        thread = threading.Thread(target=_reader, args=(proc.stdout,), daemon=True)
        thread.start()
        thread.join(_RPC_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        if proc is not None:
            _reap(proc)
    if not answer:
        return None
    result = answer[0].get("result")
    return result if isinstance(result, dict) else None


def _sign_in(path: str) -> tuple:
    """`(signed, result)` — one app-server probe, classified. `result` is the
    raw `account/read` reply (None when there was none), for `account`."""
    result = _app_server_probe(path)
    return _classify(result), result


def signed_in(path: Optional[str] = None) -> Optional[bool]:
    """Whether Codex has a credential — True, False, or None for unknown.

    Tri-state like claude_health.signed_in and for the same reason: False is
    what puts "sign in" in front of the user, so only the CLI may say it.
    Without a runnable CLI there is nothing to ask and no positive evidence
    worth trusting (an `auth.json` may hold an expired login), so None.
    """
    if path:
        return _sign_in(path)[0]
    return None


#: The CLI's account `type` → the snapshot's `method`. `apiKey` is respelled
#: so the strip's vocabulary stays snake_case; an unknown type passes through
#: as the CLI spelled it, and the UI shows it as a plain "signed in".
_METHODS = {"chatgpt": "chatgpt", "apiKey": "api_key", "amazonBedrock": "bedrock"}


def account(result: Optional[dict]) -> Optional[dict]:
    """WHO is signed in, in the shape the wizard's "Signed in" row reads:
    `{method, email, org, plan}` — from an `account/read` result whose
    `account` is an object, else None. A ChatGPT login carries `email` and
    `planType` (verified, plan `go`); an API key carries neither; there is no
    org field in the reply, so `org` is always None."""
    acct = result.get("account") if isinstance(result, dict) else None
    if not isinstance(acct, dict):
        return None

    def _s(key: str) -> Optional[str]:
        v = acct.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    kind = _s("type")
    return {"method": _METHODS.get(kind or "", kind), "email": _s("email"),
            "org": None, "plan": _s("planType")}


# --- what kind of install is this, and can we repair it ----------------------

#: The install line the app runs itself (codex_install.py) and shows first —
#: the route OpenAI's README documents. `brew install codex` exists too, and
#: `install_method` recognises it, but npm is the one route that is the same
#: on every POSIX machine with Node.
INSTALL_COMMAND_POSIX = "npm install -g @openai/codex"


def install_command() -> str:
    """The install line, as a user would type it."""
    return INSTALL_COMMAND_POSIX


#: Install methods the app updates itself, with the line it runs (the same
#: npm line: `npm install -g` with no version installs the `latest` tag over
#: whatever is there), and the ones it only names. 0.163.0 also has a
#: `codex update`; it is not used, because what it does under each install
#: method is unverified and an npm reinstall is not.
UPDATES_ITSELF = ("npm",)
MANAGED_UPDATE_COMMANDS = {
    "brew": "brew upgrade codex",
}

#: Path fingerprints, matched against the path AND its realpath (lowercased).
#: npm is tested FIRST, and that order is the point: a global npm install
#: under a Homebrew-installed Node lives in /opt/homebrew/lib/node_modules and
#: is linked from /opt/homebrew/bin, so a brew-first test would hand an npm
#: install to `brew upgrade`, which does not own it. A brew-installed Codex is
#: a native binary under the Cellar/Caskroom with no node_modules anywhere.
_NPM_NEEDLES = ("/node_modules/", "/.npm-global/", "/.nvm/", "/.volta/",
                "/.fnm/", "/fnm/", "/.bun/", "/library/pnpm/",
                "/.local/share/pnpm/")
_BREW_NEEDLES = ("/opt/homebrew/", "/usr/local/cellar/", "/usr/local/caskroom/",
                 "/home/linuxbrew/")


def install_method(path: Optional[str]) -> Optional[str]:
    """How this copy was installed: `npm` | `brew` | `other`, or None with no
    path. By path (and the path a symlink resolves to — `/usr/local/bin/codex`
    is an npm link into `…/lib/node_modules/@openai/codex/bin/codex.js` as
    often as a Homebrew one), since nothing asks the CLI. `other` keeps the
    update on offer (see `update_plan`)."""
    if not path:
        return None
    seen = [path.lower()]
    try:
        seen.append(os.path.realpath(path).lower())
    except (OSError, ValueError):
        pass
    if any(n in p for p in seen for n in _NPM_NEEDLES):
        return "npm"
    if any(n in p for p in seen for n in _BREW_NEEDLES):
        return "brew"
    return "other"


def update_plan(method: Optional[str]) -> dict:
    """Whether an update can be offered, and what would run — the three
    outcomes of claude_health.update_plan. An install the app cannot place
    (`other`, None) keeps the offer with an unknown `updatable`: a wrong
    guess that withholds an update is the failure these modules avoid."""
    if method in UPDATES_ITSELF:
        return {"updatable": True, "command": INSTALL_COMMAND_POSIX,
                "manager": None, "reason": None}
    if method in MANAGED_UPDATE_COMMANDS:
        return {"updatable": False, "command": MANAGED_UPDATE_COMMANDS[method],
                "manager": method,
                "reason": f"{method} owns this install, so it updates through {method}"}
    return {"updatable": None, "command": INSTALL_COMMAND_POSIX, "manager": None,
            "reason": None}


def add_to_shell_path() -> dict:
    """Append the PATH line to the user's shell rc — the strip's one-click fix.

    claude_health.add_to_shell_path against this module's resolver; the
    idempotence rule (by directory, not by line) and the error sentences are
    its. Returns `{ok, rc_file, line}` or `{ok: False, error}`.
    """
    path, _source = resolve(allow_shell=False)
    if not path or not executable(path):
        return {"ok": False, "error": "there is no Codex CLI on this machine "
                                      "to put on the PATH — install it first"}
    fix = path_fix(path)
    if not fix:
        return {"ok": False, "error": "this shell's profile isn't one the app "
                                      "can safely edit — add the directory to "
                                      "your PATH by hand"}
    rc_path = os.path.expanduser(fix["rc_file"])
    bindir = os.path.dirname(os.path.abspath(path))
    rel = "$HOME" + bindir[len(os.path.expanduser("~")):]
    try:
        os.makedirs(os.path.dirname(rc_path) or ".", exist_ok=True)
        existing = ""
        if os.path.exists(rc_path):
            with open(rc_path, "r", encoding="utf-8", errors="replace") as f:
                existing = f.read()
        if bindir in existing or rel in existing:
            return {"ok": True, "rc_file": fix["rc_file"], "line": fix["line"],
                    "already": True}
        with open(rc_path, "a", encoding="utf-8") as f:
            lead = "" if (not existing or existing.endswith("\n")) else "\n"
            f.write(f"{lead}{fix['line']}\n")
    except OSError as exc:
        return {"ok": False, "error": f"could not write {fix['rc_file']}: {exc}"}
    return {"ok": True, "rc_file": fix["rc_file"], "line": fix["line"],
            "already": False}


# --- the cached snapshot ------------------------------------------------------
#
# Same shape and same two gates as claude_health: a fingerprint for "is this
# the same binary" and an age for "could the answer have changed anyway" (a
# sign-out moves no file this watches). Its own cache file, so the three
# reports never read each other's.

_CACHE_NAME = "codex-health.json"
_LOCK = threading.Lock()
_CACHE_VERSION = 1
_MAX_AGE_S = 60.0


def _cache_path() -> str:
    return os.path.join(storage.home_dir(), _CACHE_NAME)


def _fingerprint(path: Optional[str]) -> str:
    """What must be unchanged for a cached snapshot to still be true.
    CODEX_HOME is in it because it moves the credential store, which moves
    `signed_in` and `account`."""
    stamp = ""
    if path:
        try:
            stamp = str(os.stat(path).st_mtime_ns)
        except OSError:
            stamp = "missing"
    return "\x1f".join([str(_CACHE_VERSION), os.environ.get(BIN_ENV, ""),
                        os.environ.get("PATH", ""),
                        os.environ.get("CODEX_HOME", ""),
                        path or "", stamp])


def _measure(allow_shell: bool = True) -> dict:
    """Run every probe and build a fresh snapshot. Never raises."""
    path, source = resolve(allow_shell=allow_shell)
    if source == "shell" and path:
        adopt(path)
    version = probe_version(path) if path else None
    usable = bool(path) and (source != "override" or executable(path))
    outdated = is_outdated(version, MIN_VERSION)
    broken = usable and version is None
    method = install_method(path) if usable else None
    plan = update_plan(method)

    on_shell_path: Optional[bool] = None
    if usable:
        if source in ("path", "shell"):
            on_shell_path = True
        elif source == "candidate" and allow_shell:
            on_shell_path = _shell_probe() is not None
    fix = path_fix(path) if (usable and on_shell_path is False) else None

    # One app-server spawn answers both "signed in?" and "as whom?".
    signed, result = _sign_in(path) if usable else (None, None)
    acct = account(result) if signed else None
    if signed is None:
        # A probe that could not tell is no news about the credential, so the
        # last snapshot's account stands: one slow read must not blank the
        # strip's "Signed in as …" row.
        acct = _carried_account() if usable else None

    return {
        "found": usable,
        "path": path,
        "source": source,
        "version": version,
        "min_version": MIN_VERSION,
        "outdated": outdated,
        "broken": broken,
        "signed_in": signed,
        "account": acct,
        "config_dir": config_dir(),
        "platform": sys.platform,
        "install_command": install_command(),
        "install_method": method,
        "updatable": plan["updatable"],
        "update_command": plan["command"],
        "update_manager": plan["manager"],
        "update_blocked_reason": plan["reason"],
        # `codex doctor` exists but is not consulted (D10); kept so the
        # snapshots share a shape.
        "doctor": None,
        "on_shell_path": on_shell_path,
        "path_fix_command": fix["command"] if fix else None,
        "checked_at": time.time(),
        "fingerprint": _fingerprint(path),
    }


def _read_cache() -> Optional[dict]:
    data = storage.read_json(_cache_path())
    return data if isinstance(data, dict) else None


def _carried_account() -> Optional[dict]:
    """The last snapshot's `account`, for a measure whose probe could not
    tell. Read regardless of the fingerprint — an upgraded binary or a moved
    PATH does not move the credential — but only from the same credential
    store, which is what `config_dir` names."""
    prev = _read_cache()
    if not prev or prev.get("config_dir") != config_dir():
        return None
    acct = prev.get("account")
    return dict(acct) if isinstance(acct, dict) else None


def cached() -> Optional[dict]:
    """The cached snapshot if still trustworthy, else None — NEVER a measure."""
    c = _read_cache()
    if c and c.get("fingerprint") == _fingerprint(c.get("path")) and not _too_old(c):
        return dict(c)
    return None


def snapshot(refresh: bool = False) -> dict:
    """The health snapshot: cached when still valid, re-measured when not."""
    with _LOCK:
        if not refresh:
            c = _read_cache()
            if (c and c.get("fingerprint") == _fingerprint(c.get("path"))
                    and not _too_old(c)):
                return dict(c)
        fresh = _measure()
        try:
            storage.write_json(_cache_path(), fresh)
        except OSError:
            pass  # an unwritable home is not a reason to withhold the answer
        return fresh


def warm_in_background() -> None:
    """Fill the cache off the request path. Called from the server entry
    points, never from create_app — importing the server in a test must not
    spawn the user's login shell. Failures are swallowed."""
    def _run() -> None:
        try:
            snapshot()
        except Exception:  # noqa: BLE001 - a warm-up must never be the failure
            pass

    threading.Thread(target=_run, daemon=True, name="codex-health").start()


def _public(data: dict) -> dict:
    """A snapshot without its cache bookkeeping (the fingerprint carries PATH)."""
    return {k: v for k, v in data.items() if k != "fingerprint"}


def summary() -> dict:
    """The cached snapshot, as the endpoint answers it."""
    return _public(snapshot())


def summary_refreshed() -> dict:
    """`summary()` after a forced re-measure, answering with the measurement
    it just took (see claude_health.summary_refreshed for the two reasons)."""
    return _public(snapshot(refresh=True))
