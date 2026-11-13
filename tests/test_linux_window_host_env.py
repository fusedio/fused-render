"""The Linux window host uses WebKitGTK's DMABUF (GPU) renderer unless the
user turned it off in the environment."""
import pytest

from fused_render.supervisor._linux import window_host

VAR = "WEBKIT_DISABLE_DMABUF_RENDERER"


@pytest.mark.parametrize(("value", "active"), [(None, True), ("0", True), ("1", False)])
def test_gpu_rendering_active_follows_webkit_env(monkeypatch, value, active):
    if value is None:
        monkeypatch.delenv(VAR, raising=False)
    else:
        monkeypatch.setenv(VAR, value)
    assert window_host.gpu_rendering_active() is active


def test_load_toolkit_leaves_the_renderer_alone(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.setitem(window_host.sys.modules, "gi", None)
    with pytest.raises(window_host.ToolkitUnavailable):
        window_host.load_toolkit()
    assert VAR not in window_host.os.environ
