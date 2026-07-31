"""Shell user-data dir (~/.fused-render) and atomic JSON I/O.

Shared foundation for every shell state backend: one home dir, one pair of
read/write helpers. Adding a resource = a new module that resolves a path
under home_dir() and uses read_json/write_json.

The dir also roots the user-template override channel under its templates/
subdir (server.py's USER_TEMPLATES_DIR = home_dir()/templates, D76): the home
holds bookmarks.json + templates/. server imports home_dir from here, never
the reverse (no server <-> shell import cycle).
"""
import json
import ntpath
import os
import tempfile


def home_dir() -> str:
    """User-data dir for shell state. FUSED_RENDER_HOME overrides the default
    ~/.fused-render — tests set it so they never touch the real home dir.

    When a branch ref is set (FUSED_RENDER_BRANCH, see fused_render._branch),
    all shell state (templates, bookmarks, prefs) nests under
    ~/.fused-render/branches/<ref>/ so parallel branches don't collide; baseline
    (no ref) is the unnested dir, byte-identical to today."""
    from fused_render._branch import branch_dir

    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return branch_dir(base)


def read_json(path: str):
    """Parse the JSON at `path`; return None if it is absent OR corrupt. The
    None-vs-value distinction lets a caller tell 'never written' from an empty
    resource (e.g. the bookmarks `exists` flag / one-time import gate)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def write_json(path: str, data) -> None:
    """Atomically write `data` as JSON to `path` (temp file in the same dir +
    os.replace), creating the home dir if needed. Last write wins — no locking
    (single local user, D3)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)  # atomic on the same filesystem
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ----------------------------------------------------------- sidecar mapping
#
# Every per-file sidecar (`<file>.json` next to a claude session, bookmark
# history, annotation log, ...) lives under home_dir()/sidecar/, mirroring the
# source's absolute path into a subtree instead of writing beside the file.
# This is what actually fixes the sidecar-write incident: a mount-backed
# source path never touches the sidecar write path at all anymore, because
# the sidecar isn't on the mount. Mirrored in templates/shared/appenv.py for
# template subprocesses, which cannot import this module — keep the two in
# step.

def _sidecar_subpath(abs_path: str) -> str:
    """Pure classification of an absolute path (Windows or POSIX-shaped) into
    a forward-slash-joined relative location under the sidecar subtree.

    Built on ntpath.splitdrive rather than os.path so this stays correct (and
    testable) for Windows-shaped input on any host, the same discipline
    _view_url_codec.py uses. A drive letter becomes its own single-letter
    folder ("C:\\Users\\..." -> "C/Users/..."), a UNC share nests under
    "unc/<server>/<share>/..." (a literal "\\\\server\\share" cannot be a
    filesystem entry, backslash is always a separator), and a POSIX path just
    drops its leading "/". Case is preserved exactly throughout: folding case
    would collide two distinct paths on a case-sensitive filesystem.
    """
    drive, tail = ntpath.splitdrive(abs_path)
    tail = tail.replace("\\", "/").lstrip("/")
    if not drive:
        return tail
    if drive.endswith(":"):
        return "/".join(filter(None, [drive[0].upper(), tail]))
    return "/".join(filter(None, ["unc", *drive.strip("\\").replace("\\", "/").split("/"), tail]))


def sidecar_path(file: str) -> str:
    """The `<file>.json` sidecar's new home: home_dir()/sidecar/<mapped path>.

    `file` is resolved with abspath (not realpath) so this matches the prior
    co-located behavior exactly: a symlink's own apparent location decides
    where its sidecar lives, not whatever it resolves to.
    """
    parts = [p for p in _sidecar_subpath(os.path.abspath(file)).split("/") if p]
    return os.path.join(home_dir(), "sidecar", *parts) + ".json"
