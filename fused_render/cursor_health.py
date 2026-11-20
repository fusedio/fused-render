"""Whether Cursor's CLI (`cursor-agent`) is usable on this machine — asked
BEFORE anything needs it. The Cursor twin of `opencode_health.py`
(docs/CURSOR_BACKEND.md D10), with the same public surface and the same
snapshot keys (plus `current_model`), so the strip that renders one can
render the other.

What is shared and what is copied follows the line the other agents draw:
the GENERIC pieces (subprocess kwargs, version parsing, `executable`, the
shell-rc / `path_fix` machinery, the cache-age rule) are imported from
`claude_health`; the pieces that spell "cursor-agent" in a string literal are
COPIED from opencode_health with the name changed.

The signed-in question is the CLI's own: `cursor-agent status --format json`
answers `{status: "authenticated" | "partially-authenticated" |
"unauthenticated", userInfo: {email, teamName, …}}` without starting a
session or calling a model. A `CURSOR_API_KEY` in the environment signs the
CLI in without a stored login (the ACP server counts it so), so that reads
as signed in with `method: "api_key"`. The account's model list comes off
the ACP server (`cursor-agent acp`): `initialize` (asking for the
parameterized model picker) then `cursor/list_available_models`, the CLI's
own extension method — no session is created and nothing is sent to a
model. A list that would not come leaves the last snapshot's standing.

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
# spawn paths (cursor_host via agent.py) resolve the same binary health did.
BIN_ENV = "FUSED_RENDER_CURSOR_BIN"

# Where the installer puts `cursor-agent`, for when it isn't on the PATH this
# process inherited (D10, in this order): `curl https://cursor.com/install |
# bash` links it into ~/.local/bin (beside a bare `agent` alias, never used
# — too generic a name); Homebrew's cask links into /opt/homebrew/bin or
# /usr/local/bin.
POSIX_CANDIDATES = (
    "~/.local/bin/cursor-agent",
    "/opt/homebrew/bin/cursor-agent",
    "/usr/local/bin/cursor-agent",
)

# Windows: the installer's own dir. Expanded by the same
# expanduser(expandvars()) the resolvers already apply.
WINDOWS_CANDIDATES = (
    r"%LOCALAPPDATA%\cursor-agent\cursor-agent.cmd",
    r"%LOCALAPPDATA%\cursor-agent\cursor-agent.exe",
)


def candidates() -> tuple:
    """The known install locations, unexpanded."""
    return WINDOWS_CANDIDATES if sys.platform == "win32" else POSIX_CANDIDATES


# The lowest `cursor-agent` the host is known to work with: the month of the
# release whose bundle the design was read from (2026.11.19-f005e9d). Lower
# it once an older release is verified, and say which.
MIN_VERSION = "2026.11.0"


def is_outdated(found: Optional[str], floor: str = MIN_VERSION) -> bool:
    """claude_health.is_outdated against THIS floor — a bare re-export would
    default to Claude's 2.0.0 and call every release fine."""
    return claude_health.is_outdated(found, floor)


_VERSION_TIMEOUT_S = 10
_SHELL_TIMEOUT_S = 8
#: `status --format json`: one authenticated request to Cursor's backend.
_STATUS_TIMEOUT_S = 20
#: The ACP models probe: a Node start, `initialize`, and one backend fetch.
#: Bounded so a wedged CLI cannot pin a health read; the timeout reads as
#: "keep the last list", never as "no models".
_ACP_TIMEOUT_S = 25
#: How long each reap step waits before escalating (see `_reap`).
_REAP_GRACE_S = 2.0

#: The dotted date at the front of the release string (`2026.11.19-…`).
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

#: The env var that signs the CLI in without a stored login.
API_KEY_ENV = "CURSOR_API_KEY"


