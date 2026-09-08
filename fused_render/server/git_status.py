"""Per-entry git status for one directory listing — what tints a row's NAME.

The explorer already dims what `.gitignore` excludes (server/gitignore.py).
This is the other half of the same idea, and the one VS Code's file tree is
known for: a name that is a different colour because git has something to say
about that path. Four states are distinguished, and they are the four a person
acts on differently:

  untracked   git has never seen this path
  modified    tracked, and the WORK TREE differs from the index
  staged      tracked, and the INDEX differs from HEAD with a clean work tree
  conflicted  an unresolved merge

`deleted` is deliberately not among them: a deleted file has no row to tint.
It still reaches the UI, folded into `modified`, because it is a change inside
whatever FOLDER used to hold it, and that folder does have a row.

WHY ONE `git status` AND NOT `git diff` PER ROW
A listing can hold thousands of entries, and the per-path question ("what is
this one's state?") has no cheap per-path answer — every shape of it ends in a
diff against the index. `git status` computes all of them in one pass over the
one index git already has cached, so the cost is one spawn per listing however
many rows it has. It is scoped with a `.` pathspec so a subdirectory of a huge
monorepo does not pay for its siblings.

Untracked files are asked for at git's DEFAULT depth (`normal`), never
`--untracked-files=all` — and passed EXPLICITLY as `--untracked-files=normal`,
never left to fall through to whatever `status.showUntrackedFiles` says in the
user's config (`=no` renders every untracked file invisible; `=all` is the
100k-file `.venv` enumeration this paragraph exists to avoid). `normal`
collapses a wholly untracked directory into one `dir/` record, which is
exactly the row this listing wants to tint. A tracked directory holding one
new file still reports `dir/newfile`, so the roll-up below still sees it.
`templates/git/log.py`'s own porcelain parser pins the same two flags
(`--untracked-files=normal --ignored=no`) for the identical reason.

FOLDERS ROLL UP
A folder is tinted with the most urgent state anywhere beneath it — the whole
point being that a collapsed folder must not hide a change. `STATUS_ORDER` is
that ranking, and it is a judgement rather than git's own ordering: a conflict
has to be resolved before anything else can happen, tracked work in flight is
next, then paths git does not know about yet, and a staged-and-clean path is
last because it is the one already recorded.

BEST-EFFORT, LIKE EVERY OTHER GIT READ HERE
git may be missing, the folder may not be a repo, the index may be locked by a
concurrent commit. A tint is a display hint, so every failure returns `{}` and
the listing renders undecorated — never an error. Same discipline, and the
same spawn rules (absolute git, `-C`, `close_fds=False`), as gitignore.py;
`git_bin` and the warning helpers are reused from there because they carry no
state of their own. `_spawn_kwargs` is NOT imported the same way: it is spread
into the call with `**`, and `tests/test_git_posix_spawn.py`'s static sweep
resolves a `**helper()` spread by reading the helper's dict literal out of THIS
file's own AST — it does not follow the call into another module. So this file
keeps its own copy of the dict literal, one field at a time identical to
gitignore's, rather than importing the function and leaving the spread
unverifiable.
"""
import logging
import os
import subprocess
import sys
import threading
import time
from collections import OrderedDict

from fused_render.server.gitignore import (
    _is_ordinary_negative,
    _repo_toplevel,
    _warn_git_refused,
    _warn_git_unusable,
    git_bin,
)

logger = logging.getLogger(__name__)

# The states a row can carry, MOST URGENT FIRST. The order is the folder
# roll-up's ranking (see the header) and nothing else reads it as a sequence.
STATUS_ORDER = ("conflicted", "modified", "untracked", "staged")
_RANK = {name: i for i, name in enumerate(STATUS_ORDER)}

# Whether this platform's paths fold case. Decided once, here, because the two
# spellings being compared come from different authorities: the listing's names
# come from `scandir` (the filesystem's spelling) and git's paths come from its
# index (the spelling that was committed). On APFS/HFS+/NTFS those can differ
# in case for the same path, and a prefix test that missed would silently drop
# every decoration in a subdirectory rather than fail loudly.
#
# `os.path.normcase` is NOT that test: `posixpath.normcase` is the identity
# function on every POSIX platform, macOS included, so `os.path.normcase("A")
# == "a"` is only ever True on Windows. A `Pkg/` committed to the index but
# checked out as `pkg/` on an APFS volume would then miss the prefix test here
# and lose every decoration beneath it — the exact failure this comment used
# to say the line prevented. `sys.platform` names the two case-insensitive-by-
# default platforms directly instead.
_FOLDS_CASE = sys.platform in ("win32", "darwin")


