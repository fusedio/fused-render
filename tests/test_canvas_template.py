"""Tests for the `canvas` conditional preview template (SPEC §28, D105).

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

from fused_render import server


TEMPLATES_DIR = server.TEMPLATES_DIR


def modes(path, is_dir=False):
    entries, error = server._templates_for(path, is_dir)
    return [e["mode"] for e in entries], error


def canvas_verdict(path):
    # The deferred half of CT-12: the background /api/fs/conditions payload.
    payload = server._conditions_payload(path)
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
    assert m == ["canvas", "code", "annotate"]
    assert error is None
    entries, _ = server._templates_for(str(p), False)
    assert entries[0]["path"].endswith("canvas/template.html")
    assert entries[0]["icon"] is not None
    # stat only MARKS the gate (deferred CT-12) …
    assert entries[0].get("conditional") is True
    assert all("conditional" not in e for e in entries[1:])
    # … and the background verdict allows a genuine canvas.
    allowed, err = canvas_verdict(str(p))
    assert allowed is True
    assert err is None


def test_plain_toml_denied_canvas_mode(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text("[tool.black]\nline-length = 88\n")
    m, error = modes(str(p))
    # stat still lists canvas (marked conditional, gate not run at stat time)
    assert m == ["canvas", "code", "annotate"]
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


def test_condition_module_method_directly(tmp_path):
    # The condition runs in the server process (plain module, stdlib) — exercise
    # `method` directly to prove it never raises on odd input.
    cond = _load_module("condition.py")
    assert cond.method(str(_write_canvas(tmp_path / "ok"))) is True
    assert cond.method(str(tmp_path / "does-not-exist" / "canvas.toml")) is False
    assert cond.method("") is False
    assert cond.method(None) is False


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
