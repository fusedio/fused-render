"""Canvas sync, local loop: every source file has a row (canvas_snapshot).

An orphan source file (a `.py/.json/.md/.html` in the clone root with no
`[[canvas.nodes]]` row) is given a SYNTHESIZED row — by the disk reading the
delta leg diffs (`compact_state_from_disk`), by the writer that persists it
(`persist_synthesized`), and by a snapshot whose new UDF arrived without its
node (`apply_snapshot`). Every test name carries "compact", "snapshot" or
"delta" so the targeted `-k` run picks it up.
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


A_ROW = {"udfName": "a", "x": 10, "y": 20, "width": 700, "height": 300,
         "zIndex": 4, "title": "a"}


def test_compact_orphan_py_synthesized_after_last_node(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "udf_4.py": "code4"})
    disk = cs.compact_state_from_disk(d)
    assert disk["synthesized"] == ["udf_4"]
    assert disk["order"] == ["a", "udf_4"]
    row = disk["nodes"]["udf_4"]
    assert row == {"udfName": "udf_4", "x": 10, "y": 20 + 300 + 40, "zIndex": 5,
                   "width": 700, "height": 500, "title": "udf_4", "visible": True,
                   "nodeDescriptionScaleFactor": 1.5}
    assert "type" not in row  # every non-folder node is simple-udf
    assert disk["files"]["udf_4"] == "udf_4.py"
    assert disk["bodies"]["udf_4"] == "code4"
    assert disk["missing"] == []
    # Nothing written: the reading only reports.
    assert [r["udfName"] for r in _rows(d)] == ["a"]


def test_compact_orphans_chain_in_slug_order(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "zz.py": "z", "bb.html": "<p/>"})
    disk = cs.compact_state_from_disk(d)
    assert disk["synthesized"] == ["bb", "zz"]
    assert disk["order"] == ["a", "bb", "zz"]
    bb, zz = disk["nodes"]["bb"], disk["nodes"]["zz"]
    assert bb["y"] == 20 + 300 + 40
    assert zz["y"] == bb["y"] + 500 + 40      # chained off the first synthesized row
    assert zz["zIndex"] == bb["zIndex"] + 1


def test_compact_orphan_on_empty_canvas_uses_origin(tmp_path):
    d = _clone(tmp_path, [], {"only.py": "x"})
    row = cs.compact_state_from_disk(d)["nodes"]["only"]
    assert (row["x"], row["y"], row["zIndex"]) == (100, 100, 1)


def test_compact_md_orphan_is_a_note_row(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "notes.md": "# hi\n"})
    disk = cs.compact_state_from_disk(d)
    row = disk["nodes"]["notes"]
    assert "type" not in row  # kind comes from the suffix, not the row
    assert (row["width"], row["height"]) == (350, 250)  # NODE_DEFAULTS.TEXTBOX
    assert row["textBoxColor"] == "#FFF9C4"
    assert row["textBoxGradient"] is True
    assert row["textBoxScaleFactor"] == 1.0
    assert row["textBoxAlignment"] == "left"
    assert "nodeDescriptionScaleFactor" not in row
    assert disk["files"]["notes"] == "notes.md"
    # The patch the frame merges carries the note styling.
    patch = cs.studio_patch_from_row(row)
    assert patch["data"]["textBoxColor"] == "#FFF9C4"
    assert patch["data"]["config"] == {"visible": True}


def test_compact_json_orphan_carries_widget_background(tmp_path):
    d = _clone(tmp_path, [], {"w.json": "{}"})
    row = cs.compact_state_from_disk(d)["nodes"]["w"]
    assert row["widgetBackgroundColor"] == "#2e2e2e"


def test_compact_ignored_basenames_never_synthesized(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {
        "a.py": "A", "CLAUDE.md": "seeded", ".fusedignore": "CLAUDE.md\n.fusedignore\nscratch_*.py\n",
        ".hidden.py": "x", "scratch_1.py": "x", "data.csv": "1,2", "canvas.toml.tmp": "",
    })
    os.mkdir(os.path.join(d, "sub.py"))  # a directory, not a source file
    disk = cs.compact_state_from_disk(d)
    assert disk["synthesized"] == []
    assert disk["order"] == ["a"]


def test_compact_stem_collision_resolves_like_find_source(tmp_path):
    d = _clone(tmp_path, [], {"dup.md": "m", "dup.py": "p"})
    disk = cs.compact_state_from_disk(d)
    assert disk["synthesized"] == ["dup"]
    assert disk["files"]["dup"] == "dup.py"
    assert "textBoxColor" not in disk["nodes"]["dup"]


def test_compact_row_with_missing_file_still_missing(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW), {"udfName": "gone", "x": 0, "y": 0}],
               {"a.py": "A", "orphan.py": "o"})
    disk = cs.compact_state_from_disk(d)
    assert disk["missing"] == ["gone"]
    assert disk["synthesized"] == ["orphan"]


def test_compact_persist_synthesized_writes_once_and_is_idempotent(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "udf_4.py": "x", "n.md": "t"})
    disk = cs.compact_state_from_disk(d)
    assert cs.persist_synthesized(d, disk["synthesized"]) == ["n", "udf_4"]
    rows = _rows(d)
    assert [r["udfName"] for r in rows] == ["a", "n", "udf_4"]
    # Byte-identical to the reading that reported them: the next reading
    # synthesizes nothing and its rows equal the earlier synthesized ones.
    again = cs.compact_state_from_disk(d)
    assert again["synthesized"] == []
    assert again["nodes"]["n"] == disk["nodes"]["n"]
    assert again["nodes"]["udf_4"] == disk["nodes"]["udf_4"]
    assert cs.diff_views(again, disk) == []
    mtime = os.stat(os.path.join(d, "canvas.toml")).st_mtime_ns
    assert cs.persist_synthesized(d, disk["synthesized"]) == []
    assert os.stat(os.path.join(d, "canvas.toml")).st_mtime_ns == mtime


def test_compact_persist_synthesized_only_named_and_present(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "b.py": "b", "c.py": "c"})
    disk = cs.compact_state_from_disk(d)
    os.remove(os.path.join(d, "c.py"))
    # 'c' vanished since the cut, 'zz' was never an orphan.
    assert cs.persist_synthesized(d, ["b", "c", "zz"]) == ["b"]
    assert _rows(d)[-1] == disk["nodes"]["b"]
    assert cs.persist_synthesized(d, []) == []


def test_delta_full_state_upserts_synthesized_with_code(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "udf_4.py": "code4", "n.md": "note"})
    disk = cs.compact_state_from_disk(d)
    ops = {o["name"]: o for o in cs.diff_views(disk, None) if o["op"] == "upsert"}
    assert ops["udf_4"]["code"] == "code4"
    assert ops["udf_4"]["file"] == "udf_4.py"
    assert ops["udf_4"]["row"] == disk["nodes"]["udf_4"]
    # 'n' (a 250-high note) sorts first: 20 + 300 + 40 = 360, then udf_4
    # at 360 + 250 + 40.
    assert ops["n"]["node"]["position"] == {"x": 10, "y": 360}
    assert ops["udf_4"]["node"]["position"] == {"x": 10, "y": 650}
    assert (ops["udf_4"]["row"]["width"], ops["udf_4"]["row"]["height"]) == (700, 500)
    assert ops["n"]["body"] == "note" and "code" not in ops["n"]


def test_delta_against_held_view_upserts_only_the_synthesized_node(tmp_path):
    """The embed holds `a` (a held_view of the snapshot that carried only
    `a`); the orphan is not in the view, so the next delta adds it."""
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "udf_4.py": "code4"})
    disk = cs.compact_state_from_disk(d)
    view = cs.held_view(disk, ["a"], edges=[], ids={}, bodies={"a": "A"}, embed_rows=None)
    assert "udf_4" not in view["nodes"]
    ops = cs.diff_views(disk, view)
    assert [o["op"] for o in ops] == ["upsert"]
    assert ops[0]["name"] == "udf_4"
    assert ops[0]["code"] == "code4"
    assert ops[0]["row"]["udfName"] == "udf_4"


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
    synthesized here would move it. File only, slug reported rowless."""
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
    assert result["synthesized"] == []
    assert result["changed"] is True
    assert open(os.path.join(d, "udf_4.py")).read() == "@fused.udf\ndef udf(): pass\n"
    assert open(toml).read() == before            # no row written
    # The embed's next snapshot brings the node where the user put it.
    nxt = _snapshot_new([_studio_node("a"), _studio_node("udf_4", x=900, y=700)], udfs)
    r2 = cs.apply_snapshot(d, nxt, None)
    assert r2["rowless_files"] == [] and r2["upserted"] == ["udf_4"]
    row = _rows(d)[1]
    assert (row["udfName"], row["x"], row["y"]) == ("udf_4", 900, 700)