def augmented_path() -> str:
    """PATH with the known install dirs appended (deduped, order preserved).

    Built on top of claude_health's, not instead of it: `~/.local/bin`, where
    Cursor's installer links `cursor-agent`, is on that list, and so are the
    Homebrew prefixes.
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
    exports both), PATH augmented. `CURSOR_API_KEY` passes through as found
    (it is a credential the CLI reads, D10), and `NO_OPEN_BROWSER=1` always:
    a health read must never open a window. `extra` lands on top."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH")}
    env["PATH"] = augmented_path()
    env["NO_OPEN_BROWSER"] = "1"
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
    """Ask the user's LOGIN SHELL where `cursor-agent` is.

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
                detached_argv([shell, *flags, "command -v cursor-agent"]),
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
    """`(path, source)` for the `cursor-agent` to run, or `(None, None)`.

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
    found = shutil.which("cursor-agent")
    if found:
        return found, "path"
    for candidate in candidates():
        path = os.path.expanduser(os.path.expandvars(candidate))
        if executable(path):
            return path, "candidate"
    augmented = shutil.which("cursor-agent", path=augmented_path())
    if augmented:
        return augmented, "candidate"
    if allow_shell:
        via_shell = _shell_probe()
        if via_shell:
            return via_shell, "shell"
    return None, None


def probe_version(path: str) -> Optional[str]:
    """What `path --version` says, or None if it would not tell us.

    The release string is a date and a commit (`2026.11.19-f005e9d`); the
    dotted date is the version. The first dotted run of STDOUT;
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
    """Cursor's own config dir, `~/.cursor` — the one place the app spells
    it. Its credential and `cli-config.json` are never read (D10)."""
    return os.path.join(os.path.expanduser("~"), ".cursor")


# --- the signed-in probe --------------------------------------------------------


def _status(path: str) -> Optional[dict]:
    """What `cursor-agent status --format json` says, or None when it would
    not say (timeout, no JSON). Exit 1 with `{status: "error"}` is a reply
    too — read as unknown by `_classify`."""
    try:
        res = _run_probe(path, "status", "--format", "json",
                         timeout=_STATUS_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    text = (res.stdout or "").strip()
    start = text.find("{")
    if start < 0:
        return None
    try:
        body = json.loads(text[start:])
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _classify(body: Optional[dict]) -> Optional[bool]:
    """A `status` reply → True / False / None-unknown. `authenticated` is
    True; `unauthenticated` and `partially-authenticated` (a token with no
    refresh token: the next request fails) are False — unless a
    `CURSOR_API_KEY` is set, which signs the CLI in by itself. Anything else
    (`error`, a reply we could not read) is None, because False is what puts
    "sign in" in front of the user and a sign-in cannot fix that."""
    status = body.get("status") if isinstance(body, dict) else None
    if status == "authenticated":
        return True
    if status in ("unauthenticated", "partially-authenticated"):
        return True if os.environ.get(API_KEY_ENV) else False
    return None


def signed_in(path: Optional[str] = None) -> Optional[bool]:
    """Whether the CLI is signed in — True, False, or None for unknown.
    Tri-state like claude_health.signed_in and for the same reason."""
    if path:
        return _classify(_status(path))
    return None


def account(signed: Optional[bool], body: Optional[dict] = None) -> Optional[dict]:
    """WHO is signed in, in the shape the wizard's "Signed in" row reads:
    `{method, email, org, plan}`. `method` is `cursor` (a stored login) or
    `api_key` (`CURSOR_API_KEY`); `org` the team the login is acting for.
    None unless `signed` is True.

    A set `CURSOR_API_KEY` is `api_key` WHATEVER `status` says: the CLI
    reaches for the key before a stored login (auth token, then key, then
    login), and its first key-signed run stores the exchanged token, after
    which `status` answers `authenticated` with the key owner's `userInfo`
    (observed on 2026.11.19-f005e9d) — not a login the user made."""
    if signed is not True:
        return None
    info = body.get("userInfo") if isinstance(body, dict) else None
    info = info if isinstance(info, dict) else {}
    stored = (isinstance(body, dict) and body.get("status") == "authenticated"
              and not os.environ.get(API_KEY_ENV))
    email = info.get("email")
    org = info.get("teamName")
    return {"method": "cursor" if stored else "api_key",
            "email": email if isinstance(email, str) and email else None,
            "org": org if isinstance(org, str) and org else None,
            "plan": None}


# --- the account's model list (ACP) ---------------------------------------------


def _reap(proc: subprocess.Popen) -> None:
    """Close stdin, then SIGTERM, wait, then SIGKILL. Cursor's ACP server
    does NOT exit on stdin EOF (observed on 2026.11.19-f005e9d) but exits
    at once on SIGTERM, so there is no grace period for the EOF alone."""
    try:
        proc.stdin.close()
    except (OSError, ValueError):
        pass
    for step, grace in ((proc.terminate, _REAP_GRACE_S), (proc.kill, 5)):
        try:
            if step is not None:
                step()
            proc.wait(grace)
            return
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return


#: The CLI's own extension method for the account's models (`cursor/
#: list_available_models`, served by its ACP agent's `extMethod`).
MODELS_METHOD = "cursor/list_available_models"


def _acp_models(path: str) -> Optional[dict]:
    """The raw reply to `cursor/list_available_models`, or None when it
    would not say. `initialize` asks for the PARAMETERIZED model picker, the
    same capability the session host declares, so the ids are the base
    names `session/set_model` takes. No `cwd=`, no session."""
    proc = None
    answer: list = []

    def _reader(stream) -> None:
        try:
            for line in stream:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict) or "method" in msg:
                    continue
                if msg.get("id") == 2:
                    answer.append(msg)
                    return
        except (OSError, ValueError):
            pass

    try:
        proc = subprocess.Popen(
            [path, "acp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_child_env(),
            **SUBPROCESS_KWARGS,
        )
        for msg in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": 1,
                        "clientCapabilities": {"_meta": {"parameterizedModelPicker": True}},
                        "clientInfo": {"name": "fused-render", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": MODELS_METHOD, "params": {}},
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


def normalize_models(raw) -> list:
    """`cursor/list_available_models` rows (`{value, name, configOptions}`)
    as the snapshot stores them: `{id, label, description}`, in Cursor's
    order, malformed rows and duplicates dropped."""
    out, seen = [], set()
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("value") or row.get("id") or row.get("modelId") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid, "label": str(row.get("name") or row.get("label") or mid),
                    "description": str(row.get("description") or "") or None})
    return out


