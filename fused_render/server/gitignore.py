import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- how git is run
#
# THE bug behind "the Git side panel is disabled for every repository". With
# libproj resident in this process — and it becomes resident the moment any map /
# geotiff / zarr template or daemon imports rasterio or pyproj — a plain `fork()`
# runs PROJ's pthread_atfork child handler straight into a SIGSEGV, *before*
# exec. The child dies with signal 11, so `returncode == -11` with empty stdout
# and stderr and NO exception, because the spawn itself worked. Every call site
# here then fails closed on a git that never ran, for every repository at once,
# and it looks exactly like "not a repository".
#
# `close_fds=False` is the well-known half of the fix and it is NOT sufficient.
# CPython reaches posix_spawn only when every clause of this holds
# (`subprocess.py::_execute_child`):
#
#     _USE_POSIX_SPAWN and os.path.dirname(executable) and preexec_fn is None
#     and not close_fds and not pass_fds and cwd is None and ... and umask < 0
#
# Two were being violated across the whole codebase: argv[0] was the BARE NAME
# "git" (dirname "" — falsy), and some callers passed `cwd=`. Either one alone
# forces the fork path however carefully close_fds is set. So all three parts are
# required together, and `tests/test_git_posix_spawn.py` pins them as behaviour.
_GIT_BIN: str | None = None


def git_bin() -> str:
    """An ABSOLUTE path to git, so CPython can posix_spawn it.

    Resolved once and cached: `shutil.which` walks PATH, and these calls run on
    every directory the user opens. Falling back to the bare name keeps a
    PATH-less environment behaving as it did before (a fork, and a
    FileNotFoundError we already report) rather than raising from here.
    """
    global _GIT_BIN
    if _GIT_BIN is None:
        import shutil
        _GIT_BIN = shutil.which("git") or "git"
    return _GIT_BIN


def _spawn_kwargs() -> dict:
    """The kwargs every git spawn here shares. See the note above.

    `cwd` is deliberately absent, not None-by-omission: adding one silently
    reintroduces the fork. Every caller passes `-C <path>` to git instead, which
    is stricter anyway — it cannot be changed by this process's cwd.
    """
    return {
        "close_fds": False,
        "creationflags": (subprocess.CREATE_NO_WINDOW
                          if sys.platform == "win32" else 0),
    }


# Every git call site here fails CLOSED, which is right — but "git said no" and
# "git could not be run at all" are very different facts and used to look
# identical from the outside AND leave nothing in the log. A spawn failure (no
# binary on PATH, EMFILE/EAGAIN under fd or process pressure) or a timeout
# disables every git-backed feature in the app at once, for every repository:
# /api/fs/conditions reports "git": false with no error key, /api/fs/git-repo
# reports is_repo_root false for a real root, and /api/fs/list stops dimming
# .git — all of it indistinguishable from "not a repository". So the
# CANNOT-RUN case gets a WARNING while the ordinary negative stays silent.
#
# There is a SECOND, harder half, and it is the one that let a real
# investigation reach the wrong conclusion. A git that is spawned fine and
# answers in the NEGATIVE is also indistinguishable from "not a repository", and
# every call site here used to throw git's stderr away, so there was nothing to
# read. The absence of a "could not be run" warning was then taken as proof that
# git was healthy — when in fact git had run and refused. Two negative shapes
# therefore have to be visible, because they have different causes:
#
#   * a NON-ZERO exit whose stderr is not the ordinary "not a git repository":
#     `detected dubious ownership`, a bad config, an unreadable object store, a
#     working directory deleted under the process.
#   * exit ZERO with a negative ANSWER, which is what a polluted `GIT_DIR` /
#     `GIT_WORK_TREE` / `GIT_CEILING_DIRECTORIES` in this process produces.
#     There is no stderr at all in that case, so stderr capture alone cannot
#     explain it and the inherited git environment has to be in the report.
#
# What stays silent is the ORDINARY negative — a folder that genuinely is not a
# repository, which is most folders a user opens.
#
# Throttled, because these run on every directory the user opens: a broken git
# would otherwise write one line per stat and bury the signal it is meant to be.
# One throttle PER KIND, so a storm of one cannot hide the first of the other.
_SPAWN_WARN_INTERVAL_S = 60.0
_warn_lock = threading.Lock()
_warned_at: dict[str, float] = {}

# git's own words for "this is not a repository", the one negative that is
# ordinary. Matched as a substring, case-folded, against stderr — the exact
# sentence carries the searched path and varies by subcommand.
_ORDINARY_NEGATIVES = (
    "not a git repository",
    "does not have a commit checked out",   # a fresh clone with no HEAD
)

