"""Shared mechanics for the fused-render Claude config editor.

Ports server/lib.ts from claude-config-ui to stdlib Python. Owns:
  - CLAUDE_DIR resolution + config file paths        (config-store.md §1-2)
  - atomic read/write JSON + read-modify-write merge  (config-store.md §3-5)
  - lock-serialized mutation                          (concurrency)
  - the packaged settings catalog

Every feature module imports from here; no feature reimplements these mechanics
(specs: "one concept, one owner"). Stdlib only bar two intra-repo imports:
`_platform` (the cross-platform file lock) and `shell.storage.home_dir()` (the
single owner of ~/.fused-render, re-exported for mcp.py's cache path).
"""
import contextlib
import itertools
import json
import os
import shutil
import subprocess
import threading
from typing import Any, Generator, Optional

from fused_render import _platform
from fused_render.shell.storage import home_dir


# --- config-store.md §1: base directory ------------------------------------

CLAUDE_DIR = os.environ.get("CLAUDE_DIR") or os.path.expanduser("~/.claude")
SETTINGS_PATH = os.path.join(CLAUDE_DIR, "settings.json")
INSTALLED_PLUGINS_PATH = os.path.join(CLAUDE_DIR, "plugins", "installed_plugins.json")
KNOWN_MARKETPLACES_PATH = os.path.join(CLAUDE_DIR, "plugins", "known_marketplaces.json")
MARKETPLACES_DIR = os.path.join(CLAUDE_DIR, "plugins", "marketplaces")

# The kwargs EVERY subprocess in this package is spawned with. Two unrelated
# defaults, both of which are wrong for a server process, both of which fail in
# ways that name nothing:
#
# 1. close_fds=False — the SPAWN discipline app_git.py's module docstring
#    documents in full (read it; this is the same crash, not a similar one).
#    Short version: the server has libproj resident, so a plain fork() runs
#    PROJ's pthread_atfork child handler into a SIGSEGV before exec, and the
#    default close_fds=True is what takes the fork path on macOS. close_fds=False
#    makes CPython use posix_spawn, which runs no atfork handlers. Without it
#    every `claude` here dies rc=-11 with EMPTY stderr, in 0.0s, and the MCP page
#    reports "failed to list MCP servers". The same command runs fine from a
#    shell, because a shell has no PROJ loaded.
#
# 2. text/encoding/errors — `text=True` alone decodes with
#    locale.getpreferredencoding(False), and a GUI-launched server inherits no
#    LANG/LC_ALL, so on macOS that resolves to ASCII. The moment a child prints
#    a non-ASCII byte — `claude mcp list` draws ✔/✘/⏸, a statusline script emits
#    a nerd-font glyph, a project path is accented — the decode raises
#    UnicodeDecodeError and the whole action 500s. Pinning UTF-8 (what all of
#    these actually emit) with errors="replace" makes the worst case a mojibake
#    character rather than a dead page.
#
# One dict rather than two, and spread at every call site including the
# detached Popen — the decode kwargs are inert against its DEVNULL streams, and
# that is a cheaper price than a second constant nobody remembers to apply.
SUBPROCESS_KWARGS = {
    "close_fds": False,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
}

# One lock serializes all config mutation (the read-modify-write of
# settings.json and the other config files), so two concurrent actions can't
# clobber each other. TWO mechanisms: requests run as threadpool workers inside
# ONE process, where the threading lock serializes them; the flock keeps a
# SEPARATE process (a CLI run of these modules, a second desktop instance) from
# interleaving with us. Always acquired in this order, so the pair can't
# deadlock. The threading lock is not reentrant: a caller holding config_lock()
# must not call a helper that takes it again.
_LOCK_PATH = os.path.join(CLAUDE_DIR, ".config-ui.lock")
_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def config_lock() -> Generator[None, None, None]:
    os.makedirs(CLAUDE_DIR, exist_ok=True)
    with _THREAD_LOCK, open(_LOCK_PATH, "w") as lock:
        _platform.flock(lock)
        yield


# --- preferences.md §5: the settings catalog ---------------------------------
# settings_catalog.json ships next to this module. It is regenerated from
# Anthropic's docs by `python3 -m fused_render.claude_config.refresh_catalog`
# (a development script that rewrites the checked-in file) and read as-is at
# runtime.
CATALOG_FILENAME = "settings_catalog.json"


def packaged_catalog_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CATALOG_FILENAME)


def load_catalog() -> list:
    with open(packaged_catalog_path(), "r", encoding="utf-8") as f:
        return json.load(f)


# --- config-store.md §3-5: read / write / merge -----------------------------