def classify(xy: str) -> str | None:
    """One porcelain-v1 two-letter code -> one of `STATUS_ORDER`, or None.

    `X` is the index-vs-HEAD column, `Y` the work-tree-vs-index one. The order
    of the tests is the whole content of this function:

      * `??` is untracked, and is its own code rather than a column pair.
      * `!!` is ignored. It cannot arrive (we never pass `--ignored`), and it
        is rejected explicitly anyway — falling through would read `!` as a
        dirty work tree and report a `.venv` as modified.
      * a `U` in either column, plus `AA` and `DD`, are git's full set of
        unmerged codes.
      * a non-space `Y` means the file on disk differs from the index. This
        wins over a dirty index on purpose: `MM` is both, and the edit you have
        not staged yet is the more useful thing to be told about.
      * a non-space `X` with a clean `Y` is staged and nothing more.
    """
    if xy == "??":
        return "untracked"
    if xy == "!!" or len(xy) != 2:
        return None
    x, y = xy[0], xy[1]
    if x == "U" or y == "U" or xy in ("AA", "DD"):
        return "conflicted"
    if y != " ":
        return "modified"
    if x != " ":
        return "staged"
    return None


def parse_porcelain(out: bytes) -> list[tuple[str, str]]:
    """`git status --porcelain -z` output -> `[(code, repo_relative_path)]`.

    NUL-separated so a path containing a newline — or bytes that are not UTF-8
    — survives; git only quotes paths in the newline-separated format. Each
    record is `XY<space><path>`, and with `--no-renames` in the argv there is
    never the second path a rename would append, so a record is exactly one
    chunk.

    The path is decoded with `os.fsdecode`, the same call `_git_ignored` uses
    for this identical job (gitignore.py) — not `.decode("utf-8", "replace")`.
    A path is only ever compared against a name `os.scandir` produced from the
    SAME bytes, and `fsdecode` is defined to round-trip those bytes exactly
    (surrogateescape on POSIX) so the two always compare equal. `utf-8/replace`
    is neither safer nor simpler: it doesn't raise, but it DOES mangle every
    undecodable byte into U+FFFD, a string `scandir` will never produce for the
    same file — a `café.txt` written in a latin-1 locale would decode to two
    different strings from the two calls and lose its tint either way, so
    there is no version of "replace" that helps here.
    """
    records = []
    for chunk in out.split(b"\0"):
        if len(chunk) < 4 or chunk[2:3] != b" ":
            continue
        code = chunk[:2].decode("ascii", "replace")
        records.append((code, os.fsdecode(chunk[3:])))
    return records


def entry_statuses(
    prefix: str, records: list[tuple[str, str]], names: list[str],
    _folds_case: bool | None = None,
) -> dict[str, str]:
    """Map the listed `names` to their status, rolling subtrees up to folders.

    `prefix` is the listed directory's own path from the repo root, `""` at the
    root and otherwise slash-terminated; porcelain paths are always repo-root
    relative regardless of where git was invoked, which is why one is needed.
    `records` outside it are skipped — the pathspec already narrows them, but a
    parent-directory record can still slip through as `sub/` for a wholly
    untracked tree we are looking INSIDE of.

    Anything deeper than one segment is credited to the folder that heads it,
    keeping the most urgent state found (see `STATUS_ORDER`). Names not in the
    listing are dropped rather than reported: the caller's rows are the only
    thing that can wear a tint.

    `_folds_case` overrides the module-level `_FOLDS_CASE` for tests; every
    real caller leaves it `None` and gets this platform's answer.
    """
    fold_case = _FOLDS_CASE if _folds_case is None else _folds_case
    fold = str.lower if fold_case else None
    # Keyed by the FOLDED leaf name, valued by the caller's own spelling: the
    # dict this function returns must use the spelling `names` arrived with
    # (scandir's), never git's index spelling, because the frontend joins rows
    # onto this result by the name it already has. Building the table once
    # keeps the per-record lookup below a single dict access either way.
    want_by_fold = {fold(n): n for n in names} if fold else None
    want = None if fold else set(names)
    head = prefix if fold is None else fold(prefix)
    out: dict[str, str] = {}

    def _credit(name: str, status: str) -> None:
        prev = out.get(name)
        if prev is None or _RANK[status] < _RANK[prev]:
            out[name] = status

    for code, path in records:
        candidate = path[: len(prefix)]
        if (candidate if fold is None else fold(candidate)) != head:
            continue
        status = classify(code)
        if status is None:
            continue
        rel = path[len(prefix):]
        if not rel:
            # A record for the listed directory ITSELF, not for anything
            # inside it. Git only produces this shape with a trailing slash,
            # and only for the untracked-directory collapse the module header
            # describes — in which case every name this listing renders is
            # untracked, because `-u normal` folded the whole subtree into
            # this one record and there is nothing more specific to read.
            # Without this, opening `brandnew/` after its parent listing
            # tinted it green would show every child undecorated — the
            # collapse this file exists to expand back out, silently un-done
            # one level down. A record that does NOT end in `/` (a
            # submodule's own path, say) is not that collapse and is not a
            # blanket statement about its contents, so it is simply dropped —
            # there is no listed row named after the folder being listed.
            if path.endswith("/"):
                for name in names:
                    _credit(name, status)
            continue
        # The first segment is the listed row; deeper segments are what makes
        # this a roll-up rather than a lookup.
        leaf = rel.split("/", 1)[0]
        if want is None:
            name = want_by_fold.get(fold(leaf))
            if name is None:
                continue
        else:
            if leaf not in want:
                continue
            name = leaf
        _credit(name, status)
    return out


