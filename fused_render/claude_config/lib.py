"""Shared mechanics for the fused-render Claude config editor.

Ports server/lib.ts from claude-config-ui to stdlib Python. Owns:
  - CLAUDE_DIR resolution + config file paths        (config-store.md §1-2)
  - atomic read/write JSON + read-modify-write merge  (config-store.md §3-5)
  - lock-serialized mutation                          (concurrency)
  - the settings-catalog path split (packaged vs override)
  - the git layer over CLAUDE_DIR: whitelist .gitignore,
    ensure_repo, commit, log, status, diff, drift      (version-control.md)

Every feature module imports from here; no feature reimplements these mechanics
(specs: "one concept, one owner"). Stdlib only bar one intra-repo import — the
catalog override resolves under `shell.storage.home_dir()`, which is the single
owner of ~/.fused-render in this codebase and the reason FUSED_RENDER_HOME
redirects the override for tests for free.
"""
import contextlib
import json
import os
import shutil
import subprocess
import threading
from typing import Any, Generator, Optional

from fused_render.shell.storage import home_dir

# flock is POSIX-only and this module is imported at server-startup time (the
# router is registered unconditionally in server/app.py), so a bare
# `import fcntl` would take the WHOLE server down on Windows over a lock that
# is not even the one that matters any more — see config_lock().
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

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
#    makes CPython use posix_spawn, which runs no atfork handlers, and degrades
#    to CreateProcess on Windows rather than raising.
#
#    This is why the MCP page stayed broken after the UTF-8 fix below: every
#    `claude` and every `git` in this package died rc=-11 with EMPTY stderr, in
#    0.0s, so the module reported "failed to list MCP servers" and git_ops
#    reported `git add -A failed: ` with nothing after the colon. The same
#    commands ran fine from a shell, because a shell has no PROJ loaded.
#
# 2. text/encoding/errors — `text=True` alone decodes with
#    locale.getpreferredencoding(False), and a GUI-launched server inherits no
#    LANG/LC_ALL, so on macOS that resolves to ASCII. The moment a child prints
#    a non-ASCII byte — `claude mcp list` draws ✔/✘/⏸, a commit message has an
#    em dash, a statusline script emits a nerd-font glyph, a project path is
#    accented — the decode raises UnicodeDecodeError and the whole action 500s.
#    Pinning UTF-8 (what all of these actually emit) with errors="replace" makes
#    the worst case a mojibake character rather than a dead page.
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

# One lock serializes all config mutation (read-modify-write of settings.json +
# the git add/commit that follows), so two concurrent actions can't clobber each
# other. TWO mechanisms, because the port changed where the concurrency comes
# from: as an html+py app every action was its own subprocess, and an flock on a
# shared lock file was the only thing that could serialize them; as a server
# router they are threadpool workers inside ONE process, and there flock is not
# available at all on Windows — where the whole feature would otherwise run
# unserialized. The threading lock covers this process everywhere; the flock
# still keeps a SEPARATE process (a CLI run of these modules, a second desktop
# instance, the example app) from interleaving with us on POSIX. Always acquired
# in this order, so the pair can't deadlock.
_LOCK_PATH = os.path.join(CLAUDE_DIR, ".config-ui.lock")
_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def config_lock() -> Generator[None, None, None]:
    os.makedirs(CLAUDE_DIR, exist_ok=True)
    with _THREAD_LOCK, open(_LOCK_PATH, "w") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


# --- preferences.md §5: the settings catalog, packaged + overridable ---------
# The catalog used to be a checked-in file the refresh rewrote IN PLACE, next to
# the script. That is impossible now: the shipped copy lives in site-packages
# (or inside a signed .app bundle), which is read-only in every install that
# isn't an editable checkout — a refresh there would either fail with EACCES or,
# worse, succeed and silently disappear on the next upgrade.
#
# So the two directions are split. READS prefer a user-writable override and fall
# back to the packaged copy; WRITES only ever go to the override. Deleting the
# override is therefore a clean "reset to what shipped", and an upgrade that
# ships a better catalog is visible to everyone who never pressed Refresh.
CATALOG_FILENAME = "settings_catalog.json"


