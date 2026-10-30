"""Canvas sync, local loop: rows and source files (canvas_snapshot)."""
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
    note = {"udfName": "note", "folderName": "note", "type": "udf-folder", "x": 0, "y": 0}
    rows = [dict(A_ROW), {"udfName": "gone", "x": 0, "y": 0}, dict(A_ROW, x=99), folder,
            dict(folder, x=5, childUdfOrder=["orphan"]), note,
            {"udfName": "b", "x": 40, "y": 75, "height": 5000}, {"udfName": "_init", "x": 0, "y": 0}]
    d = _clone(tmp_path, rows, {"a.py": "A", "b.json": "{}", "orphan.py": "o", "note.md": "n",
                                "_init.py": "", ".hidden.py": "", "CLAUDE.md": "",
                                "late.py": "", "nobody.py": "", "secret.py": "",
                                ".fusedignore": "/secret.py\n"},
               edges=[["a", "gone"], ["a", "b"], ["F", "a"]])
    disk = cs.compact_state_from_disk(d)
    assert disk["order"] == ["a", "F", "note", "b"] and disk["nodes"]["a"]["x"] == 99
    assert list(disk["rowed"]) == ["a", "gone", "F", "note", "b", "_init"]
    assert disk["files"] == {"a": "a.py", "b": "b.json", "late": "late.py", "nobody": "nobody.py",
                             "orphan": "orphan.py"}
    assert disk["edges"] == [["a", "gone"], ["a", "b"], ["F", "a"]]
    view = cs.held_view(disk, ["a", "F", "note", "b", "gone"], embed_rows=None, ids={},
                        edges=[["a", "gone"], ["a", "b"], ["F", "a"]], bodies={"a": "A", "b": "{}"})
    os.remove(os.path.join(d, "b.json"))
    assert cs.diff_views(cs.compact_state_from_disk(d), view) == []
    (full_edges,) = [o for o in cs.diff_views(disk, None) if o["op"] == "edges"]
    assert full_edges["set"] == [["a", "b"], ["F", "a"]]
    open(os.path.join(d, "b.json"), "w").write("{}")
    doc = rtoml.loads(open(os.path.join(d, "canvas.toml")).read())
    del doc["type"]
    open(os.path.join(d, "canvas.toml"), "w").write(rtoml.dumps(doc))
    old = open(os.path.join(d, "canvas.toml"), "rb").read()
    trashed = []
    res = cs.reconcile_rows(d, pending=["late"],
                            backup=lambda rel, data: trashed.append((rel, data)))
    assert res == {"changed": True, "dropped": ["a"], "added": ["nobody", "orphan"]}
    assert trashed == [("canvas.toml", old)]
    doc = rtoml.loads(open(os.path.join(d, "canvas.toml")).read())
    assert doc["type"] == "canvas"
    assert doc["canvas"]["edges"] == [["a", "gone"], ["a", "b"], ["F", "a"]]
    assert [(r["udfName"], r.get("type"), r.get("x"), r.get("y"), r.get("height"), r.get("zIndex"))
            for r in doc["canvas"]["nodes"]] == [
        ("gone", None, 0, 0, None, None), ("a", None, 99, 20, 300, 4),
        ("F", "udf-folder", 1000, 0, 5690, None), ("F", "udf-folder", 5, 0, 800, None),
        ("note", "udf-folder", 0, 0, None, None), ("b", None, 40, 75, 5000, None),
        ("_init", None, 0, 0, None, None), ("nobody", None, 0, 5150, 500, 5),
        ("orphan", None, 40, 75, 500, 6)]
    assert doc["canvas"]["nodes"][-1] == {
        "udfName": "orphan", "x": 40, "y": 75, "zIndex": 6, "width": 700, "height": 500,
        "title": "orphan", "visible": True, "nodeDescriptionScaleFactor": 1.5}
    assert [r.get("childUdfOrder") for r in doc["canvas"]["nodes"][2:4]] == [
        ["b", "gone", "nobody"], ["orphan"]]
    assert cs.reconcile_rows(d, pending=["late"])["changed"] is False
    assert cs.reconcile_rows(d, publish=True) == {
        "changed": True, "dropped": ["gone", "_init"], "added": ["late"]}
    doc = rtoml.loads(open(os.path.join(d, "canvas.toml")).read())
    assert doc["canvas"]["edges"] == [["a", "b"], ["F", "a"]]
    assert [r["udfName"] for r in doc["canvas"]["nodes"]] == [
        "a", "F", "F", "note", "b", "nobody", "orphan", "late"]
    assert (doc["canvas"]["nodes"][-1]["x"], doc["canvas"]["nodes"][-1]["y"]) == (0, 5765)


def test_delta_and_snapshot_hold_a_node_through_a_two_step_rename(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW), {"udfName": "foo", "x": 1, "y": 2}],
               {"a.py": "A", "foo.py": "F"}, edges=[["a", "foo"]])
    disk = cs.compact_state_from_disk(d, {"foo": "sid-foo"})
    view = cs.held_view(disk, ["a", "foo"], edges=[["a", "foo"]], ids={"foo": "sid-foo"},
                        bodies={"a": "A", "foo": "F"}, embed_rows=None)
    doc = rtoml.loads(open(os.path.join(d, "canvas.toml")).read())
    doc["canvas"]["nodes"][1]["udfName"] = "bar"
    doc["canvas"]["edges"] = [["a", "bar"]]
    open(os.path.join(d, "canvas.toml"), "w").write(rtoml.dumps(doc))
    assert cs.diff_views(cs.compact_state_from_disk(d), view) == [{"op": "edges", "set": []}]
    udfs = [{"slug": "a", "file": "a.py", "id": "sid-a"},
            {"slug": "foo", "file": "foo.py", "id": "sid-foo"}]
    nodes = [_studio_node("a"), _studio_node("foo")]
    res = cs.apply_snapshot(d, _snapshot_new(nodes, udfs), view)
    assert res["upserted"] == [] and res["deleted"] == [] and res["fileless"] == []
    assert [r["udfName"] for r in _rows(d)] == ["a", "bar"]
    res = cs.apply_snapshot(d, _snapshot_new(nodes[:1], udfs[:1], base_nodes=["a", "foo"]), None)
    assert res["deleted"] == [] and os.path.exists(os.path.join(d, "foo.py"))
    os.replace(os.path.join(d, "foo.py"), os.path.join(d, "bar.py"))
    ops = cs.diff_views(cs.compact_state_from_disk(d), view)
    assert [(o["op"], o.get("name")) for o in ops] == [
        ("delete", "foo"), ("upsert", "bar"), ("edges", None)]
    assert ops[2]["set"] == [["a", "bar"]]


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
    assert "x" in view1["nodes"]
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