def _prefix_of(cwd: str, top: str) -> str | None:
    """`cwd`'s path from the repo root `top`, slash-terminated (`""` at root).

    Derived from the cached `_repo_toplevel` answer instead of asking git for
    `rev-parse --show-prefix`, which would be a second spawn on the listing's
    critical path for a fact the first one already implies. Both sides are
    resolved first so a symlinked checkout (or macOS's `/var` -> `/private/var`)
    does not read as "outside the repo" — the residual case difference is what
    `entry_statuses` folds over.

    None means the two paths do not nest, which should not happen for a
    toplevel git itself reported for `cwd`; it is handled rather than asserted
    because the answer may be up to `_TOPLEVEL_MAX_AGE_S` old, and by then the
    repository could have moved.
    """
    try:
        rel = os.path.relpath(os.path.realpath(cwd), os.path.realpath(top))
    except (OSError, ValueError):  # unrelated Windows drives raise ValueError
        return None
    if rel == os.curdir:
        return ""
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel.replace(os.sep, "/") + "/"


def _spawn_kwargs() -> dict:
    """The kwargs the status spawn needs to reach posix_spawn, not fork.

    A plain dict literal, not a call into gitignore.py's copy: the static sweep
    in `tests/test_git_posix_spawn.py` resolves a `**_spawn_kwargs()` spread by
    reading this function's `return {...}` out of THIS file's AST, and it does
    not follow an import to check another module's literal. Kept field-for-
    field identical to gitignore.py's `_spawn_kwargs` on purpose — `close_fds`
    is the half of the posix_spawn condition git_bin()'s absolute path does not
    cover, and `creationflags` keeps a spawned git from popping a console
    window on Windows.
    """
    return {
        "close_fds": False,
        "creationflags": (subprocess.CREATE_NO_WINDOW
                          if sys.platform == "win32" else 0),
    }


# Memoizes `_run_status`'s answer, keyed on `cwd`. Every one of `/api/fs/list`'s
# callers pays this spawn even when it only wants filenames — `BookmarkCards`
# fans out `1 + APP_PROBE_LIMIT` listings per card on the explorer home screen,
# and several `fs-actions.ts` callers list a directory purely to dedupe a
# filename — so an unmemoized `listing_statuses` runs a full subtree `git
# status` whose result is thrown straight away, over and over, for the same
# directory in the same second. Mirrors `_repo_toplevel`'s LRU + TTL in
# gitignore.py rather than inventing a second caching idiom in the same file
# that already imports the first one's helpers.
_STATUS_CACHE_SIZE = 256
_status_cache: "OrderedDict[str, tuple[float, list[tuple[str, str]]]]" = OrderedDict()
_status_lock = threading.Lock()

# Deliberately much shorter than `_TOPLEVEL_MAX_AGE_S` (300s): a stale
# toplevel answer is "which repo", which barely ever changes, but a stale
# STATUS answer is "what's dirty right now", which the explorer's git panel
# and Job B's live-listing refresh both promise to show promptly after a
# stage/unstage. Two seconds keeps the common "list a folder in a fan-out
# loop" case cheap (one spawn services every listing that lands inside the
# window) without a browsing session ever waiting more than a couple of
# seconds to notice a change made OUTSIDE this process — a `git add` typed in
# an external terminal, say. A change made THROUGH this process's own git
# template (`templates/git/ops.py`) does not wait even that long: `_stage` /
# `_unstage` and friends call `invalidate_status_cache` themselves, so the
# very next listing sees the write immediately rather than replaying whatever
# this cache happened to hold.
_STATUS_CACHE_TTL_S = 2.0


