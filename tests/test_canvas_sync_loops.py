"""Canvas sync, local loop: rows and source files (canvas_snapshot).

A source file with no `[[canvas.nodes]]` row is left out of the disk reading
the delta leg diffs (`compact_state_from_disk`); `reconcile_rows` gives it a
default row at push and after a pull.
Every test name carries "compact", "snapshot" or "delta" so the targeted
`-k` run picks it up.
"""
from __future__ import annotations

import os

import rtoml

from fused_render import canvas_snapshot as cs


def _clone(tmp_path, rows, files, edges=None):
    d = tmp_path / "clone"
    d.mkdir()
    doc = {"type": "canvas", "canvas": {"nodes": rows, "edges": edges or []}}
    (d / "canvas.toml").write_text(rtoml.dumps(doc))
    for name, text in files.items():
        (d / name).write_text(text)
    return str(d)


def _rows(clone_dir):
    doc = rtoml.loads(open(os.path.join(clone_dir, "canvas.toml")).read())
    return doc["canvas"]["nodes"]


def _drop_rows(clone_dir, keep):
    doc = rtoml.loads(open(os.path.join(clone_dir, "canvas.toml")).read())
    doc["canvas"]["nodes"] = [r for r in doc["canvas"]["nodes"] if r["udfName"] in keep]
    open(os.path.join(clone_dir, "canvas.toml"), "w").write(rtoml.dumps(doc))


def _add_row(clone_dir, row):
    doc = rtoml.loads(open(os.path.join(clone_dir, "canvas.toml")).read())
    doc["canvas"]["nodes"].append(row)
    open(os.path.join(clone_dir, "canvas.toml"), "w").write(rtoml.dumps(doc))


A_ROW = {"udfName": "a", "x": 10, "y": 20, "width": 700, "height": 300,
         "zIndex": 4, "title": "a"}
U4_ROW = {"udfName": "udf_4", "x": 10, "y": 360, "width": 700, "height": 500,
          "zIndex": 5, "title": "udf_4"}


def test_compact_and_reconcile_take_the_files_as_the_nodes(tmp_path):
    folder = {"udfName": "F", "folderName": "F", "type": "udf-folder", "x": 1000, "y": 0,
              "height": 800, "childUdfOrder": ["b", "gone", "nobody"]}
    rows = [dict(A_ROW), {"udfName": "gone", "x": 0, "y": 0}, dict(A_ROW, x=99), folder,
            {"udfName": "b", "x": 40, "y": 75, "height": 5000}]
    d = _clone(tmp_path, rows, {"a.py": "A", "b.json": "{}", "orphan.py": "o", "note.md": "n",
                                "_init.py": "", ".hidden.py": "", "CLAUDE.md": "",
                                "late.py": "", "nobody.py": "", "secret.py": "",
                                ".fusedignore": "secret.py\n"},
               edges=[["a", "gone"], ["a", "b"], ["F", "a"]])
    disk = cs.compact_state_from_disk(d)
    assert disk["order"] == ["a", "F", "b"] and disk["edges"] == [["a", "b"], ["F", "a"]]
    doc = rtoml.loads(open(os.path.join(d, "canvas.toml")).read())
    del doc["type"]
    open(os.path.join(d, "canvas.toml"), "w").write(rtoml.dumps(doc))
    old = open(os.path.join(d, "canvas.toml"), "rb").read()
    trashed = []
    res = cs.reconcile_rows(d, pending=["late"], skip=["CLAUDE.md"],
                            backup=lambda rel, data: trashed.append((rel, data)))
    assert res == {"changed": True, "dropped": ["gone", "a"], "added": ["nobody", "note", "orphan"]}
    assert trashed == [("canvas.toml", old)]
    doc = rtoml.loads(open(os.path.join(d, "canvas.toml")).read())
    assert doc["type"] == "canvas"
    assert doc["canvas"]["edges"] == [["a", "b"], ["F", "a"]]
    assert [(r["udfName"], r.get("x"), r.get("y"), r.get("width"), r.get("height"))
            for r in doc["canvas"]["nodes"]] == [
        ("a", 10, 20, 700, 300), ("F", 1000, 0, None, 5690), ("b", 40, 75, None, 5000),
        ("nobody", 40, 5150, 700, 500), ("note", 10, 875, 350, 250),
        ("orphan", 10, 1200, 700, 500)]
    assert doc["canvas"]["nodes"][1]["childUdfOrder"] == ["b", "nobody"]
    assert cs.reconcile_rows(d, pending=["late"], skip=["CLAUDE.md"])["changed"] is False


