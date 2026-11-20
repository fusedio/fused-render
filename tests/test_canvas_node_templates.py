"""The canvas node templates' server half (SPEC §53.2 NT-1..NT-3): the three
gated template folders — `canvas_widget`, `canvas_note`, `canvas_udf` — their
registry bindings, their gates, stat's `node` / `prefer` marks, and
GET /api/canvases/node, which shares the gates' one rule
(`canvases.canvas_node_file`).

The canvases root is a tmp folder (FUSED_RENDER_CANVASES_DIR), the
`canvases_enabled` preference is the real prefs.json under a per-test
FUSED_RENDER_HOME, and the gates run exactly as /api/fs/conditions runs them
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


# Which node template each node suffix is bound to.
TEMPLATE_OF = {".py": "canvas_udf", ".json": "canvas_widget",
               ".html": "canvas_widget", ".md": "canvas_note"}
NODE_TEMPLATES = ("canvas_widget", "canvas_note", "canvas_udf")


def template_of(path) -> str:
    return TEMPLATE_OF[os.path.splitext(str(path))[1]]


def gate(path) -> bool:
    """The verdict exactly as /api/fs/conditions computes it, for the node
    template the file's suffix is bound to."""
    payload = server_templates._conditions_payload(str(path))
    assert isinstance(payload, dict), payload
    assert payload.get("error") is None, payload
    return payload["conditions"][template_of(path)]


def modes(path):
    entries, error = server_templates._templates_for(str(path), False)
    assert error is None
    return [e["mode"] for e in entries]


# ----------------------------------------------------------- registry (NT-1)

def test_each_node_kind_is_bound_after_its_keys_default():
    with open(server_templates.BUILTIN_REGISTRY, encoding="utf-8") as fh:
        registry = json.load(fh)
    for key, name in TEMPLATE_OF.items():
        assert registry[key][1] == name, key
        assert sum(registry[key].count(n) for n in NODE_TEMPLATES) == 1, key
    # Nowhere else — not `.*.json` either: a compound stem is never a node
    # name, so every such file would pay a gate evaluation for nothing.
    others = [k for k, v in registry.items()
              if any(n in v for n in NODE_TEMPLATES) and k not in TEMPLATE_OF]
    assert others == []
    # The retired sentinel is in no list.
    assert not [k for k, v in registry.items() if "_node" in v]


def test_node_templates_are_conditional_so_no_default_moves(tmp_path):
    assert not [e for e in server_templates._templates_for(
        str(tmp_path / "a.b.json"), False)[0] if e["mode"] in NODE_TEMPLATES]
    for name, default in (("a.py", "code"), ("a.json", "tree"),
                          ("a.html", "_render"), ("a.md", "markdown")):
        entries, error = server_templates._templates_for(str(tmp_path / name), False)
        assert error is None
        assert entries[0]["mode"] == default, name
        mode = template_of(name)
        entry = next(e for e in entries if e["mode"] == mode)
        folder = os.path.join(server_templates.TEMPLATES_DIR, mode)
        assert entry == {"mode": mode, "path": os.path.join(folder, "template.html"),
                         "icon": os.path.join(folder, "icon.svg"),
                         "conditional": True, "node": True}, name


def test_node_templates_are_folders_with_a_page_a_gate_and_an_icon():
    for name in NODE_TEMPLATES:
        folder = os.path.join(server_templates.TEMPLATES_DIR, name)
        for part in ("template.html", "condition.py", "icon.svg"):
            assert os.path.isfile(os.path.join(folder, part)), (name, part)
        assert not os.path.exists(os.path.join(folder, "native")), name
    # The node view they replace is gone: no sentinel, no gate table, no folder.
    assert "_node" not in server_templates.KNOWN_SENTINELS
    assert not hasattr(server_templates, "SENTINEL_CONDITIONS")
    assert not os.path.exists(os.path.join(server_templates.TEMPLATES_DIR, "udf"))
    path, err = server_templates._resolve_name("_node")
    assert path is None and err


def test_each_gate_answers_for_its_own_kinds_only(clone):
    """Rebound by a user registry onto every node suffix, a node template
    still says yes only to the kinds it draws."""
    want = {"canvas_udf": {"udf_1.py"}, "canvas_widget": {"chart.json", "page.html"},
            "canvas_note": {"notes.md"}}
    for name, yes in want.items():
        gate_file = os.path.join(server_templates.TEMPLATES_DIR, name, "condition.py")
        for node in ("udf_1.py", "chart.json", "page.html", "notes.md"):
            allowed, err = server_templates._run_condition(gate_file, str(clone / node))
            assert err is None
            assert allowed is (node in yes), (name, node)


