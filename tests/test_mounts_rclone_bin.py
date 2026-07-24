"""rclone_bin() resolution order: FUSED_RENDER_RCLONE_BIN override →
macOS bundle → PATH. The override is what the packaged Windows installer and
Linux AppImage set (via the supervisor's child_environment) so bundled rclone
wins over path-guessing. Monkeypatched env/fs — no real rclone needed."""
import pytest

import fused_render.shell.mounts as mounts_mod


@pytest.fixture(autouse=True)
def _clear_override(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_RCLONE_BIN", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)


def test_env_override_wins_when_it_is_a_file(tmp_path, monkeypatch):
    bundled = tmp_path / "rclone"
    bundled.write_text("")
    monkeypatch.setenv("FUSED_RENDER_RCLONE_BIN", str(bundled))
    # Even a packaged macOS bundle + a PATH hit must lose to the explicit env.
    monkeypatch.setattr(mounts_mod.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/usr/bin/rclone")
    assert mounts_mod.rclone_bin() == str(bundled)


def test_env_override_ignored_when_not_a_file(monkeypatch):
    # A stale/wrong override must not shadow a real PATH rclone (dev safety).
    monkeypatch.setenv("FUSED_RENDER_RCLONE_BIN", "/nonexistent/rclone")
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/usr/local/bin/rclone")
    assert mounts_mod.rclone_bin() == "/usr/local/bin/rclone"


def test_macos_bundle_used_when_no_override(tmp_path, monkeypatch):
    contents = tmp_path / "FusedRender.app" / "Contents"
    bundled = contents / "Resources" / "bin" / "rclone"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    monkeypatch.setattr(mounts_mod.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "executable", str(contents / "MacOS" / "python"))
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/should/not/be/used")
    assert mounts_mod.rclone_bin() == str(bundled)


def test_path_fallback_when_no_override_no_bundle(monkeypatch):
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/usr/local/bin/rclone")
    assert mounts_mod.rclone_bin() == "/usr/local/bin/rclone"
