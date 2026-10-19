"""Canvas sync, folder grammar (canvas_snapshot + the pull merge's check).

A folder is a `[[canvas.nodes]]` row with `type = "udf-folder"`; membership
lives only in its `childUdfOrder`, and a child's x/y are relative to the
folder's x/y. These pin that the sync keeps both halves together: the
three-way merge, folder rows written without `udfName`, synthesized rows,
the delta's `position`, and the folder checks on pull and snapshot.
"""
from __future__ import annotations

import json
import os

import rtoml

from fused_render import canvas_snapshot as cs


def folder(name, order, **kw):
    row = {"udfName": name, "type": "udf-folder", "folderName": name,
           "childUdfOrder": list(order), "x": 1000.0, "y": 1000.0, "zIndex": 0,
           "width": 1000, "height": 700, "title": name}
    row.update(kw)
    return row


def udf(name, x, y, **kw):
    row = {"udfName": name, "x": x, "y": y, "zIndex": 1, "width": 700,
           "height": 500, "title": name}
    row.update(kw)
    return row


def doc(rows, edges=None):
    return {"canvas": {"nodes": rows, "edges": edges or []}}


def by_name(merged):
    return {cs.row_name(r): r for r in merged["canvas"]["nodes"]}


def _clone(tmp_path, rows, files, edges=None):
    d = tmp_path / "clone"
    d.mkdir()
    (d / "canvas.toml").write_text(rtoml.dumps(doc(rows, edges)))
    for name, text in files.items():
        (d / name).write_text(text)
    return str(d)


def _disk_rows(clone_dir):
    return rtoml.loads(open(os.path.join(clone_dir, "canvas.toml")).read())["canvas"]["nodes"]


# -- 1. merge: membership and coordinates as a unit --------------------------

def test_merge_remote_nests_a_node_local_nests_another_both_stay_on_screen():
    base = cs.rows_of_doc(doc([folder("F", ["a"]), udf("a", 40.0, 60.0),
                               udf("b", 2000.0, 2000.0)]))
    local = doc([folder("F", ["a", "c"]), udf("a", 40.0, 60.0),
                 udf("b", 2000.0, 2000.0), udf("c", 40.0, 600.0)])
    remote = doc([folder("F", ["a", "b"]), udf("a", 40.0, 60.0), udf("b", 40.0, 600.0)])
    merged, _info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    # Both sides' additions survive the list merge …
    assert rows["F"]["childUdfOrder"] == ["a", "c", "b"]
    # … and b keeps the remote's coordinates, relative to the same folder.
    assert (rows["b"]["x"], rows["b"]["y"]) == (40.0, 600.0)
    assert (rows["c"]["x"], rows["c"]["y"]) == (40.0, 600.0)
    assert cs.folder_problems(merged) == []


def test_merge_remote_released_local_dragged_inside_converts_to_absolute():
    base = cs.rows_of_doc(doc([folder("F", ["b"]), udf("b", 40.0, 60.0)]))
    local = doc([folder("F", ["b"]), udf("b", 80.0, 120.0)])
    remote = doc([folder("F", []), udf("b", 1040.0, 1060.0)])
    merged, _info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert rows["F"]["childUdfOrder"] == []
    # Local's drag wins the coordinates; they were relative to F at 1000,1000.
    assert (rows["b"]["x"], rows["b"]["y"]) == (1080.0, 1120.0)


def test_merge_local_released_remote_moved_inside_converts_remote_coords():
    base = cs.rows_of_doc(doc([folder("F", ["b"]), udf("b", 40.0, 60.0)]))
    # Local released b (absolute coords, same spot on screen), untouched since.
    local = doc([folder("F", []), udf("b", 1040.0, 1060.0)])
    # Remote moved b inside F only along y.
    remote = doc([folder("F", ["b"]), udf("b", 40.0, 300.0)])
    merged, _info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert rows["F"]["childUdfOrder"] == []
    # x: only local changed it; y: both did, local wins. Both are local's
    # absolute values, which match local's membership: kept as they are.
    assert (rows["b"]["x"], rows["b"]["y"]) == (1040.0, 1060.0)


