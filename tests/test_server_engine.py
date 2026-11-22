"""Tests for _forced_engine (D69/D70 + SPEC §20): FUSED_RENDER_ENGINE forces
the whole process; unset returns None and the engine follows the persisted
preference (shell/prefs.py — covered in test_shell_prefs.py)."""
import pytest

from fused_render.server import common as server


def test_unset_returns_none(monkeypatch):
    # No override -> the pref decides (since D204 that default is fused,
    # in shell/prefs.py; this function's job is only to stay out of the way).
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
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
    assert server._forced_engine() == "builtin"


def test_explicit_fused_probes_the_app_interpreter(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "fused")
    probed = []
    monkeypatch.setattr("fused_render.engine.app_interpreter", lambda: probed.append(1))
    assert server._forced_engine() == "fused"
    assert probed == [1]


def test_invalid_override_raises(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "nonsense")
    with pytest.raises(RuntimeError, match="not one of"):
        server._forced_engine()


def test_auto_is_no_longer_a_value(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "auto")
    with pytest.raises(RuntimeError, match="not one of"):
        server._forced_engine()