# The environment that can make a healthy git answer about the WRONG repository,
# plus the two that decide which config it reads. Reported on a contradiction
# because a stray value here is invisible from the outside and instantly
# explains one: `git -C <repo> rev-parse` honours `GIT_DIR` over `-C`.
_GIT_ENV_KEYS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
    "HOME", "XDG_CONFIG_HOME",
)


def _reset_spawn_failure_throttle() -> None:
    """Forget every last-warned timestamp. For tests."""
    with _warn_lock:
        _warned_at.clear()


def _throttled(kind: str) -> bool:
    """True when `kind` has been warned about too recently to warn again."""
    now = time.monotonic()
    with _warn_lock:
        last = _warned_at.get(kind)
        if last is not None and now - last < _SPAWN_WARN_INTERVAL_S:
            return True
        _warned_at[kind] = now
    return False


def _git_context() -> str:
    """The process facts that explain a git answering about the wrong thing.

    Only variables that are SET are named — a list of eleven `None`s buries the
    one that matters. The working directory is included because a git whose
    inherited cwd has been deleted fails even with `-C` (it calls `getcwd()`
    before anything else).
    """
    parts = [f"{k}={os.environ[k]!r}" for k in _GIT_ENV_KEYS if os.environ.get(k)]
    try:
        parts.append(f"cwd={os.getcwd()!r}")
    except OSError as exc:      # the cwd has been removed under this process
        parts.append(f"cwd=UNAVAILABLE ({exc})")
    return ", ".join(parts) or "no git-related environment set"


def _is_ordinary_negative(stderr: bytes) -> bool:
    text = stderr.decode("utf-8", "replace").lower()
    return any(phrase in text for phrase in _ORDINARY_NEGATIVES)


def _warn_git_unusable(what: str, exc: BaseException) -> None:
    """Record that a git subprocess could not be RUN (not that git said no).

    Never raises and never changes a caller's answer: the callers all fail
    closed either side of this call.
    """
    if _throttled("unusable"):
        return
    logger.warning(
        "git could not be run (%s): %s: %s — every git-backed feature "
        "(the git side panel, repo-root detection, .gitignore dimming) is "
        "failing closed until this clears. Check that `git` is on the server "
        "process's PATH and that the process is not out of file descriptors or "
        "child-process slots.",
        what, type(exc).__name__, exc,
    )


def _warn_git_refused(what: str, path: str, rc: int, stderr: bytes) -> None:
    """Record that git RAN and failed for a reason that is not "not a repo".

    The ordinary negative is filtered out by the CALLER (it has the context to
    know what ordinary means for its own subcommand); anything reaching here is
    a refusal worth a human's attention.
    """
    if _throttled("refused"):
        return
    logger.warning(
        "git refused (%s) for %s: exit %s: %s [%s] — git ran fine, so this is "
        "not a spawn problem; the git-backed features are failing closed on "
        "git's own answer.",
        what, path, rc,
        stderr.decode("utf-8", "replace").strip() or "(no stderr)",
        _git_context(),
    )


def _warn_git_contradicted(what: str, path: str, answer: str) -> None:
    """Record that git answered NEGATIVELY about a path that has a `.git`.

    Exit zero, nothing on stderr, and an answer that disagrees with the
    filesystem — the shape a stray `GIT_DIR` in this process produces, and the
    one shape stderr capture cannot explain. So the report is the environment.
    """
    if _throttled("contradicted"):
        return
    logger.warning(
        "git contradicted the filesystem (%s): %s has a .git entry but git "
        "answered %s — the git-backed features are failing closed on that. "
        "This process's git environment is: %s",
        what, path, answer, _git_context(),
    )


def _has_dot_git(path: str) -> bool:
    """Whether a `.git` entry sits directly in `path` — one stat, no walk.

    Only ever consulted on a NEGATIVE answer, and only to decide whether that
    negative deserves a log line, so it never influences a verdict. `exists`
    rather than `isdir` because a linked worktree and a submodule carry a `.git`
    FILE, and both are cases where git answering "no" would be just as wrong.
    """
    try:
        return os.path.exists(os.path.join(path, ".git"))
    except OSError:
        return False




# Lazily-created empty git dir backing check-ignore for NON-repo directories
# that still carry a .gitignore (an un-inited project, an Obsidian vault…).
# With GIT_DIR pointing here and GIT_WORK_TREE at the directory, git applies
# that tree's .gitignore files exactly as it would inside a real repo. One
# per process, a few KB, left for the OS tempdir cleanup.
_EMPTY_GIT_DIR: str | None | bool = None  # None = not tried, False = failed


