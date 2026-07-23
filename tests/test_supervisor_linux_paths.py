"""Linux XDG path layout, autostart round-trip, and UI backend resolution.

These are pure logic (path computation, file round-trips, first-available-tool
selection) with no Linux kernel dependency, so they run on macOS too. The
dialogs themselves are gate-tested manually (docs/LINUX_DESKTOP_SPEC.md).
"""
import os
from pathlib import Path

import pytest

from fused_render.supervisor import paths as paths_mod
from fused_render.supervisor._linux import startup, tree, ui

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


# -- PDEATHSIG orphan-race check (cross-platform pure logic) ----------------

def test_parent_changed_detects_reparenting_not_only_pid1():
    # The race check must fire whenever the parent changed — under systemd an
    # orphan reparents to a session subreaper, NOT pid 1, so a `== 1` test would
    # miss the death. Guard against regressing to that.
    assert tree._parent_changed(expected_ppid=4321, current_ppid=1) is True
    assert tree._parent_changed(expected_ppid=4321, current_ppid=9999) is True  # subreaper
    assert tree._parent_changed(expected_ppid=4321, current_ppid=4321) is False


# -- shared child env-block contract ---------------------------------------

def test_stripped_env_vars_are_single_sourced():
    # The tuple is defined once in paths; the Linux tree keeper imports it (via
    # environment_block) rather than duplicating it — pin both the contents and
    # the no-duplication so a drift between backends can't creep back in.
    assert set(paths_mod.STRIPPED_ENV_VARS) == {
        "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONINSPECT",
    }
    assert tree.environment_block is paths_mod.environment_block


def test_environment_block_strips_identity_vars_and_applies_overrides(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/evil")
    monkeypatch.setenv("PYTHONHOME", "/evil")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = paths_mod.environment_block({"OVERRIDE": "1"})
    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env
    assert env["KEEP_ME"] == "yes"
    assert env["OVERRIDE"] == "1"


# -- XDG path layout -------------------------------------------------------

def _clear_xdg(monkeypatch):
    for var in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)


