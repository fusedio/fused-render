import json
import os
import shutil
import stat as stat_mod
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from fastapi import APIRouter, Body, File, Form, Header, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from fused_render import calls as shell_calls
from fused_render.server.common import _error, _require_fused
from fused_render.server.gitignore import _is_repo_root
from fused_render.server.mount import _invalidate_stat_cache, _is_under_snapshot_root, _mount_probe, _mount_stat_payload, _mutation_result_payload, _probe_path, _stat_payload, _writable
from fused_render.server.walk import _mount_list_error_response
from fused_render.shell import storage as shell_storage


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


router = APIRouter()


def _is_under_sidecar_root(path: str) -> bool:
    """True when `path` sits under home_dir()/sidecar/ (D83-reversal). Those
    paths are server-derived (fused.sidecarPath), never user-typed, so an
    absent deep subtree there is expected on a first write — unlike the
    general "no mkdir -p" rule below, which exists so a typo'd arbitrary path
    can't silently spawn a deep tree."""
    root = os.path.abspath(os.path.join(shell_storage.home_dir(), "sidecar"))
    ap = os.path.abspath(path)
    return ap == root or ap.startswith(root + os.sep)


def _snapshot_refusal(*paths: str | None):
    """The 403 for any mutation whose WRITE side lands in a `history` snapshot
    tree (see mount._is_under_snapshot_root), or None.

    Reuses the `readonly` wire string rather than inventing one: runtime.js
    `writeFile` already turns it into a typed error the code editor renders as
    "Save failed: file is read-only", and the explorer's friendlyFsError already
    phrases it as `"<name>" is read-only — <verb> isn't allowed here.` A new
    string would have reached the user as an unhandled generic — and the point of
    the guard is that a refusal READS as a refusal, not as a save that did
    nothing.

    Callers pass every path they would MODIFY. A copy's source is deliberately not
    among them: read-only means read-only, not sealed, and taking a copy of an old
    revision somewhere the user owns is what a history view is for.
    """
    for p in paths:
        if isinstance(p, str) and p and os.path.isabs(p) and _is_under_snapshot_root(p):
            return JSONResponse({"error": "readonly"}, status_code=403)
    return None