def test_snapshot_rowless_file_deleted_in_grace_is_trashed(tmp_path):
    """The user deletes the new UDF before its node ever reached disk: the
    next snapshot lists neither, so the in-grace file goes (else it would be
    synthesized back once the grace window ends)."""
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
    assert cs.compact_state_from_disk(d)["synthesized"] == []


def test_compact_exclude_orphans_hides_in_grace_file_then_synthesizes(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "udf_4.py": "c", "z.py": "z"})
    held = cs.compact_state_from_disk(d, None, exclude_orphans=["udf_4"])
    assert "udf_4" not in held["nodes"] and "udf_4" not in held["order"]
    assert "udf_4" not in held["files"] and "udf_4" not in held["bodies"]
    assert held["synthesized"] == ["z"]           # other orphans unaffected
    assert held["missing"] == []
    assert not [o for o in cs.diff_views(held, None) if o.get("name") == "udf_4"]
    assert [s for s, _ in cs.orphan_sources(d, ["a"], exclude=["udf_4"])] == ["z"]
    # Grace over, still no row: a true orphan, synthesized as before.
    lifted = cs.compact_state_from_disk(d)
    assert lifted["synthesized"] == ["udf_4", "z"]
    assert lifted["files"]["udf_4"] == "udf_4.py"
    assert lifted["nodes"]["udf_4"] == cs.synthesize_row([dict(A_ROW)], "udf_4", "udf_4.py")