def packaged_catalog_path() -> str:
    """The catalog that shipped with this package. Read-only by policy."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CATALOG_FILENAME)


def catalog_override_path() -> str:
    """The user-writable catalog: <shell home>/claude-config/settings_catalog.json.
    Under home_dir() so FUSED_RENDER_HOME redirects it (tests never touch the
    real one) and so a branch ref nests it like every other shell resource."""
    return os.path.join(home_dir(), "claude-config", CATALOG_FILENAME)


def catalog_read_path() -> str:
    """Where to READ the catalog from: the override when it exists, else the
    packaged copy."""
    override = catalog_override_path()
    return override if os.path.isfile(override) else packaged_catalog_path()


# --- config-store.md §3-5: read / write / merge -----------------------------

def as_bool(v: Any) -> bool:
    """Coerce a param to bool. fused coerces `bool`-annotated params before
    calling main(), but a raw string "false" is truthy in Python — guard so the
    modules are correct whether the value arrives as bool or string."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "on")


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


def write_json(path: str, value: Any) -> None:
    """Atomic write (config-store.md §4): mkdir -p, write a sibling temp file,
    then os.replace over the target (atomic on the same filesystem). Pretty
    2-space + trailing newline keeps git diffs clean (version-control.md §3)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


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


def flatten(obj: Optional[dict], prefix: str = "") -> dict:
    """Flatten a settings object to dotted leaf paths -> value, for key-level
    diffs (version-control.md §6 settings delta)."""
    out = {}
    for k, v in (obj or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


# --- version-control.md §2: whitelist .gitignore (secret safety) ------------
# ignore-everything-then-opt-in. Verbatim port; the trailing projects/* lines
# are the surgical re-include that tracks ONLY projects/*/memory/** while
# leaving transcripts and session state ignored.
GITIGNORE = """/*
!.gitignore
!settings.json
!settings.local.json
!CLAUDE.md
!keybindings.json
!statusline-command.sh
!hooks/
!agents/
!skills/
!commands/
!projects/
projects/*
!projects/*/
projects/*/*
!projects/*/memory/
**/.DS_Store
"""


def git(*args: str, check: bool = True) -> str:
    """Run a git command against CLAUDE_DIR, returning stripped stdout.

    `git -C <dir>` rather than `cwd=`, and SUBPROCESS_KWARGS rather than the
    defaults: both halves of the discipline app_git.py's module docstring lays
    out, for the same reasons — a `cwd=` that no longer exists fails inside the
    spawn instead of inside git, and a forking spawn dies rc=-11 the moment
    PROJ is resident in this process."""
    res = subprocess.run(
        ["git", "-C", CLAUDE_DIR, *args],
        capture_output=True,
        **SUBPROCESS_KWARGS,
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def ensure_repo() -> None:
    """version-control.md §1. Idempotent; safe to call at the top of every git
    action (there is no persistent server to bootstrap once)."""
    os.makedirs(CLAUDE_DIR, exist_ok=True)
    if not os.path.isdir(os.path.join(CLAUDE_DIR, ".git")):
        git("init")
        # local identity so commits work without a global git config
        if not git("config", "user.email", check=False).strip():
            git("config", "user.email", "config-ui@local")
        if not git("config", "user.name", check=False).strip():
            git("config", "user.name", "Claude Config UI")
    gi_path = os.path.join(CLAUDE_DIR, ".gitignore")
    if read_text(gi_path) != GITIGNORE:
        write_text(gi_path, GITIGNORE)
    # seed commit if the repo has no HEAD yet
    if git("rev-parse", "--verify", "HEAD", check=False).strip() == "":
        git("add", "-A")
        if git("status", "--porcelain").strip():
            git("commit", "-m", "Initial snapshot of Claude config")


def read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def commit(message: str, pathspec: Optional[str] = None) -> Optional[str]:
    """version-control.md §3: add, no-op if nothing staged, else commit.
    Returns the new HEAD sha or None. `pathspec` narrows the commit to a
    subset (e.g. one memory folder) leaving other drift uncommitted."""
    ensure_repo()
    if pathspec:
        git("add", "-A", "--", pathspec)
    else:
        git("add", "-A")
    if not git("status", "--porcelain").strip():
        return None
    if pathspec:
        git("commit", "-m", message, "--", pathspec)
    else:
        git("commit", "-m", message)
    return git("rev-parse", "HEAD").strip()


def log(n: int = 50) -> list:
    """version-control.md §4: newest-first [{sha, date, message}]."""
    ensure_repo()
    out = git("log", f"-{n}", "--pretty=format:%H%x1f%cI%x1f%s", check=False)
    entries = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, date, message = line.split("\x1f", 2)
        entries.append({"sha": sha, "date": date, "message": message})
    return entries


def status() -> dict:
    """version-control.md §5: {dirty, files} from porcelain -uall."""
    ensure_repo()
    out = git("status", "--porcelain", "-uall")
    files = [line[3:] for line in out.splitlines() if line.strip()]
    return {"dirty": bool(files), "files": files}


def _settings_at_ref(ref: str) -> dict:
    out = git("show", f"{ref}:settings.json", check=False)
    try:
        return json.loads(out) if out.strip() else {}
    except ValueError:
        return {}


def _settings_delta(before: dict, after: dict) -> list:
    fa, fb = flatten(before), flatten(after)
    keys = sorted(set(fa) | set(fb))
    delta = []
    for k in keys:
        frm, to = fa.get(k), fb.get(k)
        if frm != to:
            delta.append({"key": k, "from": frm, "to": to})
    return delta


def diff(target: str) -> dict:
    """version-control.md §6: change preview HEAD -> target (restore/switch)."""
    ensure_repo()
    if git("rev-parse", "--verify", target, check=False).strip() == "":
        raise ValueError(f"unknown ref: {target}")
    files = []
    out = git("diff", "--name-status", "HEAD", target, check=False)
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        files.append({"status": parts[0][0], "path": parts[-1]})
    settings = _settings_delta(_settings_at_ref("HEAD"), _settings_at_ref(target))
    return {"files": files, "settings": settings}


def drift_diff() -> dict:
    """version-control.md §6: uncommitted drift, HEAD -> working tree. Uses the
    same porcelain source as the status badge so untracked whitelisted files
    (e.g. a new memory file) are included."""
    ensure_repo()
    files = []
    for line in git("status", "--porcelain").splitlines():
        if not line.strip():
            continue
        code, path = line[:2], line[3:]
        st = "A" if code.strip() == "??" else code.strip()[0]
        files.append({"status": st, "path": path})
    on_disk = read_json(SETTINGS_PATH, {})
    settings = _settings_delta(_settings_at_ref("HEAD"), on_disk)
    return {"files": files, "settings": settings}


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
    string. macOS `open`; falls back to xdg-open on Linux."""
    import sys
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
# `plugin:context-mode:context-mode`), so split status on the LAST " - " and the
# name/endpoint boundary on the FIRST ": " (colon-space, which bare-colon names lack).
_MCP_STATUS = {
    "✔ connected": "connected",
    "! needs authentication": "needs-auth",
    "✘ failed to connect": "failed",
    "⏸ pending approval": "pending",
}