def _models_of(reply) -> Optional[list]:
    result = reply.get("result") if isinstance(reply, dict) else None
    rows = result.get("models") if isinstance(result, dict) else None
    out = normalize_models(rows)
    return out or None


def cached_models() -> Optional[list]:
    """The last snapshot's `models`, read raw (no freshness gate: the list
    does not go stale with the PATH), or None. What `agent_backends.cursor.
    discovered_models` falls back to when the catalog has no row to read."""
    prev = _read_cache()
    rows = prev.get("models") if prev else None
    return list(rows) if isinstance(rows, list) and rows else None


# --- what kind of install is this, and can we repair it ----------------------

#: The install line the app runs itself (cursor_install.py) and shows first.
INSTALL_COMMAND_POSIX = "curl https://cursor.com/install -fsS | bash"


def install_command() -> str:
    """The install line, as a user would type it."""
    return INSTALL_COMMAND_POSIX


#: Install methods the app updates itself, and the ones it only names.
UPDATES_ITSELF = ("installer",)
MANAGED_UPDATE_COMMANDS = {
    "brew": "brew upgrade --cask cursor-cli",
}

#: Path fingerprints, matched against the path AND its realpath (lowercased):
#: the installer keeps every release under ~/.local/share/cursor-agent/versions.
_INSTALLER_NEEDLES = ("/cursor-agent/versions/", "/.local/bin/cursor-agent")
_BREW_NEEDLES = ("/opt/homebrew/", "/usr/local/cellar/", "/usr/local/caskroom/",
                 "/home/linuxbrew/")


