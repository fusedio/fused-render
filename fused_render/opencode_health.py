"""Whether OpenCode is usable on this machine — asked BEFORE anything needs
it. The OpenCode twin of `gemini_health.py` (docs/OPENCODE_BACKEND.md D10),
with the same public surface and the same snapshot keys (plus Gemini's
`current_model`), so the strip that renders one can render the other.

What is shared and what is copied follows the line the other agents draw:
the GENERIC pieces (subprocess kwargs, version parsing, `executable`, the
shell-rc / `path_fix` machinery, the cache-age rule) are imported from
`claude_health`; the pieces that spell "opencode" in a string literal are
COPIED from gemini_health with the name changed.

The signed-in question goes to OpenCode's own ACP server (`opencode acp`,
the wire the sessions use): `initialize` → `session/new` on a throwaway cwd.
"Signed in" for OpenCode means "has at least one usable provider": the probe
injects the same `fused` provider the sessions get (D20) through
`OPENCODE_CONFIG_CONTENT`, so a machine with a loaded local model is signed
in with no credential at all. A `session/new` RESULT is signed in, and its
`configOptions[id=model]` is the provider-wide model list (D11a) — kept in
the snapshot FILTERED to the `fused/` rows, since the reply does not say
which of the hundreds of models.dev providers is connected. An ERROR
`-32000` worded as "no provider" means signed out. No model is called and no
prompt is sent: the probe is free.

There is no OAuth prompt on stdout (verified 1.19.5): a provider needing a
credential it does not have fails the `session/prompt`, never the handshake,
so the probe never has to guard against a browser window.

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
    detached_argv,
    _too_old,
    executable,
    parse_version,
    path_fix,
)
from fused_render.shell import storage

# An explicit override beats every probe below. Same role as
# FUSED_RENDER_CLAUDE_BIN: `adopt` publishes a shell-only find into it so the
# spawn paths (opencode_host via agent.py) resolve the same binary health did.
BIN_ENV = "FUSED_RENDER_OPENCODE_BIN"

# Where the installers put `opencode`, for when it isn't on the PATH this
# process inherited (D10, in this order). The curl installer
# (`https://opencode.ai/install`) lands in ~/.opencode/bin; `npm install -g`
# in whichever prefix npm is configured for; Homebrew links into
# /opt/homebrew/bin or /usr/local/bin; the rest are user-local prefixes and
# the bun global bin.
POSIX_CANDIDATES = (
    "~/.opencode/bin/opencode",
    "~/.npm-global/bin/opencode",
    "/opt/homebrew/bin/opencode",
    "/usr/local/bin/opencode",
    "~/.local/bin/opencode",
    "~/.bun/bin/opencode",
)


# Windows installs: the npm .cmd shim, the curl installer's own dir, bun and
# pnpm global bins. Expanded by the same expanduser(expandvars()) the
# resolvers already apply.
WINDOWS_CANDIDATES = (
    r"%APPDATA%\npm\opencode.cmd",
    "~/.opencode/bin/opencode.exe",
    r"%LOCALAPPDATA%\opencode\bin\opencode.exe",
    "~/.bun/bin/opencode.exe",
    r"%LOCALAPPDATA%\pnpm\opencode.cmd",
)


def candidates() -> tuple:
    """The known install locations, unexpanded."""
    return WINDOWS_CANDIDATES if sys.platform == "win32" else POSIX_CANDIDATES


# The lowest `opencode` the host is known to work with: the verified
# release's minor (1.19.5 was the one on the wire; D10 says the floor is
# assumed). Lower it once an older release is verified, and say which.
MIN_VERSION = "1.19.0"


def is_outdated(found: Optional[str], floor: str = MIN_VERSION) -> bool:
    """claude_health.is_outdated against THIS floor — a bare re-export would
    default to Claude's 2.0.0 and call every OpenCode release too old."""
    return claude_health.is_outdated(found, floor)