def parse_mcp_list(text: str) -> list:
    """Parse `claude mcp list` stdout into server dicts (mcp.md §2)."""
    servers = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if " - " not in line or ": " not in line:
            continue  # banner ("Checking MCP server health…"), blanks
        body, _, status_part = line.rpartition(" - ")
        name, _, endpoint = body.partition(": ")
        name, endpoint = name.strip(), endpoint.strip()
        if not name:
            continue
        status = _MCP_STATUS.get(status_part.strip().lower(), "unknown")

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
            "kind": kind,
            "connected": status == "connected",
            "needsAuth": status == "needs-auth",
            "canAuth": can_auth,
            "removable": kind == "user",
        })
    return servers


def restore(sha: str) -> Optional[str]:
    """version-control.md §4: checkout whitelisted files at sha, then a forward
    commit (history is never rewritten)."""
    ensure_repo()
    if git("rev-parse", "--verify", sha, check=False).strip() == "":
        raise ValueError(f"unknown ref: {sha}")
    git("checkout", sha, "--", ".")
    return commit(f"Restore config to {sha[:8]}")


# --- profiles.md §1-5: git branches over CLAUDE_DIR -------------------------
# Profiles are branches; these are the branch primitives profiles.py composes.
# They live here (not in profiles.py) so the whole git layer has one owner.

