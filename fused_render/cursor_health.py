"""Whether Cursor CLI is usable on this machine — asked BEFORE anything needs
it. The Cursor twin of `gemini_health.py`, `codex_health.py` and
`copilot_health.py`, with the same public surface and the same snapshot keys,
so the strip that renders one can render the other without learning a new
shape.

What is shared and what is copied follows the line the others draw: the
GENERIC pieces (subprocess kwargs, version parsing, `executable`, the
shell-rc / `path_fix` machinery, the cache-age rule) are imported from
`claude_health`; the pieces that spell "cursor" in a string literal (override
name, candidate list, login-shell probe, `resolve` and its adoption record,
cache file, fingerprint) are COPIED with the name changed.

**Two binary names.** The CLI installs as `cursor-agent` and as `agent`.
`agent` is far too generic a name to trust, so it is a fallback only: every
`cursor-agent` route is tried first, and an `agent` is taken only when its
`--version` prints a dated Cursor build (`YYYY.MM.DD-hash`).

**The probes never spawn the ACP server and never open a browser.** Signed in?
is `cursor-agent status` (text: "Not logged in" signed out, an account line
with the email signed in); the account's model list is `cursor-agent models`,
asked only once signed in (it needs the login and the network). A
`CURSOR_API_KEY` / `CURSOR_AUTH_TOKEN` in the environment is itself a
credential and counts as signed in without a status call.

**Nothing here raises.** Same doctrine as the Claude module: a health report
that 500s is worth less than one that says "I could not tell".
"""
from __future__ import annotations

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
    path_fix,
)
from fused_render.shell import storage

# An explicit override beats every probe below. Same role as
# FUSED_RENDER_CLAUDE_BIN: `adopt` publishes a shell-only find into it so the
# spawn paths (cursor_host via agent.py) resolve the same binary health did.
BIN_ENV = "FUSED_RENDER_CURSOR_BIN"

#: The CLI's two names, in preference order. `agent` is verified (see
#: `_looks_like_cursor`) before it is believed.
NAME = "cursor-agent"
ALIAS = "agent"

# Where the installer puts the CLI, for when it isn't on the PATH this process
# inherited: the installer links into ~/.local/bin; ~/.cursor/bin is the
# editor-side install; Homebrew's cask links into /opt/homebrew/bin (Apple
# Silicon) or /usr/local/bin (Intel). Every `cursor-agent` first, then the
# `agent` aliases.
POSIX_CANDIDATES = (
    "~/.local/bin/cursor-agent",
    "~/.cursor/bin/cursor-agent",
    "/opt/homebrew/bin/cursor-agent",
    "/usr/local/bin/cursor-agent",
    "~/.local/bin/agent",
    "~/.cursor/bin/agent",
)


def candidates() -> tuple:
    """The known install locations, unexpanded. None on Windows: the CLI has
    no Windows build."""
    return () if sys.platform == "win32" else POSIX_CANDIDATES


# The lowest `cursor-agent` the host is known to work with: the VERIFIED
# release (ACP `initialize`, signed out), not a few under it. Cursor ships a
# dated build a day; the date part is what is compared. Lower it once an
# older release is known to work, and say which in the commit.
MIN_VERSION = "2026.11.13"


def is_outdated(found: Optional[str], floor: str = MIN_VERSION) -> bool:
    """claude_health.is_outdated against THIS floor — a bare re-export would
    default to Claude's 2.0.0 and call every Cursor build too new to compare."""
    return claude_health.is_outdated(found, floor)


_VERSION_TIMEOUT_S = 10
_SHELL_TIMEOUT_S = 8
#: `status` talks to Cursor's servers once signed in; `models` as well.
_STATUS_TIMEOUT_S = 20
_MODELS_TIMEOUT_S = 30

#: `cursor-agent --version` prints `2026.11.13-eacade1` bare. STDOUT first;
#: stderr is where a wrapper's warnings go.
_VERSION_RE = re.compile(r"\b(\d{4}\.\d{2}\.\d{2})-([0-9a-f]+)\b")

#: The environment variables that are themselves a credential.
_API_KEY_ENV = ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")


