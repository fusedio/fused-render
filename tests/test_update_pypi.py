"""CLI/PyPI update notice (fused_render/update/pypi.py). No real network."""
import io
import json

import pytest

from fused_render import update
from fused_render.update import linux, mac, pypi


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(pypi, "_manager", None)
    monkeypatch.setattr(update, "_cli_check", False)
    monkeypatch.setattr(mac, "_manager", None)
    monkeypatch.setattr(linux, "_manager", None)
    monkeypatch.delenv("FUSED_RENDER_UPDATE_DEV_MANAGER", raising=False)
    monkeypatch.setenv("FUSED_RENDER_NO_AUTO_UPDATE", "1")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve(monkeypatch, version):
    calls = []

    def fake(url, timeout):
        calls.append(url)
        return _Resp(json.dumps({"info": {"version": version}}).encode())

    monkeypatch.setattr(pypi.common, "urlopen", fake)
    return calls


def _mgr(monkeypatch, running="0.6.98"):
    m = pypi.PyPIUpdateManager()
    monkeypatch.setattr(m, "_running_version", lambda: running)
    return m


def test_newer_is_available_check_only(monkeypatch):
    calls = _serve(monkeypatch, "0.6.99")
    s = _mgr(monkeypatch).check(force=True)
    assert calls == [pypi.PYPI_URL]
    assert s["state"] == "available" and s["latest_version"] == "0.6.99"
    assert s["check_only"] is True
    assert s["check_error"] is None


@pytest.mark.parametrize("prefix,cmd", [
    ("/home/u/.local/share/uv/tools/fused-render", "uv tool upgrade fused-render"),
    ("/home/u/.local/pipx/venvs/fused-render", "pipx upgrade fused-render"),
    ("C:\\Users\\u\\AppData\\Roaming\\uv\\tools\\fused-render", "uv tool upgrade fused-render"),
    ("/usr/lib/venv", "pip install -U fused-render"),
])
def test_manual_command_per_installer(monkeypatch, prefix, cmd):
    _serve(monkeypatch, "0.6.99")
    monkeypatch.setattr(pypi.sys, "prefix", prefix)
    assert _mgr(monkeypatch).check(force=True)["manual_command"] == cmd


@pytest.mark.parametrize("latest", ["0.6.98", "0.6.97"])
def test_same_or_older_is_idle(monkeypatch, latest):
    _serve(monkeypatch, latest)
    assert _mgr(monkeypatch).check(force=True)["state"] == "idle"


def test_dev_version_is_quietly_idle(monkeypatch):
    _serve(monkeypatch, "0.6.99")
    s = _mgr(monkeypatch, running="0.6.99.dev1+gabc").check(force=True)
    assert s["state"] == "idle" and s["check_error"] is None


def test_stderr_line_printed_once(monkeypatch, capsys):
    _serve(monkeypatch, "0.6.99")
    m = _mgr(monkeypatch)
    m.check(force=True)
    m.check(force=True)
    err = capsys.readouterr().err
    assert err.count("fused-render 0.6.99 is available (you have 0.6.98)") == 1
    assert "run: " in err


def test_fetch_failure_is_a_check_error(monkeypatch):
    def boom(url, timeout):
        raise OSError("offline")

    monkeypatch.setattr(pypi.common, "urlopen", boom)
    s = _mgr(monkeypatch).check(force=True)
    assert s["state"] == "idle" and s["check_error"] == "offline"


def test_start_gated_on_cli_flag(monkeypatch):
    # Unpackaged run: no bundle/AppImage.
    monkeypatch.setattr(mac, "start", lambda: None)
    monkeypatch.setattr(linux, "start", lambda: None)
    assert update.start() is None
    assert update.manager() is None
    update.enable_cli_check()
    m = update.start()
    assert isinstance(m, pypi.PyPIUpdateManager)
    assert update.manager() is m
    assert update.start() is m


def test_packaged_manager_wins(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(mac, "start", lambda: sentinel)
    monkeypatch.setattr(linux, "start", lambda: sentinel)
    update.enable_cli_check()
    assert update.start() is sentinel
    assert pypi.manager() is None