def install_method(path: Optional[str]) -> Optional[str]:
    """How this copy was installed: `installer` | `brew` | `other`, or None
    with no path. By path (and the path a symlink resolves to)."""
    if not path:
        return None
    seen = [path.lower()]
    try:
        seen.append(os.path.realpath(path).lower())
    except (OSError, ValueError):
        pass
    if any(n in p for p in seen for n in _INSTALLER_NEEDLES):
        return "installer"
    if any(n in p for p in seen for n in _BREW_NEEDLES):
        return "brew"
    return "other"


def update_command(path: Optional[str] = None) -> str:
    """`cursor-agent update`, the CLI's own updater."""
    return "cursor-agent update"


def update_plan(method_: Optional[str]) -> dict:
    """Whether an update can be offered, and what would run — the three
    outcomes of claude_health.update_plan."""
    if method_ in UPDATES_ITSELF:
        return {"updatable": True, "command": update_command(),
                "manager": None, "reason": None}
    if method_ in MANAGED_UPDATE_COMMANDS:
        return {"updatable": False, "command": MANAGED_UPDATE_COMMANDS[method_],
                "manager": method_,
                "reason": f"{method_} owns this install, so it updates through {method_}"}
    return {"updatable": None, "command": update_command(), "manager": None,
            "reason": None}


def add_to_shell_path() -> dict:
    """Append the PATH line to the user's shell rc — the strip's one-click fix.
    Returns `{ok, rc_file, line}` or `{ok: False, error}`."""
    path, _source = resolve(allow_shell=False)
    if not path or not executable(path):
        return {"ok": False, "error": "there is no Cursor CLI on this machine "
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

_CACHE_NAME = "cursor-health.json"
_LOCK = threading.Lock()
_CACHE_VERSION = 1
_MAX_AGE_S = 60.0


def _cache_path() -> str:
    return os.path.join(storage.home_dir(), _CACHE_NAME)


def _fingerprint(path: Optional[str]) -> str:
    """What must be unchanged for a cached snapshot to still be true.
    Whether a `CURSOR_API_KEY` is set is in it (never the key itself),
    because it signs the CLI in."""
    stamp = ""
    if path:
        try:
            stamp = str(os.stat(path).st_mtime_ns)
        except OSError:
            stamp = "missing"
    return "\x1f".join([str(_CACHE_VERSION), os.environ.get(BIN_ENV, ""),
                        os.environ.get("PATH", ""),
                        "key" if os.environ.get(API_KEY_ENV) else "",
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

    body = _status(path) if usable else None
    signed = _classify(body) if usable else None
    acct = account(signed, body)
    models = None
    if signed:
        models = _models_of(_acp_models(path)) or cached_models()
    if signed is None and usable:
        # A probe that could not tell is no news about the account, so the
        # last snapshot's account and list stand.
        acct = _carried_account()
        models = cached_models()

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
        # No `cursor-agent doctor`; kept so the snapshots share a shape.
        "doctor": None,
        # The account's models off `cursor/list_available_models`, or None:
        # the static catalog stands.
        "models": models,
        # The session decides its model (`session/new`'s currentModelId);
        # the probe opens none, so it does not know one.
        "current_model": None,
        "on_shell_path": on_shell_path,
        "path_fix_command": fix["command"] if fix else None,
        "checked_at": time.time(),
        "fingerprint": _fingerprint(path),
    }


def _read_cache() -> Optional[dict]:
    data = storage.read_json(_cache_path())
    return data if isinstance(data, dict) else None


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

    threading.Thread(target=_run, daemon=True, name="cursor-health").start()


def _public(data: dict) -> dict:
    """A snapshot without its cache bookkeeping (the fingerprint carries PATH)."""
    return {k: v for k, v in data.items() if k != "fingerprint"}


def summary() -> dict:
    """The cached snapshot, as the endpoint answers it."""
    return _public(snapshot())


def summary_refreshed() -> dict:
    """`summary()` after a forced re-measure."""
    return _public(snapshot(refresh=True))
