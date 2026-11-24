"""WSL: opening a web address in the Windows browser (fused_render/_wsl.py)."""
import webbrowser

import pytest

from fused_render import _wsl

# Auth0's authorize address has this shape; the `=` in its query is what made `explorer.exe` (the end of both
# `webbrowser.open` and `xdg-open` under WSL) open the Documents folder instead of the browser.
SIGN_IN = "https://auth.example.invalid/authorize?audience=a&scope=openid+email&redirect_uri=http%3A%2F%2Flocalhost%3A3000"


def test_is_wsl_reads_the_distro_name_or_the_kernel_release():
    assert _wsl.is_wsl(env={"WSL_DISTRO_NAME": "Ubuntu"}, release="6.6.0", platform="linux")
    assert _wsl.is_wsl(env={}, release="5.15.167.4-microsoft-standard-WSL2", platform="linux")
    assert not _wsl.is_wsl(env={}, release="6.8.0-generic", platform="linux")
    assert not _wsl.is_wsl(env={"WSL_DISTRO_NAME": "Ubuntu"}, release="x", platform="darwin")


def test_a_sign_in_address_goes_to_rundll32_whole():
    argv = _wsl.url_argv(SIGN_IN, which=lambda n: f"/mnt/c/WINDOWS/system32/{n}")
    assert argv == ["/mnt/c/WINDOWS/system32/rundll32.exe", "url.dll,FileProtocolHandler", SIGN_IN]


def test_rundll32_is_found_without_the_windows_path():
    argv = _wsl.url_argv(SIGN_IN, which=lambda n: None, exists=lambda p: p == "/mnt/c/Windows/System32/rundll32.exe")
    assert argv and argv[0] == "/mnt/c/Windows/System32/rundll32.exe"
    assert _wsl.url_argv(SIGN_IN, which=lambda n: None, exists=lambda p: False) is None


@pytest.mark.parametrize("target", ["/home/ada/file.pdf", "file:///home/ada/x", "mailto:a@b.c", "fused-render://relaunch", "", None])
def test_only_web_addresses_go_that_way(target):
    assert _wsl.url_argv(target, which=lambda n: "/x/rundll32.exe") is None


def test_open_url_reports_a_start_that_failed(monkeypatch):
    def boom(*a, **k):
        raise OSError("Exec format error")  # Windows interop off

    monkeypatch.setattr(_wsl, "url_argv", lambda url: ["rundll32.exe", "url.dll,FileProtocolHandler", url])
    monkeypatch.setattr(_wsl.subprocess, "Popen", boom)
    assert _wsl.open_url(SIGN_IN) is False


@pytest.fixture
def registry(monkeypatch):
    """Leave `webbrowser`'s process-wide registry as it was."""
    webbrowser.get  # noqa: B018 — the module is imported; its registry fills lazily
    monkeypatch.setattr(_wsl, "_registered", False)
    yield
    webbrowser._browsers.pop(_wsl.BROWSER_NAME, None)
    if webbrowser._tryorder:
        webbrowser._tryorder[:] = [n for n in webbrowser._tryorder if n != _wsl.BROWSER_NAME]


def test_webbrowser_open_takes_the_windows_route_under_wsl(monkeypatch, registry):
    opened = []
    monkeypatch.setattr(_wsl, "is_wsl", lambda: True)
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(_wsl, "open_url", lambda url: opened.append(url) or True)
    assert _wsl.use_windows_browser() is True
    assert _wsl.use_windows_browser() is True  # twice: still one entry
    assert webbrowser._tryorder.count(_wsl.BROWSER_NAME) == 1
    assert webbrowser.open(SIGN_IN) is True
    assert opened == [SIGN_IN]


def test_a_browser_the_person_chose_stays_in_charge(monkeypatch, registry):
    monkeypatch.setattr(_wsl, "is_wsl", lambda: True)
    monkeypatch.setenv("BROWSER", "wslview")
    assert _wsl.use_windows_browser() is False
    assert _wsl.BROWSER_NAME not in webbrowser._browsers


def test_nothing_changes_off_wsl(monkeypatch, registry):
    monkeypatch.setattr(_wsl, "is_wsl", lambda: False)
    assert _wsl.use_windows_browser() is False
    assert _wsl.BROWSER_NAME not in webbrowser._browsers


def test_the_supervisor_sends_a_web_address_past_xdg_open_under_wsl(monkeypatch):
    from fused_render.supervisor._linux import ui

    opened, spawned = [], []
    monkeypatch.setattr(_wsl, "is_wsl", lambda: True)
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(_wsl, "open_url", lambda url: opened.append(url) or url.startswith("http"))

    class Proc:
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(ui.subprocess, "Popen", lambda argv, **k: spawned.append(argv) or Proc())
    ui.open_url(SIGN_IN)
    assert opened == [SIGN_IN] and spawned == []
    ui.open_path(ui.Path("/home/ada"))  # a folder still goes to xdg-open (the file manager)
    assert spawned == [["xdg-open", "/home/ada"]]
