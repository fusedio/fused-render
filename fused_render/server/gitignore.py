import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict

if sys.platform != "win32":
    import select

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

    A stalled child (an `index.lock` another process is holding, `gc --auto`,
    a degraded filesystem) is the same shape of failure, just slower to show
    up: every read that comes back with bytes pushes a rolling deadline
    forward by `DEADLINE_S`, so `ignored()` gives up only when NOTHING has
    arrived for that long, marks the oracle broken, and returns the same
    "nothing ignored" rather than hanging the request thread forever. POSIX
    only (`select` on a pipe) — see `_read_chunk` for why Windows is left
    unbounded, same as before this existed.
    """

    # Queries per write/read cycle: bounded so git's stdout can't fill the
    # pipe while we are still writing stdin (classic co-process deadlock).
    CHUNK = 200

    # `read1`'s request size, pulled out as a name rather than a repeated
    # literal: `_read_chunk` compares a read's length against this to decide
    # whether more may already be sitting in the BufferedReader's own buffer
    # (see the note there) — a magic-number `65536` in two places would be a
    # trap for whoever changes one and not the other.
    _READ1_SIZE = 65536

    # How long `ignored()` may go with NO PROGRESS before it gives up on the
    # co-process.
    #
    # This is a PER-READ deadline that gets pushed forward by every read that
    # returns bytes, not a budget for the whole call: a big batch is
    # genuinely slow-but-fine (index_gitignore.py's own docstring puts a
    # home-sized sweep at ~1.5s, scaling past 4s on a 571k-entry corpus) and
    # must not be killed just for taking a while while it keeps making
    # progress — the earlier version of this bound was a flat per-call
    # timeout, and a big sweep tripping it would have silently turned OFF
    # gitignore filtering for the whole batch, which is exactly the failure
    # index_gitignore.py's header calls unacceptable (a gitignored `dist/` of
    # 100k files flooding search). A STALLED child, by contrast, produces
    # nothing at all on its very first read and trips this immediately.
    # Matches the one-shot calls' `timeout=5` elsewhere in this file for the
    # same git-is-hung shape of failure.
    DEADLINE_S = 5.0

    def __init__(self, repo_root):
        self.root = repo_root
        self.broken = False
        # Whether the last successful read might have left bytes sitting in
        # `self.proc.stdout`'s OWN internal buffer — see `_read_chunk`.
        # `False` here is correct at start-of-day: nothing has been read yet,
        # so there is nothing to assume is buffered.
        self._may_have_buffered = False
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
            chunk = self._read_chunk()
            if not chunk:
                raise OSError("check-ignore stream closed")
            self._buf += chunk

    def _read_chunk(self):
        """Up to `_READ1_SIZE` bytes from the co-process, bounded by
        `self._deadline` (a `time.monotonic()` timestamp, pushed forward by
        every read that returns bytes — see `DEADLINE_S`).

        `select()` polls the underlying FD, but `self.proc.stdout` is a
        `BufferedReader` (`Popen` is created with no `bufsize`, so it defaults
        to `io.DEFAULT_BUFFER_SIZE` — 128 KiB on the machine this was found
        on) with its OWN buffer, invisible to `select`. When that buffer is
        empty, filling it does ONE raw read sized to the WHOLE buffer, which
        can pull in far more than the `_READ1_SIZE` this method hands back —
        `check-ignore -v` echoes the queried path back in full, so one
        `CHUNK`-sized batch (200 queries) of ordinary path lengths can exceed
        64 KiB by itself. `select` would then see the FD genuinely empty
        (everything already moved into the BufferedReader's buffer) and block
        for the rest of the deadline on bytes that were already in hand.
        `read1(n)` returning exactly `n` is therefore treated as "there may be
        more waiting in that buffer" and the NEXT read skips `select`
        entirely; a short read means the buffer was actually drained, so the
        read after THAT one goes through `select` again. `peek()` was
        considered and rejected: on an empty buffer it performs a raw read
        and blocks, which reintroduces exactly the hang this exists to avoid.

        POSIX only: `select.select` is the portable way to put a timeout on a
        blocking pipe read, but it does not work on pipes on Windows at all
        (only sockets). This repo ships on Windows (`_spawn_kwargs`'s
        `CREATE_NO_WINDOW`), so on that platform this falls back to the old,
        unbounded `read1` — a stalled git still hangs the request thread there,
        same as before this existed. Fixing that would need a reader thread or
        overlapped I/O, which is a bigger change than this bug fix; POSIX is
        where the reported hang actually happened.
        """
        if sys.platform == "win32":
            return self.proc.stdout.read1(self._READ1_SIZE)
        if not self._may_have_buffered:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0 or not select.select(
                    [self.proc.stdout], [], [], remaining)[0]:
                raise TimeoutError(
                    f"check-ignore made no progress for {self.DEADLINE_S}s")
        chunk = self.proc.stdout.read1(self._READ1_SIZE)
        self._may_have_buffered = len(chunk) == self._READ1_SIZE
        if chunk:
            self._deadline = time.monotonic() + self.DEADLINE_S
        return chunk

    def ignored(self, rel_paths):
        """Subset of `rel_paths` (POSIX, relative to repo root) git ignores."""
        if self.broken or not rel_paths:
            return set()
        self._deadline = time.monotonic() + self.DEADLINE_S
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
            # TimeoutError is an OSError subclass, so a stalled stream lands
            # here exactly like a broken pipe: the whole batch this call was
            # answering is discarded (the buffer's field boundaries are no
            # longer trustworthy once a read is abandoned mid-stream) rather
            # than returned partial, and the oracle stops trying git at all.
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


# Memoizes `_repo_toplevel`'s answer, keyed on the exact path a caller passed
# (the same convention `index_gitignore._cache` uses — callers already hand in
# a canonical form, so a second canonicalization pass here would just be
# redundant `os.path` work on every call). Every rank request calls
# `_repo_toplevel` at least once, and TWICE when a query escalates to the
# subsequence pass (`index/query.py`'s `pass_over` re-runs the whole gitignore
# filter per pass) — and unmemoized, each of those is an uncached
# `git rev-parse` spawn, the one unamortized subprocess cost left on the
# keystroke path.
#
# Bounded like `index_gitignore._cache`, but larger: that cache is keyed on a
# handful of configured INDEX roots, while this one is keyed on whatever path
# a caller happens to ask about (a right-click target, a walk's start
# directory) — plausibly every folder a user opens in one session. 256 keeps
# that bounded without evicting the roots actually in active use; an eviction
# only costs one repeated `rev-parse`, never a wrong answer.
_TOPLEVEL_CACHE_SIZE = 256
_toplevel_cache: "OrderedDict[str, tuple[float, str | None]]" = OrderedDict()
_toplevel_lock = threading.Lock()

# How long a `_repo_toplevel` answer may be trusted before git is asked again.
#
# Both shapes of cached answer can go stale in a way nothing here observes: a
# `None` ("not a repo") goes stale the instant someone runs `git init`; a real
# toplevel goes stale if the repository is moved or removed out from under it.
# Time is the only thing that can bound either, which is the same posture
# `index_gitignore.VERDICT_MAX_AGE_S` takes for an identical shape of problem
# (an edited `.gitignore` no verdict pool can observe) — and the same value:
# a few minutes of a stale answer costs far less than paying for a `rev-parse`
# on every keystroke, and it is bounded rather than cached forever.
_TOPLEVEL_MAX_AGE_S = 300.0


def _reset_toplevel_cache() -> None:
    """Drop every memoized `_repo_toplevel` answer. For tests."""
    with _toplevel_lock:
        _toplevel_cache.clear()


def _repo_toplevel(path):
    """The git work-tree root containing `path`, or None. Memoized — see
    `_TOPLEVEL_CACHE_SIZE` / `_TOPLEVEL_MAX_AGE_S` above — because callers
    (the walk, `_is_repo_root`, the index's gitignore filter) ask about the
    same handful of paths over and over on a single browsing session or a
    single rank request escalated across both search passes."""
    with _toplevel_lock:
        cached = _toplevel_cache.get(path)
        if cached is not None and \
                (time.monotonic() - cached[0]) < _TOPLEVEL_MAX_AGE_S:
            _toplevel_cache.move_to_end(path)
            return cached[1]

    # The actual spawn happens OUTSIDE the lock — it can take up to the 5s
    # timeout under contention, and holding a module-global lock across that
    # would serialize every caller in the app behind whichever one is
    # currently blocked on git (the same reason `index_gitignore._pooled_verdicts`
    # never holds its lock across a git call).
    cacheable, top = _repo_toplevel_uncached(path)

    if cacheable:
        # Stamped with the time AFTER the spawn returns, not before it
        # started: `_repo_toplevel_uncached` can take up to its own 5s
        # timeout, and stamping on entry would insert the entry already
        # partway aged — under contention, by as much as the TTL's own
        # ceiling.
        with _toplevel_lock:
            _toplevel_cache[path] = (time.monotonic(), top)
            _toplevel_cache.move_to_end(path)
            while len(_toplevel_cache) > _TOPLEVEL_CACHE_SIZE:
                _toplevel_cache.popitem(last=False)
    return top


def _repo_toplevel_uncached(path) -> "tuple[bool, str | None]":
    """The uncached `rev-parse --show-toplevel`, one call per walk — covers
    walking a SUBDIRECTORY of a repo, where no `.git` marker is ever seen
    during the walk itself.

    Returns `(cacheable, answer)`. Only two shapes are durable facts about
    `path` and therefore safe to memoize: a successful toplevel, and the
    ORDINARY negative (exit 128, "not a git repository"). Everything else is a
    fact about git's current ability to answer, not about `path`, so it must
    never be cached:

    * `OSError` / `TimeoutExpired` — git could not be run RIGHT NOW (no
      binary, an fd/process shortage, a slow disk tripping the timeout).
      Caching that would let one blip freeze a wrong answer for the whole TTL.
    * an ABNORMAL refusal (dubious ownership, a bad config, a deleted cwd) —
      also transient in the sense that matters here: the environment can be
      fixed (a `safe.directory` entry added, the config repaired) without
      `path` itself changing at all, and a cached refusal would hide that fix
      until the TTL expired. So this branch is treated the same as a spawn
      failure, not as an answer about the path.
    """
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
        return False, None
    if proc.returncode != 0:
        # Exit 128 is BOTH "not a git repository" (ordinary, silent, and the
        # answer for most folders) and "detected dubious ownership" / "bad
        # config" / "cannot get current working directory" (abnormal, and the
        # reason every git-backed feature just went dark). The exit code cannot
        # tell them apart; git's words can.
        if not _is_ordinary_negative(proc.stderr):
            _warn_git_refused("rev-parse --show-toplevel", path,
                              proc.returncode, proc.stderr)
            return False, None
        return True, None
    top = os.fsdecode(proc.stdout.strip())
    return True, (top or None)


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
