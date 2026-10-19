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


def test_merge_keeps_unknown_names_and_never_lists_a_child_twice():
    base = cs.rows_of_doc(doc([folder("F", ["a"]), folder("G", []),
                               udf("a", 40.0, 60.0), udf("b", 0.0, 0.0)]))
    local = doc([folder("F", ["a", "b"]), folder("G", []),
                 udf("a", 40.0, 60.0), udf("b", 0.0, 0.0)])
    remote = doc([folder("F", ["a"]), folder("G", ["b", "gone"]),
                  udf("a", 40.0, 60.0), udf("b", 0.0, 0.0)])
    merged, info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert rows["F"]["childUdfOrder"] == ["a", "b"]
    # b: local nests it in F, so G drops it; "gone" names no row, as the
    # workbench leaves one behind on delete: tolerated, kept.
    assert rows["G"]["childUdfOrder"] == ["gone"]
    assert info["folder_fixes"] == ["folder 'G': dropped 'b' (folder 'F' keeps it)"]
    assert cs.folder_problems(merged) == []


def test_merge_both_sides_nest_one_child_in_different_folders_local_wins():
    base = cs.rows_of_doc(doc([folder("G", [], x=0.0, y=0.0),
                               folder("F", [], x=1000.0, y=0.0), udf("c", 500.0, 500.0)]))
    local = doc([folder("G", [], x=0.0, y=0.0), folder("F", ["c"], x=1000.0, y=0.0),
                 udf("c", 10.0, 10.0)])
    remote = doc([folder("G", ["c"], x=0.0, y=0.0), folder("F", [], x=1000.0, y=0.0),
                  udf("c", 20.0, 20.0)])
    merged, _info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    # G is first in the document, but local nested c in F: F keeps it, and
    # local's coordinates are already relative to F.
    assert rows["G"]["childUdfOrder"] == []
    assert rows["F"]["childUdfOrder"] == ["c"]
    assert (rows["c"]["x"], rows["c"]["y"]) == (10.0, 10.0)


def test_merge_two_folders_claiming_a_child_local_never_nested_first_wins():
    base = cs.rows_of_doc(doc([folder("G", []), folder("F", []), udf("c", 0.0, 0.0)]))
    # Neither nesting is local's: the remote doc lists c in G, local's own
    # stale F list (not a both-sides field) names it too, local's G does not.
    local = doc([folder("G", []), folder("F", []), udf("c", 0.0, 0.0)])
    remote = doc([folder("G", ["c"]), folder("F", ["c"]), udf("c", 0.0, 0.0)])
    merged, _info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert rows["G"]["childUdfOrder"] == ["c"] and rows["F"]["childUdfOrder"] == []


def test_merge_survives_junk_child_entries():
    base = cs.rows_of_doc(doc([folder("F", ["a"]), udf("a", 0.0, 0.0), udf("b", 0.0, 0.0)]))
    local = doc([folder("F", ["a", ["nested"], 7]), udf("a", 0.0, 0.0), udf("b", 0.0, 0.0)])
    remote = doc([folder("F", ["a", "b", "F"]), udf("a", 0.0, 0.0), udf("b", 0.0, 0.0)])
    merged, info = cs.merge_toml(base, local, remote)
    # Junk on local: no list merge (local wins the field), then the junk and
    # the folder's own name are dropped; nothing raises.
    assert by_name(merged)["F"]["childUdfOrder"] == ["a"]
    assert len(info["folder_fixes"]) == 2, info["folder_fixes"]


def test_merge_a_folder_and_a_node_sharing_a_name_merge_independently():
    base = cs.rows_of_doc(doc([folder("X", ["a"]), udf("a", 10.0, 10.0), udf("X", 500.0, 500.0)]))
    local = doc([folder("X", ["a"], folderColor="#f00"), udf("a", 20.0, 10.0),
                 udf("X", 500.0, 500.0)])
    remote = doc([folder("X", ["a"]), udf("a", 10.0, 10.0), udf("X", 600.0, 500.0),
                  udf("new", 0.0, 0.0)])
    merged, info = cs.merge_toml(base, local, remote)
    assert info["collisions"] == []
    rows = merged["canvas"]["nodes"]
    kinds = [(cs.is_folder(r), cs.row_name(r)) for r in rows]
    assert kinds == [(True, "X"), (False, "a"), (False, "X"), (False, "new")]
    assert rows[0]["folderColor"] == "#f00" and rows[0]["childUdfOrder"] == ["a"]
    assert rows[1]["x"] == 20.0                 # local's move of a
    assert rows[2]["x"] == 600.0                # the remote's move of the UDF X
    # The view leg still keys by name alone: first row wins, clash reported.
    assert cs.rows_of_doc(local)["order"] == ["X", "a"]
    assert cs.is_folder(cs.rows_of_doc(local)["nodes"]["X"])
    assert "a folder and a node share the name 'X'" in cs.folder_problems(local)


