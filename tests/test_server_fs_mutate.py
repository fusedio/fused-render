"""Tests for the file-mutation POSTs (fused_render/server.py):
/api/fs/mkdir, /api/fs/delete, /api/fs/rename, /api/fs/copy.

Like test_server_fs_write.py these drive the module-level _fs_* helpers
directly (not through the starlette TestClient), asserting both the happy
path on disk and the wire error contract shared with _fs_write:
  400 relative/invalid path, 403 readonly ("readonly"), 404 missing source,
  409 conflict ("conflict"). All four also carry the X-Fused guard.
"""
import ctypes
import datetime
import errno
import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

from fused_render.server import fs_mutate as _server_fs_mutate
from fused_render.server.fs_mutate import _fs_copy as COPY
from fused_render.server.fs_mutate import _fs_delete as DELETE
from fused_render.server.fs_mutate import _fs_mkdir as MKDIR
from fused_render.server.fs_mutate import _fs_rename as RENAME
from fused_render.server.fs_mutate import _fs_trash_move as TRASH_MOVE

# os.access always says yes for root, so the chmod-based gates can't trip.
skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


def _status(resp) -> int:
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _data(resp) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return resp


# ---------------------------------------------------------------- X-Fused guard

@pytest.mark.parametrize("fn,body", [
    (MKDIR, {"path": "/x"}),
    (DELETE, {"path": "/x"}),
    (RENAME, {"src": "/x", "dst": "/y"}),
    (COPY, {"src": "/x", "dst": "/y"}),
])
def test_guard_rejects_missing_header(fn, body):
    resp = fn(body, x_fused=None)
    assert _status(resp) == 403
    assert "X-Fused" in _data(resp)["error"]


# --------------------------------------------------------------------- mkdir

def test_mkdir_creates_and_returns_stat(tmp_path):
    d = tmp_path / "newdir"
    out = _data(MKDIR({"path": str(d)}, x_fused="1"))
    assert d.is_dir()
    assert out["is_dir"] is True
    assert out["path"] == str(d)
    assert out["name"] == "newdir"


def test_mkdir_relative_path_400(tmp_path):
    resp = MKDIR({"path": "relative/dir"}, x_fused="1")
    assert _status(resp) == 400


def test_mkdir_missing_parent_400(tmp_path):
    resp = MKDIR({"path": str(tmp_path / "a" / "b")}, x_fused="1")
    assert _status(resp) == 400
    assert not (tmp_path / "a").exists()


def test_mkdir_existing_path_409(tmp_path):
    d = tmp_path / "exists"
    d.mkdir()
    resp = MKDIR({"path": str(d)}, x_fused="1")
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"


@skip_root
def test_mkdir_readonly_parent_403(tmp_path):
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
    try:
        resp = MKDIR({"path": str(tmp_path / "nope")}, x_fused="1")
        assert _status(resp) == 403
        assert _data(resp)["error"] == "readonly"
    finally:
        os.chmod(tmp_path, stat.S_IRWXU)


# -------------------------------------------------------------------- delete

