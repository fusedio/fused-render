"""GET /api/git/snapshot — the enclosing APP FOLDER, materialised on disk as of
one commit.

This is the write side of the git sidebar's revision selection, replacing the
predecessor per-file design (`git_show.py`, deleted alongside this route's
introduction): rather than resolving one file's bytes on read out of the object
database, the whole app folder enclosing the open path is `git archive`d into

    ~/.fused-render/app-versions/<key>/<sha>/

where `<key>` identifies the (repo root, app-relative folder) pair. A real tree
on disk is what lets `runtime.js` rewrite EVERY read under the app folder — not
just `readFile`/`rawUrl`/`stat` but `runPython` too — to the same relative path
under the extracted tree, so a `.py` reader runs the commit's own code rather
than today's. `/api/git/show` could not do that without changing the `main()`
contract (see the plan's decisions log); this route pays disk instead.

    GET /api/git/snapshot?path=<ABSOLUTE working-tree path>&sha=<hex object name>

`path` is whatever the runtime already has open (a file or a folder inside the
app); the enclosing app folder is resolved HERE, by walking up from it
(`app_listing.enclosing_app_dir`), bounded at the repository root. `sha` is any
hex object name, full or abbreviated — the same rule `git_show.py` applied and
for the same reason: it goes into an argv, and hex-only is what keeps it from
ever being option-shaped or a revision expression with side effects.

CACHING. The key is `sha256(repo_root + "\\0" + app_rel)[:16]`, the same
shaping `templates/shared/file_history.py` uses for its own path keys. An
existing `<key>/<sha>/` is a hit: no git is forked at all, not even to
re-resolve the repository root — that resolution is a pure filesystem walk for
the nearest `.git`, not a `rev-parse` shell-out, precisely so a hit costs a stat
and nothing else. A miss extracts to a SIBLING temporary directory and
`os.replace`s it into place, so a killed extraction (server restart, client
disconnect) can never leave a partial tree at the final path for a later
request to serve as complete. Oldest-mtime trees are garbage-collected past a
cap on every miss.

FOUR CLEAN ERRORS, never a traceback (mirroring `git_show.py`'s contract):
  400  a relative `path`, a non-hex `sha`, a mount-backed path
  404  no app folder encloses `path`; the sha is unknown; the app folder did
       not exist at that revision
  502  git (or tar) is missing, hung, or failed for a reason of its own

Response: `{"ok": true, "dir": <extracted tree>, "entry": <its entry page, or
null>, "app_dir": <the LIVE app folder the rewrite rule is keyed on>}`. `entry`
is resolved from the EXTRACTED tree, deliberately: an app whose entry page was
renamed since the commit must open at the entry that commit had, not at a
filename that did not exist yet.
"""
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from fused_render.app_listing import app_entry, enclosing_app_dir
from fused_render.server.common import _error
from fused_render.shell import mounts as shell_mounts
from fused_render.shell import storage as shell_storage

logger = logging.getLogger(__name__)
router = APIRouter()

# Same bound git_show.py and log.py put on their own git calls: local plumbing
# with no network step, so the timeout exists only for a stalled filesystem or
# a very large tree.
TIMEOUT_S = 20.0

# A hex object name, full or abbreviated — the same rule git_show.py's
# `_SHA_RE` applies, for the same reason: this is what keeps an option-shaped
# or expression-shaped `sha` out of an argv.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")

# How many extracted `<key>/<sha>/` trees the cache keeps before it starts
# reaping the oldest ones. Each is one app folder at one commit — small next to
# a repo clone — so this is generous; it exists to bound disk over a long
# session of clicking through history, not to keep the cache tiny.
MAX_CACHED_SNAPSHOTS = 200

# Non-interactivity, as environment. Copied from git_show.py (which copied it
# from templates/git/condition.py) rather than imported — see git_show.py's
# own comment for why this is a copy and not a shared import.
_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "Never",
    "GIT_LFS_SKIP_SMUDGE": "1",
}

_GIT_BIN = None


def _git_bin():
    global _GIT_BIN
    if _GIT_BIN is None:
        import shutil as _shutil
        _GIT_BIN = _shutil.which("git") or "git"
    return _GIT_BIN


# `tar`'s own absolute path, resolved the same way and for the same reason as
# `_git_bin()`: the posix_spawn hazard is not git-specific — ANY child process
# started with a bare, un-dirname'd argv[0] forks first, and a fork in this
# process with libproj resident SIGSEGVs before exec regardless of which
# program the fork was headed towards. `tar` runs in the same request as
# `git archive`, so it needs the identical discipline.
_TAR_BIN = None


def _tar_bin():
    global _TAR_BIN
    if _TAR_BIN is None:
        import shutil as _shutil
        _TAR_BIN = _shutil.which("tar") or "tar"
    return _TAR_BIN