def test_merge_refuses_two_rows_of_one_kind_sharing_a_name():
    base = cs.rows_of_doc(doc([udf("a", 0.0, 0.0)]))
    local = doc([udf("a", 1.0, 0.0)])
    remote = doc([udf("a", 0.0, 0.0), udf("a", 9.0, 9.0), udf("new", 0.0, 0.0)])
    merged, info = cs.merge_toml(base, local, remote)
    assert merged is local and info["collisions"] == ["a"]
    assert "two rows of one kind share the name 'a'" in cs.folder_problems(remote)


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


def test_merge_remote_rename_pairs_when_local_added_a_child():
    base = cs.rows_of_doc(doc([folder("F", ["a", "b"]), udf("a", 10.0, 10.0),
                               udf("b", 10.0, 200.0), udf("c", 900.0, 900.0)]))
    local = doc([folder("F", ["a", "b", "c"]), udf("a", 10.0, 10.0),
                 udf("b", 10.0, 200.0), udf("c", 5.0, 400.0)])
    remote = doc([folder("G", ["a", "b"]), udf("a", 10.0, 10.0),
                  udf("b", 10.0, 200.0), udf("c", 900.0, 900.0)])
    merged, info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert info["renamed"] == [["F", "G"]]
    assert [n for n, r in rows.items() if cs.is_folder(r)] == ["G"]
    assert rows["G"]["childUdfOrder"] == ["a", "b", "c"]
    assert (rows["c"]["x"], rows["c"]["y"]) == (5.0, 400.0)     # local's, in G (was F)


def test_merge_remote_rename_that_added_a_child_pairs_with_a_moved_local_folder():
    base = cs.rows_of_doc(doc([folder("F", ["a", "b"], x=100.0, y=100.0),
                               udf("a", 10.0, 10.0), udf("b", 10.0, 200.0),
                               udf("c", 900.0, 900.0)]))
    local = doc([folder("F", ["a", "b"], x=300.0, y=300.0), udf("a", 10.0, 10.0),
                 udf("b", 10.0, 200.0), udf("c", 900.0, 900.0)])
    remote = doc([folder("G", ["a", "b", "c"], x=100.0, y=100.0), udf("a", 10.0, 10.0),
                  udf("b", 10.0, 200.0), udf("c", 700.0, 700.0)])
    merged, info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert info["renamed"] == [["F", "G"]]
    g = rows["G"]
    assert (g["x"], g["y"], g["childUdfOrder"]) == (300.0, 300.0, ["a", "b", "c"])
    # c's coordinates are the remote's, relative to G: it rides with G.
    assert (rows["c"]["x"], rows["c"]["y"]) == (700.0, 700.0)


def test_merge_remote_rename_pairs_on_half_of_three_or_more_children():
    base = cs.rows_of_doc(doc([folder("F", ["a", "b", "c"]), udf("a", 0.0, 0.0),
                               udf("b", 0.0, 0.0), udf("c", 0.0, 0.0)]))
    local = doc([folder("F", ["a", "b", "c"]), udf("a", 0.0, 0.0), udf("b", 0.0, 0.0),
                 udf("c", 0.0, 0.0)])
    # The remote renamed F and released c in the same edit.
    remote = doc([folder("G", ["a", "b"]), udf("a", 0.0, 0.0), udf("b", 0.0, 0.0),
                  udf("c", 1000.0, 1000.0)])
    merged, info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert info["renamed"] == [["F", "G"]]
    assert rows["G"]["childUdfOrder"] == ["a", "b"]
    assert (rows["c"]["x"], rows["c"]["y"]) == (1000.0, 1000.0)


