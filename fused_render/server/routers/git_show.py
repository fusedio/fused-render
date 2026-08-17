"""GET /api/git/show — one file's bytes AS OF one commit.

This is the read side of the git sidebar's revision selection: click a commit in
the `git` companion and the CONTENT pane beside it renders the open file as that
commit left it. The predecessor design (a per-path timeline mode, since removed)
answered the same question by `git archive`-ing a whole snapshot into
`~/.fused-render/app-versions/<key>/<sha>/` and pointing the frame at the temp
path. Nothing is written here: the bytes are
resolved on read, straight out of the object database, so closing the sidebar
leaves nothing behind to clean up and no half-extracted tree to go stale.

THE CONTRACT is deliberately shaped so the caller has nothing to resolve:

    GET /api/git/show?path=<ABSOLUTE working-tree path>&sha=<hex object name>

`path` is the path the runtime already has (the frame's `_file`), not a
repo-relative one — the repository root and the relative path are found HERE, by
asking git, because a JS caller resolving them would be a second, worse copy of
`log.py::_locate`. `sha` is any hex object name git will accept, full or
abbreviated: the sidebar sends the full one it holds, and a hand-typed short one
still works.

WHAT IT IS NOT. It is not a general "read this from git" endpoint: no ref names,
no `HEAD~2`, no `:/subject search` — only hex, because the value goes into an argv
and the one property worth guaranteeing is that it can never be option-shaped or
be a revision expression with side conditions. It is not writable, obviously, and
it is not a mount path: `git -C` over an rclone-NFS mount walks the remote tree,
the known mount-wedging pattern that `templates/git/condition.py` and `log.py`
both refuse, so this refuses it too rather than being the one git call that does
not.

FOUR CLEAN ERRORS, never a traceback (each is a state the pane renders):
  400  a relative `path`, a non-hex `sha`, a mount-backed path
  404  the path is not inside a work tree; the sha is unknown; the path did not
       exist at that revision (git's own message, first line)
  413  the blob is larger than MAX_SHOW_BYTES
  502  git is missing, hung, or failed for a reason of its own

WHY THE RESPONSE IS NOT A StreamingResponse. `git show` reports "no such path in
that revision" by EXIT STATUS, which is known only once its output has ended — and
a streaming response has already committed 200 with its first chunk. Streaming
would therefore have to answer a missing path with a 200 and an empty body, which
is precisely the lying-pane failure this route exists to avoid. So the read is
bounded instead: at most MAX_SHOW_BYTES + 1 bytes ever enter memory, the +1 being
how "too large" is detected without reading the rest, and the process is killed the
moment the cap is passed. Unbounded `subprocess.run(...).stdout` would be the bug
here — a 2GB blob in a repository is a 2GB allocation in this process.
"""
import logging
import mimetypes
import os
import re
import subprocess
import sys
import threading

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from fused_render.server.common import _error
from fused_render.server.proxy import _harden_raw
from fused_render.shell import mounts as shell_mounts


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


logger = logging.getLogger(__name__)
router = APIRouter()

# Hard ceiling on the bytes a single revision read may materialize. Generous for
# the things a preview template actually renders (source, markdown, json, a
# screenshot) and small enough that a checked-in binary cannot make the server
# allocate its way into swap. A blob over the cap is a clean 413, never a silent
# truncation: a pane labelled "as of abc1234" showing the first 8MB of a file
# would be a subtler lie than showing nothing.
MAX_SHOW_BYTES = 8_000_000

# Same bound `log.py` puts on its own git calls. Every command here is local
# plumbing with no network step; the timeout exists for a stalled filesystem.
TIMEOUT_S = 10.0

# A hex object name, full or abbreviated — the SAME rule log.py's `_SHA_RE`
# applies, and for the same reason: this is what keeps an option-shaped or
# expression-shaped `sha` out of an argv.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")

# Non-interactivity, as environment. Copied from templates/git/condition.py
# (which explains each knob) rather than imported: that module is a standalone
# template gate exec'd with a stripped path, so importing it from the server
# would couple this route to a file whose whole design is not to be imported.
# Deliberately NOT disabling the user's git config — `safe.directory` lives
# there, and a repo the user marked safe must keep working here.
_ENV = {
    "GIT_TERMINAL_PROMPT": "0",   # never prompt for credentials
    "GIT_OPTIONAL_LOCKS": "0",    # never take a lock just to answer a question
    "GIT_PAGER": "cat",           # a pager on a pipe would deadlock
    "GIT_ASKPASS": "",            # no GUI/askpass helper
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "Never",   # git-credential-manager
    "GIT_LFS_SKIP_SMUDGE": "1",   # never fetch an LFS object to answer this
}


