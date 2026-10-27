"""The Linux window host turns WebKitGTK's DMABUF renderer off before WebKit
loads, without overriding a value the user set."""
from fused_render.supervisor._linux import window_host

VAR = "WEBKIT_DISABLE_DMABUF_RENDERER"


def test_disables_dmabuf_renderer_by_default(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    window_host.configure_webkit_env()
    assert window_host.os.environ[VAR] == "1"


def test_user_value_wins(monkeypatch):
    monkeypatch.setenv(VAR, "0")
    window_host.configure_webkit_env()
    assert window_host.os.environ[VAR] == "0"


def test_load_toolkit_sets_env_before_importing_gi(monkeypatch):
    import builtins

    monkeypatch.delenv(VAR, raising=False)
    seen = {}
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "gi":
            seen["value"] = window_host.os.environ.get(VAR)
            raise ImportError("no gi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    try:
        window_host.load_toolkit()
    except window_host.ToolkitUnavailable:
        pass
    assert seen["value"] == "1"