def test_merge_two_child_folder_needs_a_subset_to_pair():
    base = cs.rows_of_doc(doc([folder("F", ["a", "b"]), udf("a", 0.0, 0.0),
                               udf("b", 0.0, 0.0)]))
    # Local moved F, so the remote dropping it does not delete it.
    local = doc([folder("F", ["a", "b"], x=300.0), udf("a", 0.0, 0.0), udf("b", 0.0, 0.0)])
    # One shared child of two is too weak to call G a rename of F.
    remote = doc([folder("G", ["a"]), udf("a", 0.0, 0.0), udf("b", 1000.0, 1000.0)])
    merged, info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert info["renamed"] == []
    assert rows["F"]["childUdfOrder"] == ["a", "b"]       # local's F, untouched
    assert rows["G"]["childUdfOrder"] == []               # F keeps a: local nests it


def test_merge_pairs_each_remote_rename_with_its_best_base_folder():
    base = cs.rows_of_doc(doc([folder("F1", ["a", "b"], x=0.0), folder("F2", ["c", "d"], x=500.0),
                               udf("a", 0.0, 0.0), udf("b", 0.0, 100.0),
                               udf("c", 0.0, 0.0), udf("d", 0.0, 100.0)]))
    local = doc([folder("F1", ["a", "b"], x=50.0), folder("F2", ["c", "d"], x=550.0),
                 udf("a", 0.0, 0.0), udf("b", 0.0, 100.0),
                 udf("c", 0.0, 0.0), udf("d", 0.0, 100.0)])
    # G2 comes first and holds half of F1, but ALL of F2: it is F2's rename.
    remote = doc([folder("G2", ["a", "c", "d"], x=500.0), folder("G1", ["b"], x=0.0),
                  udf("a", 0.0, 200.0), udf("b", 0.0, 100.0),
                  udf("c", 0.0, 0.0), udf("d", 0.0, 100.0)])
    merged, info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert info["renamed"] == [["F2", "G2"]]
    assert rows["G2"]["x"] == 550.0 and rows["G2"]["childUdfOrder"] == ["c", "d"]
    # F1 (two children) cannot pair below a subset: local's F1 keeps a and b.
    assert rows["F1"]["childUdfOrder"] == ["a", "b"]
    assert rows["G1"]["childUdfOrder"] == []
    assert cs.folder_problems(merged) == []


def test_merge_local_ungroup_keeps_a_remote_folder_that_only_overlaps():
    base = cs.rows_of_doc(doc([folder("F", ["a", "b", "c", "d"], x=100.0, y=100.0),
                               udf("a", 0.0, 0.0), udf("b", 0.0, 100.0),
                               udf("c", 0.0, 200.0), udf("d", 0.0, 300.0)]))
    local = doc([udf("a", 100.0, 100.0), udf("b", 100.0, 200.0),
                 udf("c", 100.0, 300.0), udf("d", 100.0, 400.0)])
    remote = doc([folder("G", ["a", "b", "z"], x=2000.0, y=2000.0), udf("a", 0.0, 0.0),
                  udf("b", 0.0, 100.0), udf("c", 100.0, 300.0), udf("d", 100.0, 400.0),
                  udf("z", 0.0, 200.0)])
    merged, info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    # Half of F is not a rename: G is a folder the remote made, kept.
    assert info["renamed"] == [] and rows["G"]["childUdfOrder"] == ["a", "b", "z"]
    # a and b keep local's on-screen place, now inside G.
    assert (rows["a"]["x"], rows["a"]["y"]) == (100.0 - 2000.0, 100.0 - 2000.0)


def test_merge_local_delete_beats_a_remote_folder_rename():
    base = cs.rows_of_doc(doc([folder("F", ["a"]), udf("a", 40.0, 60.0)]))
    # Local deleted F, releasing a where it was on screen.
    local = doc([udf("a", 1040.0, 1060.0)])
    remote = doc([folder("G", ["a"]), udf("a", 40.0, 60.0)])
    merged, info = cs.merge_toml(base, local, remote)
    rows = by_name(merged)
    assert [n for n, r in rows.items() if cs.is_folder(r)] == []
    assert info["renamed"] == []
    assert (rows["a"]["x"], rows["a"]["y"]) == (1040.0, 1060.0)


