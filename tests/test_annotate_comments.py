"""Tests for the annotate comments sidecar mirror
(fused_render/templates/annotate/annotate.py).

annotate.py is a stdlib-only runPython target (not a package module), so — like
test_claude_agent_sidecar.py — these load it via importlib and drive its
functions directly with a tmp_path target. The sidecar now lives under
home_dir()/sidecar/<mapped path>.json (D83-reversal), never next to the
TARGET file — see shared/appenv.py's sidecar_path. FUSED_RENDER_HOME is
pinned to an isolated tmp dir for every test so a real sidecar under the
developer's actual ~/.fused-render is never touched.

Semantics under test: the sidecar is a WRITE-ONLY LOG. Comments upsert by `id`
(update in place + bump updated_at, or append with recorded_at+updated_at); a
comment dropped from the incoming array is NEVER deleted — last-seen state
persists forever; only an id named in `deleted_ids` (same call, same atomic
write) is tombstoned with `deleted_at`, and the stamp is permanent — a stale
URL re-recording the id can't undo the delete. Unowned keys (claudeSessions/bookmarkHistory/lastSession) are
preserved through the read-merge-write.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

# os.access always says yes for root, so the chmod-based gates can't trip.
skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


def _load_annotate():
    path = os.path.join("fused_render", "templates", "annotate", "annotate.py")
    spec = importlib.util.spec_from_file_location("annotate_target", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def _sidecar(ann, f) -> Path:
    return Path(ann._sidecar_path(str(f)))


def _target(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    return f


def test_record_creates_comments_key(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    resp = ann._record(str(f), [
        {"id": "c1", "content": "hi", "createdAt": 1720000000000, "view": "_render"},
    ], [])
    assert resp == {"recorded": True, "count": 1, "deleted": 0}

    data = json.loads(_sidecar(ann, f).read_text())
    assert data["claudeSessions"] == []  # backfilled so a claude turn round-trips
    log = data["comments"]
    assert len(log) == 1
    e = log[0]
    assert e["id"] == "c1"
    assert e["content"] == "hi"
    assert e["createdAt"] == 1720000000000  # comment's own ms epoch, untouched
    assert e["recorded_at"] == e["updated_at"]  # server seconds, equal on first write


def test_second_record_same_id_updates_in_place(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    first = json.loads(_sidecar(ann, f).read_text())["comments"][0]

    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    log = json.loads(_sidecar(ann, f).read_text())["comments"]
    assert len(log) == 1  # not duplicated
    e = log[0]
    assert e["recorded_at"] == first["recorded_at"]  # first-seen time is stable
    assert e["updated_at"] >= first["updated_at"]     # bumped on every record


def test_resolved_change_flows_through_as_update(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1, "resolved": True}], [])

    log = json.loads(_sidecar(ann, f).read_text())["comments"]
    assert len(log) == 1
    assert log[0]["resolved"] is True


def test_dropped_comment_stays_in_sidecar(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    # Two comments recorded, then only the first is re-recorded (B deleted from
    # the URL). B must remain in the log — the sidecar never deletes.
    ann._record(str(f), [
        {"id": "A", "content": "keep", "createdAt": 1},
        {"id": "B", "content": "gone-from-url", "createdAt": 2},
    ], [])
    ann._record(str(f), [{"id": "A", "content": "keep", "createdAt": 1}], [])

    log = json.loads(_sidecar(ann, f).read_text())["comments"]
    ids = sorted(e["id"] for e in log)
    assert ids == ["A", "B"]
    b = next(e for e in log if e["id"] == "B")
    assert b["content"] == "gone-from-url"  # untouched, no deleted_at
    assert "deleted_at" not in b


def test_preserves_unowned_keys(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    sess = [{"id": "s1", "preview": "hi", "created_at": 1, "last_used": 1, "cwd": "/x"}]
    hist = [{"id": "bk-1", "search": "a=1", "recorded_at": 1.0, "updated_at": 1.0}]
    sidecar = _sidecar(ann, f)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps({
        "claudeSessions": sess,
        "bookmarkHistory": hist,
        "lastSession": "s1",
    }))

    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    data = json.loads(sidecar.read_text())
    assert data["claudeSessions"] == sess
    assert data["bookmarkHistory"] == hist
    assert data["lastSession"] == "s1"
    assert len(data["comments"]) == 1


def test_empty_array_is_no_op(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    resp = ann._record(str(f), [], [])
    assert resp == {"recorded": True, "count": 0, "deleted": 0}
    # A true no-op: nothing to record never touches disk, so no sidecar appears.
    assert not _sidecar(ann, f).exists()


def test_empty_array_leaves_existing_log_untouched(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    before = _sidecar(ann, f).read_text()

    ann._record(str(f), [], [])  # user cleared the URL — log must survive
    assert _sidecar(ann, f).read_text() == before


def test_main_dispatch_and_missing_file(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    assert ann.main(action="record", file=str(f),
                    comments=[{"id": "c1", "content": "x", "createdAt": 1}]) == \
        {"recorded": True, "count": 1, "deleted": 0}
    assert "error" in ann.main(action="record", file="")
    assert "error" in ann.main(action="bogus", file=str(f))

def test_deleted_ids_tombstone_in_same_write(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    ann._record(str(f), [
        {"id": "A", "content": "keep", "createdAt": 1},
        {"id": "B", "content": "bye", "createdAt": 2},
    ], [])
    # Delete B: absent from the array AND named in deleted_ids — one call, one
    # write, so a concurrent-record ordering race cannot drop the tombstone.
    resp = ann._record(str(f), [{"id": "A", "content": "keep", "createdAt": 1}], ["B"])
    assert resp == {"recorded": True, "count": 1, "deleted": 1}

    log = json.loads(_sidecar(ann, f).read_text())["comments"]
    b = next(e for e in log if e["id"] == "B")
    assert b["deleted_at"] == b["updated_at"]  # stamped, seconds
    a = next(e for e in log if e["id"] == "A")
    assert "deleted_at" not in a


def test_rerecording_keeps_tombstone(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    ann._record(str(f), [{"id": "A", "content": "hi", "createdAt": 1}], [])
    ann._record(str(f), [], ["A"])
    # A stale bookmarked URL still carrying A re-records it on its next save —
    # fields merge, but the tombstone is permanent (deleted stays deleted).
    ann._record(str(f), [{"id": "A", "content": "hi again", "createdAt": 1}], [])

    log = json.loads(_sidecar(ann, f).read_text())["comments"]
    assert log[0]["deleted_at"]  # survives the re-record
    assert log[0]["content"] == "hi again"


def test_deleted_ids_alone_still_writes_and_unknown_ignored(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    ann._record(str(f), [{"id": "A", "content": "hi", "createdAt": 1}], [])
    # Tombstone-only call (emptied URL) must still land on disk.
    resp = ann._record(str(f), [], ["A", "no-such-id"])
    assert resp == {"recorded": True, "count": 0, "deleted": 1}
    log = json.loads(_sidecar(ann, f).read_text())["comments"]
    assert log[0]["deleted_at"]
    # Unknown ids alone are a true no-op: nothing recorded, nothing stamped.
    before = _sidecar(ann, f).read_text()
    resp = ann._record(str(f), [], ["ghost"])
    assert resp == {"recorded": True, "count": 0, "deleted": 0}
    assert _sidecar(ann, f).read_text() == before


# ------------------------------------------------------- status (writability)

def test_status_writable_sidecar_dir(tmp_path):
    ann = _load_annotate()
    target = tmp_path / "page.html"
    target.write_text("<html></html>")
    assert ann.main(action="status", file=str(target)) == {"writable": True}


@skip_root
def test_status_readonly_sidecar_file(tmp_path):
    ann = _load_annotate()
    target = tmp_path / "page.html"
    target.write_text("<html></html>")
    sidecar = _sidecar(ann, target)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}")
    os.chmod(sidecar, 0o444)
    try:
        assert ann.main(action="status", file=str(target)) == {"writable": False}
    finally:
        os.chmod(sidecar, 0o644)


@skip_root
def test_status_readonly_parent_dir(tmp_path):
    # The sidecar's home-dir subtree doesn't exist yet, so writability walks up
    # to the nearest existing ancestor (nearest_existing_dir) — which is
    # tmp_path itself here, since FUSED_RENDER_HOME (tmp_path/home) hasn't been
    # created. An unwritable tmp_path therefore still means "not writable",
    # exactly as it did when the sidecar was a sibling of the target.
    ann = _load_annotate()
    target = tmp_path / "page.html"
    target.write_text("<html></html>")
    os.chmod(tmp_path, 0o555)
    try:
        assert ann.main(action="status", file=str(target)) == {"writable": False}
    finally:
        os.chmod(tmp_path, 0o755)


# --------------------------------------------------- read-only remote mounts
# D83-reversal: the sidecar now lives under home_dir()/sidecar/, never on the
# mounted file's own filesystem, so a read-only remote mount no longer has any
# bearing on whether its sidecar can be written or recorded to — the old
# sidecar-write incident (CacheMode=full 403-looping a doomed PutObject)
# structurally can't happen anymore, and the mount_read_only check that used
# to gate _sidecar_writable has been removed entirely.

@pytest.fixture
def ro_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    import fused_render.shell.mounts as mounts

    m = mounts.add_mount("pub", "pub-remote:bucket", read_only=True)
    mp = mounts.mountpoint(m)
    os.makedirs(mp)
    f = os.path.join(mp, "page.html")
    with open(f, "w") as fh:
        fh.write("<html></html>")
    return f


def test_status_writable_under_read_only_mount(ro_mount):
    ann = _load_annotate()
    assert ann.main(action="status", file=ro_mount) == {"writable": True}


def test_record_succeeds_under_read_only_mount(ro_mount):
    ann = _load_annotate()
    resp = ann._record(ro_mount, [
        {"id": "c1", "content": "hi", "createdAt": 1720000000000,
         "view": "_render"},
    ], [])
    assert resp == {"recorded": True, "count": 1, "deleted": 0}
    assert os.path.exists(ann._sidecar_path(ro_mount))


def test_ro_mount_flag_no_longer_affects_sidecar_writability(ro_mount):
    """FUSED_RENDER_RO_MOUNTS still gets set (real mount plumbing), but
    _sidecar_writable no longer consults it (D83-reversal) — the sidecar
    isn't on the mount anymore, so the flag is irrelevant to it."""
    assert os.environ["FUSED_RENDER_RO_MOUNTS"] == os.path.dirname(ro_mount)
    assert _load_annotate()._sidecar_writable(ro_mount) is True


def test_sidecar_computation_requires_appenv(ro_mount):
    """Unlike the old mount_read_only check this guarded, sidecar PATH
    computation now hard-depends on appenv.home_dir() (D83-reversal) — a copy
    of this folder taken without its `shared/` sibling can no longer degrade
    gracefully to pure os.access, since there's nowhere to put the sidecar
    without a home dir to root it under."""
    import builtins

    ann = _load_annotate()
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "appenv":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    saved = sys.modules.pop("appenv", None)
    builtins.__import__ = blocked
    try:
        with pytest.raises(ImportError):
            ann._sidecar_writable(ro_mount)
    finally:
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules["appenv"] = saved
