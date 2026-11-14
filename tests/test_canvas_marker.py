"""The per-folder canvas marker, `.fused-canvas.json` (canvas_marker.py, D1476).

The format is the `fused canvas mirror` contract's; fused-render reads both
kinds and writes only `sync: "live"`. What these pin: the strict reader, the
one deliberately lenient answer ("mirror" wins over a malformed marker, so a
reference copy can never read as a clone), the atomic and idempotent write,
and the three conditions a folder must meet before fused-render may delete it.
"""
import json
import os

import pytest

from fused_render import canvas_marker, canvas_snapshot


def _write(folder, data):
    folder.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (folder / canvas_marker.MARKER_NAME).write_text(text, encoding="utf-8")


def _mirror(**over):
    return {"version": 1, "sync": "mirror", "scope": "public", "id": "col-1",
            "name": "Some_Canvas", "owner": "ada", "revision": 12,
            "last_updated": "2026-11-01T10:00:00Z",
            "pulled_at": "2026-11-02T10:00:00Z", **over}


def test_the_names_are_the_contracts():
    assert canvas_marker.MARKER_NAME == ".fused-canvas.json"
    assert canvas_marker.VERSION == 1
    assert canvas_marker.SCOPES == ("user", "team", "public")


def test_read_returns_every_field_of_a_well_formed_marker(tmp_path):
    _write(tmp_path / "c", _mirror())
    assert canvas_marker.read(str(tmp_path / "c")) == canvas_marker.Marker(
        version=1, sync="mirror", scope="public", id="col-1", name="Some_Canvas",
        owner="ada", revision=12, last_updated="2026-11-01T10:00:00Z",
        pulled_at="2026-11-02T10:00:00Z")


def test_read_accepts_the_nulls_the_contract_allows(tmp_path):
    _write(tmp_path / "c", _mirror(owner=None, revision=None))
    marker = canvas_marker.read(str(tmp_path / "c"))
    assert marker is not None and marker.owner is None and marker.revision is None


@pytest.mark.parametrize("content", [
    "",                                   # empty file
    "{not json",                          # not JSON
    "[1, 2]",                             # not an object
    json.dumps(_mirror(sync="frozen")),   # unknown owner kind
    json.dumps(_mirror(scope="mine")),    # unknown scope
    json.dumps(_mirror(version="1")),     # version of the wrong type
    json.dumps(_mirror(version=0)),
    json.dumps(_mirror(version=True)),    # a bool is not a version
    json.dumps(_mirror(revision="12")),
    json.dumps(_mirror(id=7)),
    json.dumps(_mirror(name=["x"])),
    json.dumps({"sync": "live"}),         # nothing else
])
def test_read_is_none_for_anything_malformed(tmp_path, content):
    _write(tmp_path / "c", content)
    assert canvas_marker.read(str(tmp_path / "c")) is None


def test_read_is_none_without_a_marker_or_a_folder(tmp_path):
    (tmp_path / "c").mkdir()
    assert canvas_marker.read(str(tmp_path / "c")) is None
    assert canvas_marker.read(str(tmp_path / "missing")) is None
    assert canvas_marker.sync_of(str(tmp_path / "c")) == "none"
    assert canvas_marker.sync_of(str(tmp_path / "missing")) == "none"


def test_read_refuses_an_oversized_file(tmp_path):
    _write(tmp_path / "c", json.dumps(_mirror(name="x" * (canvas_marker._MAX_BYTES + 1))))
    assert canvas_marker.read(str(tmp_path / "c")) is None


def test_a_later_version_with_the_same_fields_still_reads(tmp_path):
    """A CLI one version ahead must not turn every mirror into "unmanaged",
    which fused-render reads as a live clone."""
    _write(tmp_path / "c", _mirror(version=2, extra="field"))
    marker = canvas_marker.read(str(tmp_path / "c"))
    assert marker is not None and marker.version == 2 and marker.sync == "mirror"


def test_sync_of_names_the_owner(tmp_path):
    _write(tmp_path / "m", _mirror())
    _write(tmp_path / "l", _mirror(sync="live", scope="user"))
    assert canvas_marker.sync_of(str(tmp_path / "m")) == "mirror"
    assert canvas_marker.sync_of(str(tmp_path / "l")) == "live"


