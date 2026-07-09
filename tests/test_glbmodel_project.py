"""Tests for the glbmodel editor's Python backend
(fused_render/templates/glbmodel/{project_ops.py,sculpt_bake.py}) and the
glb/glbmodel template registration.

project_ops.py and sculpt_bake.py are stdlib-only runPython targets (not package
modules), so — like test_annotate_comments.py — these load them via importlib
and drive main() directly against a tmp `*.glbproj/` dir. A model project is a
self-contained `*.glbproj/` directory (parts/manifest.json + parts/<name>.glb +
placements.json + overrides.json); there is no workspace/repo-root.

The GLB bytes here are structurally minimal — project_ops.write_part only checks
the "glTF" magic, and sculpt_bake.main checks magic + declared-length, neither
parses geometry — so a 12-byte header is a valid fixture for both.
"""
import base64
import importlib.util
import json
import os
import struct

from fused_render import server

_GLBMODEL = os.path.join("fused_render", "templates", "glbmodel")


def _load(name):
    path = os.path.join(_GLBMODEL, f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"glbmodel_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _glb_bytes(total=12):
    """A minimal syntactically-valid GLB header: magic, version 2, declared
    total length. `total` defaults to the 12-byte header's own length so
    declared == len (what sculpt_bake.main requires)."""
    return struct.pack("<III", 0x46546C67, 2, total)


def _b64(data):
    return base64.b64encode(data).decode()


def _proj(tmp_path):
    return str(tmp_path / "mage.glbproj")


# ------------------------------------------------------------- project_ops

def test_create_and_info_scaffold(tmp_path):
    po = _load("project_ops")
    proj = _proj(tmp_path)
    assert po.main("create", proj) == {"model_dir": proj, "parts": []}
    assert os.path.exists(os.path.join(proj, "parts", "manifest.json"))
    assert os.path.exists(os.path.join(proj, "placements.json"))
    # info is idempotent scaffolding and returns the (empty) parts list
    assert po.main("info", proj) == {"model_dir": proj, "parts": []}


def test_write_part_success_and_manifest(tmp_path):
    po = _load("project_ops")
    proj = _proj(tmp_path)
    res = po.main("write_part", proj, "Head", _b64(_glb_bytes()))
    assert res == {"part": "Head", "path": os.path.join("parts", "Head.glb")}
    manifest = json.load(open(os.path.join(proj, "parts", "manifest.json")))
    assert manifest == {"parts": ["Head"]}
    assert os.path.exists(os.path.join(proj, "parts", "Head.glb"))


def test_write_part_reexport_overwrites_without_duplicate(tmp_path):
    po = _load("project_ops")
    proj = _proj(tmp_path)
    po.main("write_part", proj, "Head", _b64(_glb_bytes()))
    po.main("write_part", proj, "Head", _b64(_glb_bytes()))
    manifest = json.load(open(os.path.join(proj, "parts", "manifest.json")))
    assert manifest == {"parts": ["Head"]}  # not duplicated


def test_write_part_rejects_non_gltf(tmp_path):
    po = _load("project_ops")
    res = po.main("write_part", _proj(tmp_path), "Head", _b64(b"NOPExxxx"))
    assert "not a binary glTF" in res["error"]


def test_write_part_rejects_bad_base64(tmp_path):
    po = _load("project_ops")
    res = po.main("write_part", _proj(tmp_path), "Head", "!!!not base64!!!")
    assert res["error"] == "invalid base64 payload"


def test_write_part_rejects_unsanitizable_name(tmp_path):
    po = _load("project_ops")
    res = po.main("write_part", _proj(tmp_path), "///", _b64(_glb_bytes()))
    assert "can't derive a part name" in res["error"]


def test_write_part_rejects_orphan_on_disk(tmp_path):
    po = _load("project_ops")
    proj = _proj(tmp_path)
    po.main("create", proj)
    # a stale <name>.glb on disk but NOT in the manifest is a partial write
    open(os.path.join(proj, "parts", "Ghost.glb"), "wb").write(b"x")
    res = po.main("write_part", proj, "Ghost", _b64(_glb_bytes()))
    assert "isn't in the manifest" in res["error"]


def test_delete_part_soft_deletes_and_strips_placements(tmp_path):
    po = _load("project_ops")
    proj = _proj(tmp_path)
    po.main("write_part", proj, "Head", _b64(_glb_bytes()))
    po.main("write_part", proj, "Torso", _b64(_glb_bytes()))
    # user nudges saved for both parts
    json.dump({"Head": {"x": 1}, "Torso": {"y": 2}},
              open(os.path.join(proj, "placements.json"), "w"))
    json.dump({"Head": {"z": 3}},
              open(os.path.join(proj, "overrides.json"), "w"))

    res = po.main("delete_part", proj, "Head")
    assert res["part"] == "Head"
    manifest = json.load(open(os.path.join(proj, "parts", "manifest.json")))
    assert manifest == {"parts": ["Torso"]}
    # GLB moved to .trash, not destroyed
    assert os.path.exists(os.path.join(proj, ".trash", "Head.glb"))
    assert not os.path.exists(os.path.join(proj, "parts", "Head.glb"))
    # only Head's placement/override entries stripped; Torso survives
    assert json.load(open(os.path.join(proj, "placements.json"))) == {"Torso": {"y": 2}}
    assert json.load(open(os.path.join(proj, "overrides.json"))) == {}


def test_delete_unknown_part_errors(tmp_path):
    po = _load("project_ops")
    proj = _proj(tmp_path)
    po.main("create", proj)
    assert "unknown part" in po.main("delete_part", proj, "Nope")["error"]


def test_rejects_non_glbproj_dir(tmp_path):
    po = _load("project_ops")
    bad = str(tmp_path / "notaproj")
    assert "not a .glbproj directory" in po.main("create", bad)["error"]
    assert po.main("info", "")["error"] == "no model_dir given"


def test_unknown_action(tmp_path):
    po = _load("project_ops")
    assert "unknown action" in po.main("frobnicate", _proj(tmp_path))["error"]


# -------------------------------------------------------------- sculpt_bake

def test_sculpt_bake_overwrites_existing_part(tmp_path):
    po = _load("project_ops")
    sb = _load("sculpt_bake")
    proj = _proj(tmp_path)
    po.main("write_part", proj, "Head", _b64(_glb_bytes()))
    baked = _glb_bytes(16) + b"\x00\x00\x00\x00"  # declared length 16 == len
    res = sb.main(proj, "Head", _b64(baked))
    assert res["part"] == "Head" and res["bytes"] == 16
    assert open(os.path.join(proj, "parts", "Head.glb"), "rb").read() == baked


def test_sculpt_bake_rejects_missing_part(tmp_path):
    sb = _load("sculpt_bake")
    proj = _proj(tmp_path)
    _load("project_ops").main("create", proj)
    assert "not an existing frozen part" in sb.main(proj, "Head", _b64(_glb_bytes()))["error"]


def test_sculpt_bake_rejects_bad_glb(tmp_path):
    po = _load("project_ops")
    sb = _load("sculpt_bake")
    proj = _proj(tmp_path)
    po.main("write_part", proj, "Head", _b64(_glb_bytes()))
    # declared length won't match actual length
    bad = struct.pack("<III", 0x46546C67, 2, 999)
    assert "bad magic or length" in sb.main(proj, "Head", _b64(bad))["error"]


def test_sculpt_bake_rejects_non_glbproj(tmp_path):
    sb = _load("sculpt_bake")
    assert "not a .glbproj directory" in sb.main(str(tmp_path / "x"), "Head", "")["error"]


def test_sculpt_bake_rejects_path_traversal_name(tmp_path):
    sb = _load("sculpt_bake")
    proj = _proj(tmp_path)
    _load("project_ops").main("create", proj)
    assert "bad part name" in sb.main(proj, "../evil", _b64(_glb_bytes()))["error"]


# ---------------------------------------------------------------- registry

def test_glb_template_resolves_for_glb_and_gltf():
    entries, err = server._templates_for("/tmp/model.glb", False)
    assert err is None
    assert [e["mode"] for e in entries][0] == "glb"
    entries, err = server._templates_for("/tmp/model.gltf", False)
    assert [e["mode"] for e in entries] == ["glb", "code"]


def test_glbproj_dir_resolves_to_glbmodel():
    entries, err = server._templates_for("/tmp/mage.glbproj", True)
    assert err is None
    modes = [e["mode"] for e in entries]
    assert modes[0] == "glbmodel"
    assert "_listing" in modes
