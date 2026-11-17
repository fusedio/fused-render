"""The node view's server half (SPEC §53.3 NV-1, NV-2): the native `udf`
template — registry, resolution, its condition.py gate — and
GET /api/canvases/node, which shares the gate's one rule
(`canvases.canvas_node_file`).

The canvases root is a tmp folder (FUSED_RENDER_CANVASES_DIR), the
`canvases_enabled` preference is the real prefs.json under a per-test
FUSED_RENDER_HOME, and the gate runs exactly as /api/fs/conditions runs it
(`_conditions_payload`)."""
import json
import os

import pytest
from fastapi.testclient import TestClient

import fused_render.canvases as canvases_mod
from fused_render import canvas_marker
from fused_render.server import create_app
from fused_render.server import templates as server_templates
from fused_render.shell import prefs as prefs_mod
from fused_render.shell import storage

ENV = "unstable"

CANVAS_TOML = """\
type = "canvas"
version = 2
name = "demo"

[canvas]
edges = [["udf_1", "chart"]]

[[canvas.nodes]]
udfName = "udf_1"
title = "Load data"
x = 0.0
y = 0.0

[[canvas.nodes]]
udfName = "chart"
x = 400.0
y = 0.0

[[canvas.nodes]]
udfName = "page"
x = 800.0
y = 0.0

[[canvas.nodes]]
udfName = "notes"
x = 0.0
y = 400.0

[[canvas.nodes]]
udfName = "_helper"
x = 0.0
y = 800.0

[[canvas.nodes]]
udfName = "grp"
type = "udf-folder"
folderName = "grp"
childUdfOrder = ["udf_1"]
"""

NODE_FILES = {
    "udf_1.py": "@fused.udf\ndef udf():\n    return 1\n",
    "chart.json": "{}\n",
    "page.html": "<p>hi</p>\n",
    "notes.md": "# notes\n",
    "_helper.py": "X = 1\n",
    "grp.py": "# a file named like the folder row\n",
    "orphan.py": "# no row for this one\n",
    "CLAUDE.md": "# seeded\n",
}


def _set_pref(on: bool) -> None:
    data = storage.read_json(prefs_mod._path())
    data = data if isinstance(data, dict) else {}
    data["canvases_enabled"] = on
    os.makedirs(os.path.dirname(prefs_mod._path()), exist_ok=True)
    with open(prefs_mod._path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _canvas(folder, toml=CANVAS_TOML, files=None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "canvas.toml").write_text(toml, encoding="utf-8")
    for name, text in (NODE_FILES if files is None else files).items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


@pytest.fixture
def base(tmp_path, monkeypatch):
    root = tmp_path / "canvases"
    # A home of this test's own: the run-wide one is shared by every xdist
    # worker, and this file flips prefs.json under it.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(root))
    monkeypatch.delenv("FUSED_RENDER_WORKBENCH_ENV", raising=False)
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(tmp_path / "creds.json"))
    canvases_mod._cli_cache_clear()
    # A verdict cached by an earlier test of this worker must not answer
    # for this one (the cache is keyed by path, prefs mtime and, for a node-
    # shaped path, canvas.toml mtime).
    server_templates._CONDITIONS_CACHE.clear()
    _set_pref(True)
    yield root
    canvases_mod._cli_cache_clear()
    server_templates._CONDITIONS_CACHE.clear()


@pytest.fixture
def clone(base):
    return _canvas(base / ENV / "user" / "demo")


def gate(path) -> bool:
    """The verdict exactly as /api/fs/conditions computes it."""
    payload = server_templates._conditions_payload(str(path))
    assert isinstance(payload, dict), payload
    assert payload.get("error") is None, payload
    return payload["conditions"]["udf"]


def modes(path):
    entries, error = server_templates._templates_for(str(path), False)
    assert error is None
    return [e["mode"] for e in entries]


# ----------------------------------------------------------- registry (NV-1)

def test_udf_is_registered_after_the_default_of_the_four_node_kinds():
    with open(server_templates.BUILTIN_REGISTRY, encoding="utf-8") as fh:
        registry = json.load(fh)
    for key in (".py", ".json", ".html", ".md"):
        assert registry[key][1] == "udf", key
        assert registry[key].count("udf") == 1, key
    # Nowhere else — not `.*.json` either: a compound stem is never a node
    # name, so every such file would pay a gate evaluation for nothing.
    others = [k for k, v in registry.items() if "udf" in v
              and k not in (".py", ".json", ".html", ".md")]
    assert others == []