def test_delta_retypes_a_node_whose_file_changed_kind(tmp_path):
    """Claude turned `w.json` into `w.html`. The frame's Udf carries its kind
    in metadata and never re-reads it from `file`, so a plain upsert would
    leave a JSON-UI widget holding HTML. The delta is delete + full upsert
    under the same name, and the edge set rides along (the delete drops the
    node's edges in the frame)."""
    w_row = {"udfName": "w", "x": 30, "y": 40, "width": 500, "height": 300, "zIndex": 2,
             "title": "w"}
    d = _clone(tmp_path, [dict(A_ROW), w_row], {"a.py": "A", "w.json": "{}"},
               edges=[["a", "w"]])
    disk = cs.compact_state_from_disk(d)
    view = cs.held_view(disk, ["a", "w"], edges=[["a", "w"]], ids={},
                        bodies={"a": "A", "w": "{}"}, embed_rows=None)
    assert view["files"]["w"] == "w.json"
    os.remove(os.path.join(d, "w.json"))
    with open(os.path.join(d, "w.html"), "w") as fh:
        fh.write("<p>hi</p>")
    ops = cs.diff_views(cs.compact_state_from_disk(d), view)
    assert [o["op"] for o in ops] == ["delete", "upsert", "edges"]
    assert ops[0]["name"] == "w"
    up = ops[1]
    assert up["name"] == "w" and up["file"] == "w.html" and up["code"] == "<p>hi</p>"
    assert up["node"]["position"] == {"x": 30, "y": 40}   # the node keeps its place
    assert up["row"]["udfName"] == "w"
    assert ops[2]["set"] == [["a", "w"]]


