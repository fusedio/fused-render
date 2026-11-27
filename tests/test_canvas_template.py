"""Tests for the `canvas` conditional preview template (SPEC §28, D114).

Two surfaces:

  * the condition gate (CT-12, deferred) — the first consumer of the built-in
    conditional template mechanism: stat lists `canvas` first and marks it
    `conditional` (never running the gate), and `/api/fs/conditions` resolves
    the verdict — True for a genuine `canvas.toml`, False (fail-closed) for
    plain / malformed / mis-named toml. Runs through the real
    `server._templates_for` + `server._conditions_payload` on tmp fixtures.
  * the reader (`canvas/reader.py`) — golden parse of a fixture canvas folder
    (nodes / folders / edges / viewport / siblings shape). Guarded on `fused`
    since the reader is a `@fused.udf` (engine contract, §28).
"""
import importlib.util
import os

import pytest

from fused_render.server import templates as _server_templates


TEMPLATES_DIR = _server_templates.TEMPLATES_DIR

def modes(path, is_dir=False):
    entries, error = _server_templates._templates_for(path, is_dir)
    return [e["mode"] for e in entries], error


def canvas_verdict(path):
    # The deferred half of CT-12: the background /api/fs/conditions payload.
    payload = _server_templates._conditions_payload(path)
    return payload["conditions"].get("canvas"), payload.get("error")


# canonical canvas.toml body reused across gate + reader tests
CANVAS_TOML = """\
type = "canvas"
version = 2
name = "Test canvas"

[canvas]
edges = [["a", "b"], ["b", "c"], ["ghost", "a"]]

[canvas.viewport]
x = 12.0
y = 34.0
zoom = 0.5

[[canvas.nodes]]
udfName = "a"
title = "Node A"
description = "first"
x = 0.0
y = 0.0
zIndex = 1
width = 200.0
height = 120.0

[[canvas.nodes]]
udfName = "b"
x = 300.0
y = 0.0
zIndex = 2
width = 200.0
height = 120.0
visible = false

[[canvas.nodes]]
udfName = "c"
title = "Node C"
x = 600.0
y = 200.0

[[canvas.nodes]]
udfName = "grp"
type = "udf-folder"
folderName = "Group"
folderColor = "#11223344"
childUdfOrder = ["a", "b"]
isLocked = true
x = -20.0
y = -20.0
width = 540.0
height = 180.0
"""


def _write_canvas(dir_path, name="canvas.toml", body=CANVAS_TOML):
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / name
    p.write_text(body)
    return p


# ------------------------------------------------------- condition gate (CT-12)

def test_canvas_toml_gets_canvas_mode_first(tmp_path):
    p = _write_canvas(tmp_path / "cv")
    m, error = modes(str(p))
    assert m == ["canvas", "code", "claude", "reader"]
    assert error is None
    entries, _ = _server_templates._templates_for(str(p), False)
    assert entries[0]["path"].endswith(os.path.join("canvas", "template.html"))
    assert entries[0]["icon"] is not None
    # stat only MARKS the gate (deferred CT-12) …
    by_mode = {e["mode"]: e for e in entries}
    assert by_mode["canvas"].get("conditional") is True
    # `reader` carries its own global feature-switch gate (accessibility opt-in),
    # so it too is marked conditional; the unconditional modes are not.
    assert by_mode["reader"].get("conditional") is True
    assert "conditional" not in by_mode["code"]
    # The gated chat (D235) is marked too — it ships a condition.py.
    assert by_mode["claude"].get("conditional") is True
    # … and the background verdict allows a genuine canvas.
    allowed, err = canvas_verdict(str(p))
    assert allowed is True
    assert err is None