def _empty_git_dir():
    global _EMPTY_GIT_DIR
    if _EMPTY_GIT_DIR is None:
        try:
            root = tempfile.mkdtemp(prefix="fused-render-emptygit-")
            subprocess.run(
                [git_bin(), "init", "-q", root],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                **_spawn_kwargs(),
            )
            _EMPTY_GIT_DIR = os.path.join(root, ".git")
        except (OSError, subprocess.SubprocessError):
            _EMPTY_GIT_DIR = False
    return _EMPTY_GIT_DIR or None


class _IgnoreOracle:
    """One repository's `git check-ignore` as a streaming co-process.

    `git check-ignore --stdin -z -v -n` answers path queries incrementally on
    one long-lived subprocess (measured ~14µs/query), so the walk can ask
    about every directory's children as it reaches them — no subprocess per
    directory, no giant upfront batch. `-v -n` makes git echo all four
    NUL-terminated fields for EVERY query (matching or not), which is what
    makes the stream pairable: query order in = verdict order out.

    Any failure (git missing, repo gone mid-walk, pipe breakage) marks the
    oracle broken and it answers "nothing ignored" from then on — gitignore
    pruning is an optimization, never a hard dependency (same posture as
    _git_ignored's dimming).
    """

    # Queries per write/read cycle: bounded so git's stdout can't fill the
    # pipe while we are still writing stdin (classic co-process deadlock).
    CHUNK = 200

    def __init__(self, repo_root):
        self.root = repo_root
        self.broken = False
        # Real repo (a .git exists at or above the root): plain `git -C`.
        # Standalone-.gitignore directory (no repo): graft the dir onto a
        # shared empty GIT_DIR as its work tree, which makes check-ignore
        # honor the tree's .gitignore files without a repository.
        env = None
        if not os.path.exists(os.path.join(repo_root, ".git")):
            empty = _empty_git_dir()
            if empty is None:
                self.proc = None
                self.broken = True
                self._buf = b""
                return
            env = {**os.environ, "GIT_DIR": empty, "GIT_WORK_TREE": repo_root}
        try:
            self.proc = subprocess.Popen(
                [git_bin(), "-C", repo_root,
                 "check-ignore", "--stdin", "-z", "-v", "-n"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # DEVNULL, unlike the one-shot calls: this co-process outlives
                # the call, and an unread stderr PIPE on a long-lived child can
                # fill and deadlock it. Its failures surface through `broken`.
                stderr=subprocess.DEVNULL,
                env=env,
                **_spawn_kwargs(),
            )
        except OSError as e:
            _warn_git_unusable("check-ignore co-process", e)
            self.proc = None
            self.broken = True
        self._buf = b""

    def _read_field(self):
        while True:
            cut = self._buf.find(b"\0")
            if cut != -1:
                field = self._buf[:cut]
                self._buf = self._buf[cut + 1:]
                return field
            chunk = self.proc.stdout.read1(65536)
            if not chunk:
                raise OSError("check-ignore stream closed")
            self._buf += chunk

    def ignored(self, rel_paths):
        """Subset of `rel_paths` (POSIX, relative to repo root) git ignores."""
        if self.broken or not rel_paths:
            return set()
        out = set()
        try:
            for i in range(0, len(rel_paths), self.CHUNK):
                chunk = rel_paths[i : i + self.CHUNK]
                payload = b"".join(os.fsencode(r) + b"\0" for r in chunk)
                self.proc.stdin.write(payload)
                self.proc.stdin.flush()
                for r in chunk:
                    # <source> NUL <linenum> NUL <pattern> NUL <path> NUL.
                    # Empty source = no pattern matched (not ignored). A
                    # NEGATED pattern ("!keep.log") is also echoed with its
                    # source under -v — that match means explicitly NOT
                    # ignored, so test the pattern's sign, not mere presence.
                    source = self._read_field()
                    self._read_field()
                    pattern = self._read_field()
                    self._read_field()
                    if source and not pattern.startswith(b"!"):
                        out.add(r)
            return out
        except OSError:
            self.broken = True
            self.close()
            return set()

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            self.proc.terminate()
            self.proc = None


def _repo_toplevel(path):
    """The git work-tree root containing `path`, or None. One call per walk —
    covers walking a SUBDIRECTORY of a repo, where no `.git` marker is ever
    seen during the walk itself."""
    try:
        proc = subprocess.run(
            [git_bin(), "-C", path, "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # CAPTURED, not discarded: this is the only place git says WHY it
            # refused, and without it a refusal is indistinguishable from "not a
            # repository". `rev-parse` writes one short line, so there is no
            # pipe-filling risk (unlike the long-lived oracle below).
            stderr=subprocess.PIPE,
            timeout=5,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _warn_git_unusable("rev-parse --show-toplevel", e)
        return None
    if proc.returncode != 0:
        # Exit 128 is BOTH "not a git repository" (ordinary, silent, and the
        # answer for most folders) and "detected dubious ownership" / "bad
        # config" / "cannot get current working directory" (abnormal, and the
        # reason every git-backed feature just went dark). The exit code cannot
        # tell them apart; git's words can.
        if not _is_ordinary_negative(proc.stderr):
            _warn_git_refused("rev-parse --show-toplevel", path,
                              proc.returncode, proc.stderr)
        return None
    top = os.fsdecode(proc.stdout.strip())
    return top or None


def _canonical(path: str) -> str:
    """A path in the one form two spellings of the same location share:
    symlinks resolved (macOS's /var -> /private/var, a symlinked checkout) and
    case folded where the platform folds it (HFS+/APFS, NTFS)."""
    return os.path.normcase(os.path.realpath(path))


def _is_repo_root(path: str) -> bool:
    """True iff `path` is the WORK-TREE ROOT of a git repository.

    Deliberately stricter than `rev-parse --is-inside-work-tree`: the Compress
    menu's git formats (`git bundle --all`, `git archive HEAD`) archive the
    whole repository, so offering them on a SUBDIRECTORY would silently hand
    back far more than the folder that was right-clicked. So git is still the
    authority — it is the only thing that knows about `.git` files, linked
    worktrees, `$GIT_DIR`, and ceiling directories — but its answer only counts
    when the toplevel IS this folder.

    Fails closed (False) on a missing git, a timeout, a bare repo, a path
    inside `.git`, or anything that isn't a directory: the menu simply drops
    the git entries, which is the safe direction."""
    if not path or not os.path.isdir(path):
        return False
    top = _repo_toplevel(path)
    if top is None:
        return False
    if _canonical(top) == _canonical(path):
        return True
    # A different toplevel is the ORDINARY answer for a subdirectory of a repo —
    # that is the whole reason this function is stricter than
    # `--is-inside-work-tree`. It is NOT ordinary for a directory that has a
    # `.git` of its own: git exited zero and pointed elsewhere, which is what a
    # stray GIT_DIR in this process does, and it leaves no stderr to read. One
    # stat, only on the negative, and it never changes the verdict.
    if _has_dot_git(path):
        _warn_git_contradicted("rev-parse --show-toplevel", path,
                               f"toplevel {top!r}")
    return False


def _git_ignored(cwd: str, rel_names: list[str]) -> set[str]:
    """Return the subset of `rel_names` git would ignore, relative to `cwd`.

    Shells out to `git check-ignore` — the authority on gitignore semantics
    (nested .gitignore, .git/info/exclude, the global excludesfile, negation).
    One batched call covers a whole listing. Returns an empty set when `cwd`
    is not in a work tree, git is missing, or anything else goes wrong:
    dimming is a display hint, never a hard dependency on git.

    The `.git` directory (or the gitfile of a worktree/submodule) is folded in
    too: git never reports it via check-ignore, but it is repository plumbing
    the user rarely wants to browse, so we dim it exactly when we know git is
    present and `cwd` is a work tree — i.e. only after a successful call.
    """
    if not rel_names:
        return set()
    try:
        # --stdin -z: NUL-separated in and out, so names with newlines or
        # non-UTF-8 bytes round-trip intact. check-ignore exits 0 when some
        # path is ignored, 1 when none are (not an error), 128 on real
        # failure incl. "not a git repository".
        payload = b"".join(os.fsencode(n) + b"\0" for n in rel_names)
        proc = subprocess.run(
            [git_bin(), "-C", cwd, "check-ignore", "--stdin", "-z"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,       # captured for the same reason as above
            timeout=5,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        _warn_git_unusable("check-ignore", e)
        return set()
    if proc.returncode not in (0, 1):
        if not _is_ordinary_negative(proc.stderr):
            _warn_git_refused("check-ignore", cwd, proc.returncode, proc.stderr)
        return set()
    ignored = {os.fsdecode(chunk) for chunk in proc.stdout.split(b"\0") if chunk}
    # Return code 0/1 (not 128) proves this is a work tree with git available,
    # so dim `.git` itself. Match the basename so both the top-level entry
    # (".git", from /api/fs/list) and a nested one ("sub/.git", from walk) go.
    ignored.update(n for n in rel_names if n == ".git" or n.endswith("/.git"))
    return ignored
