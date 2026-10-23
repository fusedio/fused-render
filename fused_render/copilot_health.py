"""Whether GitHub Copilot CLI is usable on this machine — asked BEFORE anything
needs it. The Copilot twin of `claude_health.py` (D10), with the same public
surface and the same snapshot keys, so the strip that renders one can render
the other without learning a second shape.

What is shared and what is copied, and why the line falls where it does:

  * the GENERIC pieces are imported from `claude_health` — the subprocess
    kwargs, version parsing and the floor comparison, `executable`, the
    shell-rc / `path_fix` machinery and the cache-age rule. They take a path or
    a string and have no idea which CLI they serve.
  * the pieces that spell "claude" in a string literal (the override name, the
    candidate list, the login-shell probe, `resolve` and its adoption record,
    the cache file, the fingerprint) are COPIED with the name changed. That
    module is deliberately left untouched: it is the one place in the package
    that names Claude's install directories and `server/ai.py` pins its tuples.

The probes differ where the CLIs differ. There is no `copilot auth status`;
the only party that knows whether Copilot has a usable credential is the CLI's
own ACP server, so `signed_in` speaks two JSON-RPC messages to it (D10, verified
2026-10-16 against 1.0.98: a stored credential answers `session/new` with a
result; none answers `{"code": -32000, "message": "Authentication required"}`).
There is no `copilot doctor` either, so `doctor` is always None and the install
method is a path guess.

**The snapshot is the authority on which credential a spawn uses.** The probe
runs in two steps (see `_sign_in`): the CLI's own credentials first — a
`copilot login`, or a token the USER put in the environment — and only when
that says signed out, a second probe with `gh auth token`'s answer as
COPILOT_GITHUB_TOKEN. `account.method` records which step answered, and the
host (claude_agent/agent.py) injects the gh token ONLY when the cached
snapshot's `account.method == "gh"`. So a `gh` account without Copilot access
never shadows a working `copilot login`, and a healthy machine never shells
out to `gh` at all: the second probe costs a spawn only when the first is False.

**The snapshot also carries the account's model list** (`models`), asked of
the CLI's headless SDK server right after the sign-in probe, with the
credential that probe proved: which ids `--model` takes on THIS plan, and per
model whether and at which levels `--reasoning-effort` applies. The catalog
serves it in place of the static shortlist and `agent.py` reads it to know
when to drop the effort flag — see the "which models" section below.

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
import tempfile
import threading
import time
from typing import Optional

from fused_render import github_setup
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
# spawn paths (copilot_host via agent.py) resolve the same binary health did.
BIN_ENV = "FUSED_RENDER_COPILOT_BIN"

# Where the installers put `copilot`, for when it isn't on the PATH this process
# inherited. The gh.io install script lands in ~/.local/bin (PREFIX=$HOME/.local
# as a non-root user); Homebrew is a cask; the rest are npm prefixes.
POSIX_CANDIDATES = (
    "~/.local/bin/copilot",
    "/opt/homebrew/bin/copilot",
    "/usr/local/bin/copilot",
    "~/.npm-global/bin/copilot",
    "~/.bun/bin/copilot",
    "~/Library/pnpm/copilot",
)


def candidates() -> tuple:
    """The known install locations, unexpanded."""
    return POSIX_CANDIDATES


# The lowest `copilot` the host's spawn line is known to work with.
#
# DELIBERATELY CONSERVATIVE, same argument as claude_health.MIN_VERSION. The
# host passes --acp, --agent, --plugin-dir, --additional-mcp-config, --add-dir,
# --allow-tool and --session-id; all verified on 1.0.98 only. Nobody has
# bisected which 1.0.x minor introduced each, so the floor sits a few releases
# under the verified one rather than AT it — telling someone whose 1.0.95 works
# that it is too old would be the wrong-advice failure this module avoids.
# Raise it when a newly adopted flag needs more, and say which in the commit.
MIN_VERSION = "1.0.90"


def is_outdated(found: Optional[str], floor: str = MIN_VERSION) -> bool:
    """claude_health.is_outdated against THIS floor. Re-exporting it bare
    would default a `copilot_health.is_outdated("1.0.98")` to Claude's 2.0.0
    and answer "too old" to a working install."""
    return claude_health.is_outdated(found, floor)

_VERSION_TIMEOUT_S = 10
_SHELL_TIMEOUT_S = 8
#: The ACP sign-in probe: a Node start plus two local RPCs. Measured at ~1.5s
#: signed in and signed out alike; bounded so a wedged CLI cannot pin a
#: health read, and the timeout reads as "unknown", never as "signed out".
_ACP_TIMEOUT_S = 15
#: `gh auth token` is a local credential-store read.
_GH_TOKEN_TIMEOUT_S = 5


def augmented_path() -> str:
    """PATH with the known install dirs appended (deduped, order preserved).

    Built on top of claude_health's, not instead of it: a Copilot installed by
    npm needs `node`, and the Node prefixes a GUI-launched app is missing are
    exactly the ones the Claude list already knows.
    """
    seen, parts = set(), []
    dirs = [os.path.dirname(os.path.expanduser(os.path.expandvars(c)))
            for c in candidates()]
    for part in claude_health.augmented_path().split(os.pathsep) + dirs:
        if part and part not in seen:
            seen.add(part)
            parts.append(part)
    return os.pathsep.join(parts)


def _child_env(extra: Optional[dict] = None) -> dict:
    """The environment every probe and child here runs with.

    PYTHONHOME/PYTHONPATH scrubbed (the packaged app's bundled interpreter
    exports both; a Node child that inherits them is unaffected, but a shell
    probe that execs python is not), PATH augmented so the CLI finds its own
    runtime. `extra` is for the one caller that has a token to hand over.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH")}
    env["PATH"] = augmented_path()
    if extra:
        env.update(extra)
    return env


