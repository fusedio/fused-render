"""The app's names (fused_render/_flavor.py): "Fused Render" everywhere a
person reads it, "FusedRender" only where a space would break something — the
executable process probes match, and the release artifacts' file names."""
from fastapi.testclient import TestClient

from fused_render import _flavor
from fused_render.server import create_app


def test_render_names():
    assert _flavor.display_name() == "Fused Render"
    assert _flavor.bundle_name() == "Fused Render"
    assert _flavor.app_name() == "FusedRender"
    assert _flavor.legacy_bundle_names() == ("FusedRender",)


def test_config_carries_the_bundle_name(tmp_path):
    """Unpackaged there is no running bundle, so it is `_flavor`'s."""
    body = TestClient(create_app(start_dir=str(tmp_path))).get("/api/config").json()
    assert body["display_name"] == _flavor.display_name()
    assert body["bundle_name"] == _flavor.bundle_name()