def augmented_path() -> str:
    """PATH with the known install dirs appended (deduped, order preserved).

    Built on top of claude_health's, not instead of it: the installer's
    `~/.local/bin` is exactly what a GUI-launched app is missing."""
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
    exports both), PATH augmented, and `NO_OPEN_BROWSER=1` — a health read may
    never put a browser window in front of the user. CURSOR_CONFIG_DIR and the
    credential variables are passed through exactly as found. `extra` lands on
    top."""
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
    that docstring for why it exists, why PYTHONHOME/PYTHONPATH are scrubbed,
    and why three flag spellings are tried. Last resort, and cached like
    everything else here. Only `cursor-agent`: asking a shell for the generic
    `agent` would find whatever else wears that name.
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
                detached_argv([shell, *flags, f"command -v {NAME}"]),
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
    as claude_health.adopt. The host (claude_agent/cursor_host.py, via
    agent.py) inherits the server's environment, which is how a binary only
    the login shell could find reaches code that may not import this module.
    """
    global _ADOPTED
    if os.environ.get(BIN_ENV):
        return False
    os.environ[BIN_ENV] = path
    _ADOPTED = path
    return True


def _looks_like_cursor(path: str) -> bool:
    """Whether `path` is the Cursor CLI: its `--version` is a dated build.
    Only asked of the generic `agent` name."""
    return probe_version(path) is not None


def _alias_on(path: Optional[str] = None) -> Optional[str]:
    """A verified `agent` on `path` (PATH when None), or None."""
    found = shutil.which(ALIAS, path=path)
    return found if found and _looks_like_cursor(found) else None


def resolve(allow_shell: bool = True) -> tuple:
    """`(path, source)` for the `cursor-agent` to run, or `(None, None)`.

    A copy of claude_health.resolve against this module's override and
    candidate list; the source vocabulary (override / path / candidate /
    shell) and every rule about it are that function's. Every `cursor-agent`
    route is tried before any `agent` one, and an `agent` must verify.
    """
    forced = os.environ.get(BIN_ENV)
    if forced and forced != _ADOPTED:
        return forced, "override"
    if forced:
        if executable(forced):
            return forced, "shell"
        _forget()
    found = shutil.which(NAME)
    if found:
        return found, "path"
    cands = [os.path.expanduser(os.path.expandvars(c)) for c in candidates()]
    named = [c for c in cands if os.path.basename(c) == NAME]
    aliased = [c for c in cands if os.path.basename(c) != NAME]
    for path in named:
        if executable(path):
            return path, "candidate"
    augmented = shutil.which(NAME, path=augmented_path())
    if augmented:
        return augmented, "candidate"
    if allow_shell:
        via_shell = _shell_probe()
        if via_shell:
            return via_shell, "shell"
    alias = _alias_on()
    if alias:
        return alias, "path"
    for path in aliased:
        if executable(path) and _looks_like_cursor(path):
            return path, "candidate"
    alias = _alias_on(augmented_path())
    if alias:
        return alias, "candidate"
    return None, None