def test_xdg_paths_when_all_set(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    p = paths_mod.DesktopPaths.discover_linux()
    assert p.state == tmp_path / "data" / "fused-render" / "desktop" / "state"
    assert p.cache == tmp_path / "cache" / "fused-render" / "desktop"
    assert p.runtime == tmp_path / "run" / "fused-render"
    assert p.logs == tmp_path / "data" / "fused-render" / "desktop" / "logs"
    assert p.temp == p.cache / "temp"


def test_xdg_fallbacks_when_unset(monkeypatch, tmp_path):
    _clear_xdg(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    p = paths_mod.DesktopPaths.discover_linux()
    assert p.state == tmp_path / ".local" / "share" / "fused-render" / "desktop" / "state"
    assert p.cache == tmp_path / ".cache" / "fused-render" / "desktop"
    # Runtime with XDG_RUNTIME_DIR unset falls back under the cache dir, 0700.
    assert p.runtime == tmp_path / ".cache" / "fused-render" / "desktop" / "runtime"


def test_relative_xdg_value_is_ignored(monkeypatch, tmp_path):
    _clear_xdg(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", "relative/not/absolute")
    p = paths_mod.DesktopPaths.discover_linux()
    assert p.state == tmp_path / ".local" / "share" / "fused-render" / "desktop" / "state"


def test_child_environment_keys_are_contract_identical(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    p = paths_mod.DesktopPaths.discover_linux()
    env = p.child_environment("inst-id", "tok", tmp_path / "tools")
    for key in (
        "FUSED_RENDER_HOME", "FUSED_RENDER_CACHE_DIR", "FUSED_RENDER_RUNTIME_DIR",
        "FUSED_RENDER_TEMP_DIR", "FUSED_RENDER_LOG_DIR", "FUSED_RENDER_BRANCH",
        "FUSED_RENDER_DESKTOP_INSTANCE_ID", "FUSED_RENDER_DESKTOP_INSTANCE_TOKEN",
        "OPENFUSED_ENVS_FILE", "RCLONE_CONFIG", "UV_CACHE_DIR",
    ):
        assert key in env, key
    assert env["FUSED_RENDER_DESKTOP_INSTANCE_ID"] == "inst-id"


def test_payload_tools_share_one_dir(monkeypatch, tmp_path):
    # The DuckDB extension dir, the rclone binary, and the PATH prefix must all
    # live under the SAME tools_dir — the build scripts stage them together, so
    # a split would point the child at a path the payload never populated.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    tools = tmp_path / "tools"
    env = paths_mod.DesktopPaths.discover_linux().child_environment("i", "t", tools)
    assert env["FUSED_RENDER_DUCKDB_EXTENSION_DIR"] == str(tools / "duckdb_extensions")
    assert env["FUSED_RENDER_RCLONE_BIN"] == str(tools / "rclone")
    assert env["PATH"].split(os.pathsep)[0] == str(tools)


def test_desktop_template_uses_url_field_code():
    # %u (URLs + files), not %f (files only): the DE hands `fused-render://`
    # deep links to Exec via the field code, so scheme handling needs %u. File
    # managers still pass plain paths through it. The MimeType placeholder that
    # the build substitutes must remain.
    desktop = (_SCRIPTS / "linux" / "fused-render.desktop.in").read_text()
    assert "Exec=AppRun %u" in desktop
    assert "%f" not in desktop
    assert "MimeType=@MIMETYPE@" in desktop


def test_linux_build_stages_tools_in_python_bin():
    # On Linux tools_dir resolves to $PYTHON_ROOT/bin (python-build-standalone
    # puts python3 there), so the build script MUST stage the DuckDB extensions,
    # uv, and rclone under that same bin/ — matching child_environment above.
    # This is the single-source-of-truth guard for the payload layout.
    script = (_SCRIPTS / "build_linux_appimage.sh").read_text()
    assert 'DUCKDB_EXTENSIONS="$PYTHON_ROOT/bin/duckdb_extensions"' in script
    assert '"$PYTHON_ROOT/bin/uv"' in script
    assert '"$PYTHON_ROOT/bin/rclone"' in script


# -- autostart round-trip --------------------------------------------------

def test_autostart_write_remove_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "FusedRender.AppImage"))
    (tmp_path / "FusedRender.AppImage").write_text("#!/bin/sh\n")

    assert startup.enabled() is False
    startup.set_enabled(True)
    assert startup.enabled() is True
    desktop = tmp_path / ".config" / "autostart" / "fused-render.desktop"
    assert desktop.exists()
    body = desktop.read_text()
    assert "FusedRender.AppImage" in body
    assert "--startup" in body

    startup.set_enabled(False)
    assert startup.enabled() is False
    assert not desktop.exists()


def test_autostart_fails_loudly_on_unresolvable_launcher(monkeypatch, tmp_path):
    # $APPIMAGE points at a nonexistent file and argv[0] is not runnable:
    # fail loudly (OSError) so the tray toggle reverts the checkbox rather than
    # writing a broken Exec line (mirrors _win32/startup.py's caution).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "does-not-exist.AppImage"))
    with pytest.raises(OSError):
        startup.set_enabled(True)


def test_autostart_rejects_python_m_checkout_launch(monkeypatch, tmp_path):
    # Bugbot: with no $APPIMAGE, a `python -m fused_render.supervisor` launch
    # has argv[0] = the package's __main__.py — an existing file, but one the
    # desktop session cannot exec. That must raise, not persist a broken Exec.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("APPIMAGE", raising=False)
    fake_main = tmp_path / "__main__.py"
    fake_main.write_text("")
    monkeypatch.setattr(startup.sys, "argv", [str(fake_main)])
    with pytest.raises(OSError):
        startup.set_enabled(True)
    assert not (tmp_path / ".config" / "autostart" / "fused-render.desktop").exists()


# -- UI backend resolution (subprocess-stubbed) ----------------------------

def test_ui_prefers_zenity(monkeypatch):
    monkeypatch.setattr(ui.shutil, "which", lambda tool: "/usr/bin/" + tool)
    assert ui._dialog_tool() == "zenity"


def test_ui_falls_back_to_kdialog(monkeypatch):
    monkeypatch.setattr(
        ui.shutil, "which", lambda tool: "/usr/bin/kdialog" if tool == "kdialog" else None
    )
    assert ui._dialog_tool() == "kdialog"


def test_ui_falls_back_to_tkinter(monkeypatch):
    monkeypatch.setattr(ui.shutil, "which", lambda tool: None)
    assert ui._dialog_tool() == "tkinter"


class _FakePopen:
    def __init__(self, returncode=0, hang=False):
        self._returncode = returncode
        self._hang = hang

    def wait(self, timeout=None):
        if self._hang:
            raise ui.subprocess.TimeoutExpired(cmd="xdg-open", timeout=timeout)
        return self._returncode


def test_xdg_open_raises_on_nonzero_exit(monkeypatch):
    # A failed xdg-open must raise (so core's _safe_open answers status 1 and
    # shows the rejection dialog) instead of silently reporting success.
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *a, **k: _FakePopen(returncode=3))
    with pytest.raises(OSError):
        ui.open_path(Path("/does/not/matter"))


def test_xdg_open_success_on_zero_exit(monkeypatch):
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *a, **k: _FakePopen(returncode=0))
    ui.open_uri("https://example.invalid")  # returns without raising


def test_xdg_open_treats_timeout_as_success(monkeypatch):
    # A foreground handler that never exits within the wait window is the open
    # succeeding, not failing — a timeout must NOT raise.
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *a, **k: _FakePopen(hang=True))
    ui.open_url("https://example.invalid")  # returns without raising


def test_open_default_apps_raises_on_linux(monkeypatch):
    # No cross-desktop Linux equivalent of ms-settings:defaultapps: the backend
    # method must raise so core's _safe_call logs an honest no-op, instead of
    # core hardcoding the Windows-only URI (a silent dead menu item).
    called = []
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *a, **k: called.append(a))
    with pytest.raises(OSError):
        ui.open_default_apps()
    assert called == []  # never shelled out to xdg-open with a Windows URI


def test_alert_is_a_no_op_when_tk_cannot_start(monkeypatch):
    # On a display-less session Tk() raises TclError; alert() is the unguarded
    # fatal-error reporter in __main__.py and must degrade to a no-op, not crash.
    monkeypatch.setattr(ui.shutil, "which", lambda tool: None)  # force the tkinter path

    import tkinter

    def boom():
        raise tkinter.TclError("no display name and no $DISPLAY environment variable")

    monkeypatch.setattr(tkinter, "Tk", boom)
    assert ui.alert("something went wrong") is None