def test_merge_local_edge_to_a_renamed_folder_follows_the_rename():
    base = cs.rows_of_doc(doc([folder("F", ["a"]), udf("a", 0.0, 0.0), udf("z", 0.0, 0.0)]))
    local = doc([folder("F", ["a"]), udf("a", 0.0, 0.0), udf("z", 0.0, 0.0)],
                edges=[["z", "F"]])
    remote = doc([folder("G", ["a"]), udf("a", 0.0, 0.0), udf("z", 0.0, 0.0)])
    merged, _info = cs.merge_toml(base, local, remote)
    assert merged["canvas"]["edges"] == [["z", "G"]]


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
    rows = [folder("F", ["a", "ghost", "G", "a", ["nested"]]), folder("G", ["a"]),
            {"type": "udf-folder", "childUdfOrder": []}, udf("a", 0.0, 0.0)]
    problems = cs.folder_problems(doc(rows))
    assert any("'G', which is a folder" in p for p in problems), problems
    assert any("lists 'a' twice" in p for p in problems), problems
    assert any("['nested'], which is not a name" in p for p in problems), problems
    assert any("'a' is listed by folders 'F' and 'G'" in p for p in problems), problems
    assert any("neither folderName nor udfName" in p for p in problems), problems
    # A name with no row is what the server round-trips: not a problem.
    assert not any("ghost" in p for p in problems), problems
    assert cs.folder_problems(cs.rows_of_doc(doc(rows[:2] + rows[3:]))) != []
    assert cs.folder_problems(doc([folder("F", ["a", "gone"]), udf("a", 0.0, 0.0)])) == []


def test_row_name_names_a_folder_by_its_folder_name():
    row = {"udfName": "A", "folderName": "B", "type": "udf-folder", "childUdfOrder": []}
    assert cs.row_name(row) == "B"
    rows = [row]
    cs._fill_names(rows)
    assert rows[0]["udfName"] == "B" and rows[0]["folderName"] == "B"
    assert cs.row_name({"udfName": "u", "folderName": "x"}) == "u"   # not a folder
    assert cs.node_name({"type": "udf-folder",
                         "data": {"udfName": "A", "folderName": "B"}}, 0) == "B"


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
    # The embed's F lists a name no row has (kept), a second a and b, which
    # G (later in the document) lists too. No prev: G and b are disk-only,
    # never deleted.
    res = cs.apply_snapshot(
        d, cs.compact_state(_folder_snapshot(["a", "ghost", "a", "b"])), None)
    rows = {cs.row_name(r): r for r in _disk_rows(d)}
    assert rows["F"]["childUdfOrder"] == ["a", "ghost", "b"]     # F is earlier: keeps b
    assert rows["G"]["childUdfOrder"] == []
    assert len(res["folder_fixes"]) == 2, res["folder_fixes"]
    assert cs.folder_problems(_disk_rows(d)) == []
    # The same snapshot again: the same fixes undo the same upsert, nothing
    # is written and nothing reported upserted.
    before = os.stat(os.path.join(d, "canvas.toml")).st_mtime_ns
    res = cs.apply_snapshot(
        d, cs.compact_state(_folder_snapshot(["a", "ghost", "a", "b"])), None)
    assert res["changed"] is False, res
    assert res["upserted"] == [], res
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


# -- the pull normalises, never rolls back over folders ---------------------------

def _manager(tmp_path, monkeypatch, local_toml: bytes, files: dict[str, bytes]):
    import hashlib

    from fused_render import canvases as canvases_mod

    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(tmp_path))
    manager = canvases_mod._SyncManager("folders-merge-unit")
    # The constructor starts the watcher; these tests drive `_merge_remote`
    # by hand, and a live watcher would push into the NEXT test's env.
    manager.stop()
    os.makedirs(manager.dir, exist_ok=True)
    with open(os.path.join(manager.dir, "canvas.toml"), "wb") as fh:
        fh.write(local_toml)
    for rel, data in files.items():
        with open(os.path.join(manager.dir, rel), "wb") as fh:
            fh.write(data)
    manager._base_files = {rel: hashlib.md5(data).hexdigest()
                           for rel, data in {"canvas.toml": local_toml, **files}.items()}
    manager._base_toml = cs.rows_of_doc(rtoml.loads(local_toml.decode()))
    monkeypatch.setattr(manager, "_validate_clone", lambda: True)
    return manager


def _events(manager, action):
    return [e for e in manager._log.since(0)["events"] if e["action"] == action]


