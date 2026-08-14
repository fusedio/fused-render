"""Tests for the latex template's localhost daemon (daemon.py).

Hermetic: `_make_server` binds an ephemeral loopback port and we drive it with
urllib in-process, so no fused-render server is involved. Loaded via importlib
with templates/shared on sys.path, like test_latex_compile.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/latex/tests/test_latex_daemon.py -o addopts=""
"""
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

_LATEX = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(os.path.dirname(_LATEX), "shared")


def _load(name, filename):
    if _SHARED not in sys.path:
        sys.path.insert(0, _SHARED)
    if _LATEX not in sys.path:
        sys.path.insert(0, _LATEX)
    spec = importlib.util.spec_from_file_location(name, os.path.join(_LATEX, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dm(tmp_path, monkeypatch):
    d = _load("latex_daemon", "daemon.py")
    monkeypatch.setattr(d, "STATE", str(tmp_path / "daemon.json"))
    monkeypatch.setattr(d, "LOCK", str(tmp_path / "daemon.spawn.lock"))
    return d


def test_coerce_matches_the_runpython_binding(dm):
    assert dm._coerce("5", int) == 5
    assert dm._coerce("2.5", float) == 2.5
    assert dm._coerce("true", bool) is True
    assert dm._coerce("0", bool) is False
    assert dm._coerce("off", bool) is False
    assert dm._coerce("main.tex", str) == "main.tex"


def test_dispatch_coerces_via_the_engine_signature(dm, monkeypatch):
    import inspect
    seen = {}

    def spy(**kw):
        seen.update(kw)
        return {"ok": True}
    spy.__signature__ = inspect.signature(dm.engine.main)   # keep the typed params _dispatch reads
    monkeypatch.setattr(dm.engine, "main", spy)
    dm._dispatch({"action": "compile", "force": "1", "synctex": "false", "line": "7"})
    assert seen["action"] == "compile"          # str stays a str
    assert seen["force"] == 1 and isinstance(seen["force"], int)
    assert seen["synctex"] is False             # bool, not the truthy string "false"
    assert seen["line"] == 7 and isinstance(seen["line"], int)


def test_version_is_a_stable_code_hash(dm):
    v = dm._version()
    assert isinstance(v, str) and len(v) == 16
    assert v == dm._version()


def _get(port, path):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3)


def test_server_serves_engine_over_http_and_gates_on_the_token(dm, monkeypatch):
    monkeypatch.setattr(dm.engine, "_tectonic_bin", lambda: None)  # deterministic status
    srv, port, token = dm._make_server()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        r = _get(port, f"/run?action=tectonic_status&_token={token}")
        body = json.load(r)
        assert body["available"] is False        # engine.main actually ran

        r = _get(port, f"/ping?_token={token}")
        assert json.load(r)["version"] == dm._version()

        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(port, "/run?action=tectonic_status&_token=wrong")
        assert ei.value.code == 403              # no token -> forbidden
    finally:
        srv.shutdown()       # unblocks serve_forever (running in the thread above)
        srv.server_close()


def test_blank_param_reaches_engine_as_empty_string(dm, monkeypatch):
    # An empty param must arrive as "" (keep_blank_values), matching what the
    # runPython fallback would pass — not be dropped so engine.main sees its default.
    seen = {}
    monkeypatch.setattr(dm.engine, "main", lambda **kw: seen.update(kw) or {"ok": True})
    srv, port, token = dm._make_server()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        _get(port, f"/run?action=browse&target=&_token={token}")
        assert seen.get("target") == "" and seen.get("action") == "browse"
    finally:
        srv.shutdown()
        srv.server_close()


def test_raised_exception_comes_back_under_the_sentinel(dm, monkeypatch):
    def boom(**kw):
        raise ValueError("nope")
    boom.__signature__ = __import__("inspect").signature(dm.engine.main)
    monkeypatch.setattr(dm.engine, "main", boom)
    srv, port, token = dm._make_server()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        body = json.load(_get(port, f"/run?action=outline&_token={token}"))
        assert "__exc__" in body and body["__exc__"]["type"] == "ValueError"
        assert "nope" in body["__exc__"]["message"]
        assert "error" not in body     # a raised error must NOT look like an engine {error} return
    finally:
        srv.shutdown()
        srv.server_close()


def test_main_reuses_a_live_daemon_without_spawning(dm, monkeypatch):
    monkeypatch.setattr(dm, "_read_state",
                        lambda: {"port": 7, "token": "tok", "version": dm._version()})
    monkeypatch.setattr(dm, "_alive", lambda *a: True)
    spawned = []
    monkeypatch.setattr(dm, "_spawn", lambda: spawned.append(1))
    assert dm.main() == {"port": 7, "token": "tok", "reused": True}
    assert spawned == []                 # a healthy daemon is never re-spawned


def test_main_serializes_the_spawn_so_a_waiter_reuses_the_winners_daemon(dm, monkeypatch):
    # Two concurrent ensures: the winner spawns once; the waiter blocks on the
    # file_lock, then finds the daemon the winner started and reuses it — no
    # orphaned second server.
    monkeypatch.setattr(dm, "_read_state", lambda: None)
    monkeypatch.setattr(dm, "_await_daemon", lambda v: {"port": 5, "token": "t", "reused": False})
    spawns = []

    def slow_spawn():
        spawns.append(1)
        monkeypatch.setattr(dm, "_read_state",
                            lambda: {"port": 5, "token": "t", "version": dm._version()})
        monkeypatch.setattr(dm, "_alive", lambda *a: True)
        time.sleep(0.3)
    monkeypatch.setattr(dm, "_alive", lambda *a: False)
    monkeypatch.setattr(dm, "_spawn", slow_spawn)

    results = []
    threads = [threading.Thread(target=lambda: results.append(dm.main())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(spawns) == 1                       # only one server ever spawned
    assert any(r.get("reused") for r in results)  # the waiter reused it


def test_json_response_is_no_store(dm):
    # Poll/compile GETs reuse the same URL; without no-store a memory cache would
    # replay the first response and warming/compiles would look stuck.
    srv, port, token = dm._make_server()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        r = _get(port, f"/ping?_token={token}")
        assert r.headers.get("Cache-Control") == "no-store"
    finally:
        srv.shutdown()
        srv.server_close()


def test_main_creates_cache_root_on_a_fresh_machine(dm, tmp_path, monkeypatch):
    # CACHE_ROOT doesn't exist yet; main() must create it before touching the
    # lock/state under it. LOCK points at an existing dir so only main()'s own
    # makedirs can create CACHE_ROOT here.
    fresh = tmp_path / "fresh" / "cache" / "latex"
    monkeypatch.setattr(dm.engine, "CACHE_ROOT", str(fresh))
    monkeypatch.setattr(dm, "LOCK", str(tmp_path / "daemon.spawn.lock"))
    monkeypatch.setattr(dm, "STATE", str(fresh / "daemon.json"))
    monkeypatch.setattr(dm, "_read_state", lambda: None)
    monkeypatch.setattr(dm, "_alive", lambda *a: False)
    monkeypatch.setattr(dm, "_spawn", lambda: None)
    monkeypatch.setattr(dm, "_await_daemon", lambda v: {"port": 1, "token": "t", "reused": False})
    assert not fresh.exists()
    dm.main()
    assert fresh.is_dir()


def test_state_file_records_port_token_and_version(dm):
    srv, port, token = dm._make_server()
    try:
        st = json.load(open(dm.STATE, encoding="utf-8"))
        assert st["port"] == port and st["token"] == token
        assert st["version"] == dm._version() and st["pid"] == os.getpid()
    finally:
        srv.server_close()   # no serve_forever here — shutdown() would block forever