def test_merge_local_nest_with_its_own_relative_coords_is_kept():
    base = cs.rows_of_doc(doc([folder("F", []), udf("b", 2000.0, 2000.0)]))
    # Remote moved b (absolute), local nested it without moving it on screen
    # (relative coords = 2000 - 1000).
    local = doc([folder("F", ["b"]), udf("b", 1000.0, 1000.0)])
    remote = doc([folder("F", []), udf("b", 2500.0, 2000.0)])
    merged, _info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert rows["F"]["childUdfOrder"] == ["b"]
    # x came from local (both changed: local wins) — already relative to F.
    assert rows["b"]["x"] == 1000.0
    # y: local changed it (2000 → 1000), remote did not: local's, relative.
    assert rows["b"]["y"] == 1000.0


def test_merge_remote_absolute_coords_converted_into_local_folder():
    base = cs.rows_of_doc(doc([folder("F", []), udf("b", 2000.0, 2000.0)]))
    # Local only nested b (order change; b's row untouched).
    local = doc([folder("F", ["b"]), udf("b", 2000.0, 2000.0)])
    # Remote only moved b, absolutely.
    remote = doc([folder("F", []), udf("b", 2600.0, 2400.0)])
    merged, _info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert rows["F"]["childUdfOrder"] == ["b"]
    # The remote's absolute position, expressed relative to F.
    assert (rows["b"]["x"], rows["b"]["y"]) == (1600.0, 1400.0)


def test_merge_child_rides_with_a_folder_the_other_side_moved():
    base = cs.rows_of_doc(doc([folder("F", ["a"]), udf("a", 40.0, 60.0)]))
    local = doc([folder("F", ["a"], x=3000.0), udf("a", 40.0, 60.0)])
    remote = doc([folder("F", ["a"]), udf("a", 40.0, 90.0)])
    merged, _info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert rows["F"]["x"] == 3000.0
    # Same folder on every side: relative coords untouched, never shifted by
    # the folder's own move.
    assert (rows["a"]["x"], rows["a"]["y"]) == (40.0, 90.0)


def test_merge_folder_deleted_on_remote_child_coords_from_local_go_absolute():
    base = cs.rows_of_doc(doc([folder("F", ["a"]), udf("a", 40.0, 60.0)]))
    # Local moved a inside F (relative).
    local = doc([folder("F", ["a"]), udf("a", 100.0, 200.0)])
    # Remote deleted F, releasing a where it was (absolute).
    remote = doc([udf("a", 1040.0, 1060.0)])
    merged, info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert "F" in info["removed"] and "F" not in rows
    # Local's drag, through local's (now deleted) F.
    assert (rows["a"]["x"], rows["a"]["y"]) == (1100.0, 1200.0)


def test_merge_folder_deleted_on_local_child_coords_from_remote_go_absolute():
    base = cs.rows_of_doc(doc([folder("F", ["a"]), udf("a", 40.0, 60.0)]))
    # Local deleted F by hand without touching a's row.
    local = doc([udf("a", 40.0, 60.0)])
    # Remote moved a inside F.
    remote = doc([folder("F", ["a"]), udf("a", 100.0, 200.0)])
    merged, _info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert "F" not in rows            # local delete beats an untouched remote row
    assert (rows["a"]["x"], rows["a"]["y"]) == (1100.0, 1200.0)


def test_merge_drops_names_of_deleted_rows_and_never_lists_a_child_twice():
    base = cs.rows_of_doc(doc([folder("F", ["a"]), folder("G", []),
                               udf("a", 40.0, 60.0), udf("b", 0.0, 0.0)]))
    local = doc([folder("F", ["a", "b"]), folder("G", []),
                 udf("a", 40.0, 60.0), udf("b", 0.0, 0.0)])
    remote = doc([folder("F", ["a"]), folder("G", ["b", "gone"]),
                  udf("a", 40.0, 60.0), udf("b", 0.0, 0.0)])
    merged, _info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert rows["F"]["childUdfOrder"] == ["a", "b"]
    assert rows["G"]["childUdfOrder"] == []      # b: F (earlier) has it; gone: no row
    assert cs.folder_problems(merged) == []