def test_a_user_registry_may_rebind_a_node_template(tmp_path, monkeypatch, clone):
    """Listed first by a user registry, the built-in folder keeps its gate
    and its marks."""
    udir = tmp_path / "utemplates"
    udir.mkdir()
    (udir / "registry.json").write_text(json.dumps({".py": ["canvas_udf", "code"]}),
                                        encoding="utf-8")
    monkeypatch.setattr(server_templates, "USER_TEMPLATES_DIR", str(udir))
    monkeypatch.setattr(server_templates, "USER_REGISTRY", str(udir / "registry.json"))
    entries, err = server_templates._templates_for(str(clone / "udf_1.py"), False)
    assert err is None
    assert [e["mode"] for e in entries] == ["canvas_udf", "code"]
    assert entries[0]["conditional"] is True and entries[0]["prefer"] is True
    assert entries[0]["node"] is True
    assert gate(clone / "udf_1.py") is True


# --------------------------------------------------------------- gate (NT-2)

def test_gate_allows_every_node_kind_of_a_user_clone(clone):
    for name in ("udf_1.py", "chart.json", "page.html", "notes.md"):
        assert gate(clone / name) is True, name
    assert modes(clone / "udf_1.py")[:2] == ["code", "canvas_udf"]


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
    assert canvases_mod.node_template_gate("/x/y.py", ("udf",)) is False
    for name in NODE_TEMPLATES:
        gate_file = os.path.join(server_templates.TEMPLATES_DIR, name, "condition.py")
        allowed, err = server_templates._run_condition(gate_file, "/x/y.py")
        assert allowed is False and err is None, name


def test_canvas_node_file_shape(clone):
    info = canvases_mod.canvas_node_file(str(clone / "udf_1.py"))
    row = info.pop("row")
    assert row["udfName"] == "udf_1" and row["title"] == "Load data"
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
        "file": "udf_1.py", "title": "Load data", "style": {}, "live": True,
        "reference": False, "signed_in": False,
    }


def test_node_route_carries_the_rows_style_keys_only(client, clone):
    """A note's card is drawn from its row: the colour, sheen, scale and
    alignment keys, and nothing else of the row (position, size)."""
    toml = clone / "canvas.toml"
    toml.write_text(CANVAS_TOML.replace(
        'udfName = "notes"\n',
        'udfName = "notes"\ntextBoxColor = "#A5DD6B"\ntextBoxGradient = false\n'
        'textBoxScaleFactor = 1.5\ntextBoxAlignment = "center"\nwidth = 350\n'
        'textBoxExtra = ["not", "plain"]\n'), encoding="utf-8")
    body = client.get("/api/canvases/node", params={"path": str(clone / "notes.md")}).json()
    assert body["kind"] == "note"
    assert body["style"] == {"textBoxColor": "#A5DD6B", "textBoxGradient": False,
                             "textBoxScaleFactor": 1.5, "textBoxAlignment": "center"}


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


def test_a_user_folder_of_the_same_name_is_an_ordinary_template(tmp_path, monkeypatch, clone):
    """A user folder named like a node template shadows it (CT-3) and is a
    template like any other: its own page, its own gate, never marked `node`
    and never preferred, even on a node file."""
    udir = tmp_path / "user-templates"
    (udir / "canvas_udf").mkdir(parents=True)
    (udir / "canvas_udf" / "template.html").write_text("<html>mine</html>", encoding="utf-8")
    (udir / "canvas_udf" / "condition.py").write_text("def main(path):\n    return True\n",
                                                      encoding="utf-8")
    monkeypatch.setattr(server_templates, "USER_TEMPLATES_DIR", str(udir))
    monkeypatch.setattr(server_templates, "USER_REGISTRY", str(udir / "registry.json"))
    path, err = server_templates._resolve_name("canvas_udf")
    assert err is None and path == str(udir / "canvas_udf" / "template.html")
    entries, _ = server_templates._templates_for(str(clone / "udf_1.py"), False)
    mine = next(e for e in entries if e["mode"] == "canvas_udf")
    assert mine["path"] == path and mine["conditional"] is True
    assert "node" not in mine and "prefer" not in mine


# ------------------------------------- stat's default candidate (PT-8, NT-3)

def _node_entry(path):
    entries, _ = server_templates._templates_for(str(path), False)
    return next((e for e in entries if e["mode"] in NODE_TEMPLATES), None)


def _prefer(path):
    entry = _node_entry(path)
    return None if entry is None else entry.get("prefer")