def test_snapshot_new_udf_with_node_is_not_synthesized(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A"})
    new = _snapshot_new(
        [_studio_node("a"), _studio_node("udf_4", x=900)],
        [{"slug": "a", "file": "a.py", "id": "sid-a"},
         {"slug": "udf_4", "file": "udf_4.py", "udf_body": '{"code": "x"}'}],
        base_nodes=["a"],
    )
    result = cs.apply_snapshot(d, new, None)
    assert result["synthesized"] == []
    assert result["upserted"] == ["udf_4"]
    assert [r["udfName"] for r in _rows(d)] == ["a", "udf_4"]
    assert _rows(d)[1]["x"] == 900


def test_snapshot_new_file_with_existing_disk_row_is_not_resynthesized(tmp_path):
    """Claude already wrote the row; the embed ships only the file."""
    d = _clone(tmp_path, [dict(A_ROW), {"udfName": "udf_4", "x": 5, "y": 6}], {"a.py": "A"})
    new = _snapshot_new(
        [_studio_node("a")],
        [{"slug": "a", "file": "a.py", "id": "sid-a"},
         {"slug": "udf_4", "file": "udf_4.py", "udf_body": '{"code": "x"}'}],
        base_nodes=["a"],
    )
    result = cs.apply_snapshot(d, new, None)
    assert result["synthesized"] == []
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
    assert result["rowless_files"] == ["n"] and result["synthesized"] == []
    assert open(os.path.join(d, "n.md")).read() == "note"
    rows = {r["udfName"]: r for r in _rows(d)}
    assert set(rows) == {"a"}
    assert "description" not in rows["a"]      # embed_rows key deletion
    doc = rtoml.loads(open(os.path.join(d, "canvas.toml")).read())
    assert doc["canvas"]["edges"] == []


def test_snapshot_deleting_unpersisted_synthesized_node_removes_file(tmp_path):
    """The frame got an orphan as a delta (view = that disk reading) before
    its row was persisted, then the user deleted it: the file goes too, or
    the next reading would re-synthesize it."""
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "udf_4.py": "x"})
    view = cs.compact_state_from_disk(d)
    assert view["synthesized"] == ["udf_4"]
    trashed = []
    new = _snapshot_new([_studio_node("a")], [{"slug": "a", "file": "a.py", "id": "sid-a"}])
    result = cs.apply_snapshot(d, new, view, backup=lambda rel, data: trashed.append(rel))
    assert result["deleted"] == ["udf_4"]
    assert result["changed"] is True
    assert trashed == ["udf_4.py"]
    assert not os.path.exists(os.path.join(d, "udf_4.py"))
    assert cs.compact_state_from_disk(d)["synthesized"] == []


def test_snapshot_rowless_rename_moves_file_not_trash(tmp_path):
    """A row-less (synthesized) node renamed without a hint: its id
    reappears under the new name, so the file follows the name."""
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "udf_4.py": "code4"})
    view = cs.compact_state_from_disk(d, {"udf_4": "sid-4"})
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


def test_snapshot_stale_nodes_keep_a_delivered_synthesized_node(tmp_path):
    """The embed's `nodes` still lag its `udfs` on the NEXT autosave, after
    the synthesized node was delivered (view holds it): the UDF the
    snapshot still lists is not a deletion — row and file survive."""
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A"})
    udfs = [{"slug": "a", "file": "a.py", "id": "sid-a"},
            {"slug": "udf_4", "file": "udf_4.py", "udf_body": '{"code": "c"}'}]
    first = cs.apply_snapshot(d, _snapshot_new([_studio_node("a")], udfs, ["a"]), None)
    assert first["rowless_files"] == ["udf_4"]
    # Grace over, still row-less: the cut synthesizes, the host persists,
    # the delta is acked.
    view = cs.compact_state_from_disk(d)
    assert view["synthesized"] == ["udf_4"]
    assert cs.persist_synthesized(d, view["synthesized"]) == ["udf_4"]
    assert "udf_4" in view["nodes"]
    trashed = []
    again = cs.apply_snapshot(d, _snapshot_new([_studio_node("a")], udfs), view,
                              backup=lambda rel, data: trashed.append(rel))
    assert again["deleted"] == [] and trashed == []
    assert again["deferred_deletes"] == ["udf_4"]
    assert again["rowless_files"] == []           # its row is already there
    assert [r["udfName"] for r in _rows(d)] == ["a", "udf_4"]
    assert os.path.exists(os.path.join(d, "udf_4.py"))


