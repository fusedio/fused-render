import subprocess

from fused_render.windows_process import install_no_window_policy


def test_desktop_subprocesses_are_hidden(monkeypatch):
    calls = []

    def original(self, *args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", original)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setenv("FUSED_RENDER_DESKTOP_INSTANCE_ID", "desktop-v1")
    monkeypatch.setattr("fused_render.windows_process.sys.platform", "win32")
    install_no_window_policy()
    installed = subprocess.Popen.__init__
    installed(object(), ["command"], creationflags=4)
    install_no_window_policy()

    assert calls == [{"creationflags": 0x08000004}]
    assert getattr(installed, "_fused_render_no_window", False)
    assert subprocess.Popen.__init__ is installed


def test_wheel_subprocesses_are_unchanged(monkeypatch):
    original = subprocess.Popen.__init__
    monkeypatch.delenv("FUSED_RENDER_DESKTOP_INSTANCE_ID", raising=False)
    monkeypatch.setattr("fused_render.windows_process.sys.platform", "win32")
    install_no_window_policy()
    assert subprocess.Popen.__init__ is original