# -- 1b. folder renames ------------------------------------------------------

def test_merge_remote_folder_rename_keeps_local_restyle():
    base = cs.rows_of_doc(doc([folder("F", ["a"]), udf("a", 40.0, 60.0)]))
    local = doc([folder("F", ["a"], folderColor="#ff0000"), udf("a", 40.0, 60.0)])
    remote = doc([folder("G", ["a"]), udf("a", 40.0, 60.0)])
    merged, info = cs.merge_toml(base, local, remote)
    folders = [r for r in merged["canvas"]["nodes"] if cs.is_folder(r)]
    assert len(folders) == 1
    g = folders[0]
    assert (g["udfName"], g["folderName"], g["title"]) == ("G", "G", "G")
    assert g["childUdfOrder"] == ["a"]
    assert g["folderColor"] == "#ff0000"
    assert info["renamed"] == [["F", "G"]]
    assert (by_name(merged)["a"]["x"], by_name(merged)["a"]["y"]) == (40.0, 60.0)


def test_merge_no_rename_pairing_when_local_changed_membership():
    base = cs.rows_of_doc(doc([folder("F", ["a"]), udf("a", 40.0, 60.0),
                               udf("b", 0.0, 0.0)]))
    local = doc([folder("F", ["a", "b"]), udf("a", 40.0, 60.0), udf("b", 0.0, 0.0)])
    remote = doc([folder("G", ["a"]), udf("a", 40.0, 60.0), udf("b", 0.0, 0.0)])
    merged, info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert info["renamed"] == []
    # Two folders survive, but a sits in only one of them: the earlier.
    assert rows["F"]["childUdfOrder"] == ["a", "b"]
    assert rows["G"]["childUdfOrder"] == []


def test_merge_local_folder_rename_wins_over_untouched_remote():
    base = cs.rows_of_doc(doc([folder("F", ["a"]), udf("a", 40.0, 60.0)]))
    local = doc([folder("G", ["a"]), udf("a", 40.0, 60.0)])
    remote = doc([folder("F", ["a"]), udf("a", 40.0, 90.0)])
    merged, _info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert [n for n, r in rows.items() if cs.is_folder(r)] == ["G"]
    assert (rows["a"]["x"], rows["a"]["y"]) == (40.0, 90.0)


# -- 2. folder rows without udfName ------------------------------------------

SKILL_FOLDER = {"type": "udf-folder", "folderName": "Section_1", "folderColor": "#9370DB40",
                "childUdfOrder": ["udf_0"], "x": -2600.0, "y": -350.0, "zIndex": 0,
                "width": 1000, "height": 700}


def test_folder_name_only_row_is_read_by_rows_of_doc_and_disk_reading(tmp_path):
    d = _clone(tmp_path, [dict(SKILL_FOLDER), udf("udf_0", 40.0, 60.0)],
               {"udf_0.py": "def udf(): pass\n"})
    reading = cs.rows_of_doc(doc([dict(SKILL_FOLDER), udf("udf_0", 40.0, 60.0)]))
    assert reading["order"] == ["Section_1", "udf_0"]
    assert reading["nodes"]["Section_1"]["udfName"] == "Section_1"
    disk = cs.compact_state_from_disk(d)
    assert disk["order"] == ["Section_1", "udf_0"]
    assert disk["nodes"]["Section_1"]["udfName"] == "Section_1"
    assert disk["missing"] == [] and disk["synthesized"] == []


def test_folder_name_only_row_survives_merge_and_gains_udf_name():
    raw = doc([dict(SKILL_FOLDER), udf("udf_0", 40.0, 60.0)])
    base = cs.rows_of_doc(raw)
    local = json.loads(json.dumps(raw))
    local["canvas"]["nodes"][1]["x"] = 41.0
    remote = json.loads(json.dumps(raw))
    remote["canvas"]["nodes"][1]["title"] = "t"
    merged, _info = cs.merge_toml(base, local, remote)
    rows = merged["canvas"]["nodes"]
    assert [cs.row_name(r) for r in rows] == ["Section_1", "udf_0"]
    assert rows[0]["udfName"] == "Section_1" and rows[0]["folderName"] == "Section_1"
    assert (rows[1]["x"], rows[1]["title"]) == (41.0, "t")