def test_delta_same_kind_body_change_is_a_plain_upsert(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A"})
    disk = cs.compact_state_from_disk(d)
    view = cs.held_view(disk, ["a"], edges=[], ids={}, bodies={"a": "A"}, embed_rows=None)
    with open(os.path.join(d, "a.py"), "w") as fh:
        fh.write("B")
    ops = cs.diff_views(cs.compact_state_from_disk(d), view)
    assert [o["op"] for o in ops] == ["upsert"]
    assert ops[0]["code"] == "B"


def test_snapshot_deleting_a_node_reports_its_file(tmp_path):
    """The host erases the chats filed on a deleted node's file: the snapshot
    result names the files it removed (a folder in `deleted` has none)."""
    b_row = {"udfName": "b", "x": 1, "y": 2, "width": 700, "height": 300, "zIndex": 5,
             "title": "b"}
    d = _clone(tmp_path, [dict(A_ROW), b_row], {"a.py": "A", "b.json": "{}"})
    prev = cs.compact_state_from_disk(d)
    new = _snapshot_new([_studio_node("a")], [{"slug": "a", "file": "a.py", "id": "ida"}])
    trashed = []
    result = cs.apply_snapshot(d, new, prev, backup=lambda rel, data: trashed.append(rel))
    assert result["deleted"] == ["b"]
    assert result["deleted_files"] == ["b.json"]
    assert trashed == ["b.json"]
    assert not os.path.exists(os.path.join(d, "b.json"))


def _snapshot_new(nodes, udfs, base_nodes=None):
    """A compact_state the way compact_state() builds one."""
    body = {
        "dashboard_body": {"studioState": {"nodes": nodes, "edges": []}},
        "udfs": udfs,
    }
    if base_nodes is not None:
        body["base_nodes"] = base_nodes
    return cs.compact_state(body)


def _studio_node(name, x=10, y=20):
    return {"id": f"udf-node-{name}", "type": "simple-udf",
            "position": {"x": x, "y": y}, "width": 700, "height": 300, "zIndex": 4,
            "data": {"udfName": name, "nodeTitle": name}}


def test_snapshot_new_udf_without_node_gets_file_but_no_row(tmp_path):
    """The embed HAS the node (placed where the user dropped it); a row
    invented here would move it. File only, slug reported rowless."""
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A"})
    udfs = [{"slug": "a", "file": "a.py", "id": "sid-a"},
            {"slug": "udf_4", "file": "udf_4.py",
             "udf_body": '{"code": "@fused.udf\\ndef udf(): pass\\n"}'}]
    new = _snapshot_new([_studio_node("a")], udfs, base_nodes=["a"])
    assert new["new_files"] == [{"file": "udf_4.py",
                                 "body": "@fused.udf\ndef udf(): pass\n"}]
    toml = os.path.join(d, "canvas.toml")
    before = open(toml).read()
    result = cs.apply_snapshot(d, new, None)
    assert result["created"] == ["udf_4.py"]
    assert result["rowless_files"] == ["udf_4"]
    assert result["changed"] is True
    assert open(os.path.join(d, "udf_4.py")).read() == "@fused.udf\ndef udf(): pass\n"
    assert open(toml).read() == before            # no row written
    # The embed's next snapshot brings the node where the user put it.
    nxt = _snapshot_new([_studio_node("a"), _studio_node("udf_4", x=900, y=700)], udfs)
    r2 = cs.apply_snapshot(d, nxt, None)
    assert r2["rowless_files"] == [] and r2["upserted"] == ["udf_4"]
    row = _rows(d)[1]
    assert (row["udfName"], row["x"], row["y"]) == ("udf_4", 900, 700)


def test_snapshot_rowless_file_the_embed_dropped_is_trashed(tmp_path):
    """The user deletes the new UDF before its node ever reached disk: the
    next snapshot lists neither, so the file goes (else the push would
    publish it back as a UDF)."""
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A"})
    udfs_a = [{"slug": "a", "file": "a.py", "id": "sid-a"}]
    new_udf = {"slug": "udf_4", "file": "udf_4.py", "udf_body": '{"code": "c"}'}
    r1 = cs.apply_snapshot(d, _snapshot_new([_studio_node("a")], udfs_a + [new_udf], ["a"]),
                           None)
    assert r1["rowless_files"] == ["udf_4"]
    # Still listed (nodes lagging): kept.
    r2 = cs.apply_snapshot(d, _snapshot_new([_studio_node("a")], udfs_a + [new_udf]), None,
                           rowless_prev=r1["rowless_files"])
    assert r2["deleted"] == [] and os.path.exists(os.path.join(d, "udf_4.py"))
    assert r2["rowless_files"] == ["udf_4"]
    # Gone from the embed: trashed, toml untouched.
    trashed = []
    toml = open(os.path.join(d, "canvas.toml")).read()
    r3 = cs.apply_snapshot(d, _snapshot_new([_studio_node("a")], udfs_a), None,
                           backup=lambda rel, data: trashed.append(rel),
                           rowless_prev=["udf_4"])
    assert r3["deleted"] == ["udf_4"] and r3["changed"] is True
    assert trashed == ["udf_4.py"]
    assert not os.path.exists(os.path.join(d, "udf_4.py"))
    assert open(os.path.join(d, "canvas.toml")).read() == toml
    assert r3["rowless_files"] == []


def test_compact_reading_leaves_rowless_files_out(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "udf_4.py": "c", "z.py": "z"})
    disk = cs.compact_state_from_disk(d)
    assert disk["order"] == ["a"] and set(disk["files"]) == {"a"}
    assert not [o for o in cs.diff_views(disk, None) if o.get("name") in ("udf_4", "z")]