_VERSION_TIMEOUT_S = 10
_SHELL_TIMEOUT_S = 8
#: The ACP sign-in probe: a Bun start, `initialize`, and a `session/new`
#: that loads the provider catalog. Bounded so a wedged CLI cannot pin a
#: health read; the timeout reads as "unknown", never "signed out".
_ACP_TIMEOUT_S = 25
#: How long each reap step waits before escalating (see `_reap`).
_REAP_GRACE_S = 2.0

#: `opencode --version` prints the bare version (`1.19.5`) on stdout. The
#: first dotted run of STDOUT only: stderr is where a wrapper's warnings go.
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

#: The provider prefix of the local catalog (D20): the one set of rows the
#: probe KNOWS is usable, since it injected them itself.
LOCAL_PREFIX = "fused/"
#: The env var every child runs with (D19).
_NO_AUTOUPDATE_ENV = "OPENCODE_DISABLE_AUTOUPDATE"
_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"
_CONFIG_DIR_ENV = "OPENCODE_CONFIG_DIR"


def augmented_path() -> str:
    """PATH with the known install dirs appended (deduped, order preserved).

    Built on top of claude_health's, not instead of it: an npm-installed
    OpenCode needs `node` for its shim, and the Node prefixes a GUI-launched
    app is missing are exactly the ones the Claude list knows.
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
    exports both), PATH augmented so the CLI finds `node`. OPENCODE_CONFIG_DIR
    and the user's own global config are passed through exactly as found (D19).
    `OPENCODE_DISABLE_AUTOUPDATE=1` always: a health read must never start a
    download. `extra` lands on top."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH")}
    env["PATH"] = augmented_path()
    env[_NO_AUTOUPDATE_ENV] = "1"
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
    """Ask the user's LOGIN SHELL where `opencode` is.

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
                detached_argv([shell, *flags, "command -v opencode"]),
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
    as claude_health.adopt.
    """
    global _ADOPTED
    if os.environ.get(BIN_ENV):
        return False
    os.environ[BIN_ENV] = path
    _ADOPTED = path
    return True


def resolve(allow_shell: bool = True) -> tuple:
    """`(path, source)` for the `opencode` to run, or `(None, None)`.

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
    found = shutil.which("opencode")
    if found:
        return found, "path"
    for candidate in candidates():
        path = os.path.expanduser(os.path.expandvars(candidate))
        if executable(path):
            return path, "candidate"
    augmented = shutil.which("opencode", path=augmented_path())
    if augmented:
        return augmented, "candidate"
    if allow_shell:
        via_shell = _shell_probe()
        if via_shell:
            return via_shell, "shell"
    return None, None


def probe_version(path: str) -> Optional[str]:
    """What `path --version` says, or None if it would not tell us.

    1.19.5 prints `1.19.5` and nothing else. The first dotted run of STDOUT;
    failing that, of stderr — never stderr first, where a wrapper's warnings
    can carry a temp path with digits in it.
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
    """OpenCode's own config dir: `OPENCODE_CONFIG_DIR` when the user set it,
    else `~/.config/opencode` — the one place the app spells either. Its
    `auth.json` is never read (D10)."""
    forced = os.environ.get(_CONFIG_DIR_ENV)
    if forced:
        return forced
    return os.path.join(os.path.expanduser("~"), ".config", "opencode")


# --- the injected config ------------------------------------------------------


def _local_models() -> list:
    """The local catalog rows `agent_backends.opencode_config.local_models()`
    offers, or [] — the module is another workstream's; an import that fails
    reads as "no local models", and so does anything it raises."""
    try:
        from fused_render.agent_backends import opencode_config  # noqa: WPS433
    except Exception:  # noqa: BLE001 - ImportError, or a half-written module
        return []
    try:
        rows = opencode_config.local_models()
    except Exception:  # noqa: BLE001
        return []
    return list(rows) if isinstance(rows, (list, tuple, dict)) and rows else []


def probe_config() -> Optional[dict]:
    """The config the probe injects (D20's `fused` provider, with a dead
    baseURL and no models — the probe only needs OpenCode to COUNT a provider
    as configured, never to call it), or None when the local text capability
    has no installed model: then "signed in" is OpenCode's own providers."""
    if not _local_models():
        return None
    return {"provider": {"fused": {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Fused Render (local)",
        "options": {"baseURL": "http://127.0.0.1:1/v1", "apiKey": "probe"},
        "models": {},
    }}}


