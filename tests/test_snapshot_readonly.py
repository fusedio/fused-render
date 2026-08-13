"""`~/.fused-render/app-versions/` is machine-generated history, and every
mutation endpoint refuses it.

WHY THE GUARD IS HERE AND NOT IN A TEMPLATE. A snapshot tree is `git archive`
output: the materialised bytes of one commit, extracted so a preview can be
framed. It is not a place a user's edit can mean anything — the real file is
elsewhere, the commit is immutable, and the extractor REUSED a tree whenever
`.fused-snapshot-complete` existed, so a write that lands there is served back as
that revision's content from then on. The repro this closes: open `notes.md`,
pick an old commit, and the framing view renders the snapshot through that file's
own default view — `code` or `markdown`, both of which call `fused.writeFile`.
Cmd+S SUCCEEDED, the real file was untouched, the edit was lost, and the
"historical" revision was quietly rewritten.

THE VIEW THAT MATERIALISED THESE TREES IS GONE (the `git` view resolves a
revision on read instead, /api/git/show), so nothing writes `app-versions/` any
more. The guard is still the truth for trees an older version left on disk, which
are still reachable by path and still immutable.

Fixing only the framing template would leave the path writable to everything else
(the explorer's own file ops, an /api/fs/write from any view, a rename). The
promise "this is history" has to be kept at the mutation boundary, which is the
posture the rest of the repo already takes: `mount_read_only` refuses a
read-only mount in every handler rather than once.

The refusal reuses the existing `readonly` wire contract (403 + `{"error":
"readonly"}`) rather than inventing a string: runtime.js `writeFile` already turns
it into a typed error and the code editor renders "Save failed: file is
read-only", and the explorer's fs-actions already phrases it as
`"<name>" is read-only — <verb> isn't allowed here.` A new string would have
reached the user as an unhandled generic.
"""
import json
import os

import pytest
from fastapi.responses import JSONResponse

from fused_render.server import fs_mutate, mount
from fused_render.shell import storage


@pytest.fixture
def snap(tmp_path, monkeypatch):
    """A materialised snapshot tree, exactly as one was laid out on disk."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    root = os.path.join(storage.home_dir(), "app-versions", "abc123def456",
                        "0" * 40)
    os.makedirs(root)
    f = os.path.join(root, "notes.md")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("# as it was at that commit\n")
    with open(os.path.join(os.path.dirname(root), ".keep"), "w") as fh:
        fh.write("")
    return root


@pytest.fixture
def outside(tmp_path):
    """A control target with nothing to do with the snapshot root."""
    d = tmp_path / "work"
    d.mkdir()
    f = d / "notes.md"
    f.write_text("# the real file\n")
    return f


def _status(resp) -> int:
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _data(resp) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return resp


def _refused(resp):
    assert _status(resp) == 403, _data(resp)
    # The wire string every caller already knows how to render.
    assert _data(resp).get("error") == "readonly", _data(resp)


# ------------------------------------------------------------------ every handler

def test_write_into_a_snapshot_is_refused(snap):
    """The actual repro: the framing template's Cmd+S."""
    target = os.path.join(snap, "notes.md")
    _refused(fs_mutate._fs_write({"path": target, "content": "tampered"},
                                 x_fused="1"))
    with open(target, encoding="utf-8") as f:
        assert f.read() == "# as it was at that commit\n"


def test_a_new_file_inside_a_snapshot_is_refused(snap):
    target = os.path.join(snap, "new.md")
    _refused(fs_mutate._fs_write({"path": target, "content": "x", "create": True},
                                 x_fused="1"))
    assert not os.path.exists(target)


def test_upload_into_a_snapshot_is_refused(snap):
    target = os.path.join(snap, "dropped.bin")
    _refused(fs_mutate._fs_upload(target, b"\x00\x01", x_fused="1"))
    assert not os.path.exists(target)


def test_mkdir_inside_a_snapshot_is_refused(snap):
    target = os.path.join(snap, "sub")
    _refused(fs_mutate._fs_mkdir({"path": target}, x_fused="1"))
    assert not os.path.isdir(target)


def test_delete_inside_a_snapshot_is_refused(snap):
    target = os.path.join(snap, "notes.md")
    _refused(fs_mutate._fs_delete({"path": target}, x_fused="1"))
    assert os.path.exists(target)