def _fs_write(body: dict, x_fused: str | None):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    path = body.get("path")
    content = body.get("content")
    expected_mtime = body.get("expected_mtime")
    # New File / "must not clobber" callers set create=true: an existing path
    # is a 409 conflict (same wire string as rename/copy/mkdir) instead of a
    # silent overwrite.
    create = bool(body.get("create"))

    if not path or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    if not isinstance(content, str):
        return _error("'content' must be a string")
    # A `history` snapshot is history, not a file (see _is_under_snapshot_root).
    # Refused here, ahead of every other check, because there is no shape of this
    # request that is allowed.
    snap = _snapshot_refusal(path)
    if snap is not None:
        return snap
    parent = os.path.dirname(path)

    # Mount-backed target: gate on read-only-ness and answer existence/shape via
    # the rclone rcd BEFORE any kernel probe — a cold negative os.stat here is
    # the exact enumerate-the-whole-prefix call that wedges the mount.
    from fused_render.shell import mounts as shell_mounts
    if shell_mounts.is_mount_backed(path):
        # Read-only mount: refuse first, before touching anything (the same
        # "readonly" wire contract as the local guard below).
        if shell_mounts.mount_read_only(path):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            pr = _mount_probe(path)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout) as e:
            return _mount_list_error_response(parent, e)  # indeterminate -> 503
        if pr.exists and pr.is_dir:
            return _error(f"path is a directory: {path}")
        if not pr.parent_is_dir:
            return _error(f"parent directory does not exist: {parent}", status=404)
        if create and pr.exists:
            return JSONResponse({"error": "conflict"}, status_code=409)
        if expected_mtime is not None:
            if not pr.exists:
                return JSONResponse({"error": "conflict", "mtime": None},
                                    status_code=409)
            # Cross-source compare: expected_mtime is a KERNEL /api/fs/stat
            # st_mtime, but pr.mtime is the rclone rcd ModTime — the two round
            # a mount's timestamp differently and disagree sub-second, so the
            # 1e-6 tolerance the local branch uses would 409 every save on a
            # writable mount. Tolerate < 1s here; a larger gap is a real change.
            if pr.mtime is None or abs(pr.mtime - expected_mtime) >= 1.0:
                return JSONResponse({"error": "conflict", "mtime": pr.mtime},
                                    status_code=409)
        # The write itself goes through the rclone VFS (acceptable — it is the
        # negative/list probes, not the mutation, that wedge the mount): atomic
        # temp-write + os.replace in the parent, same as the local path. No mode
        # preservation (a remote object has no unix mode, and reading it would
        # be an extra kernel getattr on the mount).
        #
        # RESIDUAL RISK: tempfile.mkstemp(dir=parent) + os.replace still do
        # kernel negative LOOKUPs on the mount (as do os.mkdir/os.remove/
        # shutil.move in the sibling handlers) — the rc probe above answers
        # existence but does NOT warm the kernel dircache, so on a huge parent
        # these lookups can still trigger the full-prefix enumeration this
        # module exists to avoid. Follow-up: route the mutations themselves
        # through rclone rc operations (uploadfile / deletefile / movefile),
        # not the kernel VFS, so no mutation touches the mount through a LOOKUP.
        fd, tmp = tempfile.mkstemp(dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return _error(f"cannot write {path}: {e}", status=400)
        # Re-arm the client's optimistic lock from a fresh rc probe; fall back to
        # the written length if the rcd can't answer (never kernel-stat).
        try:
            after = _mount_probe(path)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout):
            after = None
        size = after.size if after and after.exists else len(content.encode("utf-8"))
        mtime = after.mtime if after and after.exists else None
        return _mount_stat_payload(path, False, size, mtime)

    if os.path.isdir(path):
        return _error(f"path is a directory: {path}")
    if not os.path.isdir(parent):
        if not _is_under_sidecar_root(path):
            return _error(f"parent directory does not exist: {parent}", status=404)
        os.makedirs(parent, exist_ok=True)

    # Read-only guard: refuse before touching anything. The atomic write
    # below replaces the target via the PARENT directory, so without this
    # check a chmod -w file would be silently overwritten. The bare "readonly"
    # error string is a wire contract — runtime.js writeFile turns it into a
    # typed error, like "conflict" below.
    if not _writable(path):
        return JSONResponse({"error": "readonly"}, status_code=403)

    # Optimistic lock: the editor sends the mtime it last saw; if the file
    # changed (or was deleted) underneath it, refuse so the edit doesn't
    # clobber someone else's write. Compare against the raw st_mtime float
    # that /api/fs/stat returns, with a tolerance for float round-tripping.
    exists = os.path.exists(path)
    if create and exists:
        return JSONResponse({"error": "conflict"}, status_code=409)
    if expected_mtime is not None:
        if not exists:
            return JSONResponse({"error": "conflict", "mtime": None}, status_code=409)
        current = os.stat(path).st_mtime
        if abs(current - expected_mtime) >= 1e-6:
            return JSONResponse({"error": "conflict", "mtime": current}, status_code=409)

    # Preserve the target's permission bits across an overwrite.
    mode = stat_mod.S_IMODE(os.stat(path).st_mode) if exists else None

    # Atomic write: land the bytes in a temp file in the same directory,
    # fsync, then os.replace onto the target so a reader never sees a
    # half-written file (and a crash leaves the original intact).
    fd, tmp = tempfile.mkstemp(dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return _error(f"cannot write {path}: {e}", status=400)

    return _stat_payload(path, False)


def _fs_upload(path: str | None, data: bytes, x_fused: str | None):
    # The binary sibling of _fs_write. _fs_write's contract is "content is a
    # string" (it encodes UTF-8 on the way out), so there was no way to land
    # arbitrary bytes — a pasted screenshot or video — on disk. Rather than
    # fork that function down the middle with a binary mode (its callers depend
    # on the optimistic lock and create-exclusive semantics, neither of which
    # means anything for a fresh pasted blob), this reuses the same guard
    # sequence and drops the text-only parts.
    #
    # Like _fs_write it does NOT create intermediate directories: a missing
    # parent is a 404, and the caller does its own mkdir first.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    if not path or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    snap = _snapshot_refusal(path)   # nothing is dropped into a snapshot tree
    if snap is not None:
        return snap
    parent = os.path.dirname(path)

    # Mount-backed target: read-only refusal first, then existence/shape via the
    # rclone rcd — never a kernel probe (see _fs_write's mount branch for why a
    # cold negative os.stat here is the call that wedges the mount).
    from fused_render.shell import mounts as shell_mounts
    if shell_mounts.is_mount_backed(path):
        if shell_mounts.mount_read_only(path):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            pr = _mount_probe(path)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout) as e:
            return _mount_list_error_response(parent, e)  # indeterminate -> 503
        if pr.exists and pr.is_dir:
            return _error(f"path is a directory: {path}")
        if not pr.parent_is_dir:
            return _error(f"parent directory does not exist: {parent}", status=404)
        written = _write_bytes_atomically(path, parent, data, mode=None)
        if written is not None:
            return written
        # Re-read size/mtime from the rcd (never a kernel stat); fall back to
        # what we just wrote if the rcd can't answer, exactly as _fs_write does.
        try:
            after = _mount_probe(path)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout):
            after = None
        size = after.size if after and after.exists else len(data)
        mtime = after.mtime if after and after.exists else None
        return _mount_stat_payload(path, False, size, mtime)

    if os.path.isdir(path):
        return _error(f"path is a directory: {path}")
    if not os.path.isdir(parent):
        return _error(f"parent directory does not exist: {parent}", status=404)
    # Read-only guard, same "readonly" wire contract as _fs_write: the atomic
    # replace below goes through the PARENT, so without this a chmod -w file
    # would be silently overwritten.
    if not _writable(path):
        return JSONResponse({"error": "readonly"}, status_code=403)

    exists = os.path.exists(path)
    mode = stat_mod.S_IMODE(os.stat(path).st_mode) if exists else None
    written = _write_bytes_atomically(path, parent, data, mode)
    if written is not None:
        return written
    return _stat_payload(path, False)


