"""Whether Google Gemini CLI is usable on this machine — asked BEFORE anything
needs it. The Gemini twin of `codex_health.py` and `copilot_health.py`
(docs/GEMINI_BACKEND.md D10), with the same public surface and the same
snapshot keys, so the strip that renders one can render the other without
learning a fourth shape.

What is shared and what is copied follows the line the other two draw: the
GENERIC pieces (subprocess kwargs, version parsing, `executable`, the
shell-rc / `path_fix` machinery, the cache-age rule) are imported from
`claude_health`; the pieces that spell "gemini" in a string literal (override
name, candidate list, login-shell probe, `resolve` and its adoption record,
cache file, fingerprint) are COPIED with the name changed.

The signed-in question goes to the CLI's own ACP server (`gemini --acp`, the
wire the sessions use), as Copilot's does: `initialize` → `session/new` on a
throwaway cwd. A `session/new` RESULT means the CLI could build a content
generator with the stored credential — and the result's `models.
availableModels` is the account's own model list, kept in the snapshot for
the catalog (D11a's twin). An ERROR `-32000` worded as a missing credential
("Gemini API key is missing or not configured.", verified on 0.65.0 signed
out; "Authentication required." is the CLI's other spelling) means signed
out. No model is called and no prompt is sent: the probe is free.

**The probe never opens a browser.** A `session/new` on a Google login whose
cached token cannot be refreshed would start the CLI's OAuth flow; with
`NO_BROWSER=1` that flow prints a URL and waits on stdin instead, which the
probe's timeout turns into "unknown" (never "signed out", and never a browser
window out of nowhere). And a Google login with no `oauth_creds.json` at all
is answered signed-out FROM THE FILES, without a spawn: the only thing the
CLI could do with that state is start the flow.

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
# spawn paths (gemini_host via agent.py) resolve the same binary health did.
BIN_ENV = "FUSED_RENDER_GEMINI_BIN"

# Where the installers put `gemini`, for when it isn't on the PATH this
# process inherited (D10, in this order). `npm install -g` lands in whichever
# prefix npm is configured for — ~/.npm-global is the documented no-sudo one —
# Homebrew (`brew install gemini-cli`) links into /opt/homebrew/bin (Apple
# Silicon) or /usr/local/bin (Intel, and the default npm prefix there too);
# the rest are user-local prefixes and the bun/pnpm global bins.
POSIX_CANDIDATES = (
    "~/.npm-global/bin/gemini",
    "/opt/homebrew/bin/gemini",
    "/usr/local/bin/gemini",
    "~/.local/bin/gemini",
    "~/.bun/bin/gemini",
    "~/Library/pnpm/gemini",
)


def candidates() -> tuple:
    """The known install locations, unexpanded."""
    return POSIX_CANDIDATES


# The lowest `gemini` the host is known to work with.
#
# The VERIFIED release, not a few under it (the other agents' floors sit a
# little below theirs): 0.65.0 is the one that spells the ACP flag `--acp`
# (`--experimental-acp` is kept as a deprecated alias), and the release that
# renamed it is unknown — an older CLI could refuse the spawn line outright.
# Lower it once that release is known, and say which in the commit.
MIN_VERSION = "0.65.0"


def is_outdated(found: Optional[str], floor: str = MIN_VERSION) -> bool:
    """claude_health.is_outdated against THIS floor — a bare re-export would
    default to Claude's 2.0.0 and call every Gemini release too old."""
    return claude_health.is_outdated(found, floor)


_VERSION_TIMEOUT_S = 10
_SHELL_TIMEOUT_S = 8
#: The ACP sign-in probe: a Node start, `initialize`, and a `session/new`
#: that refreshes the token and builds the session's tool registry. Measured
#: at ~1.5 s signed out (0.65.0); a signed-in one also talks to Google once.
#: Bounded so a wedged CLI — or one waiting on the OAuth flow's stdin — cannot
#: pin a health read, and the timeout reads as "unknown", never "signed out".
_ACP_TIMEOUT_S = 25
#: How long each reap step waits before escalating (see `_reap`).
_REAP_GRACE_S = 2.0

#: `gemini --version` prints the bare version (`0.65.0`) on stdout. The first
#: dotted run of STDOUT only: stderr is where the npm wrapper's warnings go.
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

#: Settings the CLI's own files spell. `selectedType` is the auth method the
#: user picked (`gemini`'s /auth), one of the ACP `authMethods` ids.
_AUTH_OAUTH = "oauth-personal"
_AUTH_API_KEY = "gemini-api-key"
_AUTH_VERTEX = "vertex-ai"
_AUTH_GATEWAY = "gateway"
#: The ids → the snapshot's `method`, the strip's vocabulary.
_METHODS = {_AUTH_OAUTH: "google", _AUTH_API_KEY: "api_key",
            _AUTH_VERTEX: "vertex", _AUTH_GATEWAY: "gateway"}