def test_stat_marks_the_node_template_preferred_on_a_node_shaped_path(clone):
    for name in ("udf_1.py", "chart.json", "page.html", "notes.md"):
        entry = _node_entry(clone / name)
        assert entry["mode"] == template_of(name), name
        assert entry["prefer"] is True, name
        assert entry["node"] is True and entry["conditional"] is True
    # Shape only: a .py with no canvas.toml row is still a candidate — the
    # gate, not stat, says no (`orphan.py`); so is a file that is not there.
    assert _prefer(clone / "orphan.py") is True
    assert _prefer(clone / "not_yet.py") is True
    # No other entry ever carries the flag.
    entries, _ = server_templates._templates_for(str(clone / "udf_1.py"), False)
    assert [e["mode"] for e in entries if "prefer" in e] == ["canvas_udf"]
    assert [e["mode"] for e in entries if "node" in e] == ["canvas_udf"]


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
    """No `prefer` key at all, and `node` only on the node template's own
    entry — which the shell then lists on its gate's explicit yes alone."""
    plain = tmp_path / "plain.py"
    plain.write_text("x = 1\n", encoding="utf-8")
    entries, _ = server_templates._templates_for(str(plain), False)
    assert all("prefer" not in e for e in entries)
    assert [e["mode"] for e in entries if e.get("node")] == ["canvas_udf"]


def test_stat_does_not_prefer_where_a_user_registry_drops_the_node_template(tmp_path, monkeypatch, clone):
    udir = tmp_path / "user-templates"
    udir.mkdir()
    (udir / "registry.json").write_text(json.dumps({".py": ["code"]}), encoding="utf-8")
    monkeypatch.setattr(server_templates, "USER_TEMPLATES_DIR", str(udir))
    monkeypatch.setattr(server_templates, "USER_REGISTRY", str(udir / "registry.json"))
    entries, _ = server_templates._templates_for(str(clone / "udf_1.py"), False)
    assert [e["mode"] for e in entries] == ["code"]
    assert all("prefer" not in e for e in entries)


def test_stat_prefers_only_a_built_in_node_template(tmp_path, monkeypatch, clone):
    """A user template of its own, gated and bound on `.py`, is never marked:
    only the built-in node template carries the flags."""
    udir = tmp_path / "user-templates"
    (udir / "mynode").mkdir(parents=True)
    (udir / "mynode" / "template.html").write_text("<html>mine</html>", encoding="utf-8")
    (udir / "mynode" / "condition.py").write_text("def main(path):\n    return True\n",
                                                  encoding="utf-8")
    (udir / "registry.json").write_text(json.dumps({".py": ["mynode", "canvas_udf", "code"]}),
                                        encoding="utf-8")
    monkeypatch.setattr(server_templates, "USER_TEMPLATES_DIR", str(udir))
    monkeypatch.setattr(server_templates, "USER_REGISTRY", str(udir / "registry.json"))
    entries, _ = server_templates._templates_for(str(clone / "udf_1.py"), False)
    by_mode = {e["mode"]: e for e in entries}
    assert by_mode["mynode"].get("conditional") is True
    assert "prefer" not in by_mode["mynode"] and "node" not in by_mode["mynode"]
    assert by_mode["canvas_udf"]["prefer"] is True and by_mode["canvas_udf"]["node"] is True


def test_stat_route_carries_prefer(client, clone):
    body = client.get("/api/fs/stat", params={"path": str(clone / "udf_1.py")}).json()
    udf = next(e for e in body["templates"] if e["mode"] == "canvas_udf")
    assert udf["prefer"] is True and udf["node"] is True


def test_conditions_cache_follows_the_canvas_toml_of_a_node_shaped_path(client, clone):
    """A row added to canvas.toml makes a file a node at once, not after the
    conditions cache's TTL: a node-shaped path's verdict is keyed on its
    canvas.toml mtime too."""
    orphan = str(clone / "orphan.py")
    ask = lambda: client.get("/api/fs/conditions", params={"path": orphan}).json()  # noqa: E731
    assert ask()["conditions"]["canvas_udf"] is False
    toml = clone / "canvas.toml"
    toml.write_text(CANVAS_TOML + '\n[[canvas.nodes]]\nudfName = "orphan"\nx = 0.0\ny = 1200.0\n',
                    encoding="utf-8")
    st = os.stat(toml)
    os.utime(toml, (st.st_atime, st.st_mtime + 5))
    assert ask()["conditions"]["canvas_udf"] is True