class _Refused(Exception):
    """A refusal with the status the pane should see. Every failure path below
    raises one of these, so the handler has exactly one place that turns a
    problem into a response and no branch can forget to."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _popen_kwargs():
    return {
        "env": {**os.environ, **_ENV},
        "stdin": subprocess.DEVNULL,
        "creationflags": (subprocess.CREATE_NO_WINDOW
                          if sys.platform == "win32" else 0),
    }


def _locate(path: str) -> tuple[str, str]:
    """`(work-tree root, POSIX path relative to it)` for an absolute path.

    Mirrors `templates/git/log.py::_locate` — the same bootstrap
    `rev-parse --show-toplevel` from the path's own directory, the same realpath
    on both sides so a repo reached through a symlink (or macOS's
    /var -> /private/var) still passes the containment check. It is a mirror
    rather than an import for the reason above the `_ENV` copy: log.py is a
    template module executed standalone.

    The path is NOT required to exist on disk. A file deleted since the commit
    the user picked is exactly the case a revision view is for.
    """
    if not path or not os.path.isabs(path):
        raise _Refused("'path' must be an absolute filesystem path")
    if shell_mounts.is_mount_backed(path):
        # Refused BEFORE any subprocess, like the git gate does: git over an
        # rclone-NFS mount stats and lists its way through the work tree, which
        # is the pattern that wedges the mount.
        raise _Refused("git is not available on remote mounts")
    if os.path.isdir(path):
        # A file's bytes is the whole question. `git show <sha>:<dir>` answers a
        # TREE LISTING, which would be served as if it were the directory's
        # contents — so it is refused rather than allowed to look like an answer.
        # Unreachable from the shell (only a file gets a content pane), which is
        # exactly why it is worth stating.
        raise _Refused("'path' must name a file, not a directory")
    # The path's DIRECTORY, because the path itself may no longer exist (and
    # because `-C` on a file is an ENOTDIR rather than an answer). A directory
    # that is gone too walks up to the nearest one that is not — git needs a real
    # cwd to start its ascent from.
    cwd = os.path.dirname(path)
    while cwd and not os.path.isdir(cwd):
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent
    if not cwd or not os.path.isdir(cwd):
        raise _Refused(f"no such directory: {path}", status=404)
    top = _git(cwd, "rev-parse", "--show-toplevel", allow=(0, 128))
    root = top.decode("utf-8", "replace").strip()
    if not root:
        # Empty for a bare repo and inside `.git` as well as for a non-repo —
        # all three are "no work tree to resolve against".
        raise _Refused(f"{path} is not inside a git work tree", status=404)
    root = os.path.realpath(root)
    real = os.path.realpath(path)
    rel = "" if real == root else os.path.relpath(real, root).replace(os.sep, "/")
    if not rel or rel == "." or rel.startswith("../"):
        raise _Refused(f"{path} is not a file inside {root}", status=404)
    return root, rel


def _git(root: str, *args: str, allow=(0,)) -> bytes:
    """One bounded git call; its stdout. Anything outside `allow` is a refusal,
    so no caller has to branch on a raw CalledProcessError."""
    try:
        proc = subprocess.run(
            [_git_bin(), "--no-pager", "-C", root, *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=TIMEOUT_S, **_popen_kwargs())
    except FileNotFoundError as exc:
        raise _Refused("git is not installed, or not on this app's PATH",
                       status=502) from exc
    except subprocess.TimeoutExpired as exc:
        raise _Refused(f"git took longer than {TIMEOUT_S:.0f}s to answer",
                       status=502) from exc
    except OSError as exc:
        raise _Refused(f"git could not be started: {exc}", status=502) from exc
    if proc.returncode not in allow:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise _Refused(detail[0] if detail else f"git exited {proc.returncode}",
                       status=502)
    return proc.stdout


def _show(root: str, rel: str, sha: str) -> bytes:
    """`git show <sha>:<rel>`, capped at MAX_SHOW_BYTES.

    Popen + a capped read loop rather than `subprocess.run`, because a cap
    applied to run()'s result is not a bound at all: the whole blob is already in
    memory by the time the slice happens (the same argument log.py's
    `_git_stream` makes for its diffs).
    """
    spec = f"{sha}:{rel}"
    try:
        proc = subprocess.Popen(
            [_git_bin(), "--no-pager", "-C", root, "show", spec],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_popen_kwargs())
    except FileNotFoundError as exc:
        raise _Refused("git is not installed, or not on this app's PATH",
                       status=502) from exc
    except OSError as exc:
        raise _Refused(f"git could not be started: {exc}", status=502) from exc

    # The pipes, narrowed ONCE and named. `Popen.stdout`/`stderr` are Optional —
    # they are None for a child whose stream was not a PIPE — and while both were
    # PIPEs above, that is a fact about the call three lines up rather than about
    # the object, so the handles are checked here instead of asserted away. A
    # child that somehow came back without them is a failed spawn: reported as one,
    # not dereferenced.
    out, err_pipe = proc.stdout, proc.stderr
    if out is None or err_pipe is None:
        proc.kill()
        raise _Refused("git could not be started: no output pipe", status=502)

    # A watchdog rather than a `timeout=`, which does not exist on a manual read
    # loop: a git that stops writing mid-stream must not park this read forever.
    killer = threading.Timer(TIMEOUT_S, proc.kill)
    killer.daemon = True
    killer.start()
    chunks: list[bytes] = []
    total = 0
    over = False
    try:
        while True:
            # `read`, not `read1`: this loop wants a bounded TOTAL, not the
            # lowest-latency first chunk, and `read(n)` is the one that is on the
            # `IO[bytes]` interface `Popen.stdout` is typed as. It returns short
            # only at EOF, so the loop terminates on git's own exit; the watchdog
            # above covers a child that stops writing without closing.
            chunk = out.read(65536)
            if not chunk:
                break
            total += len(chunk)
            # One byte past the cap is enough to KNOW it is over; reading the
            # rest to measure it exactly would be the unbounded read again.
            if total > MAX_SHOW_BYTES:
                over = True
                break
            chunks.append(chunk)
    finally:
        killer.cancel()
        try:
            out.close()
        except OSError:
            pass
        if over:
            proc.kill()
        try:
            code = proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = None
        err = b""
        try:
            err = err_pipe.read() or b""
            err_pipe.close()
        except OSError:
            pass

    if over:
        raise _Refused(
            f"{rel} is larger than {MAX_SHOW_BYTES // 1_000_000} MB at {sha[:7]} "
            "— this view will not read it", status=413)
    if code is None:
        raise _Refused(f"git took longer than {TIMEOUT_S:.0f}s to answer",
                       status=502)
    if code != 0:
        # The ordinary negatives — an unknown sha, or a path that did not exist
        # at that revision — are exit 128 with a one-line explanation git wrote
        # better than we would. Both are 404: the caller asked for something that
        # is not there, which is not a server fault.
        detail = err.decode("utf-8", "replace").strip().splitlines()
        raise _Refused(detail[0] if detail else f"git exited {code}", status=404)
    return b"".join(chunks)


def _read(path: str, sha: str) -> bytes:
    if not _SHA_RE.match(sha or ""):
        raise _Refused("'sha' must be a hex object name (4-64 hex digits)")
    root, rel = _locate(path)
    return _show(root, rel, sha)


@router.api_route("/api/git/show", methods=["GET", "HEAD"])
async def api_git_show(path: str, sha: str, request: Request):
    """The file at `path` as of commit `sha`.

    HEAD answers the same headers with no body, which is how `fused.stat()` under
    `_rev` learns the size AT THAT REVISION (a live stat would report today's
    file). It costs the same read — there is no size in the object database to
    look up without inflating the blob — and it is bounded by the same cap.
    """
    try:
        data = await run_in_threadpool(_read, path, sha)
    except _Refused as e:
        return _error(e.message, status=e.status)
    media, _ = mimetypes.guess_type(path)
    headers = {
        "content-length": str(len(data)),
        # A revision read is immutable but never cached: a sha's content cannot
        # change, yet the pane is torn down the moment the user leaves it, so a
        # cache would only hold bytes nobody is going to ask for twice.
        "cache-control": "no-store",
    }
    body = b"" if request.method == "HEAD" else data
    # Same hardening as /api/fs/raw, for the same reason and with the same
    # threat: this route serves arbitrary on-disk bytes under a content type
    # guessed from a name, on the app's own origin.
    return _harden_raw(
        Response(content=body, media_type=media or "application/octet-stream",
                 headers=headers),
        request)
