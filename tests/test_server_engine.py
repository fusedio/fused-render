"""Tests for _select_engine (D69/D70): builtin is the default; fused is opt-in."""
import pytest

from fused_render import server


def test_default_is_builtin_even_when_fused_available(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    monkeypatch.setattr("fused_render.engine.available", lambda: True, raising=False)
    assert server._select_engine() == "builtin"


def test_explicit_builtin(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "builtin")
    monkeypatch.setattr("fused_render.engine.available", lambda: True, raising=False)
    assert server._select_engine() == "builtin"


def test_auto_uses_fused_when_available(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "auto")
    monkeypatch.setattr("fused_render.engine.available", lambda: True, raising=False)
    assert server._select_engine() == "fused"


def test_auto_falls_back_to_builtin_when_unavailable(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "auto")
    monkeypatch.setattr("fused_render.engine.available", lambda: False, raising=False)
    assert server._select_engine() == "builtin"


def test_fused_forced_but_unavailable_raises(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "fused")
    monkeypatch.setattr("fused_render.engine.available", lambda: False, raising=False)
    with pytest.raises(RuntimeError, match="not importable"):
        server._select_engine()


def test_invalid_override_raises(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "nonsense")
    with pytest.raises(RuntimeError, match="not one of"):
        server._select_engine()
