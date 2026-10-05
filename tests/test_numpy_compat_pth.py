"""Decision logic of the DMG's startup numpy selector (D1321)."""

import importlib.util
import os
import platform
import sys

import pytest

_SRC = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "dmg", "_fused_numpy_compat.py"
)


@pytest.fixture
def compat(monkeypatch, tmp_path):
    monkeypatch.delenv("FUSED_RENDER_NUMPY_COMPAT", raising=False)
    spec = importlib.util.spec_from_file_location("_fused_numpy_compat_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = tmp_path / "compat"
    d.mkdir()
    monkeypatch.setattr(mod, "compat_dir", lambda: str(d))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "platform", "darwin")
    mod.dir = str(d)
    return mod


def _mac(monkeypatch, version):
    monkeypatch.setattr(platform, "mac_ver", lambda: (version, ("", "", ""), "arm64"))


@pytest.mark.parametrize("ver", ["13.0", "13.6.4", "12.7", "11.0"])
def test_old_macos_inserts_compat_at_front(compat, monkeypatch, ver):
    _mac(monkeypatch, ver)
    assert compat.activate() is True
    assert sys.path[0] == compat.dir


@pytest.mark.parametrize("ver", ["14.0", "15.2", "26.1"])
def test_new_macos_keeps_default(compat, monkeypatch, ver):
    _mac(monkeypatch, ver)
    assert compat.activate() is False
    assert compat.dir not in sys.path


@pytest.mark.parametrize("ver", ["", "10.16"])
def test_unknown_version_keeps_default(compat, monkeypatch, ver):
    _mac(monkeypatch, ver)
    assert compat.activate() is False
    assert compat.dir not in sys.path


def test_non_darwin_keeps_default(compat, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    _mac(monkeypatch, "13.0")
    assert compat.activate() is False


def test_override_forces_compat_even_on_new_macos(compat, monkeypatch):
    _mac(monkeypatch, "26.0")
    monkeypatch.setenv("FUSED_RENDER_NUMPY_COMPAT", "1")
    assert compat.activate() is True
    assert sys.path[0] == compat.dir


def test_override_forces_default_even_on_old_macos(compat, monkeypatch):
    _mac(monkeypatch, "13.0")
    monkeypatch.setenv("FUSED_RENDER_NUMPY_COMPAT", "0")
    assert compat.activate() is False
    assert compat.dir not in sys.path


def test_activate_is_idempotent(compat, monkeypatch):
    _mac(monkeypatch, "13.0")
    compat.activate()
    compat.activate()
    assert sys.path.count(compat.dir) == 1


def test_missing_compat_dir_is_not_inserted(compat, monkeypatch, tmp_path):
    _mac(monkeypatch, "13.0")
    missing = str(tmp_path / "nope")
    monkeypatch.setattr(compat, "compat_dir", lambda: missing)
    assert compat.activate() is False
    assert missing not in sys.path


def test_exceptions_are_swallowed(compat, monkeypatch):
    def boom():
        raise RuntimeError("no plist")

    monkeypatch.setattr(platform, "mac_ver", boom)
    assert compat.activate() is False
    _mac(monkeypatch, "garbage")  # int() ValueError
    assert compat.activate() is False


def test_compat_dir_is_relative_to_the_module():
    spec = importlib.util.spec_from_file_location("_c", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    expected = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(_SRC))),
        "compat", "macos13", "site-packages",
    )
    assert mod.compat_dir() == expected


def test_pth_is_a_one_line_import():
    pth = os.path.join(os.path.dirname(_SRC), "fused_numpy_compat.pth")
    with open(pth, encoding="utf-8") as f:
        assert f.read().strip() == "import _fused_numpy_compat"