class _Refused(Exception):
    """A refusal with the status the pane should see — same shape as
    `git_show.py`'s, so both routes fail the same way for the same caller."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _popen_kwargs(stdin=subprocess.DEVNULL):
    """The posix_spawn-safe kwargs every Popen here shares. `stdin` is the one
    knob a caller may override (the tar leg of `_run_archive` pipes from
    git's stdout instead of DEVNULL) — a parameter rather than a second
    dict literal, so tests/test_git_posix_spawn.py's static sweep still finds
    ONE `return {...}` to read `close_fds`/`cwd` off of regardless of which
    call passed what; it never inspects `stdin` itself."""
    return {
        "env": {**os.environ, **_ENV},
        "stdin": stdin,
        # Required, together with the absolute argv[0] and the ABSENCE of
        # `cwd=`, to reach posix_spawn rather than fork — see git_show.py's
        # comment above its own `_GIT_BIN` for the full story.
        "close_fds": False,
        "creationflags": (subprocess.CREATE_NO_WINDOW
                          if sys.platform == "win32" else 0),
    }


def _cache_root() -> str:
    """`~/.fused-render/app-versions` — resolved per call, not at import: the
    same reason `mount.py::_is_under_snapshot_root` resolves it per call.
    home_dir() depends on FUSED_RENDER_HOME and the branch ref, and a frozen
    value would write into a directory the server is not using."""
    return os.path.join(shell_storage.home_dir(), "app-versions")


def snapshot_key(repo_root: str, app_rel: str) -> str:
    """The cache key for one (repo, app folder) pair — the same
    `sha256(...)[:16]` shaping `templates/shared/file_history.py` uses for its
    own path keys, so this codebase has one answer to "how do we turn a path
    into a short cache key" rather than two."""
    return hashlib.sha256(f"{repo_root}\0{app_rel}".encode()).hexdigest()[:16]


def _repo_root(cwd: str) -> str | None:
    """The nearest ancestor of `cwd` holding a `.git` entry (directory or
    file — a worktree/submodule's `.git` is a file), realpath'd. A PURE
    filesystem walk rather than `git rev-parse --show-toplevel`, deliberately:
    it is what lets a cache HIT resolve with no git fork at all, not merely
    skip the `archive` call. Consulted on every request (hit or miss) for
    exactly that reason — a `rev-parse` here would defeat the point of the
    cache the moment it existed.

    None when no ancestor up to the filesystem root carries a `.git`.
    """
    cur = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return os.path.realpath(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _gc(cache_root: str, cap: int = MAX_CACHED_SNAPSHOTS) -> None:
    """Reap oldest-mtime `<key>/<sha>/` trees past `cap`. Never raises — a
    listing that cannot be done this time is done next time."""
    try:
        entries = []
        for key in os.listdir(cache_root):
            key_dir = os.path.join(cache_root, key)
            if key.startswith(".") or not os.path.isdir(key_dir):
                continue
            for sha in os.listdir(key_dir):
                if sha.startswith("."):
                    continue  # an in-flight extraction's temp dir: never reap it
                sha_dir = os.path.join(key_dir, sha)
                if not os.path.isdir(sha_dir):
                    continue
                try:
                    mtime = os.stat(sha_dir).st_mtime
                except OSError:
                    continue
                entries.append((mtime, sha_dir))
    except OSError:
        return
    if len(entries) <= cap:
        return
    entries.sort()  # oldest first
    for _, sha_dir in entries[: len(entries) - cap]:
        shutil.rmtree(sha_dir, ignore_errors=True)


def _run_archive(repo_root: str, sha: str, app_rel: str, dest_tmp: str) -> None:
    """`git archive <sha> -- :(literal)<app_rel>`, piped straight into `tar -x`
    at `dest_tmp` — never buffered in this process, so a large app folder costs
    disk and pipe bandwidth, not memory. `--strip-components` drops the
    `app_rel` prefix every tar member carries, so `dest_tmp` ends up holding
    the app folder's OWN contents rather than nesting them `app_rel` levels
    deep.
    """
    strip = app_rel.count("/") + 1 if app_rel else 0

    # BOTH argv lists are inlined here, not built into a named variable first:
    # tests/test_git_posix_spawn.py's static sweep can only verify argv[0] and
    # the posix_spawn kwargs when it can read the literal list at the call
    # site — a `Name` defeats it (see that file's own comment on the
    # recognition blind spot it deliberately still flags for). This is not
    # only about passing that check: it is what makes the fact TRUE — a
    # variable holding "the argv" invites a later edit that swaps it for
    # something not-quite-inline again.
    try:
        archive = subprocess.Popen(
            [_git_bin(), "--no-pager", "-C", repo_root, "archive", sha,
             *(["--", f":(literal){app_rel}"] if app_rel else [])],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **_popen_kwargs())
    except FileNotFoundError as exc:
        raise _Refused("git is not installed, or not on this app's PATH",
                       status=502) from exc
    except OSError as exc:
        raise _Refused(f"git could not be started: {exc}", status=502) from exc

    try:
        # `_popen_kwargs()` here too: the posix_spawn hazard is not
        # git-specific (see `_tar_bin`'s own comment) — this child forks
        # exactly as readily as an un-dirname'd git would. `stdin=` is the
        # override `_popen_kwargs` takes a parameter for, rather than passed
        # as a second keyword alongside the `**` spread — that would be two
        # values for one keyword (its default is DEVNULL) and Python raises.
        extract = subprocess.Popen(
            [_tar_bin(), "-x", f"--strip-components={strip}", "-C", dest_tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            **_popen_kwargs(stdin=archive.stdout))
    except OSError as exc:
        archive.kill()
        raise _Refused(f"tar could not be started: {exc}", status=502) from exc

    # Closed in the PARENT so `extract` sees EOF once `archive` finishes
    # writing — the standard "connect two Popens" idiom; otherwise this
    # process's own held reference keeps the pipe's read end open forever.
    archive.stdout.close()

    try:
        _, tar_err = extract.communicate(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        extract.kill()
        archive.kill()
        raise _Refused(f"extraction took longer than {TIMEOUT_S:.0f}s",
                       status=502)

    try:
        archive.wait(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        archive.kill()

    archive_err = b""
    if archive.stderr is not None:
        try:
            archive_err = archive.stderr.read() or b""
        except OSError:
            pass

    if archive.returncode != 0:
        # An unknown sha, or a path that did not exist at that revision, is
        # git's own one-line message at exit 128 — the caller asked for
        # something that is not there, which is a 404, not a server fault.
        detail = archive_err.decode("utf-8", "replace").strip().splitlines()
        raise _Refused(detail[0] if detail else
                       f"git exited {archive.returncode}", status=404)
    if extract.returncode != 0:
        detail = tar_err.decode("utf-8", "replace").strip().splitlines()
        raise _Refused(detail[0] if detail else
                       f"tar exited {extract.returncode}", status=502)


def extract_snapshot(path: str, sha: str) -> dict:
    """The whole route, as a plain function so it can be called from a
    threadpool and tested with no `TestClient`.

    Returns `{"ok": True, "dir", "entry", "app_dir"}` or raises `_Refused`.
    """
    if not _SHA_RE.match(sha or ""):
        raise _Refused("'sha' must be a hex object name (4-64 hex digits)")
    if not path or not os.path.isabs(path):
        raise _Refused("'path' must be an absolute filesystem path")
    if shell_mounts.is_mount_backed(path):
        # Refused before any filesystem walk, like git_show.py: `git archive`
        # over an rclone-NFS mount walks the remote tree, the known
        # mount-wedging pattern.
        raise _Refused("git is not available on remote mounts")

    cwd = path if os.path.isdir(path) else os.path.dirname(path)
    while cwd and not os.path.isdir(cwd):
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent
    if not cwd or not os.path.isdir(cwd):
        raise _Refused(f"no such directory: {path}", status=404)

    repo_root = _repo_root(cwd)
    if repo_root is None:
        raise _Refused(f"{path} is not inside a git work tree", status=404)

    app_dir = enclosing_app_dir(path, repo_root)
    if app_dir is None:
        raise _Refused(f"no app folder encloses {path}", status=404)
    app_dir = os.path.realpath(app_dir)
    app_rel = ("" if app_dir == repo_root else
              os.path.relpath(app_dir, repo_root).replace(os.sep, "/"))

    key = snapshot_key(repo_root, app_rel)
    cache_root = _cache_root()
    dest = os.path.join(cache_root, key, sha)

    if not os.path.isdir(dest):
        key_dir = os.path.join(cache_root, key)
        os.makedirs(key_dir, exist_ok=True)
        dest_tmp = tempfile.mkdtemp(prefix=f".{sha}-", dir=key_dir)
        try:
            _run_archive(repo_root, sha, app_rel, dest_tmp)
        except _Refused:
            shutil.rmtree(dest_tmp, ignore_errors=True)
            raise
        # Atomic within the same filesystem (both are under `key_dir`), so a
        # request racing this one either sees no `dest` yet or the COMPLETE
        # tree — never a partial one.
        try:
            os.replace(dest_tmp, dest)
        except OSError:
            # Lost a race with a concurrent identical request: the other
            # extraction already occupies `dest`. That tree is just as valid
            # as the one this call produced, so use it and drop ours.
            shutil.rmtree(dest_tmp, ignore_errors=True)
            if not os.path.isdir(dest):
                raise
        _gc(cache_root)

    try:
        entry = app_entry(dest)
    except OSError:
        entry = None

    return {"ok": True, "dir": dest, "entry": entry, "app_dir": app_dir}


@router.api_route("/api/git/snapshot", methods=["GET"])
async def api_git_snapshot(path: str, sha: str):
    try:
        return await run_in_threadpool(extract_snapshot, path, sha)
    except _Refused as e:
        return _error(e.message, status=e.status)