def test_delete_file(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    out = _data(DELETE({"path": str(f)}, x_fused="1"))
    assert not f.exists()
    assert out["deleted"] == str(f)


def test_delete_empty_dir_without_flag(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert _status(DELETE({"path": str(d)}, x_fused="1")) == 200
    assert not d.exists()


def test_delete_nonempty_dir_requires_recursive(tmp_path):
    d = tmp_path / "full"
    d.mkdir()
    (d / "child").write_text("x")
    resp = DELETE({"path": str(d)}, x_fused="1")
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"
    assert d.exists()  # untouched


def test_delete_nonempty_dir_recursive(tmp_path):
    d = tmp_path / "full"
    d.mkdir()
    (d / "child").write_text("x")
    assert _status(DELETE({"path": str(d), "recursive": True}, x_fused="1")) == 200
    assert not d.exists()


def test_delete_missing_404(tmp_path):
    resp = DELETE({"path": str(tmp_path / "ghost")}, x_fused="1")
    assert _status(resp) == 404


def test_delete_symlink_to_dir_removes_link_not_target(tmp_path):
    # A symlink to a directory must be unlinked as the link itself — never
    # rmtree'd (which raises) and never followed into the target (which would
    # wipe the target's contents).
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    out = _data(DELETE({"path": str(link)}, x_fused="1"))
    assert out["deleted"] == str(link)
    assert not link.exists()  # link gone
    assert target.is_dir()  # target survives
    assert (target / "keep.txt").read_text() == "x"  # contents intact


@skip_root
def test_delete_readonly_file_403(tmp_path):
    f = tmp_path / "ro.txt"
    f.write_text("x")
    os.chmod(f, stat.S_IRUSR)
    try:
        resp = DELETE({"path": str(f)}, x_fused="1")
        assert _status(resp) == 403
        assert _data(resp)["error"] == "readonly"
        assert f.exists()
    finally:
        os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)


# --------------------------------------------------------------- delete: trash


def _fake_home(monkeypatch, tmp_path):
    """Point Path.home() at a throwaway home dir and force macOS, so the
    ~/.Trash backend is the one exercised regardless of the host. Forcing the
    PLATFORM rather than _trash_supported() is load-bearing now that
    _move_to_trash dispatches per platform: a case that only forced the
    predicate would run the XDG backend on a Linux CI box. Forced through
    _force_platform, so nothing outside the trash code sees it. Returns the fake
    home's .Trash directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    _force_platform(monkeypatch, "darwin")
    return home / ".Trash"


def test_delete_trash_moves_into_home_trash(tmp_path, monkeypatch):
    trash = _fake_home(monkeypatch, tmp_path)
    f = tmp_path / "f.txt"
    f.write_text("keep")
    out = _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert out == {"deleted": str(f), "trashed": True,
                   "trashed_to": str(trash / "f.txt")}
    assert not f.exists()  # moved out of its folder
    assert (trash / "f.txt").read_text() == "keep"  # landed in the Trash


def test_delete_trash_reports_where_it_landed(tmp_path, monkeypatch):
    # `trashed_to` is what makes a trash delete UNDOABLE: the move is an
    # os.rename we chose the destination for, so naming it back lets the client
    # record the delete as the symmetric rename pair it actually is.
    trash = _fake_home(monkeypatch, tmp_path)
    trash.mkdir(parents=True)
    (trash / "f.txt").write_text("old")  # force the dedupe suffix
    f = tmp_path / "f.txt"
    f.write_text("new")
    out = _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert out == {
        "deleted": str(f),
        "trashed": True,
        "trashed_to": str(trash / "f 2.txt"),
    }


def test_delete_trash_finder_fallback_names_no_destination(tmp_path, monkeypatch):
    # The osascript/Finder fallback: FINDER picks the location, so we cannot
    # name it — and an unnamed destination must not be reported, or the client
    # would record an undo pair pointing at a path that is not there.
    _fake_home(monkeypatch, tmp_path)
    f = tmp_path / "f.txt"
    f.write_text("x")
    monkeypatch.setattr(_server_fs_mutate, "_move_to_trash", lambda path: None)
    out = _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert out == {"deleted": str(f), "trashed": True}
    assert "trashed_to" not in out


def test_delete_hard_reports_no_trash_destination(tmp_path):
    # A hard delete has no destination at all — nothing to undo, nothing to say.
    f = tmp_path / "f.txt"
    f.write_text("x")
    out = _data(DELETE({"path": str(f)}, x_fused="1"))
    assert out == {"deleted": str(f), "trashed": False}


def test_delete_trash_dedupe_suffix(tmp_path, monkeypatch):
    trash = _fake_home(monkeypatch, tmp_path)
    trash.mkdir(parents=True)
    (trash / "f.txt").write_text("old")  # a same-named file already in the Bin
    f = tmp_path / "f.txt"
    f.write_text("new")
    _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert (trash / "f.txt").read_text() == "old"  # existing one untouched
    assert (trash / "f 2.txt").read_text() == "new"  # new one deduped


def test_delete_trash_unsupported_returns_501(tmp_path, monkeypatch):
    # Non-darwin (or Trash otherwise unavailable) → a 501 the frontend keys on
    # to fall back to a hard delete; the file must be left in place.
    monkeypatch.setattr(_server_fs_mutate, "_trash_supported", lambda: False)
    f = tmp_path / "f.txt"
    f.write_text("x")
    resp = DELETE({"path": str(f), "trash": True}, x_fused="1")
    assert _status(resp) == 501
    assert _data(resp)["error"] == "trash unsupported"
    assert f.exists()  # untouched — caller falls back


def test_delete_trash_failure_is_500_not_501(tmp_path, monkeypatch):
    # A FAILED trash on a supported platform must not reuse the 501
    # "unsupported" signal — that would route the client into the irreversible
    # hard-delete confirm as the follow-up to a recoverable-delete attempt.
    monkeypatch.setattr(_server_fs_mutate, "_trash_supported", lambda: True)

    def boom(path):
        raise OSError("disk sulking")

    monkeypatch.setattr(_server_fs_mutate, "_move_to_trash", boom)
    f = tmp_path / "f.txt"
    f.write_text("x")
    resp = DELETE({"path": str(f), "trash": True}, x_fused="1")
    assert _status(resp) == 500
    assert "cannot move to Trash" in _data(resp)["error"]
    assert f.exists()




# ------------------------------------------------- delete: trash, per platform
#
# Every case here FORCES the platform, via the module's _platform() seam rather
# than the real sys.platform (see _force_platform). The trash backend is chosen
# there in one place by design, so a test that let the host decide would assert
# the macOS path on a Mac and nothing at all on CI.


def _force_platform(monkeypatch, name: str):
    """Make the trash code believe it is running on `name`, which decides both
    _trash_supported() and which backend _move_to_trash dispatches to.

    Patches the module's own _platform() and NOT sys.platform: a module's `sys`
    attribute IS the real sys module, so setting `platform` on it is a
    process-wide change other live code reads — shell/mounts/rcd.py and
    lifecycle.py branch on sys.platform, and _fs_delete calls into shell.mounts,
    so forcing "win32" here would have any concurrent thread on a Mac take the
    Windows path. The real dispatcher still runs; only its answer to "which OS is
    this?" is substituted."""
    monkeypatch.setattr(_server_fs_mutate, "_platform", lambda: name)


def _xdg_home(monkeypatch, tmp_path):
    """Force Linux with a throwaway $XDG_DATA_HOME. Returns the Trash dir."""
    _force_platform(monkeypatch, "linux")
    data = tmp_path / "xdg"
    data.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    return data / "Trash"


def test_xdg_trash_moves_into_files_and_reports_destination(tmp_path, monkeypatch):
    trash = _xdg_home(monkeypatch, tmp_path)
    f = tmp_path / "f.txt"
    f.write_text("keep")
    out = _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert out == {"deleted": str(f), "trashed": True,
                   "trashed_to": str(trash / "files" / "f.txt")}
    assert not f.exists()
    assert (trash / "files" / "f.txt").read_text() == "keep"


def test_xdg_trash_writes_the_spec_sidecar(tmp_path, monkeypatch):
    # The two contract details a naive writer gets wrong: Path is
    # percent-encoded (this name holds a space and a '#', which would otherwise
    # read as a different path — or as a comment — to every other trash client),
    # and DeletionDate is local time with NO timezone suffix.
    trash = _xdg_home(monkeypatch, tmp_path)
    f = tmp_path / "a b#c.txt"
    f.write_text("x")
    _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    info = (trash / "info" / "a b#c.txt.trashinfo").read_text()
    lines = info.splitlines()
    assert lines[0] == "[Trash Info]"
    assert lines[1] == "Path=" + str(f).replace(" ", "%20").replace("#", "%23")
    assert "/" in lines[1]  # separators stay legible (quote's safe="/")
    date = lines[2].removeprefix("DeletionDate=")
    assert lines[2].startswith("DeletionDate=")
    # YYYY-MM-DDThh:mm:ss, and nothing after it — no "Z", no "+01:00".
    datetime.datetime.strptime(date, "%Y-%m-%dT%H:%M:%S")
    assert len(date) == 19



def test_xdg_trash_dirs_are_private(tmp_path, monkeypatch):
    # The bin holds what the user threw away. At the default 0755 every local
    # account on a shared host can list and read it, so all three directories we
    # create are 0700 — what glib/gvfs create the home trash as. Trash/ needs its
    # own assertion because pathlib's mkdir(parents=True) applies `mode` to the
    # leaf only and would have left the root world-readable.
    trash = _xdg_home(monkeypatch, tmp_path)
    f = tmp_path / "f.txt"
    f.write_text("private")
    _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    for d in (trash, trash / "files", trash / "info"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


def test_xdg_trash_leaves_an_existing_trash_dir_permissions_alone(tmp_path, monkeypatch):
    # A trash the user (or their desktop) already made is theirs. exist_ok does not
    # chmod, and quietly re-permissioning someone's directory is not this
    # function's business — asserted so a later "just chmod it" cannot slip in.
    trash = _xdg_home(monkeypatch, tmp_path)
    (trash / "files").mkdir(parents=True)
    (trash / "info").mkdir(parents=True)
    os.chmod(trash / "files", 0o755)
    f = tmp_path / "f.txt"
    f.write_text("x")
    _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert stat.S_IMODE((trash / "files").stat().st_mode) == 0o755


def test_xdg_trash_name_is_unique_across_info_as_well_as_files(tmp_path, monkeypatch):
    # The info file is the lock, so an EXISTING info file must push the new entry
    # onto the next name even though files/ is free.
    trash = _xdg_home(monkeypatch, tmp_path)
    (trash / "info").mkdir(parents=True)
    (trash / "info" / "f.txt.trashinfo").write_text("[Trash Info]\n")
    f = tmp_path / "f.txt"
    f.write_text("new")
    out = _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert out["trashed_to"] == str(trash / "files" / "f 2.txt")
    assert (trash / "files" / "f 2.txt").read_text() == "new"


def test_xdg_trash_does_not_overwrite_a_stale_files_entry(tmp_path, monkeypatch):
    # The other half of "unique across both": a files/ entry whose info file has
    # gone (another tool's crash, a hand deletion) wins the info name and would
    # be silently overwritten by the rename if only the lock were consulted.
    trash = _xdg_home(monkeypatch, tmp_path)
    (trash / "files").mkdir(parents=True)
    (trash / "files" / "f.txt").write_text("stale")
    f = tmp_path / "f.txt"
    f.write_text("new")
    out = _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert (trash / "files" / "f.txt").read_text() == "stale"  # untouched
    assert out["trashed_to"] == str(trash / "files" / "f 2.txt")
    assert (trash / "files" / "f 2.txt").read_text() == "new"
    # And the claim it gave back is not left lying around.
    assert not (trash / "info" / "f.txt.trashinfo").exists()



def test_xdg_trash_does_not_overwrite_a_dangling_symlink_entry(tmp_path, monkeypatch):
    # Same class as the stale-entry case above, but the entry is a BROKEN SYMLINK:
    # trashed while it pointed somewhere, and its target deleted afterwards.
    # Path.exists() is False for it, so a name check that used exists() would read
    # the name as free, win its O_EXCL claim, and have os.rename destroy an entry
    # already in the bin.
    trash = _xdg_home(monkeypatch, tmp_path)
    (trash / "files").mkdir(parents=True)
    dangling = trash / "files" / "f.txt"
    dangling.symlink_to(tmp_path / "target-that-is-gone")
    assert not dangling.exists() and dangling.is_symlink()  # the trap, stated
    f = tmp_path / "f.txt"
    f.write_text("new")
    out = _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert dangling.is_symlink()  # untouched
    assert out["trashed_to"] == str(trash / "files" / "f 2.txt")
    assert (trash / "files" / "f 2.txt").read_text() == "new"


def test_macos_trash_does_not_overwrite_a_dangling_symlink_entry(tmp_path, monkeypatch):
    # The macOS backend has the same hole, and there it also makes the returned
    # `trashed_to` a lie: it would name a path holding someone else's entry.
    trash = _fake_home(monkeypatch, tmp_path)
    trash.mkdir(parents=True)
    dangling = trash / "f.txt"
    dangling.symlink_to(tmp_path / "target-that-is-gone")
    assert not dangling.exists() and dangling.is_symlink()
    f = tmp_path / "f.txt"
    f.write_text("new")
    out = _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert dangling.is_symlink()  # untouched
    assert out["trashed_to"] == str(trash / "f 2.txt")
    assert (trash / "f 2.txt").read_text() == "new"


def test_trashing_a_symlink_moves_the_LINK_not_its_target(tmp_path, monkeypatch):
    # The other half of treating symlinks as entries in their own right: trashing
    # one takes the link and leaves the target alone.
    trash = _xdg_home(monkeypatch, tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("payload")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    out = _data(DELETE({"path": str(link), "trash": True}, x_fused="1"))
    assert out["trashed_to"] == str(trash / "files" / "link.txt")
    assert (trash / "files" / "link.txt").is_symlink()
    assert target.read_text() == "payload"  # the target never moved
    assert not link.is_symlink()


def test_xdg_trash_cross_device_is_501_and_leaves_no_claim(tmp_path, monkeypatch):
    # EXDEV: a file on another volume. Nothing is copied (that is the whole point
    # of refusing), so the file stays put, the answer is the same 501 the client
    # routes into its confirm-then-hard-delete flow, and the info file the
    # backend created to claim the name is removed again — an info file with no
    # entry in files/ is the orphan every trash client has to guess about.
    trash = _xdg_home(monkeypatch, tmp_path)
    f = tmp_path / "f.txt"
    f.write_text("x")

    def exdev(src, dst):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(_server_fs_mutate.os, "rename", exdev)
    resp = DELETE({"path": str(f), "trash": True}, x_fused="1")
    assert _status(resp) == 501
    assert _data(resp)["error"] == "trash unsupported"
    assert f.read_text() == "x"  # untouched — and not copied anywhere
    assert list((trash / "info").iterdir()) == []
    assert list((trash / "files").iterdir()) == []



def test_xdg_trash_unwritable_info_dir_is_500_not_501(tmp_path, monkeypatch):
    # THE 501 IS AN INVITATION TO ERASE THE FILE, so only a condition no retry
    # could fix may produce it. An unwritable Trash/info (read-only volume, out of
    # space, wrong owner) is a recoverable delete that FAILED: report it as a 500,
    # leave the file alone, and do NOT route the client into the
    # confirm-then-hard-delete.
    trash = _xdg_home(monkeypatch, tmp_path)
    f = tmp_path / "f.txt"
    f.write_text("keep")

    real_open = os.open

    def denied(p, *a, **kw):
        if str(p).endswith(".trashinfo"):
            raise PermissionError(13, "Permission denied")
        return real_open(p, *a, **kw)

    monkeypatch.setattr(_server_fs_mutate.os, "open", denied)
    resp = DELETE({"path": str(f), "trash": True}, x_fused="1")
    assert _status(resp) == 500
    assert "cannot move to Trash" in _data(resp)["error"]
    assert f.read_text() == "keep"  # still there, nothing offered to be erased
    assert list((trash / "files").iterdir()) == []


def test_xdg_trash_root_blocked_by_a_stray_file_is_500_not_501(tmp_path, monkeypatch):
    # A plain FILE sitting where ~/.local/share/Trash should be: mkdir fails with
    # EEXIST/ENOTDIR. Same rule — a failure, not an "unsupported".
    _force_platform(monkeypatch, "linux")
    data = tmp_path / "xdg"
    data.mkdir()
    (data / "Trash").write_text("not a directory")
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    f = tmp_path / "f.txt"
    f.write_text("keep")
    resp = DELETE({"path": str(f), "trash": True}, x_fused="1")
    assert _status(resp) == 500
    assert "cannot move to Trash" in _data(resp)["error"]
    assert f.read_text() == "keep"


def test_xdg_trash_only_exdev_reports_unsupported(tmp_path, monkeypatch):
    # The one errno that means "this platform cannot trash this path": nothing
    # moved, no retry helps, so the 501 and its hard-delete fallback are honest.
    # A DIFFERENT rename errno on the same code path must not borrow that answer.
    trash = _xdg_home(monkeypatch, tmp_path)
    f = tmp_path / "f.txt"
    f.write_text("keep")

    def fail(code):
        def rename(src, dst):
            raise OSError(code, os.strerror(code))
        return rename

    monkeypatch.setattr(_server_fs_mutate.os, "rename", fail(errno.EXDEV))
    assert _status(DELETE({"path": str(f), "trash": True}, x_fused="1")) == 501
    monkeypatch.setattr(_server_fs_mutate.os, "rename", fail(errno.EACCES))
    assert _status(DELETE({"path": str(f), "trash": True}, x_fused="1")) == 500
    monkeypatch.setattr(_server_fs_mutate.os, "rename", fail(errno.ENOSPC))
    assert _status(DELETE({"path": str(f), "trash": True}, x_fused="1")) == 500
    # Every one of them left the file in place and no orphan claim behind.
    assert f.read_text() == "keep"
    assert list((trash / "info").iterdir()) == []


def test_xdg_trash_dir_defaults_and_ignores_a_relative_xdg_data_home(tmp_path, monkeypatch):
    # An unset (or empty) $XDG_DATA_HOME means ~/.local/share, and a RELATIVE one
    # is treated as unset per the basedir spec — resolving it against the
    # server's cwd would scatter trash roots wherever the app was started from.
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert _server_fs_mutate._xdg_trash_dir() == home / ".local" / "share" / "Trash"
    monkeypatch.setenv("XDG_DATA_HOME", "")
    assert _server_fs_mutate._xdg_trash_dir() == home / ".local" / "share" / "Trash"
    monkeypatch.setenv("XDG_DATA_HOME", "relative/share")
    assert _server_fs_mutate._xdg_trash_dir() == home / ".local" / "share" / "Trash"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "abs"))
    assert _server_fs_mutate._xdg_trash_dir() == tmp_path / "abs" / "Trash"


# -- Windows: the Recycle Bin, against a fake shell32 ------------------------


class _FakeShell32:
    """Records the SHFILEOPSTRUCTW it is handed, and answers as told."""

    def __init__(self, rc=0, abort=False):
        self.rc, self.abort = rc, abort
        self.ops = []

    def SHFileOperationW(self, ptr):
        op = ptr.contents
        self.ops.append(op)
        if self.abort:
            op.fAnyOperationsAborted = 1
        return self.rc


def _fake_bin(monkeypatch, **kw):
    shell = _FakeShell32(**kw)
    _force_platform(monkeypatch, "win32")
    monkeypatch.setattr(_server_fs_mutate, "_shell32", lambda: shell)
    return shell


def test_recycle_bin_request_is_double_null_terminated_with_allowundo():
    p_from, flags = _server_fs_mutate._recycle_bin_request(r"C:\Users\me\f.txt")
    # pFrom is a LIST of paths, read until an empty one: the path's own
    # terminator plus the list's. A single one leaves the API reading past it.
    assert p_from == "C:\\Users\\me\\f.txt\0\0"
    # ALLOWUNDO is what makes this the bin instead of an erase; the other two
    # keep the shell's dialog and progress window out of a local app's delete.
    assert flags & 0x0040  # FOF_ALLOWUNDO
    assert flags & 0x0010  # FOF_NOCONFIRMATION
    assert flags & 0x0004  # FOF_SILENT
    # NOERRORUI, without which a locked file raises an unowned error dialog that
    # SHFileOperationW waits on — parking a worker of the bounded threadpool these
    # sync routes run on. With it, the failure returns an rc we can report.
    assert flags & 0x0400  # FOF_NOERRORUI


def test_delete_win32_recycles_and_reports_no_destination(tmp_path, monkeypatch):
    # Recoverable, but NOT undoable: the bin keeps the item as $R… beside its
    # $I… metadata under C:\$Recycle.Bin\<SID>\ and restoring goes through the
    # shell, so there is no path a rename could bring it back from. No
    # `trashed_to` — the same rule the macOS Finder fallback falls under.
    shell = _fake_bin(monkeypatch)
    f = tmp_path / "f.txt"
    f.write_text("x")
    out = _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert out == {"deleted": str(f), "trashed": True}
    assert "trashed_to" not in out
    [op] = shell.ops
    assert op.wFunc == 0x0003  # FO_DELETE
    # Read the wide chars straight out of the buffer the struct points at, so the
    # LIST TERMINATOR is part of the assertion: a single NUL leaves the API
    # reading past the buffer, and that is invisible to any check that stops at
    # the first one.
    assert ctypes.wstring_at(op.pFrom, len(str(f)) + 2) == str(f) + "\0\0"
    assert op.fFlags & 0x0040  # FOF_ALLOWUNDO
    assert op.pTo is None  # FO_DELETE has no destination


def test_delete_win32_failure_is_500_not_501(tmp_path, monkeypatch):
    # A nonzero return is a FAILED recoverable delete, never "unsupported" —
    # that signal would route the client into the irreversible hard delete.
    _fake_bin(monkeypatch, rc=124)
    f = tmp_path / "f.txt"
    f.write_text("x")
    resp = DELETE({"path": str(f), "trash": True}, x_fused="1")
    assert _status(resp) == 500
    assert "cannot move to Trash" in _data(resp)["error"]


def test_delete_win32_aborted_operation_is_a_failure(tmp_path, monkeypatch):
    # Zero return, abort flag set: the shell stopped, so the entry may still be
    # there. Reporting success would tell the user their file is in the bin.
    _fake_bin(monkeypatch, rc=0, abort=True)
    f = tmp_path / "f.txt"
    f.write_text("x")
    resp = DELETE({"path": str(f), "trash": True}, x_fused="1")
    assert _status(resp) == 500
    assert "aborted" in _data(resp)["error"]



def test_forcing_a_platform_does_not_leak_into_the_real_sys(tmp_path, monkeypatch):
    # The seam's whole point. Patching `_server_fs_mutate.sys.platform` would have
    # patched the REAL sys module — shell/mounts/rcd.py and lifecycle.py read it
    # live, and _fs_delete calls into shell.mounts — so a Windows-forcing case on a
    # Mac changed what every other thread in the process believed about the OS.
    import sys as real_sys

    before = real_sys.platform
    shell = _fake_bin(monkeypatch)
    f = tmp_path / "f.txt"
    f.write_text("x")
    out = _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    # The dispatcher really did take the win32 branch: the fake shell was asked to
    # recycle the path, and the answer has no `trashed_to` (the bin names nothing).
    # The fake does not itself remove the file, which is why this asserts on the
    # CALL rather than on the file being gone.
    assert len(shell.ops) == 1
    assert out == {"deleted": str(f), "trashed": True}
    # …while the rest of the process still sees the true platform.
    assert real_sys.platform == before
    assert _server_fs_mutate.sys.platform == before


def test_trash_supported_on_the_three_desktops_only(monkeypatch):
    for name in ("darwin", "linux", "win32"):
        _force_platform(monkeypatch, name)
        assert _server_fs_mutate._trash_supported()
    for name in ("freebsd13", "emscripten"):
        _force_platform(monkeypatch, name)
        assert not _server_fs_mutate._trash_supported()




# ------------------------------------------------------------- trash-move
#
# The ONE primitive the explorer's undo/redo calls for a "delete" op: a rename
# that also fixes the XDG sidecar in whichever direction it is going. Outside a
# recognized trash root it is a plain rename and nothing else.


def _xdg_trash_root(monkeypatch, tmp_path):
    data = tmp_path / "xdg"
    data.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    trash = data / "Trash"
    (trash / "files").mkdir(parents=True)
    (trash / "info").mkdir(parents=True)
    return trash


def test_trash_move_outside_a_trash_root_is_a_plain_rename(tmp_path, monkeypatch):
    # macOS ~/.Trash, and every other case: no sidecar exists, none is invented.
    _xdg_trash_root(monkeypatch, tmp_path)
    src = tmp_path / "a.txt"
    src.write_text("x")
    dst = tmp_path / "b.txt"
    out = _data(TRASH_MOVE({"from": str(src), "to": str(dst)}, x_fused="1"))
    assert out["path"] == str(dst)
    assert dst.read_text() == "x"
    assert not src.exists()


def test_trash_move_into_the_trash_writes_the_sidecar(tmp_path, monkeypatch):
    # Redo of a delete: back into files/, and the .trashinfo has to come back with
    # it or other trash clients can no longer restore the entry.
    trash = _xdg_trash_root(monkeypatch, tmp_path)
    src = tmp_path / "a b.txt"
    src.write_text("x")
    dst = trash / "files" / "a b.txt"
    _data(TRASH_MOVE({"from": str(src), "to": str(dst)}, x_fused="1"))
    assert dst.read_text() == "x"
    info = (trash / "info" / "a b.txt.trashinfo").read_text()
    # Path= names where it came FROM, percent-encoded like the backend's own.
    assert f"Path={str(src).replace(' ', '%20')}\n" in info
    assert "DeletionDate=" in info



def test_trash_move_redo_sidecar_is_as_private_as_the_delete_path(tmp_path, monkeypatch):
    # A REDO is the delete happening again, so it must not leave the bin more
    # exposed than the delete did. This branch used to use Path.write_text and a
    # bare mkdir, both of which take the umask (0644/0755), publishing the entry's
    # Path= and DeletionDate= to every local account on a shared host — the exact
    # posture the delete path sets 0700/0600 to avoid. (Cursor Bugbot, PR #592.)
    trash = _xdg_trash_root(monkeypatch, tmp_path)
    (trash / "info").rmdir()  # so the mode WE create it with is observable
    src = tmp_path / "a.txt"
    src.write_text("x")
    dst = trash / "files" / "a.txt"
    _data(TRASH_MOVE({"from": str(src), "to": str(dst)}, x_fused="1"))
    info = trash / "info" / "a.txt.trashinfo"
    assert info.exists()
    assert stat.S_IMODE(info.stat().st_mode) == 0o600
    assert stat.S_IMODE((trash / "info").stat().st_mode) == 0o700


def test_trash_move_redo_leaves_an_existing_info_dir_alone(tmp_path, monkeypatch):
    # Same rule as the delete path: we set modes on what we CREATE and never
    # re-permission a directory the user (or their desktop) already made. The
    # sidecar itself is still created 0600.
    trash = _xdg_trash_root(monkeypatch, tmp_path)
    os.chmod(trash / "info", 0o755)
    src = tmp_path / "a.txt"
    src.write_text("x")
    _data(TRASH_MOVE({"from": str(src), "to": str(trash / "files" / "a.txt")}, x_fused="1"))
    assert stat.S_IMODE((trash / "info").stat().st_mode) == 0o755  # untouched
    assert stat.S_IMODE((trash / "info" / "a.txt.trashinfo").stat().st_mode) == 0o600


def test_trash_move_redo_overwrites_a_stale_sidecar(tmp_path, monkeypatch):
    # The semantics the mode change must not disturb: this is deliberately NOT an
    # exclusive create. The name was decided by the recorded pair and the rename
    # proved it free in files/, so an info file still sitting there is stale and
    # gets replaced — including being TRUNCATED, not appended to.
    trash = _xdg_trash_root(monkeypatch, tmp_path)
    stale = trash / "info" / "a.txt.trashinfo"
    stale.write_text("[Trash Info]\nPath=/somewhere/else/entirely-and-much-longer\n")
    src = tmp_path / "a.txt"
    src.write_text("x")
    _data(TRASH_MOVE({"from": str(src), "to": str(trash / "files" / "a.txt")}, x_fused="1"))
    body = stale.read_text()
    assert f"Path={src}\n" in body
    assert "entirely-and-much-longer" not in body  # truncated, not left trailing


def test_trash_move_out_of_the_trash_removes_the_sidecar(tmp_path, monkeypatch):
    # Undo of a delete: the entry goes home, so its metadata must not linger
    # describing something that is no longer in files/.
    trash = _xdg_trash_root(monkeypatch, tmp_path)
    src = trash / "files" / "a.txt"
    src.write_text("x")
    info = trash / "info" / "a.txt.trashinfo"
    info.write_text("[Trash Info]\n")
    dst = tmp_path / "a.txt"
    _data(TRASH_MOVE({"from": str(src), "to": str(dst)}, x_fused="1"))
    assert dst.read_text() == "x"
    assert not info.exists()


def test_trash_move_within_the_trash_swaps_the_sidecar(tmp_path, monkeypatch):
    # Both branches at once, which is why they are independent rather than an
    # either/or: the old name's metadata goes, the new name's arrives.
    trash = _xdg_trash_root(monkeypatch, tmp_path)
    src = trash / "files" / "a.txt"
    src.write_text("x")
    (trash / "info" / "a.txt.trashinfo").write_text("[Trash Info]\n")
    dst = trash / "files" / "b.txt"
    _data(TRASH_MOVE({"from": str(src), "to": str(dst)}, x_fused="1"))
    assert not (trash / "info" / "a.txt.trashinfo").exists()
    assert (trash / "info" / "b.txt.trashinfo").exists()


def test_trash_move_refuses_to_touch_an_info_file_for_an_outside_path(tmp_path, monkeypatch):
    # THE SECURITY CASE. A caller aims the move at a path that merely LOOKS like a
    # trash entry — same basename as a real one, a parent that traverses out of
    # files/ — and the sidecar for the real entry must survive untouched. The
    # endpoint decides by resolving the parent against the server's own trash
    # root, never by matching the text it was handed.
    trash = _xdg_trash_root(monkeypatch, tmp_path)
    victim = trash / "info" / "precious.trashinfo"
    victim.write_text("[Trash Info]\n")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    src = outside / "precious"
    src.write_text("x")
    _data(TRASH_MOVE({"from": str(src), "to": str(tmp_path / "moved")}, x_fused="1"))
    assert victim.read_text() == "[Trash Info]\n"  # not unlinked

    # And the traversal shape: a path whose parent only reaches files/ by going
    # back out of it resolves elsewhere, so it gets no sidecar either.
    trav = trash / "files" / ".." / ".." / "outside.txt"
    (trash.parent / "outside.txt").write_text("y")
    _data(TRASH_MOVE({"from": str(trav), "to": str(tmp_path / "trav-moved")}, x_fused="1"))
    assert not (trash / "info" / "outside.txt.trashinfo").exists()



def test_trash_move_boundary_rejects_a_symlinked_parent(tmp_path, monkeypatch):
    # The docstring's second claim, now actually tested. A path whose parent is a
    # SYMLINK out of files/ used to pass the boundary because the check normalised
    # `link/..` lexically before resolving it — collapsing both components and
    # leaving `…/Trash/files/y.txt`, which of course looked like a trash entry —
    # while os.rename acted on the kernel-resolved path somewhere else entirely.
    trash = _xdg_trash_root(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (trash / "files" / "link").symlink_to(outside)
    victim = trash / "info" / "y.txt.trashinfo"
    victim.write_text("[Trash Info]\n")
    # The file must sit where the KERNEL resolves the attack path, or the rename
    # 404s first and the test passes for the wrong reason (it did, once):
    # `files/link/..` is the parent of the LINK'S TARGET, i.e. tmp_path — not
    # `outside`, which is what a lexical reading of `link/..` gives.
    src = tmp_path / "y.txt"
    src.write_text("x")
    sneaky = str(trash / "files" / "link" / ".." / "y.txt")
    out = _data(TRASH_MOVE({"from": sneaky, "to": str(tmp_path / "moved.txt")}, x_fused="1"))
    # The move itself is allowed — it is an ordinary rename of a path outside the
    # trash, and refusing it is not this boundary's job. What must NOT happen is
    # the sidecar unlink: the boundary has to answer "not a trash entry" for a
    # parent that only reaches files/ by traversing back out of it.
    assert out["path"] == str(tmp_path / "moved.txt")
    assert victim.read_text() == "[Trash Info]\n"  # sidecar untouched


def test_trash_move_still_recognises_a_trashed_SYMLINK_as_an_entry(tmp_path, monkeypatch):
    # The other half of the same fix, and the reason the parent is resolved rather
    # than the whole path: a trashed symlink is an entry in its own right, so
    # restoring it must still drop its sidecar. Resolving the leaf would test the
    # LINK TARGET's directory and silently skip every symlink in the bin.
    trash = _xdg_trash_root(monkeypatch, tmp_path)
    target = tmp_path / "elsewhere.txt"
    target.write_text("payload")
    entry = trash / "files" / "link.txt"
    entry.symlink_to(target)
    info = trash / "info" / "link.txt.trashinfo"
    info.write_text("[Trash Info]\n")
    dst = tmp_path / "link.txt"
    _data(TRASH_MOVE({"from": str(entry), "to": str(dst)}, x_fused="1"))
    assert dst.is_symlink()   # the LINK came home, not a copy of its target
    assert os.readlink(dst) == str(target)
    assert not info.exists()  # and its sidecar went with it
    assert target.read_text() == "payload"  # the target was never touched
    # NOTE a DANGLING trashed link cannot be restored this way, and not because of
    # the boundary above: /api/fs/rename, which this endpoint delegates every guard
    # to, gates on os.path.exists(src) and so 404s a source that is a broken link.
    # That is the shared handler's long-standing contract, reported per pair as
    # "no longer exists"; widening it is not this endpoint's business.


def test_trash_move_keeps_renames_guards(tmp_path, monkeypatch):
    # Not a looser contract than /api/fs/rename: the X-Fused guard, absolute
    # paths, 404 for a missing source and 409 for an occupied destination — with
    # overwrite off, always, because an undo must never clobber.
    trash = _xdg_trash_root(monkeypatch, tmp_path)
    assert _status(TRASH_MOVE({"from": "/a", "to": "/b"}, x_fused=None)) == 403
    assert _status(TRASH_MOVE({"from": "rel", "to": str(tmp_path / "b")}, x_fused="1")) == 400
    assert _status(TRASH_MOVE({"from": str(tmp_path / "a"), "to": "rel"}, x_fused="1")) == 400
    src = trash / "files" / "a.txt"
    src.write_text("x")
    info = trash / "info" / "a.txt.trashinfo"
    info.write_text("[Trash Info]\n")
    assert _status(TRASH_MOVE({"from": str(trash / "files" / "nope"),
                               "to": str(tmp_path / "a.txt")}, x_fused="1")) == 404
    taken = tmp_path / "taken.txt"
    taken.write_text("mine")
    resp = TRASH_MOVE({"from": str(src), "to": str(taken)}, x_fused="1")
    assert _status(resp) == 409
    # A refused move changes NOTHING about the bin's bookkeeping.
    assert info.exists()
    assert src.read_text() == "x"
    assert taken.read_text() == "mine"


# -------------------------------------------------------------------- rename

def test_rename_file(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hi")
    dst = tmp_path / "b.txt"
    out = _data(RENAME({"src": str(src), "dst": str(dst)}, x_fused="1"))
    assert not src.exists()
    assert dst.read_text() == "hi"
    assert out["path"] == str(dst)
    assert out["is_dir"] is False


def test_rename_dir(tmp_path):
    src = tmp_path / "d1"
    src.mkdir()
    (src / "c").write_text("x")
    dst = tmp_path / "d2"
    out = _data(RENAME({"src": str(src), "dst": str(dst)}, x_fused="1"))
    assert not src.exists()
    assert (dst / "c").read_text() == "x"
    assert out["is_dir"] is True


def test_rename_missing_src_404(tmp_path):
    resp = RENAME({"src": str(tmp_path / "ghost"), "dst": str(tmp_path / "x")}, x_fused="1")
    assert _status(resp) == 404


def test_rename_dst_exists_409(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "b.txt"
    dst.write_text("b")
    resp = RENAME({"src": str(src), "dst": str(dst)}, x_fused="1")
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"
    assert src.exists() and dst.read_text() == "b"


def test_rename_overwrite(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "b.txt"
    dst.write_text("b")
    _data(RENAME({"src": str(src), "dst": str(dst), "overwrite": True}, x_fused="1"))
    assert not src.exists()
    assert dst.read_text() == "a"


def test_rename_relative_dst_400(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("a")
    assert _status(RENAME({"src": str(src), "dst": "rel"}, x_fused="1")) == 400


def test_rename_dir_into_itself_400(tmp_path):
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    for dst in (d / "d", d / "sub" / "d"):
        resp = RENAME({"src": str(d), "dst": str(dst)}, x_fused="1")
        assert _status(resp) == 400
        assert "into itself" in _data(resp)["error"]
    assert (d / "sub").is_dir()  # tree untouched


@skip_root
def test_rename_readonly_src_403(tmp_path):
    # A move deletes the source, so a readonly source must refuse the same way
    # delete does — otherwise rename lifts entries off a read-only location.
    src = tmp_path / "a.txt"
    src.write_text("a")
    os.chmod(src, stat.S_IRUSR)
    try:
        resp = RENAME({"src": str(src), "dst": str(tmp_path / "b.txt")}, x_fused="1")
        assert _status(resp) == 403
        assert _data(resp)["error"] == "readonly"
        assert src.exists()
    finally:
        os.chmod(src, stat.S_IRWXU)


def test_rename_missing_dst_parent_400(tmp_path):
    # A missing dst parent is a 400 (not the misleading "readonly" 403 that
    # _writable would otherwise produce for an outside/unwritable ancestor).
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "nope" / "b.txt"
    resp = RENAME({"src": str(src), "dst": str(dst)}, x_fused="1")
    assert _status(resp) == 400
    assert "parent directory does not exist" in _data(resp)["error"]
    assert src.exists()  # untouched


# ---------------------------------------------------------------------- copy

def test_copy_file(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hi")
    dst = tmp_path / "b.txt"
    out = _data(COPY({"src": str(src), "dst": str(dst)}, x_fused="1"))
    assert src.read_text() == "hi"  # source kept
    assert dst.read_text() == "hi"
    assert out["path"] == str(dst)
    assert out["is_dir"] is False


def test_copy_dir(tmp_path):
    src = tmp_path / "d1"
    src.mkdir()
    (src / "c").write_text("x")
    dst = tmp_path / "d2"
    out = _data(COPY({"src": str(src), "dst": str(dst)}, x_fused="1"))
    assert (src / "c").exists()
    assert (dst / "c").read_text() == "x"
    assert out["is_dir"] is True


def test_copy_missing_src_404(tmp_path):
    resp = COPY({"src": str(tmp_path / "ghost"), "dst": str(tmp_path / "x")}, x_fused="1")
    assert _status(resp) == 404


def test_copy_dst_exists_409(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "b.txt"
    dst.write_text("b")
    resp = COPY({"src": str(src), "dst": str(dst)}, x_fused="1")
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"
    assert dst.read_text() == "b"


def test_copy_overwrite(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "b.txt"
    dst.write_text("b")
    _data(COPY({"src": str(src), "dst": str(dst), "overwrite": True}, x_fused="1"))
    assert dst.read_text() == "a"


def test_copy_missing_dst_parent_400(tmp_path):
    # Same honest 400 as rename when dst's parent doesn't exist.
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "nope" / "b.txt"
    resp = COPY({"src": str(src), "dst": str(dst)}, x_fused="1")
    assert _status(resp) == 400
    assert "parent directory does not exist" in _data(resp)["error"]
    assert src.exists()  # source untouched


def test_copy_dir_into_descendant_400(tmp_path):
    src = tmp_path / "d1"
    src.mkdir()
    dst = src / "sub"
    resp = COPY({"src": str(src), "dst": str(dst)}, x_fused="1")
    assert _status(resp) == 400
    assert not dst.exists()