def test_udf_is_conditional_so_no_default_moves(tmp_path):
    assert "udf" not in [e["mode"] for e in server_templates._templates_for(
        str(tmp_path / "a.b.json"), False)[0]]
    for name, default in (("a.py", "code"), ("a.json", "tree"),
                          ("a.html", "_render"), ("a.md", "markdown")):
        entries, error = server_templates._templates_for(str(tmp_path / name), False)
        assert error is None
        assert entries[0]["mode"] == default, name
        udf = next(e for e in entries if e["mode"] == "udf")
        assert udf.get("conditional") is True, name
        assert udf.get("native") is True, name


def test_udf_resolves_through_its_native_marker():
    path, err = server_templates._resolve_name("udf")
    assert err is None
    folder = os.path.dirname(path)
    assert os.path.basename(path) == server_templates.NATIVE_MARKER
    assert os.path.basename(folder) == "udf"
    assert server_templates._is_native(path)
    assert not os.path.exists(os.path.join(folder, "template.html"))
    assert os.path.isfile(os.path.join(folder, "condition.py"))
    assert server_templates._icon_for(path) == os.path.join(folder, "icon.svg")


def test_render_refuses_the_udf_native_template(tmp_path):
    path, _ = server_templates._resolve_name("udf")
    assert server_templates.is_native_template_path(path)
    assert server_templates.is_native_template_path(os.path.dirname(path))


# --------------------------------------------------------------- gate (NV-2)

def test_gate_allows_every_node_kind_of_a_user_clone(clone):
    for name in ("udf_1.py", "chart.json", "page.html", "notes.md"):
        assert gate(clone / name) is True, name
    assert modes(clone / "udf_1.py")[:2] == ["code", "udf"]


def test_gate_is_off_with_the_preference(clone):
    _set_pref(False)
    server_templates._CONDITIONS_CACHE.clear()
    assert gate(clone / "udf_1.py") is False


def test_gate_off_answers_before_touching_the_canvas(clone, monkeypatch):
    """The preference is the cheapest check and comes first: with it off the
    path is never looked at."""
    _set_pref(False)
    called = []
    monkeypatch.setattr(canvases_mod, "canvas_node_file", lambda p: called.append(p))
    assert gate(clone / "udf_1.py") is False
    assert called == []


def test_gate_refuses_underscore_py_ignored_and_rowless_files(clone):
    assert gate(clone / "_helper.py") is False       # `_x.py` is never a node source
    assert gate(clone / "CLAUDE.md") is False        # the clone's seeded file
    assert gate(clone / "orphan.py") is False        # no row of that name
    assert gate(clone / "grp.py") is False           # the row is a folder


def test_gate_refuses_a_file_the_suffix_rule_does_not_pick(clone):
    # `chart.py` beside `chart.json`: the .py is the node's source, so the
    # .json is no node file of the row (its kind does not match).
    (clone / "chart.py").write_text("x = 1\n", encoding="utf-8")
    assert gate(clone / "chart.json") is False
    assert gate(clone / "chart.py") is True


def test_gate_refuses_a_fusedignored_file(clone):
    (clone / ".fusedignore").write_text("notes.md\n", encoding="utf-8")
    assert gate(clone / "notes.md") is False


def test_gate_refuses_team_and_public_copies(base):
    for scope in ("team", "public"):
        folder = _canvas(base / ENV / scope / "demo")
        for name in ("udf_1.py", "chart.json", "page.html", "notes.md"):
            assert gate(folder / name) is False, (scope, name)


def test_gate_allows_a_reference_copy_under_user(base):
    folder = _canvas(base / ENV / "user" / "demo")
    (folder / canvas_marker.MARKER_NAME).write_text(
        json.dumps({"version": 1, "name": "demo", "sync": "mirror", "scope": "user",
                    "id": "11111111-2222-3333-4444-555555555555"}),
        encoding="utf-8")
    assert canvas_marker.is_reference(str(folder))
    assert gate(folder / "udf_1.py") is True


