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
    assert winopen._view_url(1777, None) == "http://127.0.0.1:1777/"


def test_view_url_encodes_drive_path(monkeypatch):
    # stub abspath so the encoding is exercised the same on POSIX CI as on Windows
    monkeypatch.setattr(winopen.os.path, "abspath", lambda p: p)
    assert winopen._view_url(1777, r"C:\data\sales.csv") == (
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
