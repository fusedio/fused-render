import os
import stat as stat_mod

from fused_render.server.common import _error
from fused_render.server import templates as _server_templates




def _is_under_snapshot_root(path: str) -> bool:
    """True when `path` sits under home_dir()/app-versions/ — materialised git
    snapshots.

    A snapshot tree is `git archive` output: the app folder enclosing a
    previewed path, extracted at one commit so the preview can be framed
    against real files (`server/routers/git_snapshot.py`). Nothing a user
    types there can mean anything — the real files are elsewhere and the
    commit is immutable — and a `<key>/<sha>/` tree is REUSED once it exists
    (the extractor's own cache), so a write that lands here would be served
    back as that revision's content from then on. Machine-generated history
    that silently absorbs edits is worse than no history at all.

    THIS HAS A PRODUCER AGAIN. `git_snapshot.py` is this guard's second
    producer: the first (a per-path timeline mode) was retired, and for a
    while nothing wrote here at all — the git view that replaced it resolved
    a revision's bytes on read (`server/routers/git_show.py`, since deleted)
    with nothing materialised. That design could not make a `.py` reader
    truthful without changing the runtime's read contract, which is why
    extraction came back: every read under an app folder — `readFile`,
    `rawUrl`, `stat`, and now `runPython` too — resolves against a REAL file
    under this root while a page carries `_snapshot=<sha>`. The guard's job
    is unchanged either way: nothing here is ever writable through `/api/fs`.

    Lives here rather than in fs_mutate so `_writable` can read it without a cycle
    (fs_mutate imports this module, not the other way round) — and _writable is
    half the answer: the mutation handlers refuse, and the stat payload's
    `writable: false` is what lets the framed editor render read-only mode UP
    FRONT instead of only failing at Cmd+S.

    Resolved per call, not at import: home_dir() depends on FUSED_RENDER_HOME and
    the branch ref (fused_render._branch), and a frozen value would guard a
    directory the server is not using. Segment-compared, so a sibling named
    `app-versions-notes` is not caught by a string prefix.

    The directory is spelled `app-versions` rather than something that says
    "git snapshot" — a naming mismatch kept from the ORIGINAL producer, before
    it was retired and revived. Renaming it now would orphan every tree the
    current producer has already cached under the old name.
    """
    from fused_render.shell import storage as shell_storage

    root = os.path.abspath(os.path.join(shell_storage.home_dir(), "app-versions"))
    ap = os.path.abspath(path)
    return ap == root or ap.startswith(root + os.sep)


def _writable(path: str) -> bool:
    """True iff /api/fs/write would accept this path. An existing target needs
    W_OK on itself — the atomic os.replace would otherwise bypass a read-only
    bit via the parent directory — and a new file needs W_OK on its parent.
    Templates read this off the stat payload to render read-only mode up
    front; keep the two in agreement."""
    # A `history` snapshot is history, not a file: the mutation handlers refuse
    # it outright, so saying otherwise here would put an editable editor in front
    # of a save that cannot land.
    if _is_under_snapshot_root(path):
        return False
    if os.path.exists(path):
        return os.access(path, os.W_OK)
    return os.access(os.path.dirname(path) or ".", os.W_OK)


class _PathProbe:
    """Result of an existence/shape probe. `parent_is_dir` is whether the path's
    parent is a directory; `exists`/`is_dir`/`size`/`mtime` describe the path
    itself (size/mtime are None when not probed)."""

    __slots__ = ("parent_is_dir", "exists", "is_dir", "size", "mtime")

    def __init__(self, parent_is_dir, exists, is_dir=False, size=None, mtime=None):
        self.parent_is_dir = parent_is_dir
        self.exists = exists
        self.is_dir = is_dir
        self.size = size
        self.mtime = mtime


def _probe_path(path: str) -> _PathProbe:
    """Existence + shape of `path` with plain kernel stats. Used by
    _fs_rename/_fs_copy."""
    parent_is_dir = os.path.isdir(os.path.dirname(path) or ".")
    if not os.path.exists(path):
        return _PathProbe(parent_is_dir, False)
    return _PathProbe(parent_is_dir, True, is_dir=os.path.isdir(path))


def _mutation_result_payload(path: str, is_dir: bool) -> dict:
    """The /api/fs/stat payload returned after a successful mutation."""
    return _stat_payload(path, is_dir)


def _stat_or_none(path: str) -> os.stat_result | None:
    """stat() for /api/fs/raw's 404 gate: None for missing paths and
    non-regular files alike (a directory has no raw bytes to serve)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st if stat_mod.S_ISREG(st.st_mode) else None


def _stat_payload(path: str, is_dir: bool, st: os.stat_result | None = None) -> dict:
    """The /api/fs/stat shape. /api/fs/write returns it too, so the editor can
    re-arm its optimistic lock from a save response. Pass a pre-fetched `st` to
    avoid a redundant stat()."""
    if st is None:
        st = os.stat(path)
    templates, template_error = _server_templates._templates_for(path, is_dir)

    payload = {
        "path": path,
        "name": os.path.basename(path) or path,
        "is_dir": is_dir,
        "size": None if is_dir else st.st_size,
        "mtime": st.st_mtime,
        "writable": _writable(path),
        "templates": templates,
    }
    if template_error:
        payload["template_error"] = template_error
    return payload


def _fs_stat(path: str):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return _error(f"no such file or directory: {path}", status=404)
    except OSError as e:
        # A TCC-denied local path lands here as EPERM. Tell the Full Disk
        # Access warning (shell/fda.py), and say "denied" rather than the
        # historical "no such file" — a path the OS refused is not gone.
        if isinstance(e, PermissionError):
            from fused_render.shell import fda as shell_fda
            return shell_fda.refused(path, e)
        return _error(f"no such file or directory: {path}", status=404)
    return _stat_payload(path, stat_mod.S_ISDIR(st.st_mode), st)


_STAT_TIMEOUT_S = 4.0  # a stat outliving this reports "unchanged" for this tick