# --- the ACP probe --------------------------------------------------------------


_SIGNED_OUT_CODE = -32000
#: What `session/new` says with no provider at all (assumed wording — D13's
#: markers; a verified sentence should replace the first alternative).
_SIGNED_OUT_RE = re.compile(
    r"no provider|provider not found|not authenticated|no api key|"
    r"unauthorized|invalid api key|not logged in|not signed in", re.I)


def _classify(reply: Optional[dict]) -> Optional[bool]:
    """A `session/new` reply → True / False / None-unknown.

    A result (any dict carrying `sessionId`) is True: OpenCode built a
    session, which means at least one provider is configured. An error with
    the signed-out code AND signed-out words is False. Anything else is None,
    because False is what puts "sign in" in front of the user and a sign-in
    cannot fix a reply we could not read.
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
    last. Plain signals to the child, never a process group (the fork
    argument gemini_health._reap makes)."""
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
    under fused's own home, so OpenCode's own project store never grows by
    one entry a probe. Created 0700 when missing."""
    path = os.path.join(storage.home_dir(), "opencode-probe")
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        pass
    return path


def _acp_probe(path: str) -> Optional[dict]:
    """Ask OpenCode's ACP server for a session: the raw reply to
    `session/new` (result or error), or None when it would not say.

    `initialize` then `session/new` on the probe dir. Timeout, a dead child,
    output we could not parse — all None, because "could not tell" must never
    render as "sign in". The child runs with the probe config injected
    (`OPENCODE_CONFIG_CONTENT`, D19/D20) when there are local models. No
    `cwd=`: the dir travels only in the RPC params.
    """
    proc = None
    answer: list = []

    def _reader(stream) -> None:
        try:
            for line in stream:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue   # a warning, or other prose
                # A server→client REQUEST also carries an `id` (and a
                # `method`); only a reply to one of ours is read here.
                if not isinstance(msg, dict) or "method" in msg:
                    continue
                if msg.get("id") == 2:
                    answer.append(msg)
                    return
        except (OSError, ValueError):
            pass

    extra = {}
    config = probe_config()
    if config:
        extra[_CONFIG_CONTENT_ENV] = json.dumps(config)
    try:
        proc = subprocess.Popen(
            [path, "acp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_child_env(extra),
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
    """`(signed, reply)` — one ACP probe. `reply` is the raw `session/new`
    reply (None when there was none), for `models`."""
    reply = _acp_probe(path)
    return _classify(reply), reply


def signed_in(path: Optional[str] = None) -> Optional[bool]:
    """Whether OpenCode has a usable provider — True, False, or None for
    unknown. Tri-state like claude_health.signed_in and for the same reason."""
    if path:
        return _sign_in(path)[0]
    return None


def method_of(current_model: Optional[str]) -> str:
    """`local` when the session would open on the injected local catalog,
    else `providers` (OpenCode's own credentials, never read)."""
    if isinstance(current_model, str) and current_model.startswith(LOCAL_PREFIX):
        return "local"
    return "providers"


def account(signed: Optional[bool], current_model: Optional[str] = None) -> Optional[dict]:
    """WHO is signed in, in the shape the wizard's "Signed in" row reads:
    `{method, email, org, plan}`. OpenCode has no account of its own to name:
    `method` says whether the default model is local or a provider's. None
    unless `signed` is True."""
    if signed is not True:
        return None
    return {"method": method_of(current_model), "email": None, "org": None,
            "plan": None}


# --- the provider-wide model list ---------------------------------------------


def _model_option(reply) -> Optional[dict]:
    """The `configOptions` row whose id is `model`, or None."""
    result = reply.get("result") if isinstance(reply, dict) else None
    options = result.get("configOptions") if isinstance(result, dict) else None
    if not isinstance(options, list):
        return None
    for row in options:
        if isinstance(row, dict) and row.get("id") == "model":
            return row
    return None


def normalize_models(raw, keep=None) -> list:
    """`configOptions[id=model].options` rows (`{value, name}`) as the
    snapshot stores them: `{id, label, description}`, in OpenCode's order,
    malformed rows and duplicates dropped, and FILTERED to the ids `keep`
    accepts (default: the `fused/` rows — D11a; the reply does not say which
    other provider is connected, and hundreds of unusable rows would drown
    the picker)."""
    out, seen = [], set()
    if not isinstance(raw, list):
        return out
    if keep is None:
        keep = lambda mid: mid.startswith(LOCAL_PREFIX)  # noqa: E731
    for row in raw:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("value") or row.get("id") or "").strip()
        if not mid or mid in seen or not keep(mid):
            continue
        seen.add(mid)
        out.append({"id": mid, "label": str(row.get("name") or row.get("label") or mid),
                    "description": str(row.get("description") or "") or None})
    return out