def test_snapshot_new_udf_with_node_gets_its_own_row(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A"})
    new = _snapshot_new(
        [_studio_node("a"), _studio_node("udf_4", x=900)],
        [{"slug": "a", "file": "a.py", "id": "sid-a"},
         {"slug": "udf_4", "file": "udf_4.py", "udf_body": '{"code": "x"}'}],
        base_nodes=["a"],
    )
    result = cs.apply_snapshot(d, new, None)
    assert result["upserted"] == ["udf_4"]
    assert [r["udfName"] for r in _rows(d)] == ["a", "udf_4"]
    assert _rows(d)[1]["x"] == 900


def test_snapshot_new_file_with_existing_disk_row_keeps_that_row(tmp_path):
    """Claude already wrote the row; the embed ships only the file."""
    d = _clone(tmp_path, [dict(A_ROW), {"udfName": "udf_4", "x": 5, "y": 6}], {"a.py": "A"})
    new = _snapshot_new(
        [_studio_node("a")],
        [{"slug": "a", "file": "a.py", "id": "sid-a"},
         {"slug": "udf_4", "file": "udf_4.py", "udf_body": '{"code": "x"}'}],
        base_nodes=["a"],
    )
    result = cs.apply_snapshot(d, new, None)
    assert result["created"] == ["udf_4.py"]
    rows = _rows(d)
    assert [r["udfName"] for r in rows] == ["a", "udf_4"]
    assert rows[1] == {"udfName": "udf_4", "x": 5, "y": 6}


def test_snapshot_existing_behaviours_kept_alongside_rowless_file(tmp_path):
    """Deletion and embed_rows key deletion still apply in the snapshot
    that also creates a row-less file."""
    d = _clone(tmp_path,
               [dict(A_ROW, description="old"), {"udfName": "b", "x": 0, "y": 0}],
               {"a.py": "A", "b.py": "B"}, edges=[["a", "b"]])
    trashed = []
    prev = cs.compact_state_from_disk(d)
    prev["embed_rows"] = {"a": {"udfName": "a", "description": "old"}}
    new = _snapshot_new(
        [_studio_node("a")],
        [{"slug": "a", "file": "a.py", "id": "sid-a"},
         {"slug": "n", "file": "n.md", "file_body": "note"}],
    )
    result = cs.apply_snapshot(d, new, prev, backup=lambda rel, data: trashed.append(rel))
    assert result["deleted"] == ["b"]
    assert trashed == ["b.py"]
    assert not os.path.exists(os.path.join(d, "b.py"))
    assert result["rowless_files"] == ["n"]
    assert open(os.path.join(d, "n.md")).read() == "note"
    rows = {r["udfName"]: r for r in _rows(d)}
    assert set(rows) == {"a"}
    assert "description" not in rows["a"]      # embed_rows key deletion
    doc = rtoml.loads(open(os.path.join(d, "canvas.toml")).read())
    assert doc["canvas"]["edges"] == []


def test_snapshot_deleting_a_rowless_node_removes_file(tmp_path):
    """The frame holds udf_4, its row left disk, then the user deleted it:
    the file goes too, or the next push would publish it back."""
    d = _clone(tmp_path, [dict(A_ROW), dict(U4_ROW)], {"a.py": "A", "udf_4.py": "x"})
    view = cs.compact_state_from_disk(d)
    _drop_rows(d, {"a"})
    trashed = []
    new = _snapshot_new([_studio_node("a")], [{"slug": "a", "file": "a.py", "id": "sid-a"}])
    result = cs.apply_snapshot(d, new, view, backup=lambda rel, data: trashed.append(rel))
    assert result["deleted"] == ["udf_4"]
    assert result["changed"] is True
    assert trashed == ["udf_4.py"]
    assert not os.path.exists(os.path.join(d, "udf_4.py"))


def test_snapshot_rowless_rename_moves_file_not_trash(tmp_path):
    """A row-less node renamed without a hint: its id reappears under the
    new name, so the file follows the name."""
    d = _clone(tmp_path, [dict(A_ROW), dict(U4_ROW)], {"a.py": "A", "udf_4.py": "code4"})
    view = cs.compact_state_from_disk(d, {"udf_4": "sid-4"})
    _drop_rows(d, {"a"})
    trashed = []
    new = _snapshot_new(
        [_studio_node("a"), _studio_node("renamed", x=10, y=360)],
        [{"slug": "a", "file": "a.py", "id": "sid-a"},
         {"slug": "renamed", "file": "renamed.py", "id": "sid-4"}],
    )
    result = cs.apply_snapshot(d, new, view, backup=lambda rel, data: trashed.append(rel))
    assert result["renamed"] == [["udf_4", "renamed"]]
    assert result["deleted"] == []
    assert trashed == []
    assert open(os.path.join(d, "renamed.py")).read() == "code4"
    assert not os.path.exists(os.path.join(d, "udf_4.py"))
    assert [r["udfName"] for r in _rows(d)] == ["a", "renamed"]


def test_snapshot_stale_nodes_keep_a_rowless_file_that_got_its_row(tmp_path):
    """The embed's `nodes` still lag its `udfs` on the NEXT autosave, after
    the file got a row some other way (Claude wrote it, a pull brought the
    server's) and the delta delivered it (view holds it): the UDF the
    snapshot still lists is not a deletion — row and file survive."""
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A"})
    udfs = [{"slug": "a", "file": "a.py", "id": "sid-a"},
            {"slug": "udf_4", "file": "udf_4.py", "udf_body": '{"code": "c"}'}]
    first = cs.apply_snapshot(d, _snapshot_new([_studio_node("a")], udfs, ["a"]), None)
    assert first["rowless_files"] == ["udf_4"]
    _add_row(d, dict(U4_ROW))
    view = cs.compact_state_from_disk(d)
    assert "udf_4" in view["nodes"]
    trashed = []
    again = cs.apply_snapshot(d, _snapshot_new([_studio_node("a")], udfs), view,
                              backup=lambda rel, data: trashed.append(rel),
                              rowless_prev=first["rowless_files"])
    assert again["deleted"] == [] and trashed == []
    assert again["deferred_deletes"] == ["udf_4"]
    assert again["rowless_files"] == []           # its row is already there
    assert [r["udfName"] for r in _rows(d)] == ["a", "udf_4"]
    assert os.path.exists(os.path.join(d, "udf_4.py"))


def test_snapshot_deferred_delete_kept_in_view_then_deleted(tmp_path):
    """Snapshot 1 lists X's UDF but not its node: deferred, not deleted, and
    the host's view keeps X (so no delta re-adds it). Snapshot 2 lists
    neither: X is deleted and its file trashed."""
    d = _clone(tmp_path, [dict(A_ROW), {"udfName": "x", "x": 0, "y": 400}],
               {"a.py": "A", "x.py": "X"})
    view0 = cs.compact_state_from_disk(d, {"a": "sid-a", "x": "sid-x"})
    udfs_a = [{"slug": "a", "file": "a.py", "id": "sid-a"}]
    udfs_ax = udfs_a + [{"slug": "x", "file": "x.py", "id": "sid-x"}]

    new1 = _snapshot_new([_studio_node("a")], udfs_ax)
    r1 = cs.apply_snapshot(d, new1, view0)
    assert r1["deleted"] == [] and r1["deferred_deletes"] == ["x"]
    assert os.path.exists(os.path.join(d, "x.py"))

    # The host's refresh: names = snapshot nodes | deferred deletes.
    disk1 = cs.compact_state_from_disk(d, {"a": "sid-a", "x": "sid-x"})
    bodies = dict(view0["bodies"])
    view1 = cs.held_view(disk1, new1["nodes"], edges=new1["edges"], ids=new1["ids"],
                         bodies=bodies, embed_rows=new1["nodes"],
                         extra_names=r1["deferred_deletes"])
    assert "x" in view1["nodes"] and "x" in view1["order"]
    assert not [o for o in cs.diff_views(disk1, view1)
                if o["op"] == "upsert" and o["name"] == "x"]
    # Without the extra name the next delta would re-add x (the lost delete).
    bare = cs.held_view(disk1, new1["nodes"], edges=new1["edges"], ids=new1["ids"],
                        bodies=bodies, embed_rows=new1["nodes"])
    assert [o["name"] for o in cs.diff_views(disk1, bare) if o["op"] == "upsert"] == ["x"]

    trashed = []
    new2 = _snapshot_new([_studio_node("a")], udfs_a)
    r2 = cs.apply_snapshot(d, new2, view1, backup=lambda rel, data: trashed.append(rel))
    assert r2["deleted"] == ["x"] and r2["deferred_deletes"] == []
    assert trashed == ["x.py"]
    assert not os.path.exists(os.path.join(d, "x.py"))
    assert [r["udfName"] for r in _rows(d)] == ["a"]


def test_snapshot_rowless_delete_leaves_toml_untouched(tmp_path):
    """Only the file goes: canvas.toml is not rewritten for nothing."""
    d = _clone(tmp_path, [dict(A_ROW), dict(U4_ROW)], {"a.py": "A", "udf_4.py": "x"})
    view = cs.compact_state_from_disk(d)
    _drop_rows(d, {"a"})
    toml = os.path.join(d, "canvas.toml")
    before = (open(toml).read(), os.stat(toml).st_mtime_ns)
    new = _snapshot_new([_studio_node("a")], [{"slug": "a", "file": "a.py", "id": "sid-a"}])
    result = cs.apply_snapshot(d, new, view)
    assert result["deleted"] == ["udf_4"] and result["changed"] is True
    assert (open(toml).read(), os.stat(toml).st_mtime_ns) == before


def test_snapshot_hidden_udf_row_beats_stale_node_mirror():
    """Hiding a node flips `udfs[].is_visible` only; the node's
    `data.config.visible` mirror keeps the value it was loaded with. The
    UDF row is the record, so the snapshot reads hidden — and a show
    (`is_visible: true` beside a stale `false`) reads visible again."""
    node = _studio_node("a")
    node["data"]["config"] = {"visible": True}
    new = _snapshot_new([node], [{"slug": "a", "file": "a.py", "id": "sid-a",
                                  "is_visible": False}])
    assert new["nodes"]["a"]["visible"] is False
    node["data"]["config"] = {"visible": False}
    new = _snapshot_new([node], [{"slug": "a", "file": "a.py", "id": "sid-a",
                                  "is_visible": True}])
    assert new["nodes"]["a"]["visible"] is True
    # No row flag at all: the mirror still stands in.
    new = _snapshot_new([node], [{"slug": "a", "file": "a.py", "id": "sid-a"}])
    assert new["nodes"]["a"]["visible"] is False


def test_snapshot_hidden_udf_lands_on_disk(tmp_path):
    """End to end on the clone: a hide snapshot writes `visible = false`."""
    d = _clone(tmp_path, [dict(A_ROW, visible=True)], {"a.py": "A"})
    view = cs.compact_state_from_disk(d)
    node = _studio_node("a")
    node["data"]["config"] = {"visible": True}
    new = _snapshot_new([node], [{"slug": "a", "file": "a.py", "id": "sid-a",
                                  "is_visible": False}])
    result = cs.apply_snapshot(d, new, view)
    assert result["changed"] is True
    assert _rows(d)[0]["visible"] is False