def test_snapshot_converges_a_folder_name_only_row_to_one_row(tmp_path):
    d = _clone(tmp_path, [dict(SKILL_FOLDER), udf("udf_0", 40.0, 60.0)],
               {"udf_0.py": "def udf(): pass\n"})
    body = {"dashboard_body": json.dumps({"studioState": {"nodes": [
        {"id": "udf-folder-Section_1", "type": "udf-folder",
         "position": {"x": -2600.0, "y": -350.0}, "zIndex": 0,
         "style": {"width": 1000, "height": 700},
         "data": {"folderName": "Section_1", "folderColor": "#9370DB40",
                  "childUdfOrder": ["udf_0"], "isLocked": False}},
        {"id": "n0", "type": "simple-udf", "position": {"x": 40.0, "y": 60.0},
         "zIndex": 1, "width": 700, "height": 500,
         "data": {"udfName": "udf_0", "config": {}}}], "edges": []}}),
        "udfs": [{"slug": "udf_0", "file": "udf_0.py", "id": "id0",
                  "file_body": "def udf(): pass\n"}]}
    res = cs.apply_snapshot(d, cs.compact_state(body), None)
    rows = _disk_rows(d)
    assert [(r.get("udfName"), r.get("folderName")) for r in rows] == [
        ("Section_1", "Section_1"), ("udf_0", None)]
    assert res["deleted"] == [] and res["folder_fixes"] == []


def test_persist_synthesized_counts_folder_name_only_rows(tmp_path):
    d = _clone(tmp_path, [dict(SKILL_FOLDER), udf("udf_0", 40.0, 60.0)],
               {"udf_0.py": "A", "Section_1.py": "not a node: the folder has this name",
                "extra.py": "B"})
    written = cs.persist_synthesized(d, ["Section_1", "extra"])
    assert written == ["extra"]
    rows = _disk_rows(d)
    assert [cs.row_name(r) for r in rows] == ["Section_1", "udf_0", "extra"]
    assert rows[0]["udfName"] == "Section_1"


def test_snapshot_deleting_a_folder_never_trashes_a_same_named_file(tmp_path):
    d = _clone(tmp_path, [folder("F", ["a"]), udf("a", 40.0, 60.0)],
               {"a.py": "A", "F.py": "someone else's file"})
    prev = cs.compact_state_from_disk(d)
    body = {"dashboard_body": json.dumps({"studioState": {"nodes": [
        {"id": "n0", "type": "simple-udf", "position": {"x": 1040.0, "y": 1060.0},
         "zIndex": 1, "width": 700, "height": 500,
         "data": {"udfName": "a", "config": {}}}], "edges": []}}),
        "udfs": [{"slug": "a", "file": "a.py", "id": "ida", "file_body": "A"}]}
    res = cs.apply_snapshot(d, cs.compact_state(body), prev)
    assert res["deleted"] == ["F"]
    assert os.path.isfile(os.path.join(d, "F.py"))


# -- 3. synthesized placement ------------------------------------------------

def test_synthesize_after_a_folder_child_is_placed_in_absolute_coords():
    rows = [folder("F", ["a"]), udf("a", 40.0, 60.0)]
    row = cs.synthesize_row(rows, "new", "new.py")
    assert (row["x"], row["y"]) == (1040.0, 1000.0 + 60.0 + 500 + 40)
    assert cs.folder_of(rows + [row]).get("new") is None


def test_synthesize_after_a_folder_clears_its_full_height():
    rows = [udf("a", 40.0, 60.0), folder("F", [])]
    row = cs.synthesize_row(rows, "new", "new.py")
    assert (row["x"], row["y"]) == (1000.0, 1000.0 + 700 + 40)


