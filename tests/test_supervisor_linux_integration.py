"""User-level desktop self-integration (SPEC gate (d), Task A). Headless: XDG
dirs are redirected to tmp_path, $APPIMAGE is a fake file, and the update-*
tools are subprocess-stubbed — no desktop session required (the pattern the
other Linux supervisor tests use)."""
import json
from pathlib import Path

import pytest

from fused_render.supervisor import paths as paths_mod
from fused_render.supervisor._linux import integration


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Redirect XDG dirs into tmp_path and return a helper bundle."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.delenv("APPIMAGE", raising=False)

    paths = paths_mod.DesktopPaths.discover_linux()
    paths.create()

    tool_calls: list[list[str]] = []
    monkeypatch.setattr(
        integration.subprocess, "run",
        lambda argv, **kw: tool_calls.append(argv) or _completed(),
    )
    monkeypatch.setattr(integration.shutil, "which", lambda tool: "/usr/bin/" + tool)

    data_home = tmp_path / "data"
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_text("#!/bin/sh\n")
    icon_src = tmp_path / "icon-src.png"
    icon_src.write_bytes(b"\x89PNG\r\n")

    class Bundle:
        pass

    b = Bundle()
    b.paths = paths
    b.data_home = data_home
    b.appimage = appimage
    b.icon_src = icon_src
    b.tool_calls = tool_calls
    b.desktop_file = data_home / "applications" / "fused-render.desktop"
    b.mime_file = data_home / "mime" / "packages" / "fused-render.xml"
    b.icon_file = data_home / "icons" / "hicolor" / "256x256" / "apps" / "fused-render.png"
    b.stamp_file = paths.state / "desktop-integration.json"
    return b


class _completed:
    returncode = 0


def test_no_appimage_is_a_silent_no_op(env, monkeypatch):
    # No $APPIMAGE and no injected appimage: a dev run must write nothing.
    monkeypatch.delenv("APPIMAGE", raising=False)
    integration.integrate(env.paths)
    assert not env.desktop_file.exists()
    assert not env.mime_file.exists()
    assert env.tool_calls == []


def test_first_install_writes_files_and_pokes_databases(env):
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)

    # .desktop entry: absolute quoted AppImage Exec with the URL field code.
    desktop = env.desktop_file.read_text()
    assert f"Exec={env.appimage} %u" in desktop
    assert "MimeType=" in desktop and desktop.rstrip().endswith("x-scheme-handler/fused-render;")
    assert f"Icon={env.icon_file}" in desktop

    # Custom-types MIME package, and the icon copied in.
    assert env.mime_file.read_text().startswith("<?xml")
    assert env.icon_file.read_bytes() == env.icon_src.read_bytes()

    # Databases refreshed; the scheme (only) is defaulted to us.
    tools = [c[0] for c in env.tool_calls]
    assert tools == ["update-mime-database", "update-desktop-database", "xdg-mime"]
    xdg = next(c for c in env.tool_calls if c[0] == "xdg-mime")
    assert xdg == ["xdg-mime", "default", "fused-render.desktop", "x-scheme-handler/fused-render"]

    assert env.stamp_file.exists()


def test_no_file_type_default_is_ever_set(env):
    # macOS "Alternate rank" parity: never steal the user's file defaults — the
    # ONLY xdg-mime default is the deep-link scheme handler.
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    defaults = [c for c in env.tool_calls if c[0] == "xdg-mime" and "default" in c]
    assert len(defaults) == 1
    assert defaults[0][-1] == "x-scheme-handler/fused-render"


def test_second_run_is_idempotent(env):
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    env.tool_calls.clear()
    # Files intact and stamp unchanged: a second run must skip ALL work.
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    assert env.tool_calls == []


def test_reintegrates_when_desktop_file_deleted(env):
    # A matching stamp is not proof the files are on disk. If the user (or
    # another integrator) removed the installed .desktop, a later start must
    # rewrite it rather than no-op forever on the stale stamp.
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    env.tool_calls.clear()
    env.desktop_file.unlink()
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    assert env.desktop_file.exists()  # restored
    assert env.tool_calls != []  # databases re-poked


def test_reintegrates_when_version_changes(env, monkeypatch):
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    env.tool_calls.clear()
    monkeypatch.setattr(integration, "__version__", "999.999.999")
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    assert env.tool_calls != []  # re-ran
    assert json.loads(env.stamp_file.read_text())["version"] == "999.999.999"


def test_reintegrates_when_appimage_moves(env, tmp_path):
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    env.tool_calls.clear()
    moved = tmp_path / "moved" / "FusedRender.AppImage"
    moved.parent.mkdir()
    moved.write_text("#!/bin/sh\n")
    integration.integrate(env.paths, appimage=moved, icon_source=env.icon_src)
    assert env.tool_calls != []
    assert f"Exec={moved} %u" in env.desktop_file.read_text()
    assert json.loads(env.stamp_file.read_text())["appimage"] == str(moved)


def test_missing_update_tools_do_not_break_install(env, monkeypatch):
    # A minimal desktop with no update-mime-database etc.: files must still be
    # written and the run must succeed (log-and-continue).
    monkeypatch.setattr(integration.shutil, "which", lambda tool: None)
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    assert env.desktop_file.exists()
    assert env.mime_file.exists()
    assert env.stamp_file.exists()
    assert env.tool_calls == []  # nothing was actually exec'd


def test_appimage_env_resolution(env, monkeypatch):
    # With no injected appimage, it resolves from $APPIMAGE via startup helper.
    monkeypatch.setenv("APPIMAGE", str(env.appimage))
    integration.integrate(env.paths, icon_source=env.icon_src)
    assert env.desktop_file.exists()