#: The environment variables that are themselves a credential.
_API_KEY_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
_VERTEX_ENV = "GOOGLE_GENAI_USE_VERTEXAI"


def augmented_path() -> str:
    """PATH with the known install dirs appended (deduped, order preserved).

    Built on top of claude_health's, not instead of it: an npm-installed
    Gemini is a Node script that needs `node`, and the Node prefixes a
    GUI-launched app is missing are exactly the ones the Claude list knows.
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
    """The environment every probe and child here runs with: the user's own,
    PYTHONHOME/PYTHONPATH scrubbed (the packaged app's bundled interpreter
    exports both), PATH augmented so the CLI finds `node`. GEMINI_CLI_HOME
    and the Google credential variables are passed through exactly as found
    — nothing here sets, moves or clears them. `extra` lands on top.

    `GEMINI_CLI_NO_RELAUNCH=1` always (D19): the CLI's `gemini` entry point
    is a launcher that, unless this is set, re-spawns node as a CHILD with a
    heap flag and installs no-op SIGINT/SIGTERM/SIGHUP handlers on itself
    (verified 0.65.0, `bundle/gemini.js`). To a `terminate()` the launcher
    does nothing; to a `kill()` it dies and ORPHANS the real CLI, which keeps
    our pipes open, so a reaper waiting on EOF waits forever (the login's
    Cancel hung that way). In-process, the CLI is the process we hold."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH")}
    env["PATH"] = augmented_path()
    env["GEMINI_CLI_NO_RELAUNCH"] = "1"
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
    """Ask the user's LOGIN SHELL where `gemini` is.

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
                [shell, *flags, "command -v gemini"],
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
    as claude_health.adopt. The host (claude_agent/gemini_host.py, via
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
    """`(path, source)` for the `gemini` to run, or `(None, None)`.

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
    found = shutil.which("gemini")
    if found:
        return found, "path"
    for candidate in candidates():
        path = os.path.expanduser(os.path.expandvars(candidate))
        if executable(path):
            return path, "candidate"
    augmented = shutil.which("gemini", path=augmented_path())
    if augmented:
        return augmented, "candidate"
    if allow_shell:
        via_shell = _shell_probe()
        if via_shell:
            return via_shell, "shell"
    return None, None