def test_gate_refuses_files_outside_the_canvas_root_or_too_deep(base, tmp_path):
    # The same canvas outside the canvases root.
    elsewhere = _canvas(tmp_path / "elsewhere" / "demo")
    assert gate(elsewhere / "udf_1.py") is False
    # At the pre-D1476 path, `<env>/<name>` without `user/`.
    old = _canvas(base / ENV / "demo")
    assert gate(old / "udf_1.py") is False
    # A file one level down inside a clone.
    clone = _canvas(base / ENV / "user" / "demo")
    sub = _canvas(clone / "sub")
    assert gate(sub / "udf_1.py") is False


def test_gate_refuses_an_oversize_or_unparseable_toml(clone, monkeypatch):
    (clone / "canvas.toml").write_text('type = "canvas"\n[canvas\n', encoding="utf-8")
    assert gate(clone / "udf_1.py") is False
    (clone / "canvas.toml").write_text(CANVAS_TOML, encoding="utf-8")
    server_templates._CONDITIONS_CACHE.clear()
    monkeypatch.setattr(canvases_mod, "NODE_TOML_MAX_BYTES", 16)
    assert gate(clone / "udf_1.py") is False
    monkeypatch.undo()


def test_gate_refuses_a_folder_without_canvas_toml(clone):
    (clone / "canvas.toml").unlink()
    assert gate(clone / "udf_1.py") is False


def test_gate_never_raises(monkeypatch):
    """A broken helper denies; it does not surface as a gate error."""
    def boom(path):
        raise RuntimeError("boom")

    monkeypatch.setattr(prefs_mod, "canvases_enabled", lambda: True)
    monkeypatch.setattr(canvases_mod, "canvas_node_file", boom)
    cond = os.path.join(os.path.dirname(server_templates._resolve_name("udf")[0]),
                        "condition.py")
    allowed, err = server_templates._run_condition(cond, "/x/y.py")
    assert allowed is False and err is None


def test_canvas_node_file_shape(clone):
    info = canvases_mod.canvas_node_file(str(clone / "udf_1.py"))
    assert info == {"env": ENV, "name": "demo", "node": "udf_1", "kind": "udf",
                    "file": "udf_1.py", "title": "Load data",
                    "clone_dir": canvases_mod._canvas_dir(ENV, "demo")}
    kinds = {n: canvases_mod.canvas_node_file(str(clone / n))["kind"]
             for n in ("chart.json", "page.html", "notes.md")}
    assert kinds == {"chart.json": "widget", "page.html": "html", "notes.md": "note"}
    # No title on the row: the node's name.
    assert canvases_mod.canvas_node_file(str(clone / "chart.json"))["title"] == "chart"
    assert canvases_mod.canvas_node_file("relative/udf_1.py") is None
    assert canvases_mod.canvas_node_file("") is None


# --------------------------------------------------- GET /api/canvases/node

