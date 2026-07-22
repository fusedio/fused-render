"""Registry resolution for the read-only glb viewer template.

The glb template is pure client-side HTML/JS (vendored Three.js) with no Python
backend, so the only server-side behavior to assert is that .glb/.gltf route to
it. (The editor tier — glbmodel + .glbproj/ — was removed; this also guards that
.glbproj no longer resolves to an editor.)
"""

from fused_render import server


def test_glb_template_resolves_for_glb_and_gltf():
    entries, err = server._templates_for("/tmp/model.glb", False)
    assert err is None
    assert [e["mode"] for e in entries][0] == "glb"
    entries, err = server._templates_for("/tmp/model.gltf", False)
    assert [e["mode"] for e in entries] == ["glb", "usd", "code"]


def test_glbproj_dir_no_longer_resolves_to_an_editor():
    entries, _ = server._templates_for("/tmp/mage.glbproj", True)
    modes = [e["mode"] for e in entries] if entries else []
    assert "glbmodel" not in modes