def probe_version(path: str) -> Optional[str]:
    """What `path --version` says, or None if it would not tell us.

    0.65.0 prints `0.65.0` and nothing else. The first dotted run of STDOUT;
    failing that, of stderr (a wrapper that prints the banner there) — never
    stderr first, where the npm wrapper's warnings can carry a temp path with
    digits in it.
    """
    try:
        res = _run_probe(path, "--version", timeout=_VERSION_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    for stream in (res.stdout or "", res.stderr or ""):
        m = _VERSION_RE.search(stream)
        if m:
            got = parse_version(m.group(0))
            if got:
                return ".".join(str(p) for p in got)
    return None


def config_dir() -> str:
    """Gemini CLI's own data dir: `.gemini` under `GEMINI_CLI_HOME` when the
    user set it (the CLI's own override of its home), else under `~` — the
    one place the app spells either (docs/GEMINI_BACKEND.md invariant 16)."""
    home = os.environ.get("GEMINI_CLI_HOME") or os.path.expanduser("~")
    return os.path.join(home, ".gemini")


# --- the credential, and whose it is ------------------------------------------


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def selected_auth() -> Optional[str]:
    """The auth method the user picked, as the CLI's `settings.json` spells it
    (`security.auth.selectedType`: `oauth-personal` | `gemini-api-key` |
    `vertex-ai` | `gateway`), else None. Read off the USER settings file only;
    a workspace's `.gemini/settings.json` is that folder's and the probe runs
    in none."""
    settings = _read_json(os.path.join(config_dir(), "settings.json")) or {}
    security = settings.get("security")
    auth = security.get("auth") if isinstance(security, dict) else None
    value = auth.get("selectedType") if isinstance(auth, dict) else None
    return value if isinstance(value, str) and value else None


def _env_method() -> Optional[str]:
    """The method the ENVIRONMENT alone implies, when a settings file names
    none: an API key variable is a key, the Vertex switch is Vertex."""
    if any(os.environ.get(n) for n in _API_KEY_ENV):
        return "vertex" if str(os.environ.get(_VERTEX_ENV, "")).lower() in (
            "true", "1") else "api_key"
    return None


def method() -> Optional[str]:
    """`google` | `api_key` | `vertex` | `gateway` | None — which credential a
    session would sign in with, from the settings file first and the
    environment second. None says nothing is configured."""
    picked = selected_auth()
    if picked:
        return _METHODS.get(picked, picked)
    return _env_method()


def oauth_cached() -> bool:
    """Whether a Google login has a token on disk (`oauth_creds.json`). Its
    presence is not proof of a working login (it may be revoked); its
    ABSENCE is proof of none, which is the one fact the probe needs before
    it spawns."""
    return os.path.isfile(os.path.join(config_dir(), "oauth_creds.json"))


def _signed_in_email() -> Optional[str]:
    """The Google account the CLI cached as active (`google_accounts.json`,
    `{"active": <email>, "old": [...]}`), or None."""
    data = _read_json(os.path.join(config_dir(), "google_accounts.json")) or {}
    active = data.get("active")
    return active.strip() if isinstance(active, str) and active.strip() else None


_SIGNED_OUT_CODE = -32000
#: What `session/new` says with no credential: the API-key wording (verified,
#: 0.65.0, no settings and no key in the environment) and the generic one the
#: CLI falls back to; plus the OAuth client's own refusals, assumed.
_SIGNED_OUT_RE = re.compile(
    r"api key is missing|authentication required|not authenticated|"
    r"not logged in|invalid_grant|unauthenticated|credential", re.I)


def _classify(reply: Optional[dict]) -> Optional[bool]:
    """A `session/new` reply → True / False / None-unknown.

    A result (any dict carrying `sessionId`) is True: the CLI built a session,
    which it only does once `refreshAuth` succeeded. An error with the
    signed-out code AND signed-out words is False — the code alone is the
    CLI's catch-all for every setup failure, and the words alone could be a
    message about something else. Anything else is None, because False is
    what puts "sign in" in front of the user and a sign-in cannot fix a reply
    we could not read.
    """
    if not isinstance(reply, dict):
        return None
    result = reply.get("result")
    if isinstance(result, dict) and isinstance(result.get("sessionId"), str):
        return True
    err = reply.get("error")
    if not isinstance(err, dict):
        return None
    if err.get("code") == _SIGNED_OUT_CODE and _SIGNED_OUT_RE.search(
            str(err.get("message") or "")):
        return False
    return None


def _reap(proc: subprocess.Popen) -> None:
    """Close stdin, wait, then SIGTERM, wait, then SIGKILL — in that order.

    The ACP server exits on stdin EOF, so the first step is normally the
    last. TERM BEFORE KILL because an npm install's `gemini` is a Node
    wrapper that may forward signals; a SIGKILL to the wrapper cannot be
    forwarded and would orphan a child, once per health read.

    PLAIN signals to the child, NOT A PROCESS GROUP: `start_new_session=True`
    forces CPython off posix_spawn onto fork()+exec, and this runs in the
    server, where a fork with libproj resident dies SIGSEGV before exec
    (claude_spawn.py's history).
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


def _probe_dir() -> str:
    """The throwaway cwd every probe's `session/new` names — ONE fixed dir
    under fused's own home, not a fresh tempdir each time: Gemini registers
    every workspace it sees in its `projects.json` and keeps a `tmp/<slug>`
    for it, so a new path per probe would grow that registry by one entry a
    minute. Created 0700 when missing."""
    path = os.path.join(storage.home_dir(), "gemini-probe")
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        pass
    return path


def _acp_probe(path: str) -> Optional[dict]:
    """Ask the CLI's ACP server for a session: the raw reply to `session/new`
    (result or error), or None when it would not say.

    `initialize` then `session/new` on the probe dir. Timeout, a dead child,
    output we could not parse — all None, because "could not tell" must never
    render as "sign in". The child runs with `NO_BROWSER=1` (the OAuth flow
    may never open a window from a health read) and the probe dir marked
    trusted (`GEMINI_CLI_TRUST_WORKSPACE`, so a user who turned folder trust
    on does not make the probe die at startup). No `cwd=` (the fork argument
    in `_reap`): the dir travels only in the RPC params.
    """
    proc = None
    answer: list = []

    def _reader(stream) -> None:
        # Runs until EOF or the answer; the deadline is kept by the join below.
        try:
            for line in stream:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue   # the OAuth flow's "Please visit…" line, a warning
                # A server→client REQUEST also carries an `id` (and a
                # `method`); only a reply to one of ours is read here.
                if not isinstance(msg, dict) or "method" in msg:
                    continue
                if msg.get("id") == 2:
                    answer.append(msg)
                    return
        except (OSError, ValueError):
            pass

    try:
        proc = subprocess.Popen(
            [path, "--acp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_child_env({"NO_BROWSER": "1", "GEMINI_CLI_TRUST_WORKSPACE": "true"}),
            **SUBPROCESS_KWARGS,
        )
        for msg in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": 1, "clientCapabilities": {},
                        "clientInfo": {"name": "fused-render", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "session/new",
             "params": {"cwd": _probe_dir(), "mcpServers": []}},
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
            _reap(proc)
    return answer[0] if answer else None


def _sign_in(path: str) -> tuple:
    """`(signed, reply)` — the files first, then one ACP probe. `reply` is the
    raw `session/new` reply (None when there was none), for `models`."""
    if method() == "google" and not oauth_cached():
        # Nothing to ask: the only thing `session/new` could do is start the
        # login flow, which the probe must never do.
        return False, None
    reply = _acp_probe(path)
    return _classify(reply), reply


def signed_in(path: Optional[str] = None) -> Optional[bool]:
    """Whether Gemini has a working credential — True, False, or None for
    unknown. Tri-state like claude_health.signed_in and for the same reason:
    False is what puts "sign in" in front of the user, so only the CLI (or a
    Google login with no token at all) may say it."""
    if path:
        return _sign_in(path)[0]
    return None


def account(signed: Optional[bool]) -> Optional[dict]:
    """WHO is signed in, in the shape the wizard's "Signed in" row reads:
    `{method, email, org, plan}` — the settings' method and, for a Google
    login, the cached account's email. None unless `signed` is True."""
    if signed is not True:
        return None
    how = method() or "unknown"
    return {"method": how,
            "email": _signed_in_email() if how == "google" else None,
            "org": None, "plan": None}


# --- the account's models -----------------------------------------------------


def normalize_models(raw) -> list:
    """`session/new`'s `models.availableModels` rows (`{modelId, name,
    description}`) as the snapshot stores them: `{id, label, description}`,
    in the CLI's order, malformed rows and duplicates dropped. The CLI's own
    `auto` row rides along as `auto`."""
    out, seen = [], set()
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("modelId") or row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid, "label": str(row.get("name") or row.get("label") or mid),
                    "description": str(row.get("description") or "") or None})
    return out