def _syscalls_of(monkeypatch, fn):
    seen = []
    real_stat, real_lstat = os.stat, os.lstat
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: (seen.append(str(p)), real_stat(p, *a, **k))[1])
    monkeypatch.setattr(os, "lstat", lambda p, *a, **k: (seen.append(str(p)), real_lstat(p, *a, **k))[1])
    try:
        fn()
    finally:
        monkeypatch.setattr(os, "stat", real_stat)
        monkeypatch.setattr(os, "lstat", real_lstat)
    return seen


def test_a_conditions_cache_hit_for_an_ordinary_file_costs_one_stat(base, tmp_path, monkeypatch):
    """The node shape (which resolves the canvases roots, a dozen syscalls)
    is worked out on a miss only: a hit for a plain `.py` pays the prefs.json
    stat and nothing else, as before the canvas.toml key."""
    from fused_render.server.routers import fs_read

    monkeypatch.setattr(fs_read, "_conditions_payload", lambda p: {"path": p, "conditions": {}})
    plain = str(tmp_path / "x.py")
    fs_read.api_fs_conditions(plain)                       # the miss
    hit = _syscalls_of(monkeypatch, lambda: fs_read.api_fs_conditions(plain))
    assert len(hit) == 1 and hit[0].endswith("prefs.json"), hit


def test_a_conditions_cache_hit_for_a_node_file_stats_its_canvas_toml_only(clone, monkeypatch):
    from fused_render.server.routers import fs_read

    monkeypatch.setattr(fs_read, "_conditions_payload", lambda p: {"path": p, "conditions": {}})
    node = str(clone / "udf_1.py")
    fs_read.api_fs_conditions(node)
    hit = _syscalls_of(monkeypatch, lambda: fs_read.api_fs_conditions(node))
    assert len(hit) == 2, hit
    assert hit[1] == str(clone / "canvas.toml"), hit


# ----------------------------------------- POST /api/canvases/node/run (§53.5)

GUARD = {"X-Fused": "1"}


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


@pytest.fixture
def signed_in(tmp_path):
    (tmp_path / "creds.json").write_text('{"access_token": "t"}', encoding="utf-8")


@pytest.fixture
def shim(monkeypatch):
    """Stands in for the node-run child: records what it was asked and
    answers with `shim.answer` (a _Proc, or an exception to raise)."""
    calls = []

    class Shim:
        answer = _Proc(json.dumps({"ok": True, "content_type": "text/html",
                                   "body_b64": "PHA+aGk8L3A+", "stdout": "", "stderr": "",
                                   "cached": False, "seconds": 0.5}))

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "input": json.loads(kwargs["input"]), "env": kwargs["env"],
                      "timeout": kwargs["timeout"]})
        if isinstance(Shim.answer, BaseException):
            raise Shim.answer
        return Shim.answer

    monkeypatch.setattr(canvases_mod.subprocess, "run", fake_run)
    Shim.calls = calls
    return Shim


def _run(client, path, headers=GUARD):
    return client.post("/api/canvases/node/run", json={"path": str(path)}, headers=headers)


def test_node_run_needs_the_guard_header(client, clone, signed_in, shim):
    res = _run(client, clone / "udf_1.py", headers={})
    assert res.status_code >= 400
    assert shim.calls == []


def test_node_run_runs_a_udf_nodes_file_in_its_canvases_environment(client, clone, signed_in, shim):
    res = _run(client, clone / "udf_1.py")
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True and res.json()["content_type"] == "text/html"
    (call,) = shim.calls
    assert os.path.basename(call["cmd"][-1]) == "_fused_node_run.py"
    assert call["input"] == {"path": str(clone / "udf_1.py"),
                             "max_bytes": canvases_mod.NODE_RUN_MAX_BYTES}
    assert call["timeout"] == canvases_mod.NODE_RUN_TIMEOUT
    # The canvas's own Fused, exactly as its CLI calls are pointed at it.
    cli = canvases_mod.fused_cli()
    assert call["env"].get("FUSED_ENV") == canvases_mod._cli_env(cli, ENV).get("FUSED_ENV")


def test_node_run_passes_a_runs_own_error_through(client, clone, signed_in, shim):
    shim.answer = _Proc(json.dumps({"ok": False, "error": "boom", "line": 3,
                                    "stdout": "", "stderr": "Traceback…"}))
    res = _run(client, clone / "udf_1.py")
    assert res.status_code == 200
    assert res.json() == {"ok": False, "error": "boom", "line": 3,
                          "stdout": "", "stderr": "Traceback…"}


