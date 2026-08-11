"""Tests for _forced_engine (D69/D70 + SPEC §20): FUSED_RENDER_ENGINE forces
the whole process; unset returns None and the engine follows the persisted
preference (shell/prefs.py — covered in test_shell_prefs.py)."""
import pytest

from fused_render.server import common as server


def test_unset_returns_none_even_when_fused_available(monkeypatch):
    # No override -> the pref decides (since D204 that default is fused-when-available,
    # in shell/prefs.py; this function's job is only to stay out of the way).
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    monkeypatch.setattr("fused_render.engine.available", lambda: True, raising=False)
    assert server._forced_engine() is None


def test_the_unset_path_does_not_log_a_builtin_default(monkeypatch, caplog):
    """The startup line is the only place most people ever read this contract.

    It said "default builtin" for as long as that was true and kept saying it after
    D204 flipped the default — so the log told every fresh install the opposite of
    what was about to run its code. Pinned as a test because a stale sentence in a
    log is invisible to every other kind of check.
    """
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    with caplog.at_level("INFO", logger="fused_render.server.common"):
        assert server._forced_engine() is None
    assert "default builtin" not in caplog.text
    assert "fused" in caplog.text


def test_explicit_builtin(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "builtin")
    monkeypatch.setattr("fused_render.engine.available", lambda: True, raising=False)
    assert server._forced_engine() == "builtin"


def test_auto_uses_fused_when_available(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "auto")
    monkeypatch.setattr("fused_render.engine.available", lambda: True, raising=False)
    assert server._forced_engine() == "fused"


def test_auto_falls_back_to_builtin_when_unavailable(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "auto")
    monkeypatch.setattr("fused_render.engine.available", lambda: False, raising=False)
    assert server._forced_engine() == "builtin"


def test_fused_forced_but_unavailable_raises(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "fused")
    monkeypatch.setattr("fused_render.engine.available", lambda: False, raising=False)
    with pytest.raises(RuntimeError, match="not importable"):
        server._forced_engine()


def test_invalid_override_raises(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "nonsense")
    with pytest.raises(RuntimeError, match="not one of"):
        server._forced_engine()


def test_api_config_never_cold_imports_the_engine(tmp_path, monkeypatch):
    # /api/config must resolve the engine without the cold import on the request thread.
    from fastapi.testclient import TestClient

    import fused_render.engine as engine
    from fused_render.server import create_app

    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    monkeypatch.setattr("fused_render.shell.prefs.selected_engine", lambda: "fused")

    def _boom():
        raise AssertionError("the cold engine import must not run on /api/config")

    monkeypatch.setattr(engine, "available", _boom)  # a cold import would 500 the request
    monkeypatch.setattr(engine, "_available_cached", True)  # warm-up already landed

    client = TestClient(create_app(start_dir=str(tmp_path)))
    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json()["engine"] == "fused"