def probe_version(path: str) -> Optional[str]:
    """What `path --version` says (`2026.11.13-eacade1`), or None if it would
    not tell us or is not a dated Cursor build.

    STDOUT first; failing that, stderr (a wrapper that prints the banner
    there). The dated shape is also what tells the generic `agent` name apart
    from anything else that wears it."""
    try:
        res = _run_probe(path, "--version", timeout=_VERSION_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    for stream in (res.stdout or "", res.stderr or ""):
        m = _VERSION_RE.search(stream)
        if m:
            return m.group(0)
    return None


def config_dir() -> str:
    """Cursor CLI's own data dir: `CURSOR_CONFIG_DIR` when the user set it,
    else `~/.cursor` — the one place the app spells either."""
    return os.environ.get("CURSOR_CONFIG_DIR") or os.path.expanduser("~/.cursor")


# --- the credential, and whose it is ------------------------------------------


def _env_credential() -> bool:
    return any(os.environ.get(n) for n in _API_KEY_ENV)


def parse_status(text: str, code: int) -> dict:
    """`cursor-agent status`'s output and exit code → `{signed_in, email}`.

    Signed out is "Not logged in" (exit 0, verified) or any non-zero exit.
    The signed-in text is not pinned down, so signed in is "exit 0 and not
    'not logged in'", with the first email address in the text as the
    account."""
    clean = _ANSI_RE.sub("", text or "")
    if code != 0 or "not logged in" in clean.lower():
        return {"signed_in": False, "email": None}
    m = _EMAIL_RE.search(clean)
    return {"signed_in": True, "email": m.group(0) if m else None}


def _status(path: str) -> Optional[dict]:
    """Run `status`: `{signed_in, email}`, or None when it would not answer
    (a timeout or a spawn failure — "could not tell" must never render as
    "sign in")."""
    try:
        res = _run_probe(path, "status", timeout=_STATUS_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return parse_status(f"{res.stdout or ''}\n{res.stderr or ''}", res.returncode)


def signed_in(path: Optional[str] = None) -> Optional[bool]:
    """Whether Cursor has a working credential — True, False, or None for
    unknown. Tri-state like claude_health.signed_in and for the same reason:
    False is what puts "sign in" in front of the user, so only the CLI may
    say it."""
    if _env_credential():
        return True
    if path:
        status = _status(path)
        return status["signed_in"] if status else None
    return None


def account(signed: Optional[bool], email: Optional[str] = None) -> Optional[dict]:
    """WHO is signed in, in the shape the wizard's "Signed in" row reads:
    `{method, email, org, plan}`. An environment credential is `api_key`;
    otherwise the login's `status` email. None unless `signed` is True."""
    if signed is not True:
        return None
    if _env_credential():
        return {"method": "api_key", "email": None, "org": None, "plan": None}
    return {"method": "login", "email": email, "org": None, "plan": None}


# --- the account's models -----------------------------------------------------

#: Words a `models` listing prints that are not ids.
_NOT_AN_ID = {"available", "models", "model", "loading", "error", "tip", "usage",
              "note", "name", "id"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*•·▪‣>]\s+|\d+[.)]\s+)?")
_SPLIT_RE = re.compile(r"\s+[-–—:]\s+|\s{2,}|:\s+|\s+\(")
_MARK_RE = re.compile(r"\s*\((current|default)\)\s*", re.I)


def parse_models(text: str) -> tuple:
    """`cursor-agent models`'s output → `(rows, current)`. One id per line,
    tolerant of decoration: a bullet, `id - Label` / `id: Label`, and
    `(current)` / `(default)` markers (`current` names the model marked
    current). Lines that are not an id (a header, a tip) are skipped; rows are
    `{id, label, description}` in the CLI's order, duplicates dropped."""
    rows, seen, current = [], set(), None
    for raw in _ANSI_RE.sub("", text or "").splitlines():
        line = _BULLET_RE.sub("", raw.strip(), count=1).strip()
        if not line:
            continue
        marks = {m.lower() for m in _MARK_RE.findall(line)}
        line = _MARK_RE.sub(" ", line).strip()
        parts = _SPLIT_RE.split(line, maxsplit=1)
        mid = parts[0].strip()
        if (not _ID_RE.match(mid) or mid.lower() in _NOT_AN_ID
                or (len(parts) == 1 and " " in line)):
            continue
        label = parts[1].strip() if len(parts) > 1 and parts[1].strip() else mid
        if mid in seen:
            continue
        seen.add(mid)
        rows.append({"id": mid, "label": label.rstrip(")").strip() or mid,
                     "description": None})
        if "current" in marks:
            current = mid
    return rows, current


def normalize_models(raw) -> list:
    """Model rows (`{id | modelId, label | name, description}`) as the
    snapshot stores them: `{id, label, description}`, in order, malformed rows
    and duplicates dropped."""
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