def _models_of(reply) -> Optional[list]:
    result = reply.get("result") if isinstance(reply, dict) else None
    models = result.get("models") if isinstance(result, dict) else None
    rows = models.get("availableModels") if isinstance(models, dict) else None
    if not isinstance(rows, list):
        return None
    out = normalize_models(rows)
    return out or None


def _current_model_of(reply) -> Optional[str]:
    result = reply.get("result") if isinstance(reply, dict) else None
    models = result.get("models") if isinstance(result, dict) else None
    cur = models.get("currentModelId") if isinstance(models, dict) else None
    return cur if isinstance(cur, str) and cur else None


def cached_models() -> Optional[list]:
    """The last snapshot's `models`, read raw (no freshness gate: the list
    does not go stale with the PATH), or None. What `agent_backends.gemini.
    discovered_models` falls back to when the catalog has no row to read."""
    prev = _read_cache()
    rows = prev.get("models") if prev else None
    return list(rows) if isinstance(rows, list) and rows else None


# --- what kind of install is this, and can we repair it ----------------------

#: The install line the app runs itself (gemini_install.py) and shows first —
#: the route Google's README documents. `brew install gemini-cli` exists too,
#: and `install_method` recognises it, but npm is the one route that is the
#: same on every POSIX machine with Node.
INSTALL_COMMAND_POSIX = "npm install -g @google/gemini-cli"


def install_command() -> str:
    """The install line, as a user would type it."""
    return INSTALL_COMMAND_POSIX


#: Install methods the app updates itself, with the line it runs (the same
#: npm line: `npm install -g` with no version installs the `latest` tag over
#: whatever is there), and the ones it only names.
UPDATES_ITSELF = ("npm",)
MANAGED_UPDATE_COMMANDS = {
    "brew": "brew upgrade gemini-cli",
}

#: Path fingerprints, matched against the path AND its realpath (lowercased).
#: npm is tested FIRST, and that order is the point: a global npm install
#: under a Homebrew-installed Node lives in /opt/homebrew/lib/node_modules and
#: is linked from /opt/homebrew/bin, so a brew-first test would hand an npm
#: install to `brew upgrade`, which does not own it.
_NPM_NEEDLES = ("/node_modules/", "/.npm-global/", "/.nvm/", "/.volta/",
                "/.fnm/", "/fnm/", "/.bun/", "/library/pnpm/",
                "/.local/share/pnpm/")