def _write_bytes_atomically(path: str, parent: str, data: bytes, mode: int | None):
    # Land the bytes in a temp file beside the target, fsync, then os.replace,
    # so a reader never sees a half-written file and a crash leaves the
    # original intact. Returns None on success, or the error response to
    # return — the two callers above differ only in the payload they build.
    fd, tmp = tempfile.mkstemp(dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return _error(f"cannot write {path}: {e}", status=400)
    return None


def _fs_mkdir(body: dict, x_fused: str | None):
    # Create a single directory. Parents are NOT auto-created (no mkdir -p):
    # a missing parent is a 400 so a typo'd path can't silently spawn a deep
    # tree. Mirrors _fs_write's guard order — X-Fused, absolute path, then
    # the filesystem-shape checks — and returns the /api/fs/stat payload so
    # the client can render the new folder without a follow-up stat.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    path = body.get("path")
    if not path or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    snap = _snapshot_refusal(path)   # a snapshot tree grows no new directories
    if snap is not None:
        return snap
    parent = os.path.dirname(path)

    # Mount-backed target: read-only refusal first, then existence/shape via the
    # rclone rcd — never a kernel probe (see _fs_write's mount branch).
    from fused_render.shell import mounts as shell_mounts
    if shell_mounts.is_mount_backed(path):
        if shell_mounts.mount_read_only(path):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            pr = _mount_probe(path)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout) as e:
            return _mount_list_error_response(parent, e)  # indeterminate -> 503
        if not pr.parent_is_dir:
            return _error(f"parent directory does not exist: {parent}")
        if pr.exists:
            return JSONResponse({"error": "conflict"}, status_code=409)
        try:
            os.mkdir(path)  # through the rclone VFS
        except OSError as e:
            return _error(f"cannot create directory {path}: {e}")
        return _mount_stat_payload(path, True, None, None)

    if not os.path.isdir(parent):
        return _error(f"parent directory does not exist: {parent}")
    if os.path.exists(path):
        return JSONResponse({"error": "conflict"}, status_code=409)
    # Read-only guard: the "readonly" wire string matches _fs_write's — the
    # parent must accept a new entry (_writable falls back to the parent's
    # W_OK for a path that does not yet exist).
    if not _writable(path):
        return JSONResponse({"error": "readonly"}, status_code=403)

    try:
        os.mkdir(path)
    except OSError as e:
        return _error(f"cannot create directory {path}: {e}")
    return _stat_payload(path, True)


# Compress: the only archive formats the endpoint will ever produce, mapped to
# the extension the sibling archive gets. An allowlist rather than a passthrough
# because the value picks the code path AND (for the git formats) part of a
# command line — anything not spelled here is a 400, never an argument.
_ARCHIVE_EXT = {"zip": ".zip", "git-bundle": ".bundle", "git-archive": ".tar.gz"}

# Long enough for a big repository on a slow disk, short enough that a hung git
# (a credential prompt, a wedged filesystem) fails the request instead of
# pinning a threadpool worker forever.
_GIT_TIMEOUT_S = 300

_GIT_ENV = {
    # Never let git stop for input: an archive is a background action with no
    # UI to answer a prompt, so a repo needing credentials must fail fast.
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
}


def _run_git(args: list[str], cwd: str):
    """Run git with an argv list (never a shell), no stdin, and a timeout.
    Returns the CompletedProcess, or None when git is missing/hung.

    `cwd` becomes `-C <cwd>` in the argv rather than a `cwd=` kwarg, and that is
    load-bearing: `cwd=` forces CPython onto the fork path, where a process with
    libproj resident dies in PROJ's atfork handler before exec (rc -11, silently).
    `-C` is also stricter — git chdirs there itself, so this process's working
    directory cannot change the answer. Callers therefore pass their own args
    WITHOUT a leading `-C`.
    """
    try:
        return subprocess.run(
            [_git_bin(), "--no-pager", "-C", cwd, *args],
            close_fds=False,
            env={**os.environ, **_GIT_ENV},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_GIT_TIMEOUT_S,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if sys.platform == "win32" else 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _has_any_ref(src: str) -> bool:
    """Whether the repo at `src` has at least one ref — what `git bundle --all`
    actually needs. `for-each-ref --count=1` exits 0 whether or not anything
    matches and prints a refname only when one exists, so emptiness is read off
    stdout rather than the exit code."""
    refs = _run_git(["for-each-ref", "--count=1",
                     "--format=%(refname)"], src)
    return refs is not None and refs.returncode == 0 and bool(refs.stdout.strip())


def _zip_tree(src: str, tmp: str, dest: str) -> None:
    """Write `src` (as a single top-level folder) into the zip at `tmp`.

    Symlinks are STORED as symlinks — the target string as the entry body,
    S_IFLNK in the external attrs, the convention Info-ZIP/`unzip` use — never
    followed. That is what keeps a link to something outside the folder from
    silently inlining its bytes, and what makes a self-referential directory
    link a single entry instead of an infinite descent (os.walk itself is
    followlinks=False, so a directory link is never recursed into either way).
    Empty directories get their own entry so the tree round-trips. Anything
    that is neither a file, a directory nor a symlink (sockets, fifos, devices)
    is skipped: there is no meaningful zip representation for it."""
    base = os.path.basename(src.rstrip(os.sep)) or src
    # The archive being written must never end up inside itself. Normally it is
    # a sibling and this is moot; an explicit `dest` inside `src` is not.
    skip = {os.path.abspath(tmp), os.path.abspath(dest)}

    # Zip entry names are always "/"-separated, whatever the host separator is.
    def arcname(*parts: str) -> str:
        return "/".join(p for p in os.path.join(*parts).split(os.sep) if p != "")

    def add_symlink(zf: zipfile.ZipFile, full: str, arc: str) -> None:
        info = zipfile.ZipInfo(arc)
        info.create_system = 3  # Unix, so the mode bits below are read back
        info.external_attr = (stat_mod.S_IFLNK | 0o777) << 16
        zf.writestr(info, os.readlink(full))

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src, followlinks=False):
            rel = os.path.relpath(root, src)
            arc_root = base if rel == os.curdir else arcname(base, rel)
            # Pull symlinked directories out of the walk and store them as
            # links (leaving them in `dirs` would only stat them again).
            links = [d for d in dirs if os.path.islink(os.path.join(root, d))]
            dirs[:] = [d for d in dirs if d not in links]
            for name in links:
                add_symlink(zf, os.path.join(root, name), arcname(arc_root, name))
            # An empty directory has no member to imply it, so it needs an
            # entry of its own or it does not survive extraction. That includes
            # the TOP directory: excluding `root == src` here meant compressing
            # an empty folder produced a zip with zero members, which extracts
            # to nothing at all — the folder simply vanished. A non-empty
            # directory is implied by its children's paths and gets no entry,
            # so this can never duplicate one.
            if not dirs and not files and not links:
                zf.writestr(zipfile.ZipInfo(arc_root + "/"), b"")  # empty dir
            for name in files:
                full = os.path.join(root, name)
                if os.path.abspath(full) in skip:
                    continue
                arc = arcname(arc_root, name)
                if os.path.islink(full):
                    add_symlink(zf, full, arc)
                elif os.path.isfile(full):
                    zf.write(full, arc)