def test_compact_synthesized_row_after_folder_child(tmp_path):
    d = _clone(tmp_path, [folder("F", ["a"]), udf("a", 40.0, 60.0)],
               {"a.py": "A", "zz.py": "Z"})
    disk = cs.compact_state_from_disk(d)
    assert disk["synthesized"] == ["zz"]
    assert (disk["nodes"]["zz"]["x"], disk["nodes"]["zz"]["y"]) == (1040.0, 1600.0)


# -- 4. delta position only when it moved --------------------------------------

def _reading(rows):
    nodes = {cs.row_name(r): dict(r) for r in rows}
    return {"nodes": nodes, "order": list(nodes), "edges": [], "canvas": {},
            "files": {n: n + ".py" for n, r in nodes.items() if not cs.is_folder(r)},
            "ids": {}, "bodies": {}}


def test_delta_upsert_omits_position_when_unchanged():
    view = _reading([folder("F", []), udf("a", 40.0, 60.0)])
    disk = _reading([folder("F", ["a"]), udf("a", 40.0, 60.0, title="renamed")])
    ops = {op["name"]: op for op in cs.diff_views(disk, view) if op["op"] == "upsert"}
    assert "position" not in ops["a"]["node"]
    assert ops["a"]["node"]["data"]["nodeTitle"] == "renamed"
    assert ops["a"]["row"]["x"] == 40.0          # the row itself is whole
    assert "position" not in ops["F"]["node"]
    assert ops["F"]["node"]["data"]["childUdfOrder"] == ["a"]


def test_delta_upsert_keeps_position_when_moved_and_on_full_state():
    view = _reading([udf("a", 40.0, 60.0)])
    disk = _reading([udf("a", 40.0, 61.0)])
    (op,) = [o for o in cs.diff_views(disk, view) if o["op"] == "upsert"]
    assert op["node"]["position"] == {"x": 40.0, "y": 61.0}
    full = [o for o in cs.diff_views(disk, None) if o["op"] == "upsert"]
    assert full[0]["node"]["position"] == {"x": 40.0, "y": 61.0}


# -- 5. folder_problems, apply_snapshot normalisation ---------------------------

def test_folder_problems_names_each_break():
    rows = [folder("F", ["a", "ghost", "G", "a"]), folder("G", ["a"]),
            {"type": "udf-folder", "childUdfOrder": []}, udf("a", 0.0, 0.0)]
    problems = cs.folder_problems(doc(rows))
    assert any("'ghost', which is not a node" in p for p in problems), problems
    assert any("'G', which is a folder" in p for p in problems), problems
    assert any("lists 'a' twice" in p for p in problems), problems
    assert any("'a' is listed by folders 'F' and 'G'" in p for p in problems), problems
    assert any("neither udfName nor folderName" in p for p in problems), problems
    assert cs.folder_problems(cs.rows_of_doc(doc(rows[:2] + rows[3:]))) != []
    assert cs.folder_problems(doc([folder("F", ["a"]), udf("a", 0.0, 0.0)])) == []


def _folder_snapshot(order):
    return {"dashboard_body": json.dumps({"studioState": {"nodes": [
        {"id": "f", "type": "udf-folder", "position": {"x": 1000.0, "y": 1000.0},
         "zIndex": 0, "style": {"width": 1000, "height": 700},
         "data": {"udfName": "F", "folderName": "F", "childUdfOrder": order}},
        {"id": "n0", "type": "simple-udf", "position": {"x": 40.0, "y": 60.0},
         "zIndex": 1, "width": 700, "height": 500,
         "data": {"udfName": "a", "config": {}}}], "edges": []}}),
        "udfs": [{"slug": "a", "file": "a.py", "id": "ida", "file_body": "A"}]}