def as_bool(v: Any) -> bool:
    """Coerce a param to bool. fused coerces `bool`-annotated params before
    calling main(), but a raw string "false" is truthy in Python — guard so the
    modules are correct whether the value arrives as bool or string."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def parse_frontmatter(text: str) -> dict:
    """Extract the name/description scalars from a leading --- ... --- block.
    Minimal line parser, no YAML dep (skills.md §3).

    Lives HERE rather than in skills.py because two features read the same block
    now: a local skill's SKILL.md, and every SKILL.md/agent/command markdown a
    PLUGIN ships (plugins.contents). One parser, so a frontmatter quirk fixed
    for one surface is fixed for the other — this function's whole history is
    such a quirk. It read only the text on the key's OWN line, which is most of
    YAML's ways of writing a string and not all of them: a description written
    as a block scalar (`description: |`, which context-mode's eight skills all
    use) put its text on the FOLLOWING lines and left "|" on the key's line, so
    every one of them displayed as a bare pipe.

    A continuation is any more-indented line under the key, and it is FOLDED
    into one space-joined string whether the block said `|` or `>`. Both
    callers put this in a one-line slot, so the distinction YAML draws between
    them — whether newlines survive — has nothing to act on here."""
    out = {"name": "", "description": ""}
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end == -1:
        return out
    lines = text[3:end].split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        key, sep, val = line.partition(":")
        indent = len(key) - len(key.lstrip())
        key = key.strip()
        if not sep or key not in ("name", "description"):
            continue
        v = val.strip()
        # A block indicator (with any chomping/indent modifier) or an empty
        # value both mean "the value is on the lines below".
        if v == "" or v[0] in "|>":
            parts = []
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break  # back out to the key's own level: a new key.
                parts.append(nxt.strip())
                i += 1
            v = " ".join(p for p in parts if p)
        elif len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
            v = v[1:-1]
        out[key] = v
    return out


def read_json(path: str, fallback: Any) -> Any:
    """Return `fallback` only when the file is ABSENT. Malformed JSON raises —
    corruption must surface, never be silently swallowed (config-store.md §3)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return fallback


def read_settings() -> dict:
    return read_json(SETTINGS_PATH, {})


def _tmp_path(path: str) -> str:
    """A sibling temp path for the atomic write below, unique to this CALL —
    not just this process. Two threadpool workers in the same server process
    share a pid, so a pid-only name would have them race the SAME tmp file (one
    thread's open()/write() clobbering the other's, one thread's os.replace()
    removing it out from under the other). threading.get_ident() (unique among
    live threads in this process) plus a counter (unique across successive
    calls a single thread makes) closes it."""
    return f"{path}.tmp-{os.getpid()}-{threading.get_ident()}-{next(_tmp_counter)}"


_tmp_counter = itertools.count()