def test_merge_remote_normalises_a_pulled_toml_instead_of_rolling_back(tmp_path, monkeypatch):
    good = rtoml.dumps(doc([folder("F", ["a"]), folder("G", []), udf("a", 40.0, 60.0)])).encode()
    # What the server round-trips: a stale name (kept) — plus junk it would
    # ignore: a nested list, a folder listed as a child, a child twice.
    remote = rtoml.dumps(doc([folder("F", ["a", "gone", ["x"], "G"]),
                              folder("G", ["a"]), udf("a", 40.0, 60.0)])).encode()
    manager = _manager(tmp_path, monkeypatch, good, {"a.py": b"A"})
    probe = {"udfs": {}}
    for _ in range(2):
        assert manager._merge_remote(probe, {"canvas.toml": remote, "a.py": b"A"}) is True
    assert manager.merge_rollback_seq == 0
    assert manager._remote == probe                       # the remote is reconciled
    rows = {cs.row_name(r): r for r in _disk_rows(manager.dir)}
    assert rows["F"]["childUdfOrder"] == ["a", "gone"]
    assert rows["G"]["childUdfOrder"] == []
    assert _events(manager, "pull rollback") == []
    # Logged once for the same set of fixes, however often it re-arrives.
    (event,) = _events(manager, "folder fixes")
    assert "'G'" in event["detail"], event


def test_merge_remote_takes_a_stale_name_verbatim(tmp_path, monkeypatch):
    base = rtoml.dumps(doc([folder("F", ["a", "b"]), udf("a", 40.0, 60.0),
                            udf("b", 40.0, 600.0)])).encode()
    # A teammate deleted b in a hosted tab: the workbench leaves it listed.
    remote = rtoml.dumps(doc([folder("F", ["a", "b"]), udf("a", 40.0, 60.0)])).encode()
    manager = _manager(tmp_path, monkeypatch, base, {"a.py": b"A", "b.py": b"B"})
    with open(os.path.join(manager.dir, "a.py"), "wb") as fh:
        fh.write(b"A-local")                               # dirty
    assert manager._merge_remote({"udfs": {}}, {"canvas.toml": remote, "a.py": b"A"}) is True
    assert manager.merge_rollback_seq == 0
    with open(os.path.join(manager.dir, "canvas.toml"), "rb") as fh:
        assert fh.read() == remote
    assert not os.path.exists(os.path.join(manager.dir, "b.py"))
    assert _events(manager, "folder fixes") == []


def test_merge_remote_refusal_keeps_the_sync_point_and_holds(tmp_path, monkeypatch):
    import hashlib
    base = rtoml.dumps(doc([udf("a", 10.0, 10.0), udf("z", 50.0, 50.0)])).encode()
    local = rtoml.dumps(doc([udf("a", 20.0, 10.0), udf("z", 50.0, 50.0)])).encode()
    # The server's canvas.toml carries two rows named z (same kind).
    remote = rtoml.dumps(doc([udf("a", 10.0, 10.0), udf("z", 900.0, 50.0),
                              udf("z", 1.0, 1.0), udf("b", 0.0, 0.0)])).encode()
    manager = _manager(tmp_path, monkeypatch, base, {"a.py": b"A", "z.py": b"Z"})
    with open(os.path.join(manager.dir, "canvas.toml"), "wb") as fh:
        fh.write(local)
    base_toml = manager._base_toml
    probe = {"udfs": {"b": {"id": "1"}}}
    for _ in range(2):
        assert manager._merge_remote(probe, {"canvas.toml": remote, "a.py": b"A",
                                             "z.py": b"Z", "b.py": b"B"}) is True
    assert manager.toml_refusal == ["z"]
    with open(os.path.join(manager.dir, "canvas.toml"), "rb") as fh:
        assert fh.read() == local
    # Neither the sync point nor the remote baseline moved.
    assert manager._base_files["canvas.toml"] == hashlib.md5(base).hexdigest()
    assert manager._base_toml == base_toml
    assert manager._remote != probe
    (event,) = _events(manager, "toml merge refused")       # once, not per pull
    assert "'z'" in event["detail"], event


def test_merge_remote_merges_a_folder_and_node_sharing_a_name(tmp_path, monkeypatch):
    base = rtoml.dumps(doc([udf("a", 10.0, 10.0), udf("z", 50.0, 50.0)])).encode()
    # Claude added a folder named like the UDF a.
    local = rtoml.dumps(doc([folder("a", []), udf("a", 10.0, 10.0),
                             udf("z", 50.0, 50.0)])).encode()
    remote = rtoml.dumps(doc([udf("a", 10.0, 10.0), udf("z", 900.0, 50.0),
                              udf("b", 400.0, 400.0)])).encode()
    manager = _manager(tmp_path, monkeypatch, base, {"a.py": b"A", "z.py": b"Z"})
    with open(os.path.join(manager.dir, "canvas.toml"), "wb") as fh:
        fh.write(local)
    probe = {"udfs": {"b": {"id": "1"}}}
    assert manager._merge_remote(probe, {"canvas.toml": remote, "a.py": b"A",
                                         "z.py": b"Z", "b.py": b"B"}) is True
    assert manager.toml_refusal is None and manager._remote == probe
    rows = [(cs.is_folder(r), cs.row_name(r), r.get("x")) for r in _disk_rows(manager.dir)]
    assert rows == [(True, "a", 1000.0), (False, "a", 10.0), (False, "z", 900.0),
                    (False, "b", 400.0)]