@pytest.mark.parametrize("broken", [
    {"sync": "mirror"},                       # every other field missing
    _mirror(scope="elsewhere"),
    _mirror(version="one"),
    _mirror(revision="twelve", id=None),
])
def test_a_broken_marker_that_says_mirror_still_reads_as_mirror(tmp_path, broken):
    """The safe direction: `read` calls it malformed (so nothing is ever
    deleted on its word), `sync_of` still refuses to call it a clone."""
    _write(tmp_path / "c", broken)
    assert canvas_marker.read(str(tmp_path / "c")) is None
    assert canvas_marker.sync_of(str(tmp_path / "c")) == "mirror"


def test_a_broken_marker_that_says_live_reads_as_none(tmp_path):
    _write(tmp_path / "c", {"sync": "live", "scope": "nowhere"})
    assert canvas_marker.sync_of(str(tmp_path / "c")) == "none"


# -- write_live ----------------------------------------------------------------


def test_write_live_writes_the_contract_shape(tmp_path):
    folder = tmp_path / "alpha"
    folder.mkdir()
    assert canvas_marker.write_live(str(folder), name="alpha", collection_id="col-9",
                                    revision=4) is True
    data = json.loads((folder / ".fused-canvas.json").read_text(encoding="utf-8"))
    assert set(data) == {"version", "sync", "scope", "id", "name", "owner",
                         "revision", "last_updated", "pulled_at"}
    assert data["version"] == 1 and data["sync"] == "live" and data["scope"] == "user"
    assert data["id"] == "col-9" and data["name"] == "alpha" and data["revision"] == 4
    assert data["owner"] is None and data["last_updated"] is None
    assert data["pulled_at"].endswith("Z")
    assert canvas_marker.sync_of(str(folder)) == "live"


def test_write_live_leaves_no_staging_file_and_only_dot_names(tmp_path):
    folder = tmp_path / "alpha"
    folder.mkdir()
    canvas_marker.write_live(str(folder), name="alpha", collection_id="col-9")
    assert os.listdir(folder) == [".fused-canvas.json"]


def test_write_live_does_not_touch_a_marker_that_already_says_so(tmp_path):
    folder = tmp_path / "alpha"
    folder.mkdir()
    canvas_marker.write_live(str(folder), name="alpha", collection_id="col-9", revision=4)
    path = folder / ".fused-canvas.json"
    os.utime(path, (1_000_000, 1_000_000))
    before = path.read_bytes()
    assert canvas_marker.write_live(str(folder), name="alpha", collection_id="col-9",
                                    revision=4) is True
    assert path.read_bytes() == before
    assert path.stat().st_mtime == 1_000_000


@pytest.mark.parametrize("change", [
    {"collection_id": "col-10"}, {"name": "beta"}, {"revision": 5}])
def test_write_live_rewrites_when_a_field_moved(tmp_path, change):
    folder = tmp_path / "alpha"
    folder.mkdir()
    base = {"name": "alpha", "collection_id": "col-9", "revision": 4}
    canvas_marker.write_live(str(folder), **base)
    assert canvas_marker.write_live(str(folder), **{**base, **change}) is True
    marker = canvas_marker.read(str(folder))
    assert (marker.name, marker.id, marker.revision) == tuple(
        {**base, **change}[k] for k in ("name", "collection_id", "revision"))


def test_write_live_replaces_a_mirror_marker_when_the_caller_says_so(tmp_path):
    """The primitive only writes; which folder may become live is the
    caller's decision (canvases._mark_live never asks for this one)."""
    _write(tmp_path / "c", _mirror(scope="user"))
    assert canvas_marker.write_live(str(tmp_path / "c"), name="c", collection_id="col-1")
    assert canvas_marker.sync_of(str(tmp_path / "c")) == "live"


@pytest.mark.parametrize("kwargs", [
    {"name": "alpha", "collection_id": ""},
    {"name": "alpha", "collection_id": None},
    {"name": "", "collection_id": "col-9"},
    {"name": "alpha", "collection_id": "col-9", "scope": "elsewhere"},
])
def test_write_live_refuses_without_an_id_a_name_or_a_known_scope(tmp_path, kwargs):
    folder = tmp_path / "alpha"
    folder.mkdir()
    assert canvas_marker.write_live(str(folder), **kwargs) is False
    assert os.listdir(folder) == []


