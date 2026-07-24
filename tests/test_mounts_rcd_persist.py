"""rcd persistence gating: `_ensure_rcd_locked` detaches the rclone rcd daemon
(start_new_session=True, so it outlives the server) ONLY when
FUSED_RENDER_RCLONE_PERSIST is truthy — the dev-iteration convenience set by
scripts/dev.sh. In production (flag unset/empty/"0") the daemon is a normal
child so app teardown reaps it. No real rclone is ever exec'd: rclone_bin, the
core/pid probe, log rotation and state-write are all monkeypatched."""
import pytest

import fused_render.shell.mounts as mounts_mod


@pytest.fixture
def captured_popen(monkeypatch):
    """Force the spawn path and capture the Popen kwargs without execing rclone."""
    calls = {}

    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: None)
    monkeypatch.setattr(mounts_mod, "reap_stale_rcd", lambda: None)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/fake/rclone")
    monkeypatch.setattr(mounts_mod, "_rotate_rcd_log", lambda: "/fake/rcd.log")
    monkeypatch.setattr(mounts_mod, "write_rcd_state", lambda *a, **k: None)
    monkeypatch.setattr(mounts_mod, "_rc", lambda *a, **k: {"pid": 4321})

    def fake_popen(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(mounts_mod.subprocess, "Popen", fake_popen)
    return calls


def test_persist_unset_does_not_detach(captured_popen, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_RCLONE_PERSIST", raising=False)
    mounts_mod._ensure_rcd_locked()
    assert captured_popen["kwargs"]["start_new_session"] is False


def test_persist_zero_does_not_detach(captured_popen, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_RCLONE_PERSIST", "0")
    mounts_mod._ensure_rcd_locked()
    assert captured_popen["kwargs"]["start_new_session"] is False


def test_persist_empty_does_not_detach(captured_popen, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_RCLONE_PERSIST", "")
    mounts_mod._ensure_rcd_locked()
    assert captured_popen["kwargs"]["start_new_session"] is False


def test_persist_one_detaches(captured_popen, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_RCLONE_PERSIST", "1")
    mounts_mod._ensure_rcd_locked()
    assert captured_popen["kwargs"]["start_new_session"] is True


def test_persist_arbitrary_truthy_detaches(captured_popen, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_RCLONE_PERSIST", "yes")
    mounts_mod._ensure_rcd_locked()
    assert captured_popen["kwargs"]["start_new_session"] is True


def test_should_persist_helper():
    import os

    saved = os.environ.pop("FUSED_RENDER_RCLONE_PERSIST", None)
    try:
        assert mounts_mod._rclone_should_persist() is False
        os.environ["FUSED_RENDER_RCLONE_PERSIST"] = "0"
        assert mounts_mod._rclone_should_persist() is False
        os.environ["FUSED_RENDER_RCLONE_PERSIST"] = ""
        assert mounts_mod._rclone_should_persist() is False
        os.environ["FUSED_RENDER_RCLONE_PERSIST"] = "1"
        assert mounts_mod._rclone_should_persist() is True
    finally:
        os.environ.pop("FUSED_RENDER_RCLONE_PERSIST", None)
        if saved is not None:
            os.environ["FUSED_RENDER_RCLONE_PERSIST"] = saved
