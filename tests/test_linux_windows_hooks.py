"""Server-side half of Linux native windows (`fused_render/linux_windows.py`):
the `window_policy.native_hooks` it installs, how they fall back to a browser
tab, and how the Preferences state reports `available`."""
import os
import shutil
import socket
import tempfile
import threading

import pytest

from fused_render import linux_windows, window_policy
from fused_render import window_host_ipc as ipc

needs_unix = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="Unix domain sockets required"
)


@pytest.fixture(autouse=True)
def clean_hooks():
    saved = dict(window_policy.native_hooks)
    window_policy.native_hooks.clear()
    yield
    window_policy.native_hooks.clear()
    window_policy.native_hooks.update(saved)


@pytest.fixture
def sock():
    d = tempfile.mkdtemp(prefix="fr")
    try:
        yield os.path.join(d, "h.sock")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def host(sock):
    seen, stop = [], threading.Event()
    reply = {"open": {"ok": True}}

    def handler(cmd):
        seen.append(cmd)
        return reply.get(cmd["cmd"], {"ok": True})

    ipc.serve(sock, handler, stop)
    import time
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.01)
    yield seen, reply
    stop.set()


def test_installs_nothing_off_linux(sock):
    assert linux_windows.install(8123, {ipc.ENV_SOCKET: sock}, platform="darwin") is False
    assert window_policy.native_hooks == {}


def test_installs_nothing_without_the_supervisor_env():
    assert linux_windows.install(8123, {}, platform="linux") is False
    assert window_policy.native_hooks == {}


def test_installs_the_three_hooks(sock):
    assert linux_windows.install(8123, {ipc.ENV_SOCKET: sock}, platform="linux") is True
    assert set(window_policy.native_hooks) == {"apply", "open_app", "usable"}


@needs_unix
def test_open_app_sends_the_apps_window_url_to_the_host(sock, host, tmp_path):
    seen, _ = host
    linux_windows.install(8123, {ipc.ENV_SOCKET: sock}, platform="linux")
    window_policy.native_hooks["open_app"](str(tmp_path))
    opens = [c for c in seen if c["cmd"] == "open"]
    assert len(opens) == 1
    assert opens[0]["url"] == "http://127.0.0.1:8123" + window_policy.app_window_path(str(tmp_path))


@needs_unix
def test_open_app_falls_back_to_a_browser_tab_when_declined(sock, host, tmp_path, monkeypatch):
    _, reply = host
    reply["open"] = {"ok": False, "reason": "disabled"}
    tabs = []
    monkeypatch.setattr(linux_windows, "_open_in_browser", tabs.append)
    linux_windows.install(8123, {ipc.ENV_SOCKET: sock}, platform="linux")
    window_policy.native_hooks["open_app"](str(tmp_path))
    assert tabs == ["http://127.0.0.1:8123" + window_policy.app_window_path(str(tmp_path))]


@needs_unix
def test_open_app_falls_back_when_the_host_is_gone(sock, tmp_path, monkeypatch):
    tabs = []
    monkeypatch.setattr(linux_windows, "_open_in_browser", tabs.append)
    linux_windows.install(8123, {ipc.ENV_SOCKET: sock}, platform="linux")  # nothing listening
    window_policy.native_hooks["open_app"](str(tmp_path))
    assert len(tabs) == 1


@needs_unix
def test_apply_forwards_the_preference(sock, host):
    seen, _ = host
    linux_windows.install(8123, {ipc.ENV_SOCKET: sock}, platform="linux")
    window_policy.native_hooks["apply"](False)
    window_policy.native_hooks["apply"](True)
    assert [c for c in seen if c["cmd"] == "set_enabled"] == [
        {"cmd": "set_enabled", "on": False}, {"cmd": "set_enabled", "on": True}]


