"""fused_render.app.configure_desktop_instance publishes the desktop instance
id + token into the env so the in-process macOS server exposes the same
token-verified /api/config echo and /api/desktop/shutdown contract as the
Windows supervisor's child server. Module-level and AppKit-free (rumps is
imported lazily inside main()), so it is testable anywhere.
"""
from fused_render import desktop_probe
from fused_render.app import configure_desktop_instance
from fused_render.paths import desktop_instance


def test_configure_publishes_instance_and_token(monkeypatch):
    # setenv (even to a placeholder) so monkeypatch restores/clears the vars at
    # teardown, including whatever configure_desktop_instance sets directly.
    monkeypatch.setenv("FUSED_RENDER_DESKTOP_INSTANCE_ID", "")
    monkeypatch.setenv("FUSED_RENDER_DESKTOP_INSTANCE_TOKEN", "")

    instance_id, token = configure_desktop_instance()

    assert instance_id == desktop_probe.DESKTOP_INSTANCE_ID
    assert len(token) == 64  # 256-bit hex
    # The server reads these lazily; they must now resolve to a live instance.
    assert desktop_instance() == (instance_id, token)


def test_configure_mints_a_fresh_token_each_call(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_DESKTOP_INSTANCE_ID", "")
    monkeypatch.setenv("FUSED_RENDER_DESKTOP_INSTANCE_TOKEN", "")
    assert configure_desktop_instance()[1] != configure_desktop_instance()[1]