def _models_of(reply) -> Optional[list]:
    option = _model_option(reply)
    rows = option.get("options") if option else None
    if not isinstance(rows, list):
        return None
    out = normalize_models(rows)
    return out or None


def _providers_seen_of(reply) -> Optional[int]:
    """How many distinct providers the full list names (for the UI's "and N
    more providers through OpenCode" line), or None without a list."""
    option = _model_option(reply)
    rows = option.get("options") if option else None
    if not isinstance(rows, list):
        return None
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("value") or "")
        if "/" in mid:
            seen.add(mid.split("/", 1)[0])
    return len(seen)


def _current_model_of(reply) -> Optional[str]:
    option = _model_option(reply)
    cur = option.get("currentValue") if option else None
    return cur if isinstance(cur, str) and cur else None


def cached_models() -> Optional[list]:
    """The last snapshot's `models`, read raw (no freshness gate: the list
    does not go stale with the PATH), or None. What `agent_backends.opencode.
    discovered_models` falls back to when the catalog has no row to read."""
    prev = _read_cache()
    rows = prev.get("models") if prev else None
    return list(rows) if isinstance(rows, list) and rows else None


# --- what kind of install is this, and can we repair it ----------------------

#: The install line the app runs itself (opencode_install.py) and shows
#: first. `brew install opencode` and `curl -fsSL https://opencode.ai/install
#: | bash` exist too, and `install_method` recognises both, but npm is the one
#: route the app runs (D10).
INSTALL_COMMAND_POSIX = "npm install -g opencode-ai"


def install_command() -> str:
    """The install line, as a user would type it."""
    return INSTALL_COMMAND_POSIX


#: Install methods the app updates itself, with the line it runs, and the
#: ones it only names (never runs — D10).
UPDATES_ITSELF = ("npm",)
MANAGED_UPDATE_COMMANDS = {
    "brew": "brew upgrade opencode",
    "curl": "curl -fsSL https://opencode.ai/install | bash",
}

#: Path fingerprints, matched against the path AND its realpath (lowercased).
#: npm is tested FIRST (gemini_health has the argument); the curl installer's
#: own dir next, then Homebrew.
_NPM_NEEDLES = ("/node_modules/", "/.npm-global/", "/.nvm/", "/.volta/",
                "/.fnm/", "/fnm/", "/.bun/", "/library/pnpm/",
                "/.local/share/pnpm/")
_CURL_NEEDLES = ("/.opencode/bin/",)
_BREW_NEEDLES = ("/opt/homebrew/", "/usr/local/cellar/", "/usr/local/caskroom/",
                 "/home/linuxbrew/")