def _fs_compress(body: dict, x_fused: str | None):
    """Archive a FOLDER into a sibling file (Finder's Compress, plus the two
    git formats the shell offers on a repository root).

    Same guard order as _fs_mkdir — X-Fused, absolute path, mount branch, then
    the filesystem-shape checks — and the same wire contract ("readonly",
    "conflict"), so the client's friendlyFsError needs no special cases for it.
    The archive is always built to a temp file in the destination's own
    directory and renamed into place, so a failure part-way through leaves
    nothing at the final path for the user (or the next listing) to find."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    path = body.get("path")
    if not path or not isinstance(path, str) or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    fmt = body.get("format")
    if fmt not in _ARCHIVE_EXT:
        return _error("'format' must be one of: " + ", ".join(sorted(_ARCHIVE_EXT)))

    src = path.rstrip(os.sep) or path
    dest = body.get("dest") or src + _ARCHIVE_EXT[fmt]
    if not isinstance(dest, str) or not os.path.isabs(dest):
        return _error("'dest' must be an absolute filesystem path")
    # `dest` only: zipping a snapshot tree somewhere the user owns is a read.
    snap = _snapshot_refusal(dest)
    if snap is not None:
        return snap
    parent = os.path.dirname(dest)

    # Mount branch, BEFORE any kernel stat of either end. Compressing across a
    # mount is not supported at all: reading the source means a recursive walk
    # of the remote prefix (the known mount-wedger), and writing the archive
    # means streaming the whole thing back up through the rclone VFS cache.
    # Read-only mounts keep the shared "readonly" wire string; a writable one
    # gets an explicit refusal rather than a walk that would hang the mount.
    from fused_render.shell import mounts as shell_mounts
    if shell_mounts.is_mount_backed(src) or shell_mounts.is_mount_backed(dest):
        if shell_mounts.mount_read_only(dest) or shell_mounts.mount_read_only(src):
            return JSONResponse({"error": "readonly"}, status_code=403)
        return _error("compress unsupported on mounted folders")

    if not os.path.exists(src):
        return _error(f"no such file or directory: {src}", status=404)
    if not os.path.isdir(src):
        return _error(f"not a directory: {src}")
    if not os.path.isdir(parent):
        return _error(f"parent directory does not exist: {parent}")
    if os.path.exists(dest):
        return JSONResponse({"error": "conflict"}, status_code=409)
    if not _writable(dest):
        return JSONResponse({"error": "readonly"}, status_code=403)

    if fmt != "zip":
        # The git formats archive the whole repository, so they are only ever
        # offered — and only ever accepted — at a work-tree ROOT; a
        # subdirectory would silently hand back far more than was asked for.
        if not _is_repo_root(src):
            return _error(f"not a git repository: {src}")
        # Both formats fail on a repo with nothing to archive, but they fail on
        # DIFFERENT preconditions and must not share one preflight.
        #
        # `bundle create --all` packs every ref and never looks at HEAD, so the
        # thing it needs is "at least one ref". `archive HEAD` needs precisely
        # HEAD. They come apart on an unborn HEAD — `git checkout --orphan` (or
        # a fresh `git switch -c`) leaves HEAD on a branch with no commit while
        # the real history sits on other refs. Asking `rev-parse --verify HEAD`
        # for both is what wrongly told such a repo it had no commits and
        # refused to bundle history it was perfectly able to pack.
        #
        # `for-each-ref --count=1` exits 0 either way and prints a refname only
        # when one exists, so emptiness is read off stdout, not the exit code.
        if fmt == "git-bundle":
            if not _has_any_ref(src):
                return _error(f"{src} has no commits yet")
        else:
            head = _run_git(["rev-parse", "--verify", "HEAD"], src)
            if head is None or head.returncode != 0:
                # A repo with no refs at all really has no commits; one whose
                # HEAD alone is unborn is full of history that just isn't
                # reachable from HEAD right now. Two different fixes for the
                # user, so two different sentences.
                return _error(f"{src} has no commits yet" if not _has_any_ref(src)
                              else f"{src} has no commit checked out")

    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".compress-", suffix=_ARCHIVE_EXT[fmt])
    os.close(fd)  # both zipfile and git want to open the path themselves
    try:
        if fmt == "zip":
            try:
                _zip_tree(src, tmp, dest)
            except OSError as e:
                return _error(f"cannot compress {src}: {e}")
        else:
            args = (["bundle", "create", tmp, "--all"] if fmt == "git-bundle"
                    else ["archive", "--format=tar.gz", "-o", tmp, "HEAD"])
            proc = _run_git(args, src)
            if proc is None:
                return _error(f"cannot compress {src}: git is unavailable or timed out")
            if proc.returncode != 0:
                detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
                return _error(f"cannot compress {src}: "
                              + (detail[-1] if detail else f"git exited {proc.returncode}"))
        try:
            os.replace(tmp, dest)
        except OSError as e:
            return _error(f"cannot compress {src}: {e}")
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return _stat_payload(dest, False)


def _trash_supported() -> bool:
    # Move-to-Trash is macOS-only (a ~/.Trash + Finder concept). Isolated so
    # tests can force it on/off without touching the global sys.platform.
    return sys.platform == "darwin"


def _trash_dest_name(name: str, counter: int) -> str:
    # Finder-style dedupe for a name already present in ~/.Trash: the first
    # occurrence keeps its name, later ones gain a " N" suffix before the
    # extension ("report.csv" -> "report 2.csv"); dirs / extensionless /
    # dotfile names take the suffix at the end ("folder" -> "folder 2").
    if counter <= 1:
        return name
    dot = name.rfind(".")
    if dot > 0:
        return f"{name[:dot]} {counter}{name[dot:]}"
    return f"{name} {counter}"


def _move_to_trash(path: str) -> str | None:
    # Move `path` into the user's ~/.Trash (macOS). A plain os.rename into
    # ~/.Trash is the fast path, with a " N" dedupe suffix when a name is
    # already there. A rename ACROSS devices (or any other OSError) can't be
    # done by rename, so it falls back to Finder via osascript, which copies +
    # removes itself. Raises on total failure so the caller reports it and the
    # frontend can fall back to a hard delete.
    #
    # RETURNS THE DESTINATION IT RENAMED TO, or None when Finder did the move.
    # The destination is what makes a trash delete undoable: on the rename path
    # WE chose it, so it is a path the caller can rename the entry back out of
    # — the same symmetric pair a move records. In the cross-device fallback
    # FINDER chooses the location, so we cannot name it, and an unnamed
    # destination must not be recorded as an undoable pair (a guess would put
    # an undo request on a path nothing is at).
    trash = Path.home() / ".Trash"
    name = os.path.basename(path.rstrip("/"))
    try:
        trash.mkdir(parents=True, exist_ok=True)
        counter = 1
        dest = trash / _trash_dest_name(name, counter)
        while dest.exists():
            counter += 1
            dest = trash / _trash_dest_name(name, counter)
        os.rename(path, dest)
        return str(dest)
    except OSError:
        subprocess.run(
            [
                "osascript",
                "-e",
                f"tell application \"Finder\" to delete POSIX file {json.dumps(path)}",
            ],
            check=True,
            capture_output=True,
        )
        return None


def _fs_delete(body: dict, x_fused: str | None):
    # Remove a file or directory. With trash=true the target is moved to the
    # user's Trash instead of being erased (recoverable, macOS only). Otherwise
    # a hard delete: a directory needs recursive=true unless it is empty (an
    # empty dir is a plain os.rmdir); a non-empty dir without the flag is a 409
    # so a stray click can't wipe a subtree. Read-only targets are refused with
    # the same "readonly" contract as _fs_write.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    path = body.get("path")
    recursive = bool(body.get("recursive", False))
    trash = bool(body.get("trash", False))
    if not path or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    # The snapshot tree IS the record, so this covers the root as well as the
    # files in it. Reclaiming disk under app-versions/ belongs to whatever
    # manages that cache, not to an /api/fs/delete caller.
    snap = _snapshot_refusal(path)
    if snap is not None:
        return snap

    # Mount-backed target: read-only refusal first; then answer shape via the
    # rclone rcd. A DIRECTORY delete (the non-recursive os.listdir emptiness
    # check or a recursive shutil.rmtree) would kernel-enumerate/walk the remote
    # tree — refused, out of scope. A single-file delete goes through the VFS.
    from fused_render.shell import mounts as shell_mounts
    if shell_mounts.is_mount_backed(path):
        if shell_mounts.mount_read_only(path):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            pr = _mount_probe(path)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout) as e:
            return _mount_list_error_response(os.path.dirname(path), e)  # 503
        if not pr.exists:
            return _error(f"no such file or directory: {path}", status=404)
        if pr.is_dir:
            return _error(
                "cannot delete a directory on a remote mount: directory-tree "
                "operations are not supported over mounts", status=400)
        if trash:
            # Move-to-Trash lifts the file OFF the mount, which reads the whole
            # file through the kernel; report it unsupported so the client
            # falls back to the confirm-then-hard-delete flow (same 501 signal
            # a non-darwin platform returns).
            return JSONResponse({"error": "trash unsupported"}, status_code=501)
        try:
            os.remove(path)  # single VFS unlink
        except OSError as e:
            return _error(f"cannot delete {path}: {e}")
        return {"deleted": path, "trashed": False}

    if not os.path.exists(path):
        return _error(f"no such file or directory: {path}", status=404)
    if not _writable(path):
        return JSONResponse({"error": "readonly"}, status_code=403)

    if trash:
        # Non-darwin (or Trash otherwise unavailable) → a distinct 501 so the
        # frontend can fall back to the confirm-then-hard-delete flow.
        if not _trash_supported():
            return JSONResponse({"error": "trash unsupported"}, status_code=501)
        try:
            dest = _move_to_trash(path)
        except Exception as e:  # noqa: BLE001 — rename OSError or osascript failure
            # A FAILED trash on a supported platform is a plain error, not the
            # 501 "unsupported" signal — that one routes the client into the
            # irreversible hard-delete fallback, which must never be the
            # response to a recoverable-delete attempt that merely failed.
            return _error(f"cannot move to Trash: {e}", status=500)
        # `trashed_to` is reported ONLY when we named the destination ourselves
        # (the os.rename path). It is what lets the client record the delete as
        # an undoable rename pair — see _move_to_trash. Omitted after the Finder
        # fallback, where the destination is unknown, and never present on a
        # hard delete, whose inverse would be data destruction.
        out: dict = {"deleted": path, "trashed": True}
        if dest is not None:
            out["trashed_to"] = dest
        return out

    try:
        # A symlink is removed as the link itself, never followed: rmtree on a
        # symlink-to-dir raises, and following it would delete the TARGET's
        # contents. Mirrors the `not os.path.islink` guard _fs_rename/_fs_copy
        # apply before their own rmtree.
        if os.path.isdir(path) and not os.path.islink(path):
            if recursive:
                shutil.rmtree(path)
            elif os.listdir(path):
                return JSONResponse(
                    {"error": "conflict", "message": "directory not empty"},
                    status_code=409,
                )
            else:
                os.rmdir(path)
        else:
            os.remove(path)
    except OSError as e:
        return _error(f"cannot delete {path}: {e}")
    return {"deleted": path, "trashed": False}


def _fs_rename(body: dict, x_fused: str | None):
    # Move/rename src -> dst. dst must be absolute and its parent writable
    # (same "outside"/readonly guards as elsewhere). An existing dst is a 409
    # unless overwrite=true; a missing src is a 404. shutil.move handles the
    # cross-device case os.replace can't; overwrite removes dst first so a
    # dir-over-dir move can't nest into it.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    src = body.get("src")
    dst = body.get("dst")
    overwrite = bool(body.get("overwrite", False))
    if not src or not os.path.isabs(src):
        return _error("'src' must be an absolute filesystem path")
    if not dst or not os.path.isabs(dst):
        return _error("'dst' must be an absolute filesystem path")
    dst_parent = os.path.dirname(dst)
    # BOTH sides: a move deletes src as well as writing dst, so moving a revision
    # OUT of a snapshot tree destroys the record just as surely as moving one in
    # falsifies it. (Copy, below, guards dst only — see _snapshot_refusal.)
    snap = _snapshot_refusal(src, dst)
    if snap is not None:
        return snap

    # A mount is involved on either side: gate mount-safely BEFORE any kernel
    # probe. A move deletes src and writes dst, so a read-only mount on EITHER
    # side refuses (readonly first, as the mount contract). Existence/shape is
    # answered through the rclone rcd; a DIRECTORY on a mount side is refused
    # (a rmtree/copytree-style walk of a remote tree is out of scope).
    from fused_render.shell import mounts as shell_mounts
    if shell_mounts.is_mount_backed(src) or shell_mounts.is_mount_backed(dst):
        if shell_mounts.mount_read_only(src) or shell_mounts.mount_read_only(dst):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            src_pr = _probe_path(src)
            dst_pr = _probe_path(dst)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout) as e:
            return _mount_list_error_response(
                os.path.dirname(src) if shell_mounts.is_mount_backed(src)
                else dst_parent, e)
        if not src_pr.exists:
            return _error(f"no such file or directory: {src}", status=404)
        if src_pr.is_dir or (dst_pr.exists and dst_pr.is_dir):
            return _error(
                "cannot move a directory to or from a remote mount: "
                "directory-tree operations are not supported over mounts",
                status=400)
        if not dst_pr.parent_is_dir:
            return _error(f"parent directory does not exist: {dst_parent}")
        if dst_pr.exists and not overwrite:
            return JSONResponse({"error": "conflict"}, status_code=409)
        # The mount read-only gate above only covers the mount side(s). A LOCAL
        # side still needs the ordinary _writable check: a move deletes src and
        # writes dst, so a chmod-protected local src or a non-writable local dst
        # must 403 "readonly" (same contract as the all-local branch below).
        # Never _writable a mount side — for a writable mount that kernel-probes
        # W_OK on the mount, the exact stat this whole path exists to avoid.
        if not shell_mounts.is_mount_backed(src) and not _writable(src):
            return JSONResponse({"error": "readonly"}, status_code=403)
        if not shell_mounts.is_mount_backed(dst) and not _writable(dst):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            if dst_pr.exists:
                os.remove(dst)  # single file (a dir dst was refused above)
            shutil.move(src, dst)
        except OSError as e:
            return _error(f"cannot rename {src} -> {dst}: {e}")
        return _mutation_result_payload(dst, False)

    # dst's parent must already exist — a rename never creates intermediate
    # dirs. Without this, a missing parent falls through to _writable (which
    # walks up to the nearest existing ancestor) and surfaces a misleading
    # "readonly" 403; a 400 is the honest error, same as _fs_write/_fs_mkdir.
    if not os.path.isdir(dst_parent):
        return _error(f"parent directory does not exist: {dst_parent}")
    if not os.path.exists(src):
        return _error(f"no such file or directory: {src}", status=404)
    if os.path.isdir(src):
        # Same self/descendant guard as copy: moving a directory into itself
        # (or a child) would build the destination inside the source.
        s = os.path.abspath(src)
        d = os.path.abspath(dst)
        if d == s or d.startswith(s + os.sep):
            return _error("cannot move a directory into itself or a descendant")

    dst_exists = os.path.exists(dst)
    if dst_exists and not overwrite:
        return JSONResponse({"error": "conflict"}, status_code=409)
    # A move deletes the source, so the source must be writable too — otherwise
    # a rename could lift entries off a read-only mount (delete/write refuse).
    if not _writable(src) or not _writable(dst):
        return JSONResponse({"error": "readonly"}, status_code=403)

    try:
        if dst_exists:
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            else:
                os.remove(dst)
        shutil.move(src, dst)
    except OSError as e:
        return _error(f"cannot rename {src} -> {dst}: {e}")
    return _stat_payload(dst, os.path.isdir(dst))


def _fs_copy(body: dict, x_fused: str | None):
    # Copy src -> dst. File via copy2 (preserves metadata), dir via copytree.
    # Same error contract as rename (400 relative, 404 missing src, 409 dst
    # exists w/o overwrite, 403 readonly dst). Copying a directory into itself
    # or a descendant is refused (400) — copytree would otherwise recurse into
    # the destination it is still writing.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    src = body.get("src")
    dst = body.get("dst")
    overwrite = bool(body.get("overwrite", False))
    if not src or not os.path.isabs(src):
        return _error("'src' must be an absolute filesystem path")
    if not dst or not os.path.isabs(dst):
        return _error("'dst' must be an absolute filesystem path")
    dst_parent = os.path.dirname(dst)
    # `dst` only: read-only means read-only, not sealed, and copying an old
    # revision out to somewhere the user owns is what a history view is for.
    snap = _snapshot_refusal(dst)
    if snap is not None:
        return snap

    # A mount is involved on either side: gate mount-safely BEFORE any kernel
    # probe. A copy writes dst (only the dst mount must be writable — readonly
    # first, as the mount contract) and never modifies src. A DIRECTORY on a
    # mount side is refused (a copytree walk of a remote tree is out of scope);
    # a single-file copy proceeds (its sequential read/write is slow, not fatal).
    from fused_render.shell import mounts as shell_mounts
    if shell_mounts.is_mount_backed(src) or shell_mounts.is_mount_backed(dst):
        if shell_mounts.mount_read_only(dst):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            src_pr = _probe_path(src)
            dst_pr = _probe_path(dst)
        except (shell_mounts.RcListUnavailable, shell_mounts.RcListTimeout) as e:
            return _mount_list_error_response(
                os.path.dirname(src) if shell_mounts.is_mount_backed(src)
                else dst_parent, e)
        if not src_pr.exists:
            return _error(f"no such file or directory: {src}", status=404)
        if src_pr.is_dir or (dst_pr.exists and dst_pr.is_dir):
            return _error(
                "cannot copy a directory to or from a remote mount: "
                "directory-tree operations are not supported over mounts",
                status=400)
        if not dst_pr.parent_is_dir:
            return _error(f"parent directory does not exist: {dst_parent}")
        if dst_pr.exists and not overwrite:
            return JSONResponse({"error": "conflict"}, status_code=409)
        # See _fs_rename: the mount read-only gate covers only the mount side. A
        # copy writes dst (never src), so a LOCAL dst still needs _writable —
        # matching the all-local branch, which checks dst only. Never _writable a
        # mount side (it kernel-stats a writable mount).
        if not shell_mounts.is_mount_backed(dst) and not _writable(dst):
            return JSONResponse({"error": "readonly"}, status_code=403)
        try:
            if dst_pr.exists:
                os.remove(dst)  # single file (a dir dst was refused above)
            shutil.copy2(src, dst)
        except OSError as e:
            return _error(f"cannot copy {src} -> {dst}: {e}")
        return _mutation_result_payload(dst, False)

    # dst's parent must already exist — a copy never creates intermediate dirs.
    # Without this, a missing parent falls through to _writable (which walks up
    # to the nearest existing ancestor) and surfaces a misleading "readonly"
    # 403; a 400 is the honest error, same as _fs_write/_fs_mkdir.
    if not os.path.isdir(dst_parent):
        return _error(f"parent directory does not exist: {dst_parent}")
    if not os.path.exists(src):
        return _error(f"no such file or directory: {src}", status=404)

    src_is_dir = os.path.isdir(src)
    if src_is_dir:
        # Normalize both ends so "self/descendant" catches ./ and trailing
        # slashes; the sep suffix stops /a/b matching /a/bc.
        s = os.path.abspath(src)
        d = os.path.abspath(dst)
        if d == s or d.startswith(s + os.sep):
            return _error("cannot copy a directory into itself or a descendant")

    dst_exists = os.path.exists(dst)
    if dst_exists and not overwrite:
        return JSONResponse({"error": "conflict"}, status_code=409)
    if not _writable(dst):
        return JSONResponse({"error": "readonly"}, status_code=403)

    try:
        if src_is_dir:
            if dst_exists:
                if os.path.isdir(dst) and not os.path.islink(dst):
                    shutil.rmtree(dst)
                else:
                    os.remove(dst)
            shutil.copytree(src, dst)
        else:
            # copy2 onto an existing dir would drop the file inside it; a
            # dir dst must be replaced wholesale to mean "become this file".
            if dst_exists and os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            shutil.copy2(src, dst)
    except OSError as e:
        return _error(f"cannot copy {src} -> {dst}: {e}")
    return _stat_payload(dst, os.path.isdir(dst))


# Every mutation endpoint invalidates the /api/fs/stat cache for the paths it
# touches (and their parents, via _invalidate_stat_cache) so the editor's
# immediate post-mutation stat re-reads fresh metadata. Invalidation runs
# unconditionally after the handler — a no-op on error/409 costs nothing, and
# doing it here (not inside each _fs_* helper's many return branches) keeps
# the contract in one obvious place per route.
#
# RESIDUAL: a RECURSIVE delete / overwriting rename of a directory does not
# walk the (now-gone) subtree to evict individually-cached child stats. Those
# entries simply age out within _STAT_TTL_S — the same bounded staleness the
# cache accepts for out-of-band changes — and the editor navigates top-down,
# so it re-lists the parent (fresh) before it would re-stat a vanished child.
@router.post("/api/fs/write")
def api_fs_write(request: Request, body: dict = Body(...),
                 x_fused: str | None = Header(default=None)):
    result = _fs_write(body, x_fused)
    _invalidate_stat_cache(body.get("path"))
    # What the app wrote and how big — never the content (calls.py).
    # `_fs_write` returns a stat payload on success and a JSONResponse on
    # every refusal, so the status has to come off the response object.
    shell_calls.enrich_write(
        getattr(request.state, "fused_call", None),
        path=body.get("path") if isinstance(body.get("path"), str) else "",
        content=body.get("content"),
        status=getattr(result, "status_code", 200),
        # Both refusals are 403; only a read-only target is `readonly`.
        # Re-asking the guard rather than re-spelling `x_fused != "1"` here,
        # so there is still one rule (it allocates nothing when it passes).
        unauthorized=_require_fused(x_fused) is not None,
    )
    return result

@router.post("/api/fs/upload")
async def api_fs_upload(request: Request, file: UploadFile = File(...),
                        path: str = Form(...),
                        x_fused: str | None = Header(default=None)):
    # Multipart rather than base64-in-JSON: base64 inflates a payload by a
    # third, which is irrelevant for a screenshot and very relevant for a
    # pasted video. python-multipart is already a core dependency and
    # templates_api.api_import_templates is the existing UploadFile precedent.
    data = await file.read()
    result = _fs_upload(path, data, x_fused)
    _invalidate_stat_cache(path)
    # A binary write is a write: it belongs in the call log for the same reason
    # /api/fs/write does — "what did my page put on disk" is a real question,
    # and a pasted screenshot would otherwise be the one mutation that leaves
    # no trace. Path and byte count only, never the bytes (calls.py). Refusals
    # are read off the response object, since the helper answers a stat payload
    # on success and a JSONResponse on every refusal.
    shell_calls.enrich_write(
        getattr(request.state, "fused_call", None),
        path=path if isinstance(path, str) else "",
        content=data,
        status=getattr(result, "status_code", 200),
        # Both refusals are 403; only a read-only target is `readonly`.
        unauthorized=_require_fused(x_fused) is not None,
    )
    return result

@router.post("/api/fs/mkdir")
def api_fs_mkdir(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    result = _fs_mkdir(body, x_fused)
    _invalidate_stat_cache(body.get("path"))
    return result

@router.post("/api/fs/compress")
def api_fs_compress(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    result = _fs_compress(body, x_fused)
    # Only the archive appears; the folder it was made from is untouched, so
    # (like copy) its cached stat stays valid.
    _invalidate_stat_cache(_compress_dest(body))
    return result

def _compress_dest(body: dict) -> str | None:
    # The path the archive lands at, mirroring _fs_compress's own default, so
    # the cache invalidation names the file that actually changed rather than
    # the folder that didn't.
    dest = body.get("dest")
    if isinstance(dest, str) and dest:
        return dest
    path, fmt = body.get("path"), body.get("format")
    if isinstance(path, str) and path and fmt in _ARCHIVE_EXT:
        return path.rstrip(os.sep) + _ARCHIVE_EXT[fmt]
    return None

@router.post("/api/fs/delete")
def api_fs_delete(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    result = _fs_delete(body, x_fused)
    _invalidate_stat_cache(body.get("path"))
    return result

@router.post("/api/fs/rename")
def api_fs_rename(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    result = _fs_rename(body, x_fused)
    # A move changes both ends: src disappears, dst appears.
    _invalidate_stat_cache(body.get("src"), body.get("dst"))
    return result

@router.post("/api/fs/copy")
def api_fs_copy(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    result = _fs_copy(body, x_fused)
    # A copy only writes dst; src is untouched, so its cached stat stays valid.
    _invalidate_stat_cache(body.get("dst"))
    return result