def test_integrate_refreshes_autostart_best_effort(env, monkeypatch):
    # integrate() must self-heal the autostart entry on every packaged start
    # (so a stale Exec= path is healed even when the desktop-integration stamp
    # is up to date) — see startup.refresh_autostart.
    calls = []
    monkeypatch.setattr(integration.startup, "refresh_autostart", lambda: calls.append(True))
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    assert calls == [True]


def test_integrate_refreshes_autostart_before_stamp_short_circuit(env, monkeypatch):
    # A stale autostart path must be healed even when the desktop-integration
    # stamp itself is up to date (the second, idempotent run): refresh_autostart
    # runs before integrate()'s stamp short-circuit return.
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    calls = []
    monkeypatch.setattr(integration.startup, "refresh_autostart", lambda: calls.append(True))
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    assert calls == [True]  # ran despite the stamp being current


def test_integrate_swallows_refresh_autostart_failure(env, monkeypatch):
    # Best-effort: a raise from refresh_autostart must be logged and must not
    # propagate out of integrate() (log-and-continue discipline).
    def boom():
        raise OSError("autostart heal failed")

    monkeypatch.setattr(integration.startup, "refresh_autostart", boom)
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    assert env.desktop_file.exists()  # the rest of integrate() still ran


def test_icon_falls_back_to_theme_name_without_source(env, monkeypatch):
    # No icon source resolvable ($APPDIR unset): the entry still writes, with
    # the theme icon name rather than an absolute path.
    integration.integrate(env.paths, appimage=env.appimage, icon_source=Path("/nope.png"))
    desktop = env.desktop_file.read_text()
    assert "Icon=fused-render\n" in desktop
    assert not env.icon_file.exists()


# ---- deintegrate(): the reverse of integrate() (Uninstall) -------------------
# Integration-only teardown: removes the four artifacts integrate() writes and
# the autostart entry, refreshes the freedesktop databases, and NEVER touches
# app data or the AppImage binary.


def test_deintegrate_removes_all_artifacts_and_refreshes(env, monkeypatch):
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    assert env.desktop_file.exists() and env.mime_file.exists()
    assert env.icon_file.exists() and env.stamp_file.exists()

    # Enable autostart too, so we can assert deintegrate removes it.
    monkeypatch.setenv("APPIMAGE", str(env.appimage))
    integration.startup.set_enabled(True)
    autostart = integration.startup._desktop_file()
    assert autostart.exists()

    env.tool_calls.clear()
    integration.deintegrate(env.paths)

    # All four installed artifacts are gone.
    assert not env.desktop_file.exists()
    assert not env.mime_file.exists()
    assert not env.icon_file.exists()
    assert not env.stamp_file.exists()
    # Autostart entry removed.
    assert not autostart.exists()
    # Databases refreshed (dropping "Open with" + scheme associations).
    tools = [c[0] for c in env.tool_calls]
    assert "update-mime-database" in tools
    assert "update-desktop-database" in tools


def test_deintegrate_leaves_binary_and_data_untouched(env, monkeypatch):
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)
    # Integration-only: the AppImage binary and the state dir itself survive.
    integration.deintegrate(env.paths)
    assert env.appimage.exists()  # the binary is never deleted
    assert env.paths.state.exists()  # app data / state dir untouched


def test_deintegrate_is_best_effort_when_artifacts_absent(env):
    # A never-integrated (or partially cleaned) install: unlinking missing files
    # must not raise, and the run must still complete.
    integration.deintegrate(env.paths)  # no integrate() first
    assert not env.desktop_file.exists()


def test_deintegrate_swallows_tool_failures(env, monkeypatch):
    integration.integrate(env.paths, appimage=env.appimage, icon_source=env.icon_src)

    def boom(argv, **kw):
        raise OSError("update tool crashed")

    monkeypatch.setattr(integration.subprocess, "run", boom)
    integration.deintegrate(env.paths)  # must not raise
    assert not env.desktop_file.exists()  # artifacts still removed


# ---- Desktop Entry Spec Exec= quoting (NOT shell/shlex quoting) --------------
# freedesktop only recognizes double-quote quoting in Exec; shlex's single
# quotes are rejected by some launchers. An argument with reserved characters
# must be double-quoted, with `"` ` $ \ backslash-escaped and every backslash
# then doubled by the general string-escape rule.


def _exec_line(appimage: str) -> str:
    entry = integration._desktop_entry(Path(appimage), "fused-render")
    (line,) = [ln for ln in entry.splitlines() if ln.startswith("Exec=")]
    return line


def test_exec_plain_path_is_unquoted():
    assert _exec_line("/opt/FusedRender.AppImage") == "Exec=/opt/FusedRender.AppImage %u"


def test_exec_path_with_space_is_double_quoted():
    assert _exec_line("/opt/Fused Render.AppImage") == 'Exec="/opt/Fused Render.AppImage" %u'


def test_exec_path_with_double_quote_is_escaped():
    # `"` -> quoting-layer `\"` -> string-layer doubles the backslash -> `\\"`.
    assert _exec_line('/opt/Fu"sed.AppImage') == 'Exec="/opt/Fu\\\\"sed.AppImage" %u'


def test_exec_path_with_dollar_is_escaped():
    # `$` -> `\$` -> string layer -> `\\$` (the exact example the spec gives).
    assert _exec_line("/opt/Fu$sed.AppImage") == 'Exec="/opt/Fu\\\\$sed.AppImage" %u'


def test_exec_path_with_backslash_becomes_four_backslashes():
    assert _exec_line("/opt/Fu\\sed.AppImage") == 'Exec="/opt/Fu\\\\\\\\sed.AppImage" %u'
