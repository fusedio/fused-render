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


# ---- refresh_autostart: self-heal the autostart Exec= after an AppImage move --
# integrate() self-heals the "Open with"/deep-link .desktop on every packaged
# start, but the parallel autostart entry (Exec=<appimage> --startup) had no such
# healing, so after a move login-autostart pointed at a dead path forever.


def _enable_autostart(monkeypatch, tmp_path, appimage):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPIMAGE", str(appimage))
    startup.set_enabled(True)
    return tmp_path / "config" / "autostart" / "fused-render.desktop"


def test_refresh_autostart_rewrites_stale_exec_path(monkeypatch, tmp_path):
    old = tmp_path / "FusedRender.AppImage"
    old.write_text("#!/bin/sh\n")
    desktop = _enable_autostart(monkeypatch, tmp_path, old)
    assert f"Exec={old} --startup" in desktop.read_text()

    # The AppImage moved: $APPIMAGE now points at the new location.
    moved = tmp_path / "moved" / "FusedRender.AppImage"
    moved.parent.mkdir()
    moved.write_text("#!/bin/sh\n")
    monkeypatch.setenv("APPIMAGE", str(moved))

    startup.refresh_autostart()

    assert f"Exec={moved} --startup" in desktop.read_text()


def test_refresh_autostart_is_noop_when_entry_already_matches(monkeypatch, tmp_path):
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_text("#!/bin/sh\n")
    desktop = _enable_autostart(monkeypatch, tmp_path, appimage)
    before = desktop.read_text()
    mtime_before = desktop.stat().st_mtime_ns

    # No write must happen when the on-disk entry already matches: guard against
    # a needless rewrite (and mtime churn) on the common no-move start.
    written = []
    monkeypatch.setattr(
        startup.Path, "write_text",
        lambda self, *a, **kw: written.append(self),
    )
    startup.refresh_autostart()

    assert written == []
    assert desktop.read_text() == before
    assert desktop.stat().st_mtime_ns == mtime_before


def test_refresh_autostart_is_noop_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_text("#!/bin/sh\n")
    monkeypatch.setenv("APPIMAGE", str(appimage))
    # Autostart never enabled: the entry file is absent and must stay absent.
    assert not startup.enabled()

    startup.refresh_autostart()

    assert not (tmp_path / "config" / "autostart" / "fused-render.desktop").exists()


def test_refresh_autostart_is_noop_when_launcher_unresolvable(monkeypatch, tmp_path):
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_text("#!/bin/sh\n")
    desktop = _enable_autostart(monkeypatch, tmp_path, appimage)
    before = desktop.read_text()

    # $APPIMAGE now points at a missing file: _launcher_path() raises, and
    # refresh must leave the (stale) entry as-is rather than write a broken one
    # or raise — matching the module's never-write-a-broken-entry discipline.
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "gone.AppImage"))

    startup.refresh_autostart()  # must not raise

    assert desktop.read_text() == before