def test_snapshot_stale_nodes_keep_an_unpersisted_synthesized_node(tmp_path):
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "udf_4.py": "c"})
    view = cs.compact_state_from_disk(d)          # synthesized, not persisted
    new = _snapshot_new([_studio_node("a")],
                        [{"slug": "a", "file": "a.py", "id": "sid-a"},
                         {"slug": "udf_4", "file": "udf_4.py", "id": "sid-4"}])
    result = cs.apply_snapshot(d, new, view)
    assert result["deleted"] == []
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


def _orphans_with_ignore(tmp_path, ignore, files):
    d = _clone(tmp_path, [], dict(files, **{".fusedignore": ignore}))
    return [s for s, _ in cs.orphan_sources(d, [])]


def test_compact_fusedignore_anchored_pattern(tmp_path):
    # A leading `/` anchors to the clone root: for a root file, its basename.
    assert _orphans_with_ignore(tmp_path, "/x.py\n", {"x.py": "", "y.py": ""}) == ["y"]


def test_compact_fusedignore_negation_last_match_wins(tmp_path):
    files = {"keep.py": "", "drop.py": "", "n.md": ""}
    assert _orphans_with_ignore(tmp_path, "*.py\n!keep.py\n", files) == ["keep", "n"]


def test_compact_fusedignore_negation_then_ignore(tmp_path):
    files = {"keep.py": "", "n.md": ""}
    assert _orphans_with_ignore(tmp_path, "!keep.py\n*.py\n", files) == ["n"]


def test_compact_fusedignore_comments_blanks_dirs_and_paths(tmp_path):
    ignore = "# x.py\n\n   \ny.py/\nsub/z.py\n!\n"
    files = {"x.py": "", "y.py": "", "z.py": ""}
    # A comment is no rule, a directory-only rule never matches a file, a
    # pattern with a `/` never matches a root file, a bare `!` is skipped.
    assert _orphans_with_ignore(tmp_path, ignore, files) == ["x", "y", "z"]


def test_snapshot_rowless_delete_leaves_toml_untouched(tmp_path):
    """Only the file goes: canvas.toml is not rewritten for nothing."""
    d = _clone(tmp_path, [dict(A_ROW)], {"a.py": "A", "udf_4.py": "x"})
    view = cs.compact_state_from_disk(d)
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


PROBE_NOTE = ("# Sync probe\n\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\n"
              "About ~10 s, a <name> tag, a [lon, lat] pair, **bold `code` inside**.\n")
PROBE_NOTE_TIPTAP = ("# Sync probe\n\nab1234\n\nAbout \\~10 s, a &lt;name&gt; tag, "
                     "a \\[lon, lat\\] pair, **bold** `code` **inside**.")
BROAD_NOTE = ("Setext\n======\n\n* star\n+ plus\n\n> quote\nlazy\n\n***\n\n"
              "Footnote[^1], a_b_c, _f_ and __g__, 5 < 6 &amp; ~7.\n\nline one  \nline two\n\n"
              "    indented\n\n- [ ] task\n\n| k | v |\n|:--|--:|\n| `x_y` | **z** |\n")
BROAD_NOTE_TIPTAP = ("# Setext\n\n- star\n\n\n- plus\n\n> quote lazy\n\n---\n\n"
                     "Footnote\\[^1\\], a_b_c, *f* and **g**, 5 &lt; 6 & \\~7.\n\nline one\\\n"
                     "line two\n\n```\nindented\n```\n\n- [ ] task\n\nkv`x_y`**z**")


def test_compact_note_text_ignores_what_the_workbench_editor_reserializes():
    assert cs.note_text(PROBE_NOTE) == cs.note_text(PROBE_NOTE_TIPTAP)
    assert cs.note_text(BROAD_NOTE) == cs.note_text(BROAD_NOTE_TIPTAP)
    assert cs.note_text(PROBE_NOTE) != cs.note_text(PROBE_NOTE_TIPTAP.replace("About", "About."))


def test_compact_duplicate_node_rows_ignore_folders_sharing_a_name():
    rows = [{"udfName": "a"}, {"udfName": "b"}, {"udfName": "a", "x": 1},
            {"type": "udf-folder", "folderName": "b"}, {"type": "udf-folder", "folderName": "F"},
            {"type": "udf-folder", "folderName": "F"}]
    assert cs.duplicate_node_rows({"canvas": {"nodes": rows}}) == ["a"]
