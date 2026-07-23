"""Start-at-login autostart entry (Linux). The Exec= line must follow the
freedesktop Desktop Entry Spec's own quoting (double quotes), not shell/shlex
single-quote quoting — same bug/fix as integration._desktop_entry, sharing the
one `_exec_quote` helper."""
import pytest

from fused_render.supervisor._linux import startup


def test_exec_quote_plain_path_unquoted():
    assert startup._exec_quote("/opt/FusedRender.AppImage") == "/opt/FusedRender.AppImage"


def test_exec_quote_space_double_quoted():
    assert startup._exec_quote("/opt/Fused Render.AppImage") == '"/opt/Fused Render.AppImage"'


def test_exec_quote_double_quote_escaped():
    assert startup._exec_quote('/a/b"c') == '"/a/b\\\\"c"'


def test_exec_quote_dollar_escaped():
    assert startup._exec_quote("/a/b$c") == '"/a/b\\\\$c"'


def test_exec_quote_backslash_becomes_four():
    assert startup._exec_quote("/a/b\\c") == '"/a/b\\\\\\\\c"'


def _autostart_exec(monkeypatch, tmp_path, appimage):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPIMAGE", str(appimage))
    startup.set_enabled(True)
    text = (tmp_path / "config" / "autostart" / "fused-render.desktop").read_text()
    (line,) = [ln for ln in text.splitlines() if ln.startswith("Exec=")]
    return line


def test_autostart_entry_double_quotes_spaced_launcher(monkeypatch, tmp_path):
    appimage = tmp_path / "Fused Render.AppImage"
    appimage.write_text("#!/bin/sh\n")
    assert _autostart_exec(monkeypatch, tmp_path, appimage) == (
        f'Exec="{appimage}" --startup'
    )


def test_autostart_entry_plain_launcher_unquoted(monkeypatch, tmp_path):
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_text("#!/bin/sh\n")
    assert _autostart_exec(monkeypatch, tmp_path, appimage) == f"Exec={appimage} --startup"
