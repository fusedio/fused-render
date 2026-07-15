from fused_render import wintray


def test_module_imports_without_pystray():
    # pystray/Pillow are Windows-bundle-only and imported inside main(), so the
    # module must import on any platform (and in CI) without them installed.
    assert callable(wintray.main)
    assert callable(wintray._kill_server)


def test_kill_server_noop_without_pidfile(monkeypatch):
    monkeypatch.setattr(wintray.winopen, "_read_int", lambda path: None)
    calls = []
    monkeypatch.setattr(wintray.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(wintray.winopen, "_remove_pidfile", lambda: calls.append("removed"))
    wintray._kill_server()
    assert calls == ["removed"]  # no taskkill when there's no recorded pid
