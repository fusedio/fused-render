import pytest

from fused_render import winopen


def test_progid():
    assert winopen._progid(".csv") == "FusedRender.csv"


def test_type_name():
    assert winopen._type_name(".csv") == "CSV File (fused-render)"


def test_extensions_are_single_suffix_and_clean():
    exts = winopen.extensions()
    assert exts
    assert all(e.startswith(".") and e.count(".") == 1 for e in exts)
    assert not (winopen._NOT_EXTENSIONS & set(exts))
    assert exts == sorted(set(exts))


def test_view_url_no_path():
    # winopen re-exports the shared codec (fused_render._view_url_codec) —
    # full encoding-rule coverage lives in tests/test_view_url_codec.py.
    assert winopen.view_url(1777, None) == "http://127.0.0.1:1777/"


def test_view_url_encodes_drive_path():
    assert winopen.view_url(1777, r"C:\data\sales.csv") == (
        "http://127.0.0.1:1777/view/C%3A/data/sales.csv"
    )


def test_find_running_server_never_waits_on_stale_portfile(monkeypatch):
    # the recorded port stopped answering (server gone, port maybe another
    # app's now) -> straight to the range scan, no grace period anywhere
    monkeypatch.setattr(winopen, "_read_int", lambda path: 1777)
    monkeypatch.setattr(winopen, "_fused_server", lambda port: port == 1780)
    assert winopen.find_running_server() == 1780


def test_ensure_server_reprobes_under_lock(monkeypatch):
    # a racing peer boots the server while we wait on the spawn lock ->
    # the re-probe inside the lock finds it and we never double-spawn
    probes = iter([None, 1777])
    monkeypatch.setattr(winopen, "find_running_server", lambda: next(probes))
    monkeypatch.setattr(winopen, "_spawn", lambda port: pytest.fail("double-spawned"))
    assert winopen._ensure_server(None) == 1777


def test_ensure_server_spawns_when_alone(monkeypatch):
    monkeypatch.setattr(winopen, "find_running_server", lambda: None)
    monkeypatch.setattr(winopen, "pick_port", lambda *a, **k: 1778)
    monkeypatch.setattr(winopen, "_spawn", lambda port: port)
    assert winopen._ensure_server(None) == 1778


def test_open_launch_deeplink_ensures_server_and_opens_no_tab(monkeypatch, tmp_path):
    # fused-render://launch (D128): server is ensured, browser stays closed —
    # the down-banner page that linked here reconnects on its own.
    monkeypatch.setattr(winopen, "APP_SUPPORT_DIR", str(tmp_path))
    monkeypatch.setattr(winopen, "setup_logging", lambda: None)
    ensured = []
    monkeypatch.setattr(winopen, "_ensure_server", lambda port: ensured.append(port) or 1777)
    monkeypatch.setattr(
        winopen.webbrowser, "open", lambda url: pytest.fail(f"opened a tab: {url}")
    )
    winopen._open("fused-render://launch", None)
    assert ensured == [None]


def test_open_clone_deeplink_still_opens_confirm_page(monkeypatch, tmp_path):
    monkeypatch.setattr(winopen, "APP_SUPPORT_DIR", str(tmp_path))
    monkeypatch.setattr(winopen, "setup_logging", lambda: None)
    monkeypatch.setattr(winopen, "_ensure_server", lambda port: 1777)
    opened = []
    monkeypatch.setattr(winopen.webbrowser, "open", lambda url: opened.append(url))
    winopen._open("fused-render://open?git=https://github.com/o/r", None)
    assert len(opened) == 1
    assert opened[0].startswith("http://127.0.0.1:1777/clone?src=")