def test_node_run_refuses_anything_that_is_not_a_udf_node(client, clone, base, tmp_path, signed_in, shim):
    team = _canvas(base / ENV / "team" / "demo")
    plain = tmp_path / "x.py"
    plain.write_text("x = 1\n", encoding="utf-8")
    for p in (clone / "chart.json", clone / "notes.md", clone / "page.html",
              clone / "_helper.py", clone / "orphan.py", team / "udf_1.py", plain):
        res = _run(client, p)
        assert res.status_code == 400, p
    assert client.post("/api/canvases/node/run", json={"path": "a/b.py"},
                       headers=GUARD).status_code == 400
    assert shim.calls == []


def test_node_run_needs_canvases_on_and_a_sign_in(client, clone, shim):
    assert _run(client, clone / "udf_1.py").status_code == 409      # signed out
    _set_pref(False)
    assert _run(client, clone / "udf_1.py").status_code == 403
    assert shim.calls == []


def test_node_run_reports_a_helper_that_could_not_run(client, clone, signed_in, shim):
    shim.answer = _Proc(stderr="please re-authenticate", returncode=1)
    assert _run(client, clone / "udf_1.py").status_code == 401
    shim.answer = _Proc(stderr="no such host", returncode=1)
    assert _run(client, clone / "udf_1.py").status_code == 502
    shim.answer = _Proc(stdout="not json")
    assert _run(client, clone / "udf_1.py").status_code == 502
    shim.answer = _Proc(stdout=json.dumps({"content_type": "text/html"}))   # no `ok`
    assert _run(client, clone / "udf_1.py").status_code == 502
    shim.answer = canvases_mod.subprocess.TimeoutExpired(cmd="x", timeout=1)
    assert _run(client, clone / "udf_1.py").status_code == 504


def test_node_run_needs_a_cli_this_process_can_drive(client, clone, signed_in, shim, monkeypatch):
    from fused_render.fusedcli import FusedCli

    monkeypatch.setattr(canvases_mod, "fused_cli",
                        lambda: FusedCli(command=["/usr/bin/fused"], external=True))
    assert _run(client, clone / "udf_1.py").status_code == 501
    assert shim.calls == []


# ------------------------------------------- the shim's answer (_fused_node_run)

class _Result:
    """The attributes of the SDK's run result the shim reads."""

    def __init__(self, **kw):
        self.stdout = kw.get("stdout")
        self.stderr = kw.get("stderr")
        self.error_message = kw.get("error_message")
        self.has_exception = kw.get("has_exception", False)
        self.error_lineno = kw.get("error_lineno")
        self.original_content = kw.get("original_content")
        self.original_headers = kw.get("original_headers")
        self.is_cached = kw.get("is_cached", False)
        self.time_taken_seconds = kw.get("time_taken_seconds")


@pytest.fixture(scope="module")
def node_run():
    pytest.importorskip("fused")
    from fused_render import _fused_node_run

    return _fused_node_run


def test_shim_hands_the_result_bytes_on_untouched(node_run):
    out = node_run.result_payload(_Result(
        original_content=b"<p>hi</p>", original_headers={"content-type": "text/html; charset=utf-8"},
        stdout="printed\n", is_cached=True, time_taken_seconds=1.25), 1024)
    assert out == {"ok": True, "content_type": "text/html; charset=utf-8",
                   "body_b64": "PHA+aGk8L3A+", "stdout": "printed\n", "stderr": "",
                   "cached": True, "seconds": 1.25}


def test_shim_reports_a_udf_that_returned_nothing_as_an_empty_body(node_run):
    out = node_run.result_payload(_Result(original_content=None, original_headers=None), 1024)
    assert out["ok"] is True and out["body_b64"] == "" and out["content_type"] == ""
    assert out["seconds"] is None


def test_shim_reports_a_run_that_raised_with_its_line(node_run):
    out = node_run.result_payload(_Result(
        error_message="NameError: x", has_exception=True, error_lineno=7,
        stderr="Traceback…", original_content=b"ignored"), 1024)
    assert out == {"ok": False, "error": "NameError: x", "line": 7,
                   "stdout": "", "stderr": "Traceback…"}
    # An exception with no message still reads as a failure, never as a result.
    out = node_run.result_payload(_Result(has_exception=True, error_lineno=True), 1024)
    assert out["ok"] is False and out["error"] and out["line"] is None


def test_shim_refuses_a_result_past_the_limit(node_run):
    out = node_run.result_payload(_Result(
        original_content=b"x" * 11, original_headers={"content-type": "image/png"}), 10)
    assert out["ok"] is False and "too large" in out["error"]
    assert "body_b64" not in out


def test_shim_asks_for_the_shared_links_formats(node_run):
    assert node_run.RESULT_FORMAT == "png,html"
