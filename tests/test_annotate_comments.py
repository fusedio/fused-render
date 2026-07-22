"""Tests for the annotate comments sidecar mirror
(fused_render/templates/annotate/annotate.py).

annotate.py is a stdlib-only runPython target (not a package module), so — like
test_claude_agent_sidecar.py — these load it via importlib and drive its
functions directly with a tmp_path target. The sidecar lives next to the TARGET
file (`<file>.json`), so no FUSED_RENDER_HOME / TestClient is involved.

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


def _sidecar(tmp_path):
    return tmp_path / "sample.html.json"


def _target(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    return f


def test_record_creates_comments_key(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    resp = ann._record(
        str(f),
        [
            {"id": "c1", "content": "hi", "createdAt": 1720000000000, "view": "_render"},
        ],
        [],
    )
    assert resp == {"recorded": True, "count": 1, "deleted": 0}

    data = json.loads(_sidecar(tmp_path).read_text())
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
    first = json.loads(_sidecar(tmp_path).read_text())["comments"][0]

    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    log = json.loads(_sidecar(tmp_path).read_text())["comments"]
    assert len(log) == 1  # not duplicated
    e = log[0]
    assert e["recorded_at"] == first["recorded_at"]  # first-seen time is stable
    assert e["updated_at"] >= first["updated_at"]  # bumped on every record


def test_resolved_change_flows_through_as_update(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1, "resolved": True}], [])

    log = json.loads(_sidecar(tmp_path).read_text())["comments"]
    assert len(log) == 1
    assert log[0]["resolved"] is True


def test_dropped_comment_stays_in_sidecar(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    # Two comments recorded, then only the first is re-recorded (B deleted from
    # the URL). B must remain in the log — the sidecar never deletes.
    ann._record(
        str(f),
        [
            {"id": "A", "content": "keep", "createdAt": 1},
            {"id": "B", "content": "gone-from-url", "createdAt": 2},
        ],
        [],
    )
    ann._record(str(f), [{"id": "A", "content": "keep", "createdAt": 1}], [])

    log = json.loads(_sidecar(tmp_path).read_text())["comments"]
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
    _sidecar(tmp_path).write_text(
        json.dumps(
            {
                "claudeSessions": sess,
                "bookmarkHistory": hist,
                "lastSession": "s1",
            }
        )
    )

    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    data = json.loads(_sidecar(tmp_path).read_text())
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
    assert not _sidecar(tmp_path).exists()


def test_empty_array_leaves_existing_log_untouched(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    ann._record(str(f), [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    before = _sidecar(tmp_path).read_text()

    ann._record(str(f), [], [])  # user cleared the URL — log must survive
    assert _sidecar(tmp_path).read_text() == before


def test_main_dispatch_and_missing_file(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    assert ann.main(
        action="record", file=str(f), comments=[{"id": "c1", "content": "x", "createdAt": 1}]
    ) == {"recorded": True, "count": 1, "deleted": 0}
    assert "error" in ann.main(action="record", file="")
    assert "error" in ann.main(action="bogus", file=str(f))


def test_deleted_ids_tombstone_in_same_write(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    ann._record(
        str(f),
        [
            {"id": "A", "content": "keep", "createdAt": 1},
            {"id": "B", "content": "bye", "createdAt": 2},
        ],
        [],
    )
    # Delete B: absent from the array AND named in deleted_ids — one call, one
    # write, so a concurrent-record ordering race cannot drop the tombstone.
    resp = ann._record(str(f), [{"id": "A", "content": "keep", "createdAt": 1}], ["B"])
    assert resp == {"recorded": True, "count": 1, "deleted": 1}

    log = json.loads(_sidecar(tmp_path).read_text())["comments"]
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

    log = json.loads(_sidecar(tmp_path).read_text())["comments"]
    assert log[0]["deleted_at"]  # survives the re-record
    assert log[0]["content"] == "hi again"


def test_deleted_ids_alone_still_writes_and_unknown_ignored(tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    ann._record(str(f), [{"id": "A", "content": "hi", "createdAt": 1}], [])
    # Tombstone-only call (emptied URL) must still land on disk.
    resp = ann._record(str(f), [], ["A", "no-such-id"])
    assert resp == {"recorded": True, "count": 0, "deleted": 1}
    log = json.loads(_sidecar(tmp_path).read_text())["comments"]
    assert log[0]["deleted_at"]
    # Unknown ids alone are a true no-op: nothing recorded, nothing stamped.
    before = _sidecar(tmp_path).read_text()
    resp = ann._record(str(f), [], ["ghost"])
    assert resp == {"recorded": True, "count": 0, "deleted": 0}
    assert _sidecar(tmp_path).read_text() == before


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
    sidecar = tmp_path / "page.html.json"
    sidecar.write_text("{}")
    os.chmod(sidecar, 0o444)
    try:
        assert ann.main(action="status", file=str(target)) == {"writable": False}
    finally:
        os.chmod(sidecar, 0o644)


@skip_root
def test_status_readonly_parent_dir(tmp_path):
    ann = _load_annotate()
    target = tmp_path / "page.html"
    target.write_text("<html></html>")
    os.chmod(tmp_path, 0o555)
    try:
        assert ann.main(action="status", file=str(target)) == {"writable": False}
    finally:
        os.chmod(tmp_path, 0o755)


# ------------------------------------------------- read-only remote mounts
# os.access(W_OK) lies under a read-only S3 mount (CacheMode=full: a write
# lands in the VFS cache and only 403s at the async upload — the sidecar-write
# incident). _sidecar_writable must consult the mount's read_only flag so the
# template shows its "history not saved" badge instead of looping the doomed
# upload. Commenting itself still works — the URL is the live store.

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


def test_status_not_writable_under_read_only_mount(ro_mount):
    ann = _load_annotate()
    assert ann.main(action="status", file=ro_mount) == {"writable": False}


def test_record_refuses_under_read_only_mount(ro_mount):
    ann = _load_annotate()
    with pytest.raises(PermissionError):
        ann._record(ro_mount, [
            {"id": "c1", "content": "hi", "createdAt": 1720000000000,
             "view": "_render"},
        ], [])
    # Nothing written next to the mounted file.
    assert not os.path.exists(ro_mount + ".json")