def invalidate_status_cache() -> None:
    """Drop every memoized `git status` answer.

    Called from `routers/run.py`'s `/api/run` handler after EVERY run, not
    from the git template itself: `templates/git/ops.py`'s stage/unstage/
    commit run on the subprocess-per-call path (D72), a fresh interpreter that
    holds no reference to this module's cache, so an invalidation call inside
    `ops.py` would only ever clear that throwaway process's own empty copy.
    `/api/run`'s handler, in contrast, runs in THIS process both before and
    after awaiting the subprocess — the same reason `noteFsChanged()`
    (`static/runtime.js`) is fired unconditionally by the frontend after every
    runPython call, success or failure: a script can write anything, anywhere,
    and there is no cheaper way to know what than to assume everything might
    have changed.

    The whole cache is cleared rather than just one `cwd`'s entry, for the
    same reason `clearListPrefetch` clears its whole map instead of doing path
    arithmetic (frontend/src/platform/lib/api.ts): a commit or checkout can
    move the dirty set across an arbitrary number of directories, and
    reasoning about which cached entries it could have touched buys nothing
    over dropping all of them — the next listing under any of them just pays
    one more spawn.
    """
    with _status_lock:
        _status_cache.clear()


def _run_status(cwd: str) -> list[tuple[str, str]] | None:
    """One scoped `git status` in `cwd` (cached, see `_STATUS_CACHE_TTL_S`),
    or None if git could not answer.

    Only a real answer is cached — never `None`. A `None` here means git
    itself could not be asked right now (missing binary, an fd shortage, a
    5s timeout tripped by a huge index): a fact about this call, not about
    `cwd`, and caching it would let one blip hide every decoration in that
    folder for the rest the TTL.
    """
    with _status_lock:
        cached = _status_cache.get(cwd)
        if cached is not None and time.monotonic() - cached[0] < _STATUS_CACHE_TTL_S:
            _status_cache.move_to_end(cwd)
            return cached[1]
    records = _run_status_uncached(cwd)
    if records is not None:
        with _status_lock:
            _status_cache[cwd] = (time.monotonic(), records)
            _status_cache.move_to_end(cwd)
            while len(_status_cache) > _STATUS_CACHE_SIZE:
                _status_cache.popitem(last=False)
    return records


def _run_status_uncached(cwd: str) -> list[tuple[str, str]] | None:
    """The uncached `git status --porcelain`, one spawn per call."""
    try:
        proc = subprocess.run(
            [
                git_bin(), "-C", cwd,
                # Read-only: without this, status may REWRITE the index to
                # record refreshed stat data. Harmless in isolation, but this
                # runs on every folder the user opens, and taking index.lock
                # out from under a concurrent commit (app_git.py commits every
                # Claude turn) is a real collision to hand a browsing hint.
                "--no-optional-locks",
                "status", "--porcelain", "-z",
                # Pinned explicitly rather than left to whatever
                # `status.showUntrackedFiles` says in the user's config —
                # the module header's claim about the cost of this call is
                # only true if this flag can't silently become `=all`
                # (enumerates every file under a huge untracked dir, the
                # 100k-file `.venv` walk this module exists to avoid) or
                # `=no` (no untracked file is ever tinted at all).
                # `--ignored=no` is git's default too, but pinned for the
                # same reason: `!!` records must never arrive, since
                # `classify` relies on that to tell an ignored `.venv` apart
                # from a dirty one. `templates/git/log.py` pins the identical
                # pair for the identical reason.
                "--untracked-files=normal", "--ignored=no",
                # Renames become the delete + add pair they are made of, which
                # is both what a listing wants (two rows, two tints) and what
                # keeps a -z record a single chunk — see `parse_porcelain`.
                "--no-renames",
                # Only this folder's subtree. `-C` put git's cwd here, so `.`
                # is that folder; the whole-repo status would make opening one
                # directory of a monorepo cost the monorepo.
                "--", ".",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        _warn_git_unusable("status --porcelain", e)
        return None
    if proc.returncode != 0:
        if not _is_ordinary_negative(proc.stderr):
            _warn_git_refused("status --porcelain", cwd,
                              proc.returncode, proc.stderr)
        return None
    return parse_porcelain(proc.stdout)


def listing_statuses(cwd: str, names: list[str]) -> dict[str, str]:
    """Status per listed name in `cwd`; `{}` when there is nothing to say.

    Names absent from the result are simply clean (or not in a repo) — the
    caller annotates the ones present and leaves the rest alone, so "no git
    here" and "everything committed" render identically, which is correct:
    both mean there is nothing to point at.
    """
    if not names:
        return {}
    top = _repo_toplevel(cwd)
    if top is None:
        return {}
    prefix = _prefix_of(cwd, top)
    if prefix is None:
        return {}
    records = _run_status(cwd)
    if not records:
        return {}
    return entry_statuses(prefix, records, names)