@contextlib.contextmanager
def _atomic_write(path: str) -> Generator[str, None, None]:
    """mkdir -p, yield a fresh sibling temp path to write into, then
    os.replace it over `path` (atomic on the same filesystem) on a clean
    exit. Uniqueness per call (`_tmp_path`) closes the cross-thread name
    collision; the cleanup below closes the other half — an ORPHAN. A failure
    between `open()` and `os.replace()` (ENOSPC, a `json.dump` error on a value
    that turns out not to be serialisable, the process killed mid-write) would
    otherwise leave the per-call tmp file behind forever.

    `os.replace` itself is INSIDE the try, not after it — a version that
    only guarded the write and let `os.replace` run unguarded afterward
    would leave exactly the orphan class this docstring claims to close: a
    cross-device `path` raises, which leaves `tmp` fully written and
    orphaned if the exception isn't caught here."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = _tmp_path(path)
    try:
        yield tmp
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def write_json(path: str, value: Any) -> None:
    """Atomic write (config-store.md §4): mkdir -p, write a sibling temp file,
    then os.replace over the target (atomic on the same filesystem). Pretty
    2-space + trailing newline."""
    with _atomic_write(path) as tmp:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")


# Dotted-path helpers for nested settings keys like "permissions.defaultMode"
# (preferences.md §3). Missing segments read as None; set creates them; delete
# removes the leaf while preserving siblings.

def get_path(obj: dict, path: str) -> Any:
    cur = obj
    for k in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def set_path(obj: dict, path: str, value: Any) -> None:
    keys = path.split(".")
    leaf = keys.pop()
    cur = obj
    for k in keys:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[leaf] = value


def delete_path(obj: dict, path: str) -> None:
    keys = path.split(".")
    leaf = keys.pop()
    cur = obj
    for k in keys:
        if not isinstance(cur.get(k), dict):
            return
        cur = cur[k]
    cur.pop(leaf, None)



def read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def write_text(path: str, content: str) -> None:
    with _atomic_write(path) as tmp:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)


def safe_subdir(base: str, slug: str, tail: str = "") -> str:
    """Resolve base/slug[/tail] and confirm it stays under base. Rejects
    traversal (`..`), absolute slugs, and anything escaping the base dir.
    Ports memoryDirPath/skillDirPath's security boundary from server/lib.ts.

    The escape check is **lexical** (normpath, symlinks not resolved): a leaf
    that is itself a symlink pointing outside `base` is allowed — linked skills
    rely on this (skills.md §5, memory.md §6). Resolving the leaf with realpath
    would reject every linked skill while adding no protection: planting a
    symlink under `base` already requires filesystem write access."""
    if not slug or "/" in slug or "\\" in slug or slug in (".", ".."):
        raise ValueError(f"invalid slug: {slug!r}")
    base_real = os.path.realpath(base)
    target = os.path.normpath(os.path.join(base_real, slug, tail))
    if target != base_real and not target.startswith(base_real + os.sep):
        raise ValueError(f"path escapes base: {slug!r}")
    return target


def reveal(path: str) -> bool:
    """Open a path in the OS file explorer. Best-effort; argv, never a shell
    string. macOS `open`, `os.startfile` on Windows, else xdg-open."""
    import sys
    if sys.platform == "win32":
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    cmd = ["open"] if sys.platform == "darwin" else ["xdg-open"]
    try:
        subprocess.run([*cmd, path], capture_output=True, timeout=10, **SUBPROCESS_KWARGS)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# A GUI-launched process inherits a minimal PATH (/usr/bin:/bin:…) that omits
# where `claude` actually lives, so `subprocess.run(["claude", …])` FileNotFounds
# even though claude runs fine in the user's shell. These are the common install
# dirs we augment PATH with, then probe directly (plugins.md §5).
_CLAUDE_BIN_DIRS = [
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.claude/local"),
    os.path.expanduser("~/.bun/bin"),
    os.path.expanduser("~/Library/pnpm"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
]


def _augmented_path() -> str:
    """PATH with the common claude/node install dirs appended (deduped)."""
    seen, parts = set(), []
    for p in (os.environ.get("PATH", "") or "").split(os.pathsep) + _CLAUDE_BIN_DIRS:
        if p and p not in seen:
            seen.add(p)
            parts.append(p)
    return os.pathsep.join(parts)


def _resolve_claude(path_env: str) -> Optional[str]:
    """Absolute path to the `claude` binary, or None. Tries PATH (augmented),
    then a direct probe of each known dir."""
    found = shutil.which("claude", path=path_env)
    if found:
        return found
    for d in _CLAUDE_BIN_DIRS:
        cand = os.path.join(d, "claude")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def option_shaped(value: str) -> bool:
    """True if `value` would be read as a FLAG rather than as a value.

    An argv array closes off shell injection — nothing here ever builds a
    command string — but it does not close off OPTION injection: argv[n] is
    still parsed by the program receiving it, and a leading "-" is what makes
    it an option. So a plugin published as `--force` yields the id
    "--force@marketplace", which `claude plugin install` reads as a flag with a
    junk argument, not as a plugin to install.

    That matters because the strings we hand the CLI are not all ours: a
    marketplace catalog is third-party content cloned off a git remote, and
    settings.json is hand-editable. Callers reject rather than escape — there is
    no legitimate plugin or server whose name starts with a dash, so refusing is
    both the safe answer and the correct one."""
    return value.startswith("-")


def claude_cli(*args: str, timeout: int = 25) -> dict:
    """Run the `claude` binary with an argv array (never a shell string, so
    args can't inject). Resolves the binary's absolute path (plugins.md §5) so
    a GUI-launched, minimal-PATH process still finds it. Best-effort: returns
    {ok, stdout, stderr}. Bounded so a hung CLI can't pin a threadpool worker."""
    path_env = _augmented_path()
    binary = _resolve_claude(path_env)
    if binary is None:
        return {"ok": False, "stdout": "",
                "stderr": "claude CLI not found (looked on PATH and in "
                          f"{', '.join(_CLAUDE_BIN_DIRS)})"}
    # Pass the augmented PATH through so the resolved `claude` can find its own node.
    env = {**os.environ, "PATH": path_env}
    try:
        res = subprocess.run(
            [binary, *args], capture_output=True, timeout=timeout, env=env, **SUBPROCESS_KWARGS
        )
        return {
            "ok": res.returncode == 0,
            "stdout": res.stdout.strip(),
            "stderr": res.stderr.strip(),
        }
    except FileNotFoundError:
        return {"ok": False, "stdout": "", "stderr": "claude CLI not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"claude {args[0]} timed out"}


def claude_cli_detached(*args: str) -> dict:
    """Spawn the `claude` binary in its OWN session and return immediately (mcp.md §3).
    For interactive commands (OAuth `mcp login`) that open a browser and block past the
    request: start_new_session=True detaches the child from this process's process
    group so it survives after the response is sent. Best-effort — success means
    'launched', not 'finished'; the outcome is observed via a later `mcp list` refresh."""
    path_env = _augmented_path()
    binary = _resolve_claude(path_env)
    if binary is None:
        return {"ok": False, "error": "claude CLI not found (looked on PATH and in "
                                      f"{', '.join(_CLAUDE_BIN_DIRS)})"}
    env = {**os.environ, "PATH": path_env}
    subprocess.Popen(
        [binary, *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=env, **SUBPROCESS_KWARGS,
    )
    return {"ok": True, "launched": True}


# mcp.md §2: `claude mcp list` has no structured output, so parse its human-readable
# lines. Names carry both spaces and colons (`claude.ai Slack`,
# `plugin:context-mode:context-mode`), so the name/endpoint boundary is the FIRST
# ": " (colon-space, which bare-colon names lack).
#
# The status is a PREFIX, not the whole trailing segment. A status may carry a
# reason after it, and the CLI joins that reason with an em dash:
#
#   plugin:github:github: https://… (HTTP) - ✘ Failed to connect — HTTP 400: …
#
# An exact-match lookup against the trailing segment therefore failed on exactly
# the servers that are broken — the only status that ever HAS a reason — and the
# one server that was genuinely down was the one the UI called "unknown". Match
# on the leading marker plus its phrase; everything after that is detail, and
# the detail is kept (statusDetail) rather than thrown away, because "failed"
# alone is a dead end and "failed — Authorization header is badly formatted" is
# something the user can act on.
_MCP_STATUS = (
    ("✔ connected", "connected"),
    ("! needs authentication", "needs-auth"),
    ("✘ failed to connect", "failed"),
    ("⏸ pending approval", "pending"),
)

# What the CLI puts between a status and its reason. Stripped from the front of
# the detail so it reads as a sentence rather than as a fragment.
_DETAIL_JOINERS = "—–-: "


def _split_status(line: str) -> tuple:
    """(body, status_part), split at the status marker.

    Deliberately NOT `rpartition(" - ")`: a reason is free-form CLI text and may
    itself contain " - ", which would put the split inside the reason and take
    the endpoint with it. The marker glyph is the reliable landmark, so find the
    FIRST " - <marker>" and cut there; fall back to the last " - " only when no
    marker is recognised, which is the `unknown` path.
    """
    best = -1
    for prefix, _ in _MCP_STATUS:
        found = line.find(" - " + prefix[0])
        if found != -1 and (best == -1 or found < best):
            best = found
    if best == -1:
        body, _, status_part = line.rpartition(" - ")
        return body, status_part
    return line[:best], line[best + len(" - "):]


def _classify_status(status_part: str) -> tuple:
    """(status, detail) from the trailing segment of a list line."""
    tail = status_part.strip()
    low = tail.lower()
    for prefix, value in _MCP_STATUS:
        if low.startswith(prefix):
            # Slice the ORIGINAL by the prefix's length — the two differ only in
            # case, so the offset holds and the detail keeps its own casing.
            return value, tail[len(prefix):].strip().lstrip(_DETAIL_JOINERS).strip()
    # An unrecognised marker: report it as unknown rather than guessing, and
    # keep the whole segment as the detail so the UI can still show what the CLI
    # actually said.
    return "unknown", tail


def parse_mcp_list(text: str) -> list:
    """Parse `claude mcp list` stdout into server dicts (mcp.md §2)."""
    servers = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if " - " not in line or ": " not in line:
            continue  # banner ("Checking MCP server health…"), blanks
        body, status_part = _split_status(line)
        name, _, endpoint = body.partition(": ")
        name, endpoint = name.strip(), endpoint.strip()
        if not name:
            continue
        status, status_detail = _classify_status(status_part)

        transport = ""
        for marker, kind_ in (("(HTTP)", "http"), ("(SSE)", "sse")):
            if endpoint.endswith(marker):
                transport = kind_
                endpoint = endpoint[: -len(marker)].strip()
                break
        is_url = endpoint.startswith(("http://", "https://"))
        if not transport:
            transport = "http" if is_url else "stdio"

        if name.startswith("plugin:"):
            kind = "plugin"
        elif name.startswith("claude.ai "):
            kind = "connector"
        else:
            kind = "user"

        can_auth = is_url or transport in ("http", "sse")
        servers.append({
            "name": name,
            "endpoint": endpoint,
            "transport": transport,
            "status": status,
            # The CLI's own words about WHY, where it gave any — "" for the
            # statuses that need no explanation (connected, needs-auth).
            "statusDetail": status_detail,
            "kind": kind,
            "connected": status == "connected",
            "needsAuth": status == "needs-auth",
            "canAuth": can_auth,
            "removable": kind == "user",
        })
    return servers
