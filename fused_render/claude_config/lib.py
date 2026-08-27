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
import itertools
import json
import os
import shutil
import subprocess
import threading
from typing import Any, Generator, Optional

from fused_render.shell.storage import home_dir


# An ABSOLUTE git path is required to reach posix_spawn, not merely tidy: CPython
# forks unless `os.path.dirname(executable)` is truthy, and a fork in a process
# with libproj resident dies with SIGSEGV before exec (rc -11, no output, no
# exception). `close_fds=False` alone does NOT achieve this — see
# fused_render/server/gitignore.py and tests/test_git_posix_spawn.py.
_GIT_BIN = None


def _git_bin():
    global _GIT_BIN
    if _GIT_BIN is None:
        import shutil
        _GIT_BIN = shutil.which("git") or "git"
    return _GIT_BIN


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

# A SEPARATE lock, only for ensure_repo()'s first-run `git init` — deliberately
# NOT config_lock()/_THREAD_LOCK. commit() calls ensure_repo(), and every
# config_lock()-holding caller that mutates settings (preferences patch,
# memory writes, profile branch ops, …) already holds _THREAD_LOCK when it
# calls commit(). _THREAD_LOCK is a plain (non-reentrant) threading.Lock, so
# routing the init check through config_lock() would self-deadlock on the very
# first edit: config_lock() acquires _THREAD_LOCK, calls commit(), which calls
# ensure_repo(), which would try to acquire _THREAD_LOCK again on the SAME
# thread and block forever (see test_config_lock_held_commit_does_not_deadlock).
# A dedicated lock sidesteps that: it is never held by config_lock(), so
# ensure_repo() can always take it, whether or not the caller already holds
# config_lock().
_INIT_LOCK = threading.Lock()


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
    not just this process. A bare f"{path}.tmp-{os.getpid()}" was a second,
    smaller instance of the same TOCTOU family as ensure_repo()'s `git init`
    race: two threadpool workers in the same server process share a pid, so
    they'd race the SAME tmp file (one thread's open()/write() clobbering the
    other's, one thread's os.replace() removing it out from under the other) —
    surfaced by ensure_repo() unconditionally rewriting .gitignore on every
    call, concurrently, from preferences.get() and gitOps.status() on first
    paint. threading.get_ident() (unique among live threads in this process)
    plus a counter (unique across nested/successive calls a single thread
    makes with the same pid+tid, however unlikely) closes it."""
    return f"{path}.tmp-{os.getpid()}-{threading.get_ident()}-{next(_tmp_counter)}"


_tmp_counter = itertools.count()


@contextlib.contextmanager
def _atomic_write(path: str) -> Generator[str, None, None]:
    """mkdir -p, yield a fresh sibling temp path to write into, then
    os.replace it over `path` (atomic on the same filesystem) on a clean
    exit. Uniqueness per call (`_tmp_path`) closed the cross-thread name
    collision; this closes the other half — an ORPHAN. Before this, a
    repeating failure between `open()` and `os.replace()` (ENOSPC, a
    `json.dump` error on a value that turns out not to be serialisable, the
    process killed mid-write) left the per-call tmp file behind forever,
    where the old pid-only name would at least have been reused by the next
    attempt. `CLAUDE_DIR`'s own `.gitignore` (`/*`, ignore-everything) hides
    the ones written there, but `catalog_override_path()` writes under
    `~/.fused-render`, which has no such rule and nothing that ever cleans
    orphaned tmp files up.

    `os.replace` itself is INSIDE the try, not after it — a version that
    only guarded the write and let `os.replace` run unguarded afterward
    would leave exactly the orphan class this docstring claims to close: on
    Windows, `os.replace` over a target another process still holds open (an
    editor, an AV scanner, a search indexer) raises `PermissionError`
    rather than replacing, and a cross-device `path` raises too — both
    leave `tmp` fully written and orphaned if the exception isn't caught
    here."""
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
    2-space + trailing newline keeps git diffs clean (version-control.md §3)."""
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
        [_git_bin(), "-C", CLAUDE_DIR, *args],
        capture_output=True,
        **SUBPROCESS_KWARGS,
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def ensure_repo() -> None:
    """version-control.md §1. Idempotent; safe to call at the top of every git
    action (there is no persistent server to bootstrap once).

    The init step (this function's own docstring used to just say "idempotent"
    and leave it there) was a bare check-then-act: `os.path.isdir(.git)` then
    `git init`, with no lock. server/routers/claude_config.py dispatches every
    module through `run_in_threadpool` — genuine concurrency inside one
    process — and on the Claude config page's first paint, PreferencesSection's
    preferences.get() and useGitStatus's gitOps.status() both land here
    UNGUARDED by config_lock() (neither action mutates anything, so neither
    takes the lock). Two threads racing `git init` on the same fresh
    CLAUDE_DIR: the loser dies mid-template-copy with "File exists" — git's
    copy_file() lstats each destination and skips existing entries, then opens
    with O_CREAT|O_EXCL, so EEXIST can only happen in that window. Windows just
    has a far wider window because process spawn is much slower there.

    Fixed with _INIT_LOCK (see its own comment for why it is a lock separate
    from config_lock()'s _THREAD_LOCK — routing this through config_lock()
    would deadlock the moment an edit's config_lock()-held commit() call
    re-enters ensure_repo()). The WHOLE bootstrap — `git init`, identity, the
    .gitignore write, the seed commit — has to live inside that one lock, not
    just the `git init` call: the seed commit is its own TOCTOU (several
    threads all read an unborn HEAD before any of them commits, then all run
    `git add -A` + `git commit` and collide on git's own `index.lock`), and
    proving that needed nothing more exotic than the SAME concurrent-first-run
    test that caught the `git init` race — it just kept failing one step later
    until this whole block was under the lock.

    Double-checked with a cheap upfront read so the fast path — true for the
    entire life of the repo after the first run — never touches the lock;
    read-only actions (status, log, diff) aren't serialized behind each other
    for the process's whole life over this. The fast path checks THREE things,
    not two: `.git` isdir, the .gitignore content already matching, AND a born
    HEAD (`git rev-parse --verify HEAD`). Omitting the HEAD check was an
    earlier version of this fix and it was wrong — it let the seed commit
    stay permanently un-retried: if that commit ever failed for a real
    reason, the first two conditions would already be true forever after, so
    the fast path would return early on every subsequent call and nothing
    would heal or report the missing HEAD. A rev-parse against the local
    on-disk repo is a stat-class git operation (no network, no working-tree
    walk), so paying it on every call is worth the guarantee.

    That still only serializes threads INSIDE this process. config_lock() also
    takes an flock, which is a no-op on Windows (fcntl is None — see the import
    guard above), so on Windows a SECOND process (another desktop instance, a
    CLI invocation of these modules) racing this bootstrap on the same fresh
    CLAUDE_DIR is not covered by any lock this module holds. Belt-and-braces
    for that gap: `git init` and the seed `git commit` treat their own
    failure as benign ONLY if the post-condition they exist to establish
    already holds afterward — a valid git dir, HEAD being born — the signature
    of "someone else already won this race" rather than a real failure.

    That post-condition for `git init` is deliberately NOT `os.path.isdir(.git)`
    — checking that would be wrong, and the ORIGINAL bug report is the proof:
    the user's error was `cannot copy … to
    C:/Users/amynr/.claude/.git/hooks/pre-applypatch.sample: File exists`,
    i.e. `.git/hooks/` already existed while `git init` was genuinely still
    failing. `git init` creates the `.git` directory FIRST, then writes
    `HEAD`/`config`, then copies hook templates — so `.git` existing is not
    proof init finished; a real failure partway through (`ENOSPC`, `EPERM`)
    leaves it behind too, and checking isdir alone would swallow that error
    and fall into `git config user.email` against a half-built repo.

    It is ALSO deliberately not `git rev-parse --git-dir` — an earlier
    version of this fix used exactly that, and a second review round caught
    why it is wrong: `--git-dir` performs git's normal repository DISCOVERY,
    which walks UP the filesystem looking for a `.git` directory in any
    ancestor. Plenty of people keep `~/.claude` (or a `CLAUDE_DIR` override)
    inside a versioned dotfiles or home repo — from a `CLAUDE_DIR` with no
    valid `.git` of its own but a repo somewhere above it, `--git-dir` exits
    0 and prints the ANCESTOR's git dir, so a genuinely failed init there
    would have looked like a won race and every git() call after this —
    identity, the seed `git add -A` + `git commit`, and every later
    commit()/status()/log()/diff() — would have run against that ancestor
    repo instead: rewriting its config and committing the user's whole
    dotfiles tree. The property this needs is "THIS EXACT DIRECTORY is a
    valid repo", never "some repo is reachable by walking up from here". `git
    rev-parse --resolve-git-dir <path>` is the command that asks exactly
    that question — it validates the literal path given (no discovery, no
    walking) and fails (128) on anything short of a genuinely complete repo:
    an absent directory, an empty one, or one missing `HEAD`/`config`. That
    still passes for a genuinely won race (the other side's init is
    functionally done even if it hasn't finished copying every hook
    template, since templates come last) while correctly failing on a
    half-built directory OR an ancestor's repo bleeding through.

    An earlier version of this fix ran the seed commit with `check=False`,
    reasoning that a losing thread's collision "just leaves HEAD as whichever
    thread committed first." True for the race, but `check=False` also
    swallows a REAL failure (a bad local identity, a failing
    `commit-msg`/`pre-commit` hook someone dropped into
    `~/.claude/.git/hooks`, a full disk, a stale `index.lock` left by a
    crashed process) with no exception and no report — the config page would
    then show no history, forever, with nothing to say why. Re-checking HEAD
    before swallowing the error is what tells the two apart, symmetric with
    `git init`'s own re-check.

    NOT given the same treatment: the identity `git config` writes, `git add
    -A`, and `git status --porcelain` inside this same block still run with
    `check=True` unguarded. All three CAN fail on a second process's
    `index.lock`/`config.lock` in that same cross-process window, and that
    failure would surface as a plain `RuntimeError` out of `status()`/`log()`
    — narrower than the bug this function exists to fix (it needs two
    concurrent PROCESSES racing a still-empty CLAUDE_DIR, not merely two
    threads in one), but not covered by the reasoning above. Left as a known
    gap rather than papered over with speculative tolerance for calls that
    have no cheap, honest post-condition to re-check (there is no equivalent
    of "is HEAD born" for "did `git add -A` succeed") — worth hardening if it
    is ever observed for real, not worth guessing at here.
    """
    os.makedirs(CLAUDE_DIR, exist_ok=True)
    git_dir = os.path.join(CLAUDE_DIR, ".git")
    gi_path = os.path.join(CLAUDE_DIR, ".gitignore")
    if (
        os.path.isdir(git_dir)
        and read_text(gi_path) == GITIGNORE
        and git("rev-parse", "--verify", "HEAD", check=False).strip() != ""
    ):
        return
    with _INIT_LOCK:
        if not os.path.isdir(git_dir):
            try:
                git("init")
            except RuntimeError:
                # `.git` existing is NOT proof `git init` succeeded (see the
                # docstring), and `git rev-parse --git-dir` is NOT a safe
                # substitute — it walks UP to an ancestor repo, so a failed
                # init inside a versioned dotfiles/home repo would read as
                # "already done" and every git() call below would run
                # against the WRONG repository. `--resolve-git-dir` checks
                # the exact given path, no discovery.
                if git("rev-parse", "--resolve-git-dir", ".git", check=False).strip() == "":
                    raise  # a real failure, not a lost race
            # local identity so commits work without a global git config
            if not git("config", "user.email", check=False).strip():
                git("config", "user.email", "config-ui@local")
            if not git("config", "user.name", check=False).strip():
                git("config", "user.name", "Claude Config UI")
        if read_text(gi_path) != GITIGNORE:
            write_text(gi_path, GITIGNORE)
        # seed commit if the repo has no HEAD yet
        if git("rev-parse", "--verify", "HEAD", check=False).strip() == "":
            git("add", "-A")
            if git("status", "--porcelain").strip():
                try:
                    git("commit", "-m", "Initial snapshot of Claude config")
                except RuntimeError:
                    if git("rev-parse", "--verify", "HEAD", check=False).strip() == "":
                        raise  # a real failure, not a lost race


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
        [_git_bin(), "-C", CLAUDE_DIR, "archive", "--format=zip", name],
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