def test_write_live_never_raises_on_a_folder_it_cannot_write(tmp_path):
    assert canvas_marker.write_live(str(tmp_path / "missing"), name="alpha",
                                    collection_id="col-9") is False


# -- find_by_id ----------------------------------------------------------------


def test_find_by_id_prefers_the_live_folder(tmp_path):
    scope = tmp_path / "user"
    _write(scope / "alpha-12345678", _mirror(scope="user", id="col-1"))
    _write(scope / "zeta", _mirror(sync="live", scope="user", id="col-1"))
    _write(scope / "other", _mirror(scope="user", id="col-2"))
    _write(scope / ".tmp-half-written", _mirror(scope="user", id="col-3"))
    (scope / "unmarked").mkdir()
    assert canvas_marker.find_by_id(str(scope), "col-1") == str(scope / "zeta")
    assert canvas_marker.find_by_id(str(scope), "col-2") == str(scope / "other")
    assert canvas_marker.find_by_id(str(scope), "col-3") is None  # dot-folders never
    assert canvas_marker.find_by_id(str(scope), "col-404") is None
    assert canvas_marker.find_by_id(str(scope), "") is None
    assert canvas_marker.find_by_id(str(tmp_path / "missing"), "col-1") is None


# -- removable_mirror ----------------------------------------------------------


def test_removable_mirror_needs_all_three_conditions(tmp_path):
    scope = tmp_path / "user"
    _write(scope / "mirror", _mirror(scope="user"))
    _write(scope / "live", _mirror(sync="live", scope="user"))
    _write(scope / "broken", {"sync": "mirror"})
    (scope / "unmarked").mkdir()
    assert canvas_marker.removable_mirror(str(scope / "mirror"), str(scope)) is True
    # A live clone, an unmarked folder, and a marker that only SAYS mirror.
    assert canvas_marker.removable_mirror(str(scope / "live"), str(scope)) is False
    assert canvas_marker.removable_mirror(str(scope / "unmarked"), str(scope)) is False
    assert canvas_marker.removable_mirror(str(scope / "broken"), str(scope)) is False
    assert canvas_marker.removable_mirror(str(scope / "missing"), str(scope)) is False
    # Not a direct child of the scope folder.
    _write(scope / "mirror" / "nested", _mirror(scope="user"))
    assert canvas_marker.removable_mirror(str(scope / "mirror" / "nested"), str(scope)) is False
    assert canvas_marker.removable_mirror(str(scope / "mirror"), str(tmp_path)) is False


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_removable_mirror_refuses_a_symlink(tmp_path):
    scope = tmp_path / "user"
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere, _mirror(scope="user"))
    scope.mkdir()
    os.symlink(elsewhere, scope / "linked")
    assert canvas_marker.removable_mirror(str(scope / "linked"), str(scope)) is False


# -- the ignore lists ----------------------------------------------------------


def test_the_marker_is_ignored_by_the_snapshot_and_the_seeded_fusedignore():
    """Never a node source, never taken from a merged bundle, never pushed."""
    import fused_render.canvases as canvases

    assert canvas_marker.MARKER_NAME in canvas_snapshot.IGNORED_BASENAMES
    assert canvas_marker.MARKER_NAME in canvases._SYNC_IGNORED_BASENAMES
    assert canvas_marker.MARKER_NAME in canvases._CLONE_FUSEDIGNORE.splitlines()


def test_the_marker_is_not_a_node_file(tmp_path):
    (tmp_path / "canvas.toml").write_text('type = "canvas"\n', encoding="utf-8")
    (tmp_path / "udf_1.py").write_text("", encoding="utf-8")
    canvas_marker.write_live(str(tmp_path), name="c", collection_id="col-1")
    assert canvas_snapshot.node_files(str(tmp_path)) == {"udf_1": "udf_1.py"}


def test_the_module_is_stdlib_only():
    """`_canvas_push.py` imports it on the startup path of every `fused`
    command, and it must import with no `fused` extra installed."""
    import ast

    with open(canvas_marker.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "dataclasses", "datetime", "json", "os", "shutil",
                        "threading", "uuid"}