def test_force_pull_normalises_the_pulled_toml_and_goes_dirty(tmp_path, monkeypatch):
    import subprocess
    import types

    from fused_render import canvases as canvases_mod

    good = rtoml.dumps(doc([folder("F", ["a"]), udf("a", 40.0, 60.0)])).encode()
    pulled = rtoml.dumps(doc([folder("F", ["a", "a", "F"]), udf("a", 40.0, 60.0)])).encode()
    manager = _manager(tmp_path, monkeypatch, good, {"a.py": b"A"})

    def fake_run(cmd, **_kw):
        if "--force" in cmd:
            with open(os.path.join(manager.dir, "canvas.toml"), "wb") as fh:
                fh.write(pulled)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "already up to date", "")
    monkeypatch.setattr(canvases_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(canvases_mod, "_cli_env", lambda cli: {})
    monkeypatch.setattr(canvases_mod, "_seed_clone_claude_files", lambda *a: None)
    probe = {"udfs": {}}
    assert manager._force_pull(probe, types.SimpleNamespace(command=["fused"])) is None
    rows = {cs.row_name(r): r for r in _disk_rows(manager.dir)}
    assert rows["F"]["childUdfOrder"] == ["a"]
    # The sync point is what the server sent; the fix is a local edit to push.
    import hashlib
    assert manager._base_files["canvas.toml"] == hashlib.md5(pulled).hexdigest()
    assert manager.push_state == "pending" and manager._dirty_since is not None
    assert manager._remote == probe
    (event,) = _events(manager, "folder fixes")
    assert "'F'" in event["detail"], event


# -- synthesis inside a folder that already lists the slug ----------------------

def test_synthesize_a_slug_a_folder_lists_inside_it_below_its_lowest_child():
    rows = [folder("F", ["a", "b", "new"]), udf("a", 40.0, 600.0), udf("b", 40.0, 60.0),
            udf("z", 5000.0, 5000.0)]
    row = cs.synthesize_row(rows, "new", "new.py")
    # Relative to F, under a (the lowest of F's rows), never after z.
    assert (row["x"], row["y"]) == (40.0, 600.0 + 500 + 40)
    assert cs.folder_of(rows + [row])["new"] == "F"


def test_synthesize_a_slug_an_empty_folder_lists_at_its_corner():
    rows = [folder("F", ["new"]), udf("z", 5000.0, 5000.0)]
    row = cs.synthesize_row(rows, "new", "new.py")
    assert (row["x"], row["y"]) == (40, 40)


def test_compact_and_persist_chain_synthesized_rows_inside_a_folder(tmp_path):
    d = _clone(tmp_path, [folder("F", ["a", "g1", "g2"]), udf("a", 40.0, 60.0)],
               {"a.py": "A", "g1.py": "1", "g2.py": "2", "zz.py": "Z"})
    disk = cs.compact_state_from_disk(d)
    assert disk["synthesized"] == ["g1", "g2", "zz"]
    xy = {n: (disk["nodes"][n]["x"], disk["nodes"][n]["y"]) for n in disk["synthesized"]}
    assert xy["g1"] == (40.0, 600.0)                    # below a, in F
    assert xy["g2"] == (40.0, 1140.0)                   # below g1, in F
    assert xy["zz"] == (1040.0, 1000.0 + 1140.0 + 540)  # after g2, absolute, no folder
    assert cs.persist_synthesized(d, disk["synthesized"]) == ["g1", "g2", "zz"]
    again = cs.compact_state_from_disk(d)
    assert again["synthesized"] == []
    assert all(again["nodes"][n] == disk["nodes"][n] for n in disk["synthesized"])


def test_normalise_reports_a_repeated_losing_entry_once():
    rows = [folder("G", ["a"]), folder("F", ["a", "a", "G", "G"]), udf("a", 0.0, 0.0)]
    fixes = cs.normalise_folders(rows)
    assert fixes == ["folder 'F': dropped 'a' (folder 'G' keeps it)",
                     "folder 'F': dropped 'G' (a folder)"], fixes
    assert rows[1]["childUdfOrder"] == []
