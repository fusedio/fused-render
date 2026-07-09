"""Tests for _forced_engine (D69/D70 + SPEC §20): FUSED_RENDER_ENGINE forces
the whole process; unset returns None and the engine follows the persisted
preference (shell/prefs.py — covered in test_shell_prefs.py)."""
import pytest

from fused_render import server


def test_unset_returns_none_even_when_fused_available(monkeypatch):
    # No override -> the pref decides (D70's builtin default lives there now).
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    monkeypatch.setattr("fused_render.engine.available", lambda: True, raising=False)
    assert server._forced_engine() is None


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
