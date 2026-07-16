import os
import runpy
from pathlib import Path

import pytest

from fused_render import paths


def _clear(monkeypatch):
    for name in (
        "FUSED_RENDER_BRANCH",
        "FUSED_RENDER_HOME",
        "FUSED_RENDER_CACHE_DIR",
        "FUSED_RENDER_RUNTIME_DIR",
        "FUSED_RENDER_TEMP_DIR",
        "FUSED_RENDER_DESKTOP_INSTANCE_ID",
        "FUSED_RENDER_DESKTOP_INSTANCE_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_unset_paths_keep_wheel_defaults(monkeypatch):
    _clear(monkeypatch)
    assert paths.state_dir() == os.path.expanduser("~/.fused-render")
    assert paths.cache_dir() == os.path.expanduser("~/.fused-render/cache")
    assert paths.daemon_cache_dir("gridv2") == os.path.expanduser("~/.cache/fused-render-gridv2")
    assert paths.binary_dir() == os.path.expanduser("~/.fused-render/bin")
    assert paths.runtime_dir("~/legacy") == os.path.expanduser("~/legacy")


def test_desktop_overrides_are_separate(monkeypatch, tmp_path):
    state = tmp_path / "state"
    cache = tmp_path / "cache"
    runtime = tmp_path / "runtime"
    temp = tmp_path / "temp"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(state))
    monkeypatch.setenv("FUSED_RENDER_CACHE_DIR", str(cache))
    monkeypatch.setenv("FUSED_RENDER_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("FUSED_RENDER_TEMP_DIR", str(temp))

    assert paths.state_dir() == str(state)
    assert paths.cache_path("excel") == str(cache / "excel")
    assert paths.daemon_cache_dir("gridv2") == str(cache / "daemons" / "gridv2")
    assert paths.binary_dir() == str(state / "bin")
    assert paths.runtime_dir("unused") == str(runtime)
    assert paths.temp_dir() == str(temp)


def test_desktop_instance_requires_complete_pair(monkeypatch):
    _clear(monkeypatch)
    assert paths.desktop_instance() is None
    monkeypatch.setenv("FUSED_RENDER_DESKTOP_INSTANCE_ID", "desktop")
    with pytest.raises(RuntimeError, match="must be set together"):
        paths.desktop_instance()
    monkeypatch.setenv("FUSED_RENDER_DESKTOP_INSTANCE_TOKEN", "token")
    assert paths.desktop_instance() == ("desktop", "token")


def test_server_config_reports_desktop_identity(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import fused_render.server as server

    shell = os.path.join(server.STATIC_DIR, "shell-dist", "index.html")
    exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda path: path == shell or exists(path))
    monkeypatch.setenv("FUSED_RENDER_DESKTOP_INSTANCE_ID", "desktop-v1")
    monkeypatch.setenv("FUSED_RENDER_DESKTOP_INSTANCE_TOKEN", "launch-token")
    app = server.create_app(start_dir=str(tmp_path))
    uvicorn_server = type("Server", (), {"should_exit": False})()
    app.state.uvicorn_server = uvicorn_server
    client = TestClient(app)

    assert client.get("/api/config").json()["desktop_instance"] == {"id": "desktop-v1"}
    assert client.get(
        "/api/config", headers={"X-Fused-Desktop-Token": "launch-token"}
    ).json()["desktop_instance"] == {
        "id": "desktop-v1",
        "token": "launch-token",
    }
    assert client.post("/api/desktop/shutdown").status_code == 403
    response = client.post(
        "/api/desktop/shutdown",
        headers={"X-Fused-Desktop-Token": "launch-token"},
    )
    assert response.json() == {"ok": True}
    assert uvicorn_server.should_exit is True


def test_export_examples_use_desktop_roots(monkeypatch, tmp_path):
    package = Path(__file__).parents[1] / "fused_render" / "templates"
    state = tmp_path / "state"
    cache = tmp_path / "cache"
    duckdb_extensions = cache / "duckdb" / "extensions"
    duckdb_temp = cache / "duckdb" / "temp"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(state))
    monkeypatch.setenv("FUSED_RENDER_CACHE_DIR", str(cache))
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_DIR", str(state / "claude"))
    monkeypatch.setenv("FUSED_RENDER_DUCKDB_EXTENSION_DIR", str(duckdb_extensions))
    monkeypatch.setenv("FUSED_RENDER_DUCKDB_TEMP_DIR", str(duckdb_temp))

    excel = runpy.run_path(str(package / "excel" / "reader.py"))
    pdf = runpy.run_path(str(package / "pdf_studio" / "pdf.py"))
    latex = runpy.run_path(str(package / "latex" / "engine.py"))
    claude = runpy.run_path(str(package / "claude" / "agent.py"))

    assert excel["CACHE_ROOT"] == str(cache / "excel")
    assert pdf["DATA_ROOT"] == str(state / "data" / "pdf_studio")
    assert pdf["CACHE_ROOT"] == str(cache / "pdf_studio")
    assert latex["CACHE_ROOT"] == str(cache / "latex")
    assert latex["BIN_DIR"] == str(state / "bin")
    assert claude["PROJECTS"] == str(state / "claude" / "projects")
    connection = excel["_duck"]()
    assert connection.execute("SELECT current_setting('extension_directory')").fetchone()[0] == str(
        duckdb_extensions
    )
    assert connection.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(
        duckdb_temp
    )