@needs_unix
def test_apply_and_open_app_use_the_shared_caller_timeout(sock, host, tmp_path, monkeypatch):
    """`apply` (the preference toggle) used to time out at 2s, shorter than
    the host's own 3s main-thread deadline — the caller could give up on a
    `set_enabled` the host was still about to carry out, desyncing the two.
    Both calls must use the same timeout as `open_app`, and it must stay
    above the host's deadline."""
    from fused_render.supervisor._linux import window_host as wh

    seen = []
    real_request = ipc.request

    def spy(path, payload, timeout=2.0):
        seen.append(timeout)
        return real_request(path, payload, timeout)

    monkeypatch.setattr(ipc, "request", spy)
    linux_windows.install(8123, {ipc.ENV_SOCKET: sock}, platform="linux")
    window_policy.native_hooks["apply"](True)
    window_policy.native_hooks["open_app"](str(tmp_path))
    assert seen == [ipc.CALLER_TIMEOUT_S, ipc.CALLER_TIMEOUT_S]
    assert ipc.CALLER_TIMEOUT_S > wh._MAIN_DEADLINE_S


@needs_unix
def test_apply_reports_false_when_the_host_refuses(sock, host, caplog):
    _, reply = host
    reply["set_enabled"] = {"ok": False, "reason": "locked"}
    linux_windows.install(8123, {ipc.ENV_SOCKET: sock}, platform="linux")
    with caplog.at_level("WARNING"):
        assert window_policy.native_hooks["apply"](True) is False
    assert "locked" in caplog.text


@needs_unix
def test_apply_never_raises_when_the_host_is_gone(sock):
    linux_windows.install(8123, {ipc.ENV_SOCKET: sock}, platform="linux")
    window_policy.native_hooks["apply"](False)  # must not raise: the pref still stores


@needs_unix
def test_usable_reflects_a_live_host(sock, host):
    linux_windows.install(8123, {ipc.ENV_SOCKET: sock}, platform="linux")
    assert window_policy.native_hooks["usable"]() is True


@needs_unix
def test_usable_is_false_when_the_host_is_gone(sock):
    linux_windows.install(8123, {ipc.ENV_SOCKET: sock}, platform="linux")
    assert window_policy.native_hooks["usable"]() is False


@needs_unix
def test_usable_is_true_when_launchable_even_with_no_host_running(sock):
    """The preference-off-after-restart case: nothing answers the ping, but
    the supervisor says a host could run here, so the switch stays usable."""
    linux_windows.install(8123, {ipc.ENV_SOCKET: sock, ipc.ENV_LAUNCHABLE: "1"},
                           platform="linux")
    assert window_policy.native_hooks["usable"]() is True


# ---- Preferences "available" ------------------------------------------------

def test_prefs_available_with_a_live_host():
    from fused_render.shell import prefs

    assert prefs._native_windows_state()["available"] is False
    window_policy.native_hooks["usable"] = lambda: True
    window_policy.native_hooks["apply"] = lambda on: None
    assert prefs._native_windows_state()["available"] is True
    window_policy.native_hooks["usable"] = lambda: False
    assert prefs._native_windows_state()["available"] is False


def test_prefs_available_with_apply_and_no_usable_hook():
    from fused_render.shell import prefs

    assert prefs._native_windows_state()["available"] is False
    window_policy.native_hooks["apply"] = lambda on: None
    assert prefs._native_windows_state()["available"] is True


def test_prefs_available_needs_apply_specifically(monkeypatch):
    """`available` is one formula everywhere — `"apply" in hooks and
    usable()` — not a platform branch: a `usable` hook with no `apply`
    (impossible in practice, since every installer sets both together) still
    reads as unavailable, and win32's backend never sets either, so its
    `native_hooks` stays `{}` and this formula reads False there too without
    needing to ask `sys.platform` at all."""
    from fused_render.shell import prefs

    window_policy.native_hooks["usable"] = lambda: True
    assert prefs._native_windows_state()["available"] is False
    window_policy.native_hooks["apply"] = lambda on: None
    assert prefs._native_windows_state()["available"] is True


def test_install_is_a_noop_without_af_unix(monkeypatch):
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    assert linux_windows.install(1, environ={ipc.ENV_SOCKET: "/x/h.sock"}, platform="win32") is False
    assert window_policy.native_hooks == {}