@pytest.fixture
def client(base, tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_node_route_with_the_preference_off(client, clone):
    _set_pref(False)
    res = client.get("/api/canvases/node", params={"path": str(clone / "udf_1.py")})
    assert res.status_code == 200
    assert res.json() == {"enabled": False}


def test_node_route_for_a_file_that_is_no_node(client, clone, tmp_path):
    for p in (clone / "_helper.py", clone / "orphan.py", tmp_path / "x.py"):
        res = client.get("/api/canvases/node", params={"path": str(p)})
        assert res.status_code == 200
        assert res.json() == {"enabled": True, "node": None}, p


def test_node_route_refuses_a_relative_path(client):
    assert client.get("/api/canvases/node", params={"path": "a/b.py"}).status_code == 400
    assert client.get("/api/canvases/node").status_code == 400


def test_node_route_for_a_live_clone_signed_out(client, clone):
    res = client.get("/api/canvases/node", params={"path": str(clone / "udf_1.py")})
    assert res.status_code == 200
    assert res.json() == {
        "enabled": True, "env": ENV, "name": "demo", "node": "udf_1", "kind": "udf",
        "file": "udf_1.py", "title": "Load data", "live": True, "reference": False,
        "signed_in": False,
    }


def test_node_route_for_a_reference_copy_signed_in_with_a_cached_handle(
        client, base, tmp_path):
    folder = _canvas(base / ENV / "user" / "demo")
    (folder / canvas_marker.MARKER_NAME).write_text(
        json.dumps({"version": 1, "name": "demo", "sync": "mirror", "scope": "user",
                    "id": "11111111-2222-3333-4444-555555555555"}),
        encoding="utf-8")
    (tmp_path / "creds.json").write_text('{"access_token": "t"}', encoding="utf-8")
    # A whoami answer this process already holds (no CLI is run by the route).
    sig = canvases_mod._creds_sig(ENV)
    canvases_mod._cli_cache[("whoami", ENV, sig)] = (
        canvases_mod.time.monotonic() + 60, {"handle": "ada", "raw": {}})
    body = client.get("/api/canvases/node", params={"path": str(folder / "notes.md")}).json()
    assert body["live"] is False and body["reference"] is True
    assert body["signed_in"] is True and body["handle"] == "ada"
    assert body["kind"] == "note" and body["node"] == "notes"


def test_node_route_needs_no_guard_header_and_writes_nothing(client, clone):
    before = sorted(os.listdir(clone))
    res = client.get("/api/canvases/node", params={"path": str(clone / "page.html")})
    assert res.status_code == 200 and res.json()["kind"] == "html"
    assert sorted(os.listdir(clone)) == before


def test_node_route_and_gate_agree(client, clone, base):
    team = _canvas(base / ENV / "team" / "demo")
    paths = [clone / n for n in NODE_FILES] + [team / "udf_1.py"]
    for p in paths:
        node = client.get("/api/canvases/node", params={"path": str(p)}).json()
        assert (node.get("node") is not None) == gate(p), p


def test_a_user_template_named_udf_cannot_shadow_the_node_view(tmp_path, monkeypatch, base):
    """PT-6 / D1310: a core NATIVE template is never shadowed. A user folder
    `udf/` with a page and a gate of its own is ignored: the entry stays the
    core native one (`native: true`, which the shell keys the node view on)
    and the core gate decides — so a plain .py is still refused."""
    udir = tmp_path / "user-templates"
    (udir / "udf").mkdir(parents=True)
    (udir / "udf" / "template.html").write_text("<html>mine</html>", encoding="utf-8")
    (udir / "udf" / "condition.py").write_text("def main(path):\n    return True\n", encoding="utf-8")
    monkeypatch.setattr(server_templates, "USER_TEMPLATES_DIR", str(udir))
    monkeypatch.setattr(server_templates, "USER_REGISTRY", str(udir / "registry.json"))
    monkeypatch.setattr(server_templates, "_WARNED_STALE", set())
    core = os.path.join(server_templates.TEMPLATES_DIR, "udf", server_templates.NATIVE_MARKER)
    path, err = server_templates._resolve_name("udf")
    assert err is None and path == core
    plain = tmp_path / "plain.py"
    plain.write_text("x = 1\n", encoding="utf-8")
    entries, _ = server_templates._templates_for(str(plain), False)
    udf = next(e for e in entries if e["mode"] == "udf")
    assert udf.get("native") is True and udf["path"] == core
    assert gate(plain) is False
    # And /render will not serve the user's page either.
    assert server_templates.is_native_template_path(str(udir / "udf" / "template.html"))


# ------------------------------------- stat's default candidate (PT-8, NV-3)

def _udf_entry(path):
    entries, _ = server_templates._templates_for(str(path), False)
    return next((e for e in entries if e["mode"] == "udf"), None)


def _prefer(path):
    entry = _udf_entry(path)
    return None if entry is None else entry.get("prefer")


def test_stat_marks_the_node_view_preferred_on_a_node_shaped_path(clone):
    for name in ("udf_1.py", "chart.json", "page.html", "notes.md"):
        entry = _udf_entry(clone / name)
        assert entry["prefer"] is True, name
        assert entry["native"] is True and entry["conditional"] is True
    # Shape only: a .py with no canvas.toml row is still a candidate — the
    # gate, not stat, says no (`orphan.py`); so is a file that is not there.
    assert _prefer(clone / "orphan.py") is True
    assert _prefer(clone / "not_yet.py") is True
    # No other entry ever carries the flag.
    entries, _ = server_templates._templates_for(str(clone / "udf_1.py"), False)
    assert [e["mode"] for e in entries if "prefer" in e] == ["udf"]


def test_stat_does_not_prefer_with_the_preference_off(clone):
    _set_pref(False)
    assert _prefer(clone / "udf_1.py") is None


def test_stat_does_not_prefer_off_the_node_shape(base, clone, tmp_path):
    for scope in ("team", "public"):
        folder = _canvas(base / ENV / scope / "demo")
        assert _prefer(folder / "udf_1.py") is None, scope
    sub = _canvas(clone / "sub")
    assert _prefer(sub / "udf_1.py") is None
    assert _prefer(clone / "_helper.py") is None
    assert _prefer(base / ENV / "demo" / "udf_1.py") is None  # pre-D1476 path
    plain = tmp_path / "plain.py"
    plain.write_text("x = 1\n", encoding="utf-8")
    assert _prefer(plain) is None


def test_stat_of_an_ordinary_file_is_unchanged(tmp_path, base):
    """No `prefer` key at all — the payload is what it was before."""
    plain = tmp_path / "plain.py"
    plain.write_text("x = 1\n", encoding="utf-8")
    entries, _ = server_templates._templates_for(str(plain), False)
    assert all("prefer" not in e for e in entries)


def test_stat_does_not_prefer_where_a_user_registry_drops_udf(tmp_path, monkeypatch, clone):
    udir = tmp_path / "user-templates"
    udir.mkdir()
    (udir / "registry.json").write_text(json.dumps({".py": ["code"]}), encoding="utf-8")
    monkeypatch.setattr(server_templates, "USER_TEMPLATES_DIR", str(udir))
    monkeypatch.setattr(server_templates, "USER_REGISTRY", str(udir / "registry.json"))
    entries, _ = server_templates._templates_for(str(clone / "udf_1.py"), False)
    assert [e["mode"] for e in entries] == ["code"]
    assert all("prefer" not in e for e in entries)


def test_stat_prefers_only_the_core_native_udf(tmp_path, monkeypatch, clone):
    """A user template of its own, gated and bound on `.py`, is never marked;
    a user `udf/` folder is ignored (D1310), so the entry that carries the
    flag is still the core native one."""
    udir = tmp_path / "user-templates"
    for name in ("mynode", "udf"):
        (udir / name).mkdir(parents=True)
        (udir / name / "template.html").write_text("<html>mine</html>", encoding="utf-8")
        (udir / name / "condition.py").write_text("def main(path):\n    return True\n",
                                                  encoding="utf-8")
    (udir / "registry.json").write_text(json.dumps({".py": ["mynode", "udf", "code"]}),
                                        encoding="utf-8")
    monkeypatch.setattr(server_templates, "USER_TEMPLATES_DIR", str(udir))
    monkeypatch.setattr(server_templates, "USER_REGISTRY", str(udir / "registry.json"))
    monkeypatch.setattr(server_templates, "_WARNED_STALE", set())
    entries, _ = server_templates._templates_for(str(clone / "udf_1.py"), False)
    by_mode = {e["mode"]: e for e in entries}
    assert by_mode["mynode"].get("conditional") is True and "prefer" not in by_mode["mynode"]
    core = os.path.join(server_templates.TEMPLATES_DIR, "udf", server_templates.NATIVE_MARKER)
    assert by_mode["udf"]["path"] == core and by_mode["udf"]["prefer"] is True


def test_stat_route_carries_prefer(client, clone):
    body = client.get("/api/fs/stat", params={"path": str(clone / "udf_1.py")}).json()
    udf = next(e for e in body["templates"] if e["mode"] == "udf")
    assert udf["prefer"] is True


def test_conditions_cache_follows_the_canvas_toml_of_a_node_shaped_path(client, clone):
    """A row added to canvas.toml makes a file a node at once, not after the
    conditions cache's TTL: a node-shaped path's verdict is keyed on its
    canvas.toml mtime too."""
    orphan = str(clone / "orphan.py")
    ask = lambda: client.get("/api/fs/conditions", params={"path": orphan}).json()  # noqa: E731
    assert ask()["conditions"]["udf"] is False
    toml = clone / "canvas.toml"
    toml.write_text(CANVAS_TOML + '\n[[canvas.nodes]]\nudfName = "orphan"\nx = 0.0\ny = 1200.0\n',
                    encoding="utf-8")
    st = os.stat(toml)
    os.utime(toml, (st.st_atime, st.st_mtime + 5))
    assert ask()["conditions"]["udf"] is True


def test_conditions_key_stats_nothing_more_for_a_path_that_is_no_node_shape(tmp_path, monkeypatch):
    seen = []
    real = os.path.getmtime
    monkeypatch.setattr(os.path, "getmtime", lambda p: (seen.append(str(p)), real(p))[1])
    server_templates._conditions_key(str(tmp_path / "x.py"))
    assert [p for p in seen if p.endswith("canvas.toml")] == []