def _list_models(path: str) -> Optional[tuple]:
    """Run `models`: `(rows, current)`, or None when it failed or listed
    nothing (needs a login and the network)."""
    try:
        res = _run_probe(path, "models", timeout=_MODELS_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    rows, current = parse_models(res.stdout or "")
    return (rows, current) if rows else None


def cached_models() -> Optional[list]:
    """The last snapshot's `models`, read raw (no freshness gate: the list
    does not go stale with the PATH), or None. What `agent_backends.cursor.
    discovered_models` falls back to when the catalog has no row to read."""
    prev = _read_cache()
    rows = prev.get("models") if prev else None
    return list(rows) if isinstance(rows, list) and rows else None


# --- what kind of install is this, and can we repair it ----------------------

#: The install line the app runs itself (cursor_install.py) and shows first —
#: Cursor's own installer. Running it again over an existing copy updates it.
INSTALL_COMMAND_POSIX = "curl https://cursor.com/install -fsS | bash"


def install_command() -> str:
    """The install line, as a user would type it."""
    return INSTALL_COMMAND_POSIX


#: Install methods the app updates itself (the same installer line), and the
#: ones it only names.
UPDATES_ITSELF = ("curl",)
MANAGED_UPDATE_COMMANDS = {
    "brew": "brew upgrade --cask cursor-cli",
}

#: Path fingerprints, matched against the path AND its realpath (lowercased).
_CURL_NEEDLES = ("/.local/bin/", "/.local/share/cursor-agent/", "/.cursor/")
_BREW_NEEDLES = ("/opt/homebrew/", "/usr/local/cellar/", "/usr/local/caskroom/",
                 "/home/linuxbrew/")


def install_method(path: Optional[str]) -> Optional[str]:
    """How this copy was installed: `curl` | `brew` | `other`, or None with no
    path. By path (and the path a symlink resolves to), since nothing asks the
    CLI. `other` keeps the update on offer (see `update_plan`)."""
    if not path:
        return None
    seen = [path.lower()]
    try:
        seen.append(os.path.realpath(path).lower())
    except (OSError, ValueError):
        pass
    if any(n in p for p in seen for n in _BREW_NEEDLES):
        return "brew"
    if any(n in p for p in seen for n in _CURL_NEEDLES):
        return "curl"
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
#
# Same shape and same two gates as claude_health: a fingerprint for "is this
# the same binary" and an age for "could the answer have changed anyway" (a
# sign-out moves no file this watches). Its own cache file, so the reports
# never read each other's.

_CACHE_NAME = "cursor-health.json"
_LOCK = threading.Lock()
_CACHE_VERSION = 1


def _cache_path() -> str:
    return os.path.join(storage.home_dir(), _CACHE_NAME)


def _fingerprint(path: Optional[str]) -> str:
    """What must be unchanged for a cached snapshot to still be true.
    CURSOR_CONFIG_DIR is in it because it moves the credential store, which
    moves `signed_in` and `account`; whether a credential variable is SET is
    in it for the same reason (never its value — the fingerprint is on disk)."""
    stamp = ""
    if path:
        try:
            stamp = str(os.stat(path).st_mtime_ns)
        except OSError:
            stamp = "missing"
    keyed = "".join("1" if os.environ.get(n) else "0" for n in _API_KEY_ENV)
    return "\x1f".join([str(_CACHE_VERSION), os.environ.get(BIN_ENV, ""),
                        os.environ.get("PATH", ""),
                        os.environ.get("CURSOR_CONFIG_DIR", ""), keyed,
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

    signed: Optional[bool] = None
    email: Optional[str] = None
    if usable:
        if _env_credential():
            signed = True
        else:
            status = _status(path)
            if status is not None:
                signed, email = status["signed_in"], status["email"]
    acct = account(signed, email)
    models: Optional[list] = None
    current_model: Optional[str] = None
    if signed:
        listed = _list_models(path)
        if listed:
            models, current_model = listed
        else:
            # Signed in, but the list did not come: the last one stands.
            models = cached_models()
            current_model = _carried_current_model()
    elif signed is None and usable:
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
        # No `cursor-agent doctor`; kept so the snapshots share a shape.
        "doctor": None,
        # The account's own model list off `cursor-agent models`, or None:
        # the static catalog stands.
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

    threading.Thread(target=_run, daemon=True, name="cursor-health").start()


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