def test_plain_toml_denied_canvas_mode(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text("[tool.black]\nline-length = 88\n")
    m, error = modes(str(p))
    # stat still lists canvas (marked conditional, gate not run at stat time)
    assert m == ["canvas", "code", "claude", "reader"]
    assert error is None
    allowed, err = canvas_verdict(str(p))
    assert allowed is False  # basename pre-check denies it
    assert err is None


def test_malformed_toml_fails_closed(tmp_path):
    p = tmp_path / "canvas.toml"
    p.write_text('type = "canvas"\n[canvas\nedges = [[\n')  # invalid TOML
    allowed, err = canvas_verdict(str(p))
    # fail-closed: a gate that can't parse returns False, and it is not an
    # error (the gate catches internally — an ordinary toml is not a failure)
    assert allowed is False
    assert err is None


def test_canvas_content_but_wrong_basename_is_dropped(tmp_path):
    # The cheap basename pre-check (§28) gates on the literal name canvas.toml —
    # canvas content under any other .toml name does not get the canvas mode.
    p = tmp_path / "layout.toml"
    p.write_text(CANVAS_TOML)
    allowed, _ = canvas_verdict(str(p))
    assert allowed is False


def test_toml_named_canvas_but_not_a_canvas_is_dropped(tmp_path):
    # Right name, wrong content: type != "canvas" -> gate returns False.
    p = tmp_path / "canvas.toml"
    p.write_text('type = "config"\n[stuff]\nx = 1\n')
    allowed, _ = canvas_verdict(str(p))
    assert allowed is False


def test_oversized_canvas_toml_skipped(tmp_path):
    # The 2 MB size guard fails closed without parsing a pathological file.
    p = tmp_path / "canvas.toml"
    p.write_text(CANVAS_TOML + "\n# pad\n" + ("x" * (2 * 1024 * 1024 + 10)))
    allowed, _ = canvas_verdict(str(p))
    assert allowed is False


def test_condition_module_main_directly(tmp_path):
    # The condition runs in the server process (plain module, stdlib) — exercise
    # `main` (the CT-12 entrypoint) directly to prove it never raises on odd input.
    cond = _load_module("condition.py")
    assert cond.main(str(_write_canvas(tmp_path / "ok"))) is True
    assert cond.main(str(tmp_path / "does-not-exist" / "canvas.toml")) is False
    assert cond.main("") is False
    assert cond.main(None) is False


# --------------------------------------------------------------- template files

def test_template_ships_expected_files():
    d = os.path.join(TEMPLATES_DIR, "canvas")
    files = sorted(f for f in os.listdir(d) if f != "__pycache__")
    assert files == ["condition.py", "icon.svg", "reader.py", "template.html"]


def test_template_html_has_no_runtime_script_tag():
    # window.fused is injected; a template must never add its own runtime.js.
    with open(os.path.join(TEMPLATES_DIR, "canvas", "template.html"), encoding="utf-8") as f:
        html = f.read()
    assert "runtime.js" not in html


# ------------------------------------------------------------------- reader.py

def _load_module(name):
    path = os.path.join(TEMPLATES_DIR, "canvas", name)
    spec = importlib.util.spec_from_file_location(f"canvas_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def reader():
    pytest.importorskip("fused")  # reader is a @fused.udf (engine contract)
    return _load_module("reader.py")


@pytest.fixture
def canvas_folder(tmp_path):
    d = tmp_path / "demo"
    _write_canvas(d)
    # sibling files named after node udfNames so `siblings` has entries
    (d / "a.py").write_text("x = 1\n")
    (d / "a.json").write_text("{}\n")
    (d / "c.md").write_text("# c\n")
    return d


def test_reader_parses_nodes_folders_edges(reader, canvas_folder):
    out = reader.main(file=str(canvas_folder / "canvas.toml"))
    assert out["name"] == "Test canvas"
    assert out["version"] == 2

    names = {n["udfName"] for n in out["nodes"]}
    assert names == {"a", "b", "c"}  # folder node lives in `folders`, not nodes
    assert len(out["folders"]) == 1
    grp = out["folders"][0]
    assert grp["folderName"] == "Group"
    assert grp["childUdfOrder"] == ["a", "b"]
    assert grp["isLocked"] is True

    # malformed / dangling edge endpoints are kept as pairs (the viewer skips
    # ones whose names don't resolve); all three well-formed pairs survive.
    assert out["edges"] == [["a", "b"], ["b", "c"], ["ghost", "a"]]


def test_reader_defaults_and_visibility(reader, canvas_folder):
    out = reader.main(file=str(canvas_folder / "canvas.toml"))
    by = {n["udfName"]: n for n in out["nodes"]}
    # title defaults to udfName when absent (§28 table)
    assert by["b"]["title"] == "b"
    # visible defaults to True; explicit false is preserved
    assert by["a"]["visible"] is True
    assert by["b"]["visible"] is False
    # missing width/height fall back to defaults, not 0
    assert by["c"]["width"] > 0 and by["c"]["height"] > 0


def test_reader_viewport(reader, canvas_folder):
    out = reader.main(file=str(canvas_folder / "canvas.toml"))
    assert out["viewport"] == {"x": 12.0, "y": 34.0, "zoom": 0.5}


def test_reader_empty_viewport_is_none(reader, tmp_path):
    # An empty (or x/y-less) [canvas.viewport] table must NOT fabricate an
    # origin camera — it returns None so the viewer falls back to fit-to-bounds.
    d = tmp_path / "emptyvp"
    d.mkdir()
    (d / "canvas.toml").write_text(
        'type = "canvas"\nversion = 2\n[canvas]\nedges = []\n'
        '[[canvas.nodes]]\nudfName = "a"\nx = 500.0\ny = 500.0\n'
        'zIndex = 1\nwidth = 100\nheight = 100\n'
        "[canvas.viewport]\n"
    )
    out = reader.main(file=str(d / "canvas.toml"))
    assert out["viewport"] is None


def test_reader_boolean_viewport_coords_are_absent(reader, tmp_path):
    # bool is an int subclass — `x = true` must not count as a coordinate and
    # fabricate an origin camera; it falls through to fit-to-bounds like an
    # empty viewport table.
    d = tmp_path / "boolvp"
    d.mkdir()
    (d / "canvas.toml").write_text(
        'type = "canvas"\nversion = 2\n[canvas]\nedges = []\n'
        '[[canvas.nodes]]\nudfName = "a"\nx = 500.0\ny = 500.0\n'
        'zIndex = 1\nwidth = 100\nheight = 100\n'
        "[canvas.viewport]\nx = true\ny = true\nzoom = 0.5\n"
    )
    out = reader.main(file=str(d / "canvas.toml"))
    assert out["viewport"] is None


def test_reader_no_file_raises(reader):
    # A missing _file param must FAIL the call (traceback overlay, §28), not
    # return a dict the viewer would render as a healthy empty canvas.
    with pytest.raises(ValueError):
        reader.main(file="")


def test_reader_siblings(reader, canvas_folder):
    out = reader.main(file=str(canvas_folder / "canvas.toml"))
    assert out["siblings"]["a"] == [".py", ".json"]
    assert out["siblings"]["c"] == [".md"]
    assert "b" not in out["siblings"]  # b has no sibling files


def test_reader_skips_broken_nodes(reader, tmp_path):
    # A malformed node entry (not a table) is skipped, never fatal (§28).
    d = tmp_path / "partial"
    d.mkdir()
    (d / "canvas.toml").write_text(
        'type = "canvas"\nversion = 2\n[canvas]\n'
        'edges = ["not-a-pair", ["a", "b"]]\n'
        '[[canvas.nodes]]\nudfName = "a"\nx = 0\ny = 0\n'
        '[[canvas.nodes]]\nx = 10\ny = 10\n'  # no udfName -> skipped
    )
    out = reader.main(file=str(d / "canvas.toml"))
    assert [n["udfName"] for n in out["nodes"]] == ["a"]
    assert out["edges"] == [["a", "b"]]  # the string edge dropped


# A canvas with two folders, written the way the workbench saves one: a
# folder's children carry x/y RELATIVE to the folder, and one name ("dup")
# is used by two nodes.
FOLDERS_TOML = """\
type = "canvas"
version = 2

[canvas]
edges = [["in_a", "in_b"]]

[canvas.viewportBounds]
minX = -10.0
minY = -20.0
maxX = 990.0
maxY = 480.0

[[canvas.nodes]]
udfName = "First"
type = "udf-folder"
folderName = "First"
childUdfOrder = ["in_a", "dup"]
x = 1000.0
y = 2000.0
zIndex = 0
width = 1500.0
height = 600.0

[[canvas.nodes]]
udfName = "Second"
type = "udf-folder"
folderName = "Second"
childUdfOrder = ["in_b", "dup", "missing"]
x = -500.0
y = 100.0
zIndex = 7
width = 1500.0
height = 600.0

[[canvas.nodes]]
udfName = "Empty"
type = "udf-folder"
folderName = "Empty"
childUdfOrder = []
x = 0.0
y = 5000.0
zIndex = 3
width = 700.0

[[canvas.nodes]]
udfName = "in_a"
x = 40.0
y = 75.0
zIndex = 1
width = 700.0
height = 500.0

[[canvas.nodes]]
udfName = "in_b"
x = 40.0
y = 75.0
zIndex = 30
width = 700.0
height = 500.0

[[canvas.nodes]]
udfName = "dup"
x = 815.0
y = 75.0
zIndex = 2

[[canvas.nodes]]
udfName = "dup"
x = 815.0
y = 75.0

[[canvas.nodes]]
udfName = "loose"
x = 9000.0
y = 9000.0
zIndex = 4

[[canvas.nodes]]
udfName = "memo"
x = 0.0
y = -400.0
textBoxColor = "#FCE4EC"
textBoxGradient = false
textBoxScaleFactor = 1.5
textBoxAlignment = "center"
"""


@pytest.fixture
def folders_canvas(tmp_path):
    d = tmp_path / "folders"
    _write_canvas(d, body=FOLDERS_TOML)
    (d / "in_a.py").write_text("x = 1\n")
    (d / "in_b.json").write_text("{}\n")
    (d / "loose.html").write_text("<p>hi</p>\n")
    (d / "memo.md").write_text("# Memo\n\nbody\n")
    return d


def test_reader_places_folder_children_absolutely(reader, folders_canvas):
    # A child's x/y is relative to its folder (the workbench's own storage);
    # `ax`/`ay` is where it is on the canvas.
    out = reader.main(file=str(folders_canvas / "canvas.toml"))
    folders = {f["udfName"]: (i, f) for i, f in enumerate(out["folders"])}
    by = {}
    for n in out["nodes"]:
        by.setdefault(n["udfName"], []).append(n)

    first_index, _ = folders["First"]
    second_index, _ = folders["Second"]
    a = by["in_a"][0]
    assert (a["x"], a["y"]) == (40.0, 75.0)  # the row, untouched
    assert (a["ax"], a["ay"]) == (1040.0, 2075.0)
    assert a["parent"] == first_index
    b = by["in_b"][0]
    assert (b["ax"], b["ay"]) == (-460.0, 175.0)
    assert b["parent"] == second_index

    # Two nodes share a name: each folder takes the first one still free.
    dup_first, dup_second = by["dup"]
    assert dup_first["parent"] == first_index
    assert (dup_first["ax"], dup_first["ay"]) == (1815.0, 2075.0)
    assert dup_second["parent"] == second_index
    assert (dup_second["ax"], dup_second["ay"]) == (315.0, 175.0)

    # A node in no folder is where its row says; a folder too.
    loose = by["loose"][0]
    assert loose["parent"] is None
    assert (loose["ax"], loose["ay"]) == (9000.0, 9000.0)
    assert (folders["First"][1]["ax"], folders["First"][1]["ay"]) == (1000.0, 2000.0)


def test_reader_stacking_follows_react_flow(reader, folders_canvas):
    # Folders that hold nodes are lifted 10 apart, in the order the rows of
    # their first children come; a child sits one above its folder unless
    # its own zIndex is already higher. A row with no zIndex stacks by its
    # place in the file (index + 1).
    out = reader.main(file=str(folders_canvas / "canvas.toml"))
    folders = {f["udfName"]: f for f in out["folders"]}
    by = {}
    for n in out["nodes"]:
        by.setdefault(n["udfName"], []).append(n)

    assert folders["First"]["z"] == 0        # first turn: + 0
    assert folders["Second"]["z"] == 17      # second turn: 7 + 10
    assert folders["Empty"]["z"] == 3        # holds nothing: untouched
    assert by["in_a"][0]["z"] == 1           # already above its folder
    assert by["in_b"][0]["z"] == 30          # already above its folder
    assert by["dup"][0]["z"] == 2
    assert by["dup"][1]["z"] == 18           # row 7 -> zIndex 7, lifted to 17 + 1
    assert by["loose"][0]["z"] == 4
    assert folders["First"]["zIndex"] == 0   # the row's own value is kept


def test_reader_stacking_keeps_react_flows_running_count(reader, tmp_path):
    # Children rows ordered A, B, A, C: React Flow sets its one running count
    # back to A's turn at the second A, so C takes turn 1 like B. The
    # workbench shows 0, 10, 10, and so does the reader.
    d = tmp_path / "turns"
    d.mkdir()
    rows = ['type = "canvas"\nversion = 2\n[canvas]\n']
    for name, kids in (("A", '["a1", "a2"]'), ("B", '["b1"]'), ("C", '["c1"]')):
        rows.append('[[canvas.nodes]]\nudfName = "%s"\ntype = "udf-folder"\n'
                    'childUdfOrder = %s\nx = 0\ny = 0\nzIndex = 0\n' % (name, kids))
    for name in ("a1", "b1", "a2", "c1"):
        rows.append('[[canvas.nodes]]\nudfName = "%s"\nx = 0\ny = 0\nzIndex = 1\n' % name)
    (d / "canvas.toml").write_text("".join(rows))
    out = reader.main(file=str(d / "canvas.toml"))
    assert {f["udfName"]: f["z"] for f in out["folders"]} == {"A": 0, "B": 10, "C": 10}
    assert {n["udfName"]: n["z"] for n in out["nodes"]} == {"a1": 1, "b1": 11, "a2": 1, "c1": 11}


def test_reader_drops_numbers_that_are_not_finite(reader, tmp_path):
    # TOML can say `nan` and `inf`. Neither is a position, a size or a scale,
    # and the result must stay JSON (the page gets it through a strict encoder).
    import json

    d = tmp_path / "nonfinite"
    d.mkdir()
    (d / "canvas.toml").write_text(
        'type = "canvas"\nversion = 2\n[canvas]\n'
        '[canvas.viewport]\nx = nan\ny = 0\nzoom = 1\n'
        '[canvas.viewportBounds]\nminX = 0\nminY = 0\nmaxX = inf\nmaxY = 10\n'
        '[[canvas.nodes]]\nudfName = "n"\nx = inf\ny = -inf\nwidth = nan\nzIndex = 2\n'
        'textBoxColor = "#FFF9C4"\ntextBoxScaleFactor = nan\nnodeDescriptionScaleFactor = inf\n'
    )
    out = reader.main(file=str(d / "canvas.toml"))
    json.dumps(out, allow_nan=False)
    node = out["nodes"][0]
    assert (node["x"], node["y"], node["width"]) == (0, 0, 350)
    assert node["textBoxScaleFactor"] is None and node["nodeDescriptionScaleFactor"] is None
    assert out["viewport"] is None and out["viewportBounds"] is None


def test_reader_takes_sources_from_regular_files_only(reader, tmp_path):
    # A folder named like a source is no source: the note beside it is what
    # the node is. And a pipe named like a note is never opened.
    d = tmp_path / "odd"
    d.mkdir()
    (d / "canvas.toml").write_text(
        'type = "canvas"\nversion = 2\n[canvas]\n'
        '[[canvas.nodes]]\nudfName = "memo"\nx = 0\ny = 0\n'
        '[[canvas.nodes]]\nudfName = "pipe"\nx = 0\ny = 400\ntextBoxColor = "#FFF9C4"\n')
    (d / "memo.py").mkdir()
    (d / "memo.md").write_text("hello\n")
    if hasattr(os, "mkfifo"):
        os.mkfifo(d / "pipe.md")
    out = reader.main(file=str(d / "canvas.toml"))
    by = {n["udfName"]: n for n in out["nodes"]}
    assert by["memo"]["kind"] == "note" and by["memo"]["note"] == "hello\n"
    assert out["siblings"]["memo"] == [".md"]
    assert by["pipe"]["kind"] == "note" and by["pipe"]["note"] == ""
    assert "pipe" not in out["siblings"]


def test_reader_keeps_a_child_finite_when_the_sum_is_not(reader, tmp_path):
    import json

    d = tmp_path / "huge"
    d.mkdir()
    (d / "canvas.toml").write_text(
        'type = "canvas"\nversion = 2\n[canvas]\n'
        '[[canvas.nodes]]\nudfName = "F"\ntype = "udf-folder"\nchildUdfOrder = ["a"]\n'
        'x = 1e308\ny = 0\n'
        '[[canvas.nodes]]\nudfName = "a"\nx = 1e308\ny = 5\n')
    out = reader.main(file=str(d / "canvas.toml"))
    json.dumps(out, allow_nan=False)
    assert out["nodes"][0]["parent"] == 0
    assert (out["nodes"][0]["ax"], out["nodes"][0]["ay"]) == (1e308, 5)


def test_reader_kinds_sizes_and_note_text(reader, folders_canvas):
    out = reader.main(file=str(folders_canvas / "canvas.toml"))
    by = {}
    for n in out["nodes"]:
        by.setdefault(n["udfName"], []).append(n)
    # The source file next to the toml says what a node is.
    assert by["in_a"][0]["kind"] == "udf"
    assert by["in_b"][0]["kind"] == "widget"
    assert by["loose"][0]["kind"] == "html"
    assert by["dup"][0]["kind"] == "udf"     # no file: a UDF
    memo = by["memo"][0]
    assert memo["kind"] == "note"
    # A note carries its text and its card's styling.
    assert memo["note"] == "# Memo\n\nbody\n"
    assert memo["textBoxColor"] == "#FCE4EC"
    assert memo["textBoxGradient"] is False
    assert memo["textBoxScaleFactor"] == 1.5
    assert memo["textBoxAlignment"] == "center"
    assert "note" not in by["in_a"][0]
    # Missing sizes fall back to the workbench's own defaults.
    assert (memo["width"], memo["height"]) == (350, 250)
    assert (by["loose"][0]["width"], by["loose"][0]["height"]) == (700, 500)
    empty = next(f for f in out["folders"] if f["udfName"] == "Empty")
    assert (empty["width"], empty["height"]) == (700.0, 115)


def test_reader_note_without_file_and_styled_row(reader, tmp_path):
    # A toml read on its own (no source files): a row carrying note styling
    # is still a note, with no text.
    d = tmp_path / "bare"
    d.mkdir()
    (d / "canvas.toml").write_text(
        'type = "canvas"\nversion = 2\n[canvas]\n'
        '[[canvas.nodes]]\nudfName = "n"\nx = 0\ny = 0\n'
        'textBoxColor = "#FFF9C4"\n'
    )
    out = reader.main(file=str(d / "canvas.toml"))
    assert out["nodes"][0]["kind"] == "note"
    assert out["nodes"][0]["note"] == ""


def test_reader_viewport_bounds(reader, folders_canvas, tmp_path):
    out = reader.main(file=str(folders_canvas / "canvas.toml"))
    assert out["viewportBounds"] == {
        "minX": -10.0, "minY": -20.0, "maxX": 990.0, "maxY": 480.0}
    # Bounds that enclose nothing, or are not all numbers, do not count.
    for body in ("minX = 5\nminY = 0\nmaxX = 5\nmaxY = 10\n",
                 "minX = 0\nminY = 0\nmaxX = true\nmaxY = 10\n",
                 "minX = 0\n"):
        d = tmp_path / ("vb%d" % len(body))
        d.mkdir()
        (d / "canvas.toml").write_text(
            'type = "canvas"\nversion = 2\n[canvas]\n'
            "[canvas.viewportBounds]\n" + body)
        assert reader.main(file=str(d / "canvas.toml"))["viewportBounds"] is None


def test_template_assets_exist():
    # The page loads three of the app's own files by absolute URL; each must
    # be a committed file (the product has no network at runtime).
    with open(os.path.join(TEMPLATES_DIR, "canvas", "template.html"), encoding="utf-8") as f:
        html = f.read()
    for url, folder in (("/template-assets/marked.min.js", "vendor"),
                        ("/template-assets/work-sans-latin-var.woff2", "vendor"),
                        ("/template-shared/canvas-node.js", "shared")):
        assert url in html
        assert os.path.isfile(os.path.join(TEMPLATES_DIR, folder, url.rsplit("/", 1)[1]))
    assert "http://" not in html.replace("http://www.w3.org/", "")
    assert "https://" not in html


def test_template_looks_names_up_by_own_key_only():
    # A node may be called "constructor" or "toString": a lookup by udfName
    # must never find something Object.prototype holds.
    with open(os.path.join(TEMPLATES_DIR, "canvas", "template.html"), encoding="utf-8") as f:
        html = f.read()
    assert "var byName = Object.create(null);" in html
    assert "Object.prototype.hasOwnProperty.call(siblingsOf, name)" in html
    assert "siblings[" not in html


def test_reader_output_is_json_serializable(reader, canvas_folder):
    import json
    json.dumps(reader.main(file=str(canvas_folder / "canvas.toml")))


def test_reader_empty_canvas(reader, tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    (d / "canvas.toml").write_text('type = "canvas"\nversion = 2\n[canvas]\n')
    out = reader.main(file=str(d / "canvas.toml"))
    assert out["nodes"] == [] and out["folders"] == [] and out["edges"] == []
    assert out["siblings"] == {}


def test_template_has_a_ghost_mode_for_the_canvas_page():
    # `ghost=1` (SPEC 28 CV-10): the canvas page frames this view over the
    # workbench while that loads.
    with open(os.path.join(TEMPLATES_DIR, "canvas", "template.html"), encoding="utf-8") as f:
        html = f.read()
    # Read off the frame's OWN address: inside the app `fused.params` is the
    # shell page's store, where this frame's query is not.
    assert "new URLSearchParams(window.location.search).get(key)" in html
    assert 'var ghostMode = ownParam("ghost") === "1";' in html
    # It stands where the workbench will: fitted like it, nothing to press,
    # nothing kept.
    assert "if (ghostMode) { cam = fitCam(); return; }" in html
    assert "if (ghostMode || e.button !== 0 || onChrome(e.target)) return;" in html
    assert "if (ghostMode || onChrome(e.target)) return;" in html
    assert "if (dragMoved || ghostMode) return;" in html
    assert "if (ghostMode) return; // a ghost keeps no camera" in html
    assert ".ghost .head .counts, .ghost .controls, .ghost .loading, .ghost .stand-in { display: none; }" in html
    # And tells the page that framed it when it is drawn, or could not be.
    assert 'type: "fused-canvas-ghost", state: state, file: file' in html
    assert 'tellHost("ready");' in html and 'tellHost("failed");' in html


def test_status_names_the_layout_view(tmp_path, monkeypatch):
    # The canvas page cannot name a template folder itself: status hands it
    # the canvas.toml view's page, which it frames in ghost mode.
    from fastapi.testclient import TestClient

    from fused_render.server import create_app

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(tmp_path / "canvases"))
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(tmp_path / "creds.json"))
    client = TestClient(create_app(start_dir=str(tmp_path)))
    body = client.get("/api/canvases/status").json()
    assert body["layout_view"] == os.path.join(TEMPLATES_DIR, "canvas", "template.html")
    assert os.path.isfile(body["layout_view"])