_BREW_NEEDLES = ("/opt/homebrew/", "/usr/local/cellar/", "/usr/local/caskroom/",
                 "/home/linuxbrew/")


def install_method(path: Optional[str]) -> Optional[str]:
    """How this copy was installed: `npm` | `brew` | `other`, or None with no
    path. By path (and the path a symlink resolves to), since nothing asks
    the CLI. `other` keeps the update on offer (see `update_plan`)."""
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


def update_plan(method_: Optional[str]) -> dict:
    """Whether an update can be offered, and what would run — the three
    outcomes of claude_health.update_plan. An install the app cannot place
    (`other`, None) keeps the offer with an unknown `updatable`."""
    if method_ in UPDATES_ITSELF:
        return {"updatable": True, "command": INSTALL_COMMAND_POSIX,
                "manager": None, "reason": None}
    if method_ in MANAGED_UPDATE_COMMANDS:
        return {"updatable": False, "command": MANAGED_UPDATE_COMMANDS[method_],
                "manager": method_,
                "reason": f"{method_} owns this install, so it updates through {method_}"}
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
        return {"ok": False, "error": "there is no Gemini CLI on this machine "
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
# sign-out moves no file this watches). Its own cache file, so the four
# reports never read each other's.

_CACHE_NAME = "gemini-health.json"
_LOCK = threading.Lock()
_CACHE_VERSION = 1
_MAX_AGE_S = 60.0


def _cache_path() -> str:
    return os.path.join(storage.home_dir(), _CACHE_NAME)


def _fingerprint(path: Optional[str]) -> str:
    """What must be unchanged for a cached snapshot to still be true.
    GEMINI_CLI_HOME is in it because it moves the credential store, which
    moves `signed_in` and `account`; whether a key variable is SET is in it
    for the same reason (never its value — the fingerprint is on disk)."""
    stamp = ""
    if path:
        try:
            stamp = str(os.stat(path).st_mtime_ns)
        except OSError:
            stamp = "missing"
    keyed = "".join("1" if os.environ.get(n) else "0" for n in _API_KEY_ENV)
    return "\x1f".join([str(_CACHE_VERSION), os.environ.get(BIN_ENV, ""),
                        os.environ.get("PATH", ""),
                        os.environ.get("GEMINI_CLI_HOME", ""), keyed,
                        os.environ.get(_VERTEX_ENV, ""),
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
    method_ = install_method(path) if usable else None
    plan = update_plan(method_)

    on_shell_path: Optional[bool] = None
    if usable:
        if source in ("path", "shell"):
            on_shell_path = True
        elif source == "candidate" and allow_shell:
            on_shell_path = _shell_probe() is not None
    fix = path_fix(path) if (usable and on_shell_path is False) else None

    # One ACP spawn answers "signed in?", and the reply carries the account's
    # model list (the catalog's `discovered_models`).
    signed, reply = _sign_in(path) if usable else (None, None)
    acct = account(signed)
    models = _models_of(reply) if signed else None
    current_model = _current_model_of(reply) if signed else None
    if signed is None and usable:
        # A probe that could not tell is no news about the credential, so the
        # last snapshot's account, list and current model stand: one slow
        # read must not blank the strip's "Signed in as …" row, the pickers'
        # list or the current-model line.
        acct = _carried_account()
        models = cached_models()
        current_model = _carried_current_model()

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
        "install_method": method_,
        "updatable": plan["updatable"],
        "update_command": plan["command"],
        "update_manager": plan["manager"],
        "update_blocked_reason": plan["reason"],
        # No `gemini doctor`; kept so the snapshots share a shape.
        "doctor": None,
        # The account's own model list off `session/new` (D11a's twin), or
        # None: the static catalog stands.
        "models": models,
        "current_model": current_model,
        "on_shell_path": on_shell_path,
        "path_fix_command": fix["command"] if fix else None,
        "checked_at": time.time(),
        "fingerprint": _fingerprint(path),
    }


def _read_cache() -> Optional[dict]:
    data = storage.read_json(_cache_path())
    return data if isinstance(data, dict) else None


def _carried_current_model() -> Optional[str]:
    """The last snapshot's `current_model`, for a measure whose probe could
    not tell — the same store rule as `_carried_account`."""
    prev = _read_cache()
    if not prev or prev.get("config_dir") != config_dir():
        return None
    model = prev.get("current_model")
    return model if isinstance(model, str) and model else None


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

    threading.Thread(target=_run, daemon=True, name="gemini-health").start()


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