def install_method(path: Optional[str]) -> Optional[str]:
    """How this copy was installed: `npm` | `curl` | `brew` | `other`, or
    None with no path. By path (and the path a symlink resolves to)."""
    if not path:
        return None
    seen = [path.lower()]
    try:
        seen.append(os.path.realpath(path).lower())
    except (OSError, ValueError):
        pass
    if any(n in p for p in seen for n in _NPM_NEEDLES):
        return "npm"
    if any(n in p for p in seen for n in _CURL_NEEDLES):
        return "curl"
    if any(n in p for p in seen for n in _BREW_NEEDLES):
        return "brew"
    return "other"


def update_plan(method_: Optional[str]) -> dict:
    """Whether an update can be offered, and what would run — the three
    outcomes of claude_health.update_plan."""
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
    Returns `{ok, rc_file, line}` or `{ok: False, error}`."""
    path, _source = resolve(allow_shell=False)
    if not path or not executable(path):
        return {"ok": False, "error": "there is no OpenCode on this machine "
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

_CACHE_NAME = "opencode-health.json"
_LOCK = threading.Lock()
_CACHE_VERSION = 1
_MAX_AGE_S = 60.0


def _cache_path() -> str:
    return os.path.join(storage.home_dir(), _CACHE_NAME)


def _fingerprint(path: Optional[str]) -> str:
    """What must be unchanged for a cached snapshot to still be true.
    OPENCODE_CONFIG_DIR is in it because it moves the credential store."""
    stamp = ""
    if path:
        try:
            stamp = str(os.stat(path).st_mtime_ns)
        except OSError:
            stamp = "missing"
    return "\x1f".join([str(_CACHE_VERSION), os.environ.get(BIN_ENV, ""),
                        os.environ.get("PATH", ""),
                        os.environ.get(_CONFIG_DIR_ENV, ""),
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

    signed, reply = _sign_in(path) if usable else (None, None)
    current_model = _current_model_of(reply) if signed else None
    acct = account(signed, current_model)
    models = _models_of(reply) if signed else None
    providers_seen = _providers_seen_of(reply) if signed else None
    if signed is None and usable:
        # A probe that could not tell is no news about the providers, so the
        # last snapshot's account, list and current model stand.
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
        # No `opencode doctor`; kept so the snapshots share a shape.
        "doctor": None,
        # The `fused/` rows off `session/new` (D11a), or None: the static
        # catalog stands. `providers_seen` says how many providers the full
        # list named, for the UI.
        "models": models,
        "current_model": current_model,
        "providers_seen": providers_seen,
        "on_shell_path": on_shell_path,
        "path_fix_command": fix["command"] if fix else None,
        "checked_at": time.time(),
        "fingerprint": _fingerprint(path),
    }


def _read_cache() -> Optional[dict]:
    data = storage.read_json(_cache_path())
    return data if isinstance(data, dict) else None


def _carried_current_model() -> Optional[str]:
    prev = _read_cache()
    if not prev or prev.get("config_dir") != config_dir():
        return None
    model = prev.get("current_model")
    return model if isinstance(model, str) and model else None


def _carried_account() -> Optional[dict]:
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
    points, never from create_app. Failures are swallowed."""
    def _run() -> None:
        try:
            snapshot()
        except Exception:  # noqa: BLE001 - a warm-up must never be the failure
            pass

    threading.Thread(target=_run, daemon=True, name="opencode-health").start()


def _public(data: dict) -> dict:
    """A snapshot without its cache bookkeeping (the fingerprint carries PATH)."""
    return {k: v for k, v in data.items() if k != "fingerprint"}


def summary() -> dict:
    """The cached snapshot, as the endpoint answers it."""
    return _public(snapshot())


def summary_refreshed() -> dict:
    """`summary()` after a forced re-measure."""
    return _public(snapshot(refresh=True))
