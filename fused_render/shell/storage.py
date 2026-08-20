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
import os
import tempfile
import time


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
        _replace_atomic(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# A handful of quick retries, not a real backoff schedule: the sharing
# violation this chases clears as soon as the OTHER writer's own os.replace
# finishes, which is microseconds, not seconds.
_REPLACE_RETRIES = 8
_REPLACE_RETRY_DELAY_S = 0.02


def _replace_atomic(tmp: str, path: str) -> None:
    """os.replace(tmp, path), with a brief retry on Windows only.

    POSIX rename is atomic and never raises for a concurrent replace — that is
    what lets write_json promise "last write wins" with no locking above. The
    identical call on Windows is backed by MoveFileExW, which CAN raise
    PermissionError ([WinError 5] "Access is denied") for a few milliseconds
    while another writer's os.replace (or a reader's plain open(), which grants
    no FILE_SHARE_DELETE by default) still holds the destination — a transient
    sharing violation, not a real permissions problem. Two concurrent
    write_json calls to the SAME path (rcd.json under racing threads is the
    known case) can hit this on every real Windows run.

    Retrying rides that out and gives Windows the same "last write wins,
    the call itself always succeeds" contract POSIX gets for free — a dropped
    write here is not "last write wins", it is data loss. Gated on os.name so
    POSIX keeps the exact single-call behavior it always had."""
    if os.name != "nt":
        os.replace(tmp, path)
        return
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_S)


