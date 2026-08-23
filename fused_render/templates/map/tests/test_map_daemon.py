"""Tests for map_render.py's contract with the server-owned tile engine.

Hermetic: no fused-render server and no real daemon are spawned — the server
POSTs are monkeypatched. Loaded via importlib with templates/shared on
sys.path, like the latex daemon tests.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_map_daemon.py -o addopts=""
"""
import ast
import importlib.util
import os
import pathlib
import sys

import pytest

_MAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(os.path.dirname(_MAP), "shared")


def _load(name, filename):
    for path in (_SHARED, _MAP):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location(name, os.path.join(_MAP, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mr(tmp_path, monkeypatch):
    m = _load("map_render", "map_render.py")
    monkeypatch.setattr(m, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(m, "ARTIFACT_DIR", tmp_path / "artifacts")
    return m


def _local_imports(module_path):
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    siblings = {path.stem for path in module_path.parent.glob("*.py")}
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names & siblings


def test_the_version_hash_covers_every_module_the_daemon_imports(mr):
    # VERSION is a hash of BACKEND_FILES, and a stale daemon is only replaced
    # when VERSION changes. A module the daemon imports but that is missing here
    # can be edited with no effect on a running daemon, which then keeps serving
    # the old code — blob_tokens.py and optional_runtime.py were both missing.
    root = pathlib.Path(mr.DAEMON).parent
    reached, pending = set(), [pathlib.Path(mr.DAEMON).stem, pathlib.Path(mr.WORKER).stem]
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        pending.extend(_local_imports(root / f"{name}.py") - reached)

    listed = {pathlib.Path(path).stem for path in mr.BACKEND_FILES}
    assert reached <= listed, (
        "these modules reach the daemon but are missing from BACKEND_FILES, so "
        f"editing them leaves VERSION unchanged: {sorted(reached - listed)}"
    )


def test_ensure_hands_the_server_this_interpreter_and_daemon(mr, monkeypatch):
    # The server's own python has no geo stack (D276); the project venv running
    # this module is the one interpreter that does, so ensure must hand it over.
    posts = []
    monkeypatch.setattr(
        mr, "_server_post",
        lambda path, payload, timeout: posts.append((path, payload)) or {"ok": True},
    )
    mr._ensure_service()
    path, payload = posts[0]
    assert path == "/api/engines/map/ensure"
    assert payload["python"] == sys.executable
    assert payload["daemon"] == str(mr.DAEMON)
    assert payload["version"] == mr.VERSION


def test_describe_goes_through_the_server_origin(mr, monkeypatch, tmp_path):
    target = tmp_path / "features.geojson"
    target.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    posts = []

    def server_post(path, payload, timeout, headers=None):
        posts.append(path)
        if path.endswith("/ensure"):
            return {"ok": True}
        return {"status": "ok", "kind": "vector_tiles_mvt",
                "bounds": [0, 0, 1, 1], "data": {}, "warnings": []}

    monkeypatch.setattr(mr, "_server_post", server_post)
    descriptor = mr.main(target=str(target))
    assert descriptor["status"] == "ok"
    assert posts == ["/api/engines/map/ensure", "/api/engines/map/proxy/describe"]


def test_without_a_server_origin_the_ensure_fails_loudly(mr, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    with pytest.raises(RuntimeError, match="FUSED_RENDER_ORIGIN"):
        mr._ensure_service()