def test_snapshot_normalises_folder_lists_and_does_not_rewrite_for_nothing(tmp_path):
    d = _clone(tmp_path, [folder("F", ["a"]), folder("G", ["b"]),
                          udf("a", 40.0, 60.0), udf("b", 0.0, 0.0)],
               {"a.py": "A", "b.py": "B"})
    # The embed's F lists a name no row has, and b, which G (later in the
    # document) lists too. No prev: G and b are disk-only, never deleted.
    res = cs.apply_snapshot(d, cs.compact_state(_folder_snapshot(["a", "ghost", "b"])), None)
    rows = {cs.row_name(r): r for r in _disk_rows(d)}
    assert rows["F"]["childUdfOrder"] == ["a", "b"]     # F is earlier: keeps b
    assert rows["G"]["childUdfOrder"] == []
    assert len(res["folder_fixes"]) == 2, res["folder_fixes"]
    assert cs.folder_problems(_disk_rows(d)) == []
    # The same snapshot again: the ghost is dropped again, nothing written.
    before = os.stat(os.path.join(d, "canvas.toml")).st_mtime_ns
    res = cs.apply_snapshot(d, cs.compact_state(_folder_snapshot(["a", "ghost", "b"])), None)
    assert res["changed"] is False, res
    assert os.stat(os.path.join(d, "canvas.toml")).st_mtime_ns == before


# -- _rename_in_doc ------------------------------------------------------------

def test_rename_follows_into_every_folder_child_order(tmp_path):
    d = _clone(tmp_path, [folder("F", ["a", "b"]), folder("G", ["c"]),
                          udf("a", 0.0, 0.0), udf("b", 0.0, 0.0), udf("c", 0.0, 0.0)],
               {"a.py": "A", "b.py": "B", "c.py": "C"}, edges=[["a", "c"]])
    res = cs.apply_rename(d, "a", "a2", "a.py", "a2.py")
    assert res["changed"] and res["moved"] == "a2.py"
    rows = {cs.row_name(r): r for r in _disk_rows(d)}
    assert rows["F"]["childUdfOrder"] == ["a2", "b"]
    assert "a2" in rows and "a" not in rows


def test_rename_in_doc_renames_a_folder_name_only_row():
    canvas = {"nodes": [dict(SKILL_FOLDER), udf("udf_0", 0.0, 0.0)], "edges": []}
    assert cs._rename_in_doc(canvas, "Section_1", "Section_2")
    row = canvas["nodes"][0]
    assert (row["udfName"], row["folderName"]) == ("Section_2", "Section_2")
    assert cs._rename_in_doc(canvas, "udf_0", "udf_9")
    assert canvas["nodes"][0]["childUdfOrder"] == ["udf_9"]


# -- the pull merge's folder check ---------------------------------------------

def test_merge_remote_rolls_back_a_pull_that_breaks_folders(tmp_path, monkeypatch):
    import hashlib

    from fused_render import canvases as canvases_mod

    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(tmp_path))
    manager = canvases_mod._SyncManager("alpha")
    os.makedirs(manager.dir, exist_ok=True)
    good = rtoml.dumps(doc([folder("F", ["a"]), udf("a", 40.0, 60.0)])).encode()
    bad = rtoml.dumps(doc([folder("F", ["a", "ghost"]), udf("a", 40.0, 60.0)])).encode()
    with open(os.path.join(manager.dir, "canvas.toml"), "wb") as fh:
        fh.write(good)
    with open(os.path.join(manager.dir, "a.py"), "wb") as fh:
        fh.write(b"A")
    manager._base_files = {"canvas.toml": hashlib.md5(good).hexdigest(),
                           "a.py": hashlib.md5(b"A").hexdigest()}
    manager._base_toml = cs.rows_of_doc(rtoml.loads(good.decode()))
    # The CLI's validate passes: only the folder check can catch this.
    monkeypatch.setattr(manager, "_validate_clone", lambda: True)
    assert manager._merge_remote({"udfs": []}, {"canvas.toml": bad, "a.py": b"A"}) is True
    assert manager.merge_rollback_seq == 1
    with open(os.path.join(manager.dir, "canvas.toml"), "rb") as fh:
        assert fh.read() == good
    (event,) = [e for e in manager._log.since(0)["events"]
                if e["action"] == "pull rollback"]
    assert "'ghost'" in event["detail"] and "canvas.toml" in event["detail"], event