def test_deleting_the_snapshot_root_itself_is_refused(snap):
    """Not just the files IN it: the tree is the record. (Garbage-collecting old
    snapshots belongs to whatever manages that cache, not to an
    /api/fs/delete caller.)"""
    _refused(fs_mutate._fs_delete({"path": snap}, x_fused="1"))
    assert os.path.isdir(snap)


def test_renaming_out_of_a_snapshot_is_refused(snap, outside):
    target = os.path.join(snap, "notes.md")
    _refused(fs_mutate._fs_rename({"src": target,
                                   "dst": str(outside.parent / "moved.md")},
                                  x_fused="1"))
    assert os.path.exists(target)


def test_renaming_into_a_snapshot_is_refused(snap, outside):
    _refused(fs_mutate._fs_rename({"src": str(outside),
                                   "dst": os.path.join(snap, "smuggled.md")},
                                  x_fused="1"))
    assert not os.path.exists(os.path.join(snap, "smuggled.md"))


def test_copying_into_a_snapshot_is_refused(snap, outside):
    _refused(fs_mutate._fs_copy({"src": str(outside),
                                 "dst": os.path.join(snap, "copy.md")},
                                x_fused="1"))
    assert not os.path.exists(os.path.join(snap, "copy.md"))


def test_copying_out_of_a_snapshot_is_allowed(snap, outside):
    """Read-only means read-only, not sealed: taking a copy of an old revision
    somewhere the user owns is exactly what looking at an old revision is
    for."""
    dest = str(outside.parent / "from-history.md")
    resp = fs_mutate._fs_copy({"src": os.path.join(snap, "notes.md"),
                               "dst": dest}, x_fused="1")
    assert _status(resp) == 200, _data(resp)
    assert os.path.exists(dest)


def test_compressing_into_a_snapshot_is_refused(snap, outside):
    _refused(fs_mutate._fs_compress({"path": str(outside.parent), "format": "zip",
                                     "dest": os.path.join(snap, "z.zip")},
                                    x_fused="1"))


# ---------------------------------------- the stat payload agrees with the handlers

def test_stat_reports_a_snapshot_as_not_writable(snap):
    """The other half of the fix. `writable` is what a template reads to render
    read-only mode UP FRONT (see tests/test_server_fs_write.py) — without this the
    framed `code`/`markdown` editor draws a normal, editable buffer and the user
    only learns the truth from a failed Cmd+S."""
    assert mount._writable(os.path.join(snap, "notes.md")) is False
    assert mount._writable(os.path.join(snap, "new.md")) is False


def test_stat_still_reports_an_ordinary_file_as_writable(snap, outside):
    assert mount._writable(str(outside)) is True


# ------------------------------------------------- the guard is scoped, not global

def test_an_ordinary_path_is_untouched(snap, outside):
    """The guard must not creep: only the snapshot root is history."""
    resp = fs_mutate._fs_write({"path": str(outside), "content": "edited"},
                               x_fused="1")
    assert _status(resp) == 200, _data(resp)
    assert outside.read_text() == "edited"


def test_a_sibling_of_the_snapshot_root_is_untouched(snap):
    """`app-versions-notes` shares a prefix with `app-versions` and is not it —
    the guard compares path SEGMENTS, never string prefixes."""
    near = os.path.join(storage.home_dir(), "app-versions-notes")
    os.makedirs(near, exist_ok=True)
    target = os.path.join(near, "f.txt")
    resp = fs_mutate._fs_write({"path": target, "content": "fine"}, x_fused="1")
    assert _status(resp) == 200, _data(resp)


def test_the_guard_reads_the_live_home_dir(tmp_path, monkeypatch):
    """Resolved per call, not at import: home_dir() depends on FUSED_RENDER_HOME
    and the branch ref, and a value frozen at import time would guard a directory
    the server is not using."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "a"))
    a = os.path.join(storage.home_dir(), "app-versions", "k", "s")
    os.makedirs(a)
    assert mount._is_under_snapshot_root(os.path.join(a, "f")) is True
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "b"))
    assert mount._is_under_snapshot_root(os.path.join(a, "f")) is False