def current_profile() -> str:
    """profiles.md §1: the checked-out branch name."""
    ensure_repo()
    return git("rev-parse", "--abbrev-ref", "HEAD").strip()


def branches() -> list:
    """profiles.md §2: all branches, marking current and default (main/master)."""
    ensure_repo()
    cur = current_profile()
    out = []
    for line in git("branch", "--format=%(refname:short)").splitlines():
        name = line.strip()
        if not name:
            continue
        out.append({
            "name": name,
            "current": name == cur,
            "isDefault": name in ("main", "master"),
        })
    return out


def create_branch(name: str, frm: Optional[str] = None) -> None:
    """profiles.md §3: create a branch from `frm` (default HEAD). Never touches
    the working tree. Raises RuntimeError if git rejects the ref name."""
    ensure_repo()
    git("branch", name, *( [frm] if frm else [] ))


def switch_branch(name: str) -> None:
    """profiles.md §4: check out `name`, rewriting tracked files in place."""
    ensure_repo()
    git("checkout", name)


def delete_branch(name: str) -> None:
    """profiles.md §5: safe delete (-d); git refuses unmerged branches and the
    'not fully merged' error surfaces via git()."""
    ensure_repo()
    git("branch", "-d", name)


def archive_zip(name: str) -> bytes:
    """profiles.md §6: the branch's tree as a .zip, returned as raw bytes.
    `git archive` emits binary, so this bypasses the text git() helper — and
    therefore SUBPROCESS_KWARGS, whose text/encoding half would corrupt the zip.
    The spawn half is NOT optional and is spelled out here instead: without
    close_fds=False this dies rc=-11 like everything else in the package (see
    SUBPROCESS_KWARGS). The tree is only the whitelisted, tracked files
    (version-control.md §2), so the archive never carries plugins/,
    ~/.claude.json, or secrets."""
    ensure_repo()
    res = subprocess.run(
        ["git", "-C", CLAUDE_DIR, "archive", "--format=zip", name],
        capture_output=True,
        close_fds=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git archive {name} failed: {res.stderr.decode(errors='replace').strip()}")
    return res.stdout


def _within_claude_dir(name: str) -> Optional[str]:
    """Confine a zip member `name` to CLAUDE_DIR (profiles.md §7 trust boundary).
    Multi-segment analog of safe_subdir: reject absolute members and any path
    whose realpath escapes CLAUDE_DIR (`../` traversal). Returns the absolute
    target, or None if the entry must be refused."""
    if name.startswith("/") or name.startswith("\\"):
        return None
    base_real = os.path.realpath(CLAUDE_DIR)
    target = os.path.realpath(os.path.join(base_real, name))
    if target != base_real and not target.startswith(base_real + os.sep):
        return None
    return target


def import_archive(zip_bytes: bytes, paths: list) -> list:
    """profiles.md §7: extract selected members of a zip into CLAUDE_DIR, each
    overwriting in place. A selected `path` matches a file (== path) or a folder
    (startswith path + '/'). Every member is confined to CLAUDE_DIR; traversal
    entries are refused, not written. Returns the sorted rel paths written."""
    import io
    import zipfile

    written = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.endswith("/"):
                continue  # directory marker; its files carry the content
            if not any(name == p or name.startswith(p.rstrip("/") + "/") for p in paths):
                continue
            target = _within_claude_dir(name)
            if target is None:
                continue  # traversal / absolute — refuse
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            written.append(name)
    return sorted(written)
