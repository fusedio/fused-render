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
    predicate would run the XDG backend on a Linux CI box. Returns the fake
    home's .Trash directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(_server_fs_mutate.sys, "platform", "darwin")
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
# Every case here FORCES the platform. The trash backend is chosen by
# sys.platform inside the module (one place, by design), so a test that let the
# host decide would assert the macOS path on a Mac and nothing at all on CI.


def _force_platform(monkeypatch, name: str):
    """Make the module believe it is running on `name`, which decides both
    _trash_supported() and which backend _move_to_trash dispatches to."""
    monkeypatch.setattr(_server_fs_mutate.sys, "platform", name)


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


def test_trash_supported_on_the_three_desktops_only(monkeypatch):
    for name in ("darwin", "linux", "win32"):
        _force_platform(monkeypatch, name)
        assert _server_fs_mutate._trash_supported()
    for name in ("freebsd13", "emscripten"):
        _force_platform(monkeypatch, name)
        assert not _server_fs_mutate._trash_supported()


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