def _run_probe(path: str, *args: str, timeout: float):
    """Run a bounded probe, returning the completed process (or raising)."""
    return subprocess.run(
        [path, *args], capture_output=True, timeout=timeout,
        env=_child_env(), **SUBPROCESS_KWARGS,
    )


def _shell_probe() -> Optional[str]:
    """Ask the user's LOGIN SHELL where `copilot` is.

    A copy of claude_health._shell_probe with the binary name changed — see
    that docstring for why it exists (volta/fnm/nvm installs no list can
    guess), why PYTHONHOME/PYTHONPATH are scrubbed, and why three flag
    spellings are tried. Last resort, and cached like everything else here.
    """
    if sys.platform == "win32":
        return None  # no login shell to ask; the plain PATH was already searched
    shell = os.environ.get("SHELL") or "/bin/bash"
    if not executable(shell):
        return None
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH")}
    for flags in (["-lic"], ["-l", "-c"], ["-ic"]):
        try:
            out = subprocess.run(
                [shell, *flags, "command -v copilot"],
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
    as claude_health.adopt. The host (claude_agent/copilot_host.py, via
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
    """`(path, source)` for the `copilot` to run, or `(None, None)`.

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
    found = shutil.which("copilot")
    if found:
        return found, "path"
    for candidate in candidates():
        path = os.path.expanduser(os.path.expandvars(candidate))
        if executable(path):
            return path, "candidate"
    augmented = shutil.which("copilot", path=augmented_path())
    if augmented:
        return augmented, "candidate"
    if allow_shell:
        via_shell = _shell_probe()
        if via_shell:
            return via_shell, "shell"
    return None, None


def probe_version(path: str) -> Optional[str]:
    """What `path --version` says, or None if it would not tell us.

    1.0.98 prints `GitHub Copilot CLI 1.0.98.` then an update hint on a second
    line; parse_version takes the first dotted run and ignores the rest.
    """
    try:
        res = _run_probe(path, "--version", timeout=_VERSION_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    text = (res.stdout or "") + "\n" + (res.stderr or "")
    got = parse_version(text)
    return ".".join(str(p) for p in got) if got else None


def config_dir() -> str:
    """Copilot's config dir: `COPILOT_HOME` is the CLI's own override
    (`copilot help environment`), `~/.copilot` otherwise."""
    return os.environ.get("COPILOT_HOME") or os.path.expanduser("~/.copilot")


# --- the credential, and whose it is ------------------------------------------
#
# Copilot has three ways to be signed in and reads them in this order: a token
# in COPILOT_GITHUB_TOKEN (then GH_TOKEN, GITHUB_TOKEN), else the credential
# `copilot login` stored. D10 adds the fused twist: when those leave Copilot
# signed out and `gh` is signed in, `gh auth token`'s answer goes into
# COPILOT_GITHUB_TOKEN, so a machine with no Copilot login and a working `gh`
# still works. It is a FALLBACK, never a precedence: a user-set
# COPILOT_GITHUB_TOKEN is never overridden, and a stored login that works is
# never shadowed by a gh account that has no Copilot seat.

#: The env names Copilot reads a token from, in its own precedence order.
TOKEN_ENV = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


def gh_token() -> Optional[str]:
    """`gh auth token`'s answer, or None. The value is handed to one child
    process and then dropped: NEVER logged, never cached, never in the
    snapshot — `account.method == "gh"` is the only trace it leaves.

    `gh` is resolved through github_setup, not `which`: the app installs its
    own `gh` under the shell home, which a Dock-launched PATH never sees.
    """
    path, _source = github_setup.resolve()
    if not path or not executable(path):
        return None
    try:
        res = subprocess.run(
            [path, "auth", "token"], capture_output=True,
            timeout=_GH_TOKEN_TIMEOUT_S, env=_child_env(), **SUBPROCESS_KWARGS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    token = (res.stdout or "").strip()
    return token or None


#: What `session/new` answers with a credential missing (observed on 1.0.98:
#: `{"code": -32000, "message": "Authentication required"}`). The message is
#: matched ANCHORED: a substring test on "auth"/"sign" also matches "not
#: authorized to use Copilot" — an entitlement error, which a sign-in does not
#: fix — and "design", "signal", "author".
_SIGNED_OUT_CODE = -32000
_SIGNED_OUT_RE = re.compile(r"(?:authentication required|not logged in)\b")

#: A Copilot session id. The probe deletes `session-state/<id>/` by it, so
#: anything else — `..`, a slash — is never joined onto a path. Either case:
#: a UUID's hex digits may be spelled upper.
_SESSION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _classify(reply: dict) -> Optional[bool]:
    """A `session/new` reply → True / False / None-unknown. Only the signed-out
    error is False; every other error is None, because False is what puts
    "sign in" in front of the user and a sign-in cannot fix those."""
    if "result" in reply:
        return True
    error = reply.get("error")
    if not isinstance(error, dict):
        return None
    text = str(error.get("message") or "").strip().lower()
    if error.get("code") == _SIGNED_OUT_CODE or _SIGNED_OUT_RE.match(text):
        return False
    return None


def _forget_session(reply: dict) -> None:
    """Delete the session the probe's `session/new` created. Copilot persists
    every ACP session under `session-state/<id>/` (verified live), so without
    this each health read leaves one behind for `copilot --resume` to list."""
    result = reply.get("result")
    sid = result.get("sessionId") if isinstance(result, dict) else None
    if isinstance(sid, str) and _SESSION_ID_RE.fullmatch(sid):
        shutil.rmtree(os.path.join(config_dir(), "session-state", sid),
                      ignore_errors=True)


def _acp_probe(path: str, token: Optional[str]) -> Optional[bool]:
    """Ask the CLI's ACP server for a session — True / False / None-unknown.

    `initialize` then `session/new` on a throwaway cwd; `_classify` reads the
    reply. Timeout, a dead child, output we could not parse — all None,
    because "could not tell" must never render as "sign in". A session that
    was created is deleted again once the child is reaped.

    `token` goes into COPILOT_GITHUB_TOKEN unless the user already set that
    name: theirs is the credential Copilot will use, so it is the one probed.

    PLAIN `proc.kill()`, NOT A PROCESS GROUP. `start_new_session=True` forces
    CPython off posix_spawn onto fork()+exec, and this runs in the server, where
    a fork with libproj resident dies SIGSEGV before exec (claude_spawn.py's
    history). --disable-builtin-mcps keeps the child from starting MCP servers
    of its own, so there is nothing a group kill would reach that a kill of the
    child does not. No `cwd=` for the same reason; the tempdir travels only in
    the RPC params.
    """
    extra = ({"COPILOT_GITHUB_TOKEN": token}
             if token and not os.environ.get("COPILOT_GITHUB_TOKEN") else None)
    workdir = tempfile.mkdtemp(prefix="fused-copilot-probe-")
    proc = None
    answer: list = []

    def _reader(stream) -> None:
        # Runs until EOF; the deadline is kept by the join below.
        try:
            for line in stream:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == 2:
                    answer.append(msg)
                    return
        except (OSError, ValueError):
            pass

    try:
        proc = subprocess.Popen(
            [path, "--acp", "--no-auto-update", "--disable-builtin-mcps"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=_child_env(extra),
            **SUBPROCESS_KWARGS,
        )
        for msg in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": 1, "clientCapabilities": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "session/new",
             "params": {"cwd": workdir, "mcpServers": []}},
        ):
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        thread = threading.Thread(target=_reader, args=(proc.stdout,), daemon=True)
        thread.start()
        thread.join(_ACP_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        if proc is not None:
            # The host's own reap order (contract §5): close stdin, wait, THEN
            # kill. An ACP server exits on stdin EOF and tears down the MCP
            # servers it started (a user's ~/.copilot/mcp-config.json ones are
            # not covered by --disable-builtin-mcps); a bare SIGKILL would
            # orphan those, once per health read.
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(5)
                except (OSError, subprocess.SubprocessError):
                    pass
            except OSError:
                pass
        shutil.rmtree(workdir, ignore_errors=True)
    if not answer:
        return None
    # After the reap, never before: a live child could still write the dir.
    _forget_session(answer[0])
    return _classify(answer[0])


def _user_tokens() -> list:
    """The token values the USER put in the environment, in TOKEN_ENV order."""
    return [v for v in ((os.environ.get(n) or "").strip() for n in TOKEN_ENV) if v]


def _sign_in_detail(path: str) -> tuple:
    """`(signed, via_gh, token)` — the two-step probe the module docstring
    describes, with the gh token the second step proved (None otherwise) so
    the model listing that follows can spawn with the same credential.

    Step one probes the CLI's own credentials (stored login, or a user-set
    token, which stays in the env). Only a definite False goes on to step two,
    and only when there is a gh token that is not already the user's — with
    GH_TOKEN set, `gh auth token` just echoes it back — and the user has not
    set COPILOT_GITHUB_TOKEN, which the gh token would otherwise override.
    A second probe that says False leaves step one's False standing: without
    `method == "gh"` the host spawns without the token, so False is the truth
    about the spawn that will happen. One that could not tell (a timeout)
    makes the whole answer None — a slow probe on a gh-only machine must not
    read as "sign in" (`_measure` then carries the last account forward).
    """
    first = _acp_probe(path, None)
    if first is not False or os.environ.get("COPILOT_GITHUB_TOKEN"):
        return first, False, None
    token = gh_token()
    if not token or token in _user_tokens():
        return first, False, None
    second = _acp_probe(path, token)
    if second is None:
        return None, False, None
    return (True, True, token) if second else (first, False, None)


def _sign_in(path: str) -> tuple:
    """`(signed, via_gh)` — `_sign_in_detail` without the token."""
    signed, via_gh, _token = _sign_in_detail(path)
    return signed, via_gh


def signed_in(path: Optional[str] = None) -> Optional[bool]:
    """Whether Copilot has a credential — True, False, or None for unknown.

    Tri-state like claude_health.signed_in and for the same reason: False is
    what puts "sign in" in front of the user, so only the CLI may say it.
    With a runnable CLI the two-step probe is the answer; without one, a
    token in the environment is positive evidence, and absence of one is
    nothing.
    """
    if path:
        answer, _via_gh = _sign_in(path)
        if answer is not None:
            return answer
    return True if _user_tokens() else None


def account(signed: Optional[bool], via_gh: bool) -> Optional[dict]:
    """WHO is signed in, in the shape the wizard's "Signed in" row reads:
    `{method, email, org, plan}`. Copilot's ACP tells us nothing about the
    account, so the three identity fields are always None; `method` says
    which credential the host will spawn with, and agent.py reads it to decide
    whether to inject the gh token:

      * "token"   — a token the user put in the environment, and it works;
      * "copilot" — no such token; the CLI's stored `copilot login` works;
      * "gh"      — neither works and the gh-token probe did (`_sign_in`).
    """
    if signed is not True:
        return None
    if via_gh:
        method = "gh"
    elif any((os.environ.get(n) or "").strip() for n in TOKEN_ENV):
        method = "token"
    else:
        method = "copilot"
    return {"method": method, "email": None, "org": None, "plan": None}


# --- which models this account may run, and what each takes ------------------
#
# Copilot's model list is PER ACCOUNT: the plan (Free, Pro, Pro+, Business,
# Enterprise) and an organisation's model policy decide which ids `--model`
# accepts, and the set moves with every release. ACP has no `models.list`, but
# the CLI's SDK server (`copilot --headless --stdio`, JSON-RPC with
# Content-Length framing, the transport the bundled `copilot-sdk` speaks) has
# one, and it answers with the ids the signed-in account may run and, per
# model, whether `--reasoning-effort` applies and which levels it takes
# (verified 2026-10-23 against 1.0.103: `connect` → `{ok, protocolVersion:3,
# version}`, `models.list` → `{models:[{id, name, capabilities{supports{
# reasoningEffort}}, policy{state}, billing{multiplier}, supportedReasoning
# Efforts?, defaultReasoningEffort?, modelPickerCategory?, vendor?}]}`; signed
# out it answers `-32603 "... Not authenticated. Please authenticate first."`).
#
# The list rides in the health snapshot as `models`, measured right after the
# sign-in probe with the credential that probe found works, and the catalog
# (`agent_backends/copilot.py`) serves it in place of the static shortlist
# while `agent.py` reads the same file to know which models take an effort.
# A listing that could not tell carries the previous snapshot's list forward,
# the way `account` is carried: a slow read must not shrink the picker.

#: The SDK server: a Node start plus two local RPCs, the second of which asks
#: the Copilot API for the account's catalog. Bounded like the ACP probe; a
#: timeout reads as "could not tell", never as "no models".
_HEADLESS_TIMEOUT_S = 20
#: How the app introduces itself on `connect` (the server refuses the ACP
#: `clientInfo` shape: it wants editor/extension names).
_HEADLESS_CLIENT = {"editorName": "fused-render", "editorVersion": "1"}


def _frame(msg: dict) -> bytes:
    body = json.dumps(msg).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)


def _read_frames(stream, want_id: int, out: list) -> None:
    """Read Content-Length frames off `stream` until the reply to `want_id`
    arrives (appended to `out`) or EOF. Runs on a thread; the deadline is the
    caller's join."""
    buf = b""
    try:
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(1)
            if not chunk:
                return
            buf += chunk
            while True:
                cut = buf.find(b"\r\n\r\n")
                if cut < 0:
                    break
                length = None
                for header in buf[:cut].decode("ascii", "replace").split("\r\n"):
                    name, _, value = header.partition(":")
                    if name.strip().lower() == "content-length":
                        try:
                            length = int(value.strip())
                        except ValueError:
                            length = None
                if length is None:
                    return  # not the protocol we expected: give up, not guess
                if len(buf) < cut + 4 + length:
                    break
                body, buf = buf[cut + 4:cut + 4 + length], buf[cut + 4 + length:]
                try:
                    msg = json.loads(body)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == want_id:
                    out.append(msg)
                    return
    except (OSError, ValueError):
        return


def _headless_models(path: str, token: Optional[str]) -> Optional[list]:
    """`models.list` over the CLI's headless server — the raw model dicts, or
    None when it could not tell (timeout, a dead child, an error reply, output
    that is not the protocol). Never raises. Same child discipline as
    `_acp_probe`: no process group, no `cwd=`, close-then-wait-then-kill.
    `token` rides in COPILOT_GITHUB_TOKEN unless the user set that name."""
    extra = ({"COPILOT_GITHUB_TOKEN": token}
             if token and not os.environ.get("COPILOT_GITHUB_TOKEN") else None)
    # BYTES on both pipes: Content-Length counts bytes, so the text mode the
    # shared kwargs ask for would miscount any non-ASCII model name.
    kwargs = {k: v for k, v in SUBPROCESS_KWARGS.items()
              if k not in ("text", "encoding", "errors", "universal_newlines")}
    proc = None
    answer: list = []
    try:
        proc = subprocess.Popen(
            [path, "--headless", "--stdio", "--no-auto-update"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=_child_env(extra), **kwargs,
        )
        proc.stdin.write(_frame({
            "jsonrpc": "2.0", "id": 1, "method": "connect",
            "params": {"clientInfo": dict(_HEADLESS_CLIENT),
                       "supportedTaskKinds": ["agent", "client", "shell"]}}))
        proc.stdin.write(_frame({
            "jsonrpc": "2.0", "id": 2, "method": "models.list", "params": {}}))
        proc.stdin.flush()
        thread = threading.Thread(target=_read_frames, args=(proc.stdout, 2, answer),
                                  daemon=True)
        thread.start()
        thread.join(_HEADLESS_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - a listing that fails is "could not tell"
        return None
    finally:
        if proc is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(5)
                except (OSError, subprocess.SubprocessError):
                    pass
            except OSError:
                pass
    if not answer:
        return None
    result = answer[0].get("result")
    models = result.get("models") if isinstance(result, dict) else None
    return models if isinstance(models, list) else None


def _effort_vocabulary() -> tuple:
    """The effort ids every surface of this app speaks (`agent_backends`
    EFFORTS, one list for all agents — pinned by tests). The CLI's flag also
    takes `none` and `minimal`, which no picker, record or router here knows,
    so a model's list is cut to this vocabulary, in this order."""
    from fused_render.agent_backends.copilot import EFFORTS
    return tuple(EFFORTS)


def normalize_models(raw: list) -> list:
    """The SDK's model dicts → the rows the snapshot stores and the catalog
    serves: `{id, label, reasoning, efforts, default_effort, category,
    vendor, multiplier}`. A model an organisation policy DISABLED is left
    out (the CLI refuses it at `--model`); `unconfigured` is kept — it is the
    state of every model on a plan without a policy page. A model whose
    capabilities say no reasoning effort gets `efforts: []`; one that says
    yes but names no levels gets the whole vocabulary (what the CLI accepted
    for every GPT and Gemini id before it published lists). Rows without an
    id, and duplicate ids, are dropped."""
    vocab = _effort_vocabulary()
    out, seen = [], set()
    for m in raw:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        policy = m.get("policy") if isinstance(m.get("policy"), dict) else {}
        if str(policy.get("state") or "") == "disabled":
            continue
        caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
        supports = caps.get("supports") if isinstance(caps.get("supports"), dict) else {}
        listed = m.get("supportedReasoningEfforts")
        listed = [str(e) for e in listed] if isinstance(listed, list) else None
        flag = supports.get("reasoningEffort")
        reasoning = bool(flag) if isinstance(flag, bool) else bool(listed)
        if not reasoning:
            efforts: list = []
        elif listed:
            efforts = [e for e in vocab if e in listed]
        else:
            efforts = list(vocab)
        default = str(m.get("defaultReasoningEffort") or "")
        if default not in efforts:
            default = ""
        billing = m.get("billing") if isinstance(m.get("billing"), dict) else {}
        multiplier = billing.get("multiplier")
        if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool):
            multiplier = None
        seen.add(mid)
        out.append({
            "id": mid,
            "label": str(m.get("name") or mid).strip() or mid,
            "reasoning": bool(efforts),
            "efforts": efforts,
            "default_effort": default,
            "category": str(m.get("modelPickerCategory") or "") or None,
            "vendor": str(m.get("vendor") or "") or None,
            "multiplier": multiplier,
        })
    return out


def list_models(path: str, token: Optional[str] = None) -> Optional[list]:
    """The account's models, normalized — or None when the CLI could not
    say (signed out, timed out, an older CLI without the method). An empty
    answer is None too: no account has no models, so an empty list is a
    reply we misread, not a catalog to serve."""
    raw = _headless_models(path, token)
    if raw is None:
        return None
    rows = normalize_models(raw)
    return rows or None


def _carried_models(acct: Optional[dict]) -> Optional[list]:
    """The last snapshot's `models`, for a measure whose listing could not
    tell — read like `_carried_account`, regardless of the fingerprint,
    and bound to the same credential: the same store (`config_dir`) AND
    the same `account.method` as the measure just settled on. The list is
    the ACCOUNT's, and `method` is the only identity Copilot's probe
    yields; a credential that changed kind (a `copilot login` replacing a
    gh token, or the reverse) may be another account, and offering it the
    old account's models would fail at `--model`. No account on either
    side → nothing carried."""
    method = acct.get("method") if isinstance(acct, dict) else None
    if not method:
        return None
    prev = _read_cache()
    if not prev or prev.get("config_dir") != config_dir():
        return None
    before = prev.get("account")
    if not isinstance(before, dict) or before.get("method") != method:
        return None
    models = prev.get("models")
    return list(models) if isinstance(models, list) and models else None


def cached_models() -> Optional[list]:
    """The models the last snapshot listed, or None — read RAW, without the
    freshness gates `cached()` applies: a moved PATH or an upgraded binary
    does not change which models the account may run, and the pickers are
    better served by yesterday's list than by the static shortlist. The
    catalog (`agent_backends/copilot.py`) and nothing else reads this;
    `agent.py` reads the same file by hand."""
    snap = _read_cache()
    models = snap.get("models") if isinstance(snap, dict) else None
    return list(models) if isinstance(models, list) and models else None


# --- what kind of install is this, and can we repair it ----------------------

#: The install line the app runs itself (copilot_install.py) and shows first.
#:
#: WHY THE SCRIPT AND NOT NPM. docs.github.com (install-copilot-cli, read
#: 2026-10-16) lists five equal ways — `npm install -g @github/copilot`,
#: `winget install GitHub.Copilot`, `brew install --cask copilot-cli`, and the
#: curl/wget forms of https://gh.io/copilot-install — and marks none as
#: primary. The script is the one a Dock-launched app can run without a
#: prerequisite it cannot check (npm needs Node 22+), it lands in ~/.local/bin
#: (first in `candidates()`), and it is the same `curl … | bash` shape
#: claude_install already runs. Windows is not a target of this package.
INSTALL_COMMAND_POSIX = "curl -fsSL https://gh.io/copilot-install | bash"


def install_command() -> str:
    """The install line, as a user would type it."""
    return INSTALL_COMMAND_POSIX


#: Install methods where `copilot update` genuinely updates the binary, and
#: the commands for the ones it does not. `copilot update --help` says a
#: downloaded version applies "on standalone-binary installations" — that is
#: the script install. A cask is Homebrew's to upgrade; an npm install is
#: npm's, and `copilot update` on one is unverified, so it is named rather
#: than offered. "system" is a package we cannot name a manager for.
UPDATES_ITSELF = ("native",)
MANAGED_UPDATE_COMMANDS = {
    "brew": "brew upgrade --cask copilot-cli",
    "npm": "npm install -g @github/copilot@latest",
}

#: Path fingerprints — `(method, needles, anchored)` exactly as claude_health
#: keeps them, including the anchoring argument (`/bin/` as a substring
#: matches nearly every path). `/usr/local/bin` is absent on purpose: an npm
#: prefix, an Intel-Mac cask link and a root-run script install all land
#: there, and unknown keeps the update on offer where a wrong guess takes it
#: away.
_PATH_METHODS = (
    ("brew", ("/opt/homebrew/", "/usr/local/cellar/", "/usr/local/caskroom/",
              "/home/linuxbrew/"), False),
    ("npm", ("/node_modules/", "/.npm-global/", "/library/pnpm/",
             "/.bun/bin/"), False),
    ("native", ("/.local/bin/", "/.local/share/copilot/"), False),
    ("system", ("/usr/bin/", "/usr/lib/", "/bin/"), True),
)


def install_method(path: Optional[str]) -> Optional[str]:
    """How this copy was installed, by path, or None. There is no `copilot
    doctor` to ask, so this is always the guess — and None is a real answer
    that never suppresses the update offer."""
    if not path:
        return None
    lowered = path.lower()
    for method, needles, anchored in _PATH_METHODS:
        match = lowered.startswith if anchored else lowered.__contains__
        if any(match(needle) for needle in needles):
            return method
    return None


def update_plan(method: Optional[str]) -> dict:
    """Whether an update can be offered, and what would run — the three
    outcomes of claude_health.update_plan. No DISABLE_UPDATES analogue:
    COPILOT_AUTO_UPDATE=false stops only the background check and leaves
    `copilot update` working (`copilot update --help`)."""
    if method in UPDATES_ITSELF:
        return {"updatable": True, "command": "copilot update", "manager": None,
                "reason": None}
    if method in MANAGED_UPDATE_COMMANDS:
        return {"updatable": False, "command": MANAGED_UPDATE_COMMANDS[method],
                "manager": method,
                "reason": f"{method} owns this install, so it updates through {method}"}
    if method == "system":
        return {"updatable": False, "command": None, "manager": "system",
                "reason": "a system package manager owns this install"}
    return {"updatable": None, "command": "copilot update", "manager": None,
            "reason": None}


def add_to_shell_path() -> dict:
    """Append the PATH line to the user's shell rc — the strip's one-click fix.

    claude_health.add_to_shell_path against this module's resolver; the
    idempotence rule (by directory, not by line) and the error sentences are
    its. Returns `{ok, rc_file, line}` or `{ok: False, error}`.
    """
    path, _source = resolve(allow_shell=False)
    if not path or not executable(path):
        return {"ok": False, "error": "there is no Copilot CLI on this machine "
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
# sign-out moves no file). Its own cache file, so the two reports never read
# each other's.

_CACHE_NAME = "copilot-health.json"
_LOCK = threading.Lock()
#: 2: the snapshot carries `models` (2026-10-23); a 1 re-measures once.
_CACHE_VERSION = 2
_MAX_AGE_S = 60.0


def _cache_path() -> str:
    return os.path.join(storage.home_dir(), _CACHE_NAME)


def _fingerprint(path: Optional[str]) -> str:
    """What must be unchanged for a cached snapshot to still be true. COPILOT_HOME
    is in it because it moves the credential store, which moves `signed_in`;
    the NAMES of the set token variables because one appearing or vanishing
    moves `signed_in` and `account.method` (never the values — this string is
    written to disk)."""
    stamp = ""
    if path:
        try:
            stamp = str(os.stat(path).st_mtime_ns)
        except OSError:
            stamp = "missing"
    tokens = ",".join(n for n in TOKEN_ENV if (os.environ.get(n) or "").strip())
    return "\x1f".join([str(_CACHE_VERSION), os.environ.get(BIN_ENV, ""),
                        os.environ.get("PATH", ""),
                        os.environ.get("COPILOT_HOME", ""), tokens,
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

    # One ACP spawn answers "signed in?", two only when the first says no and
    # `gh` has a token to try (`_sign_in`). The token lives inside that call.
    signed, via_gh, token = _sign_in_detail(path) if usable else (None, False, None)
    acct = account(signed, via_gh=via_gh)
    if signed is None:
        signed = signed_in(None)
        # A probe that could not tell is no news about the credential, so the
        # last snapshot's account stands — above all its `method`, which is
        # agent.py's gh-token gate. Dropping it on one slow read would spawn
        # a gh-only machine signed out.
        acct = (_carried_account() if usable else None) or account(signed, False)
    # WHICH MODELS THE ACCOUNT MAY RUN (see `_headless_models`): asked only of
    # a CLI that just proved a credential, with that credential. A listing
    # that could not tell keeps the last snapshot's list for the SAME
    # credential (`_carried_models`), as the account is kept; a definite
    # sign-out, or a credential of another kind, drops it (the static
    # shortlist shows again until the next listing).
    models = list_models(path, token) if (usable and signed is True) else None
    if models is None and usable and signed is not False:
        models = _carried_models(acct)

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
        # No `copilot doctor` exists; kept so the two snapshots share a shape.
        "doctor": None,
        # The account's model list (`normalize_models` rows), or None when
        # the CLI could not say — then the catalog serves its static rows.
        "models": models,
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

    threading.Thread(target=_run, daemon=True, name="copilot-health").start()


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
