"""Warm-worker app engine (/api/engine): interpreter choice, engine id,
validation, and the worker's warm-state / mtime-reload / error envelope."""
import os
import sys
import time

import pytest

import fused_render
from fused_render import projectenv
from fused_render.server import engine_host


def _load_engine_worker():
    # engine_worker.py is spawned as a script (its dir on sys.path[0]) so it can
    # `from _binding import bind_params`; import it the same way to test _Target.
    pkg_dir = os.path.dirname(fused_render.__file__)
    if pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)
    import engine_worker

    return engine_worker


def test_interpreter_for_no_env_is_app_interpreter():
    assert projectenv.interpreter_for(None) == sys.executable


def test_app_engine_id_is_stable_bare_and_per_path():
    a = engine_host.app_engine_id("/tmp/proj/app.py")
    assert a == engine_host.app_engine_id("/tmp/proj/app.py")
    assert a != engine_host.app_engine_id("/tmp/proj/other.py")
    assert a.startswith("app_")
    assert engine_host._ENGINE_ID.match(a)


def test_ensure_app_rejects_foreign_interpreter(tmp_path):
    app = tmp_path / "app.py"
    app.write_text("def main():\n    return {}\n", encoding="utf-8")
    with pytest.raises(engine_host.EngineError):
        engine_host.ensure_app(str(app), "/definitely/not/a/real/python")


def test_forward_timeout_is_a_504_and_never_heals(monkeypatch):
    # A call that outruns its budget must become a 504 with the worker left
    # intact — never a heal/restart (which would kill the still-running worker,
    # its concurrent calls, and re-run main()).
    import asyncio

    from fused_render.server.routers import engines

    child = engine_host.Child(
        engine_id="app_timeouttest", python=sys.executable,
        daemon=engine_host.APP_WORKER, cache="unused",
        version=engine_host.APP_WORKER_VERSION, module="/tmp/x.py")
    monkeypatch.setattr(engine_host, "current", lambda eid: child)
    healed = []
    monkeypatch.setattr(engine_host, "restart",
                        lambda eid, failed=None: healed.append(eid) or child)

    async def fake_proxy(c, request, path, body, call_timeout=None):
        return engines._TIMEOUT

    monkeypatch.setattr(engines, "_proxy", fake_proxy)

    resp = asyncio.run(
        engines._forward("app_timeouttest", None, "/call", b"{}", call_timeout=60.0))
    assert resp.status_code == 504
    assert healed == []  # the worker was never restarted


def test_idle_reaper_skips_a_busy_engine(monkeypatch):
    # A call in flight (mark_busy) must stop idle-retire from killing the worker,
    # and mark_idle must refresh last_used so idle is timed from the call's end —
    # keyed by engine_id so a heal-restart mid-call keeps the live call counted.
    eid = engine_host.app_engine_id("/tmp/reaper-test/app.py")
    child = engine_host.Child(
        engine_id=eid, python=sys.executable, daemon=engine_host.APP_WORKER,
        cache="unused", version=engine_host.APP_WORKER_VERSION,
        module="/tmp/reaper-test/app.py")
    stale = time.monotonic() - (engine_host.APP_IDLE_RETIRE_S + 10)
    child.last_used = stale
    reaped = []
    monkeypatch.setattr(engine_host, "_terminate",
                        lambda c: reaped.append(c.engine_id))
    engine_host._children[eid] = child
    try:
        engine_host.mark_busy(eid)
        assert engine_host.reap_idle_app_workers() == 0  # busy: not reaped
        assert eid in engine_host._children

        engine_host.mark_idle(eid)  # balances busy AND stamps last_used = now
        assert engine_host.reap_idle_app_workers() == 0  # freshly used

        child.last_used = stale  # now genuinely idle
        assert engine_host.reap_idle_app_workers() == 1
        assert reaped == [eid]
    finally:
        engine_host._children.pop(eid, None)
        engine_host._busy.pop(eid, None)


def test_warm_target_persists_then_reloads_on_mtime(tmp_path):
    ew = _load_engine_worker()
    mod = tmp_path / "mod.py"
    v1 = ("_n = 0\nMARK = 'v1'\n"
          "def main():\n    global _n\n    _n += 1\n    return {'n': _n, 'mark': MARK}\n")
    mod.write_text(v1, encoding="utf-8")
    target = ew._Target(str(mod))

    assert target.call({})["result"] == {"n": 1, "mark": "v1"}
    assert target.call({})["result"] == {"n": 2, "mark": "v1"}  # warm: globals persist

    mod.write_text(v1.replace("'v1'", "'v2'"), encoding="utf-8")
    st = mod.stat()
    os.utime(str(mod), (st.st_atime + 5, st.st_mtime + 5))  # force a distinct mtime
    assert target.call({})["result"] == {"n": 1, "mark": "v2"}  # re-imported: reset + new code


def test_warm_target_error_envelope(tmp_path):
    ew = _load_engine_worker()
    mod = tmp_path / "bad.py"
    mod.write_text("def main():\n    raise ValueError('boom')\n", encoding="utf-8")
    out = ew._Target(str(mod)).call({})
    assert out["ok"] is False
    assert out["error"]["type"] == "ValueError"
    assert "boom" in out["error"]["message"]
    assert "Traceback" in out["error"]["traceback"]


def test_warm_target_rejects_non_json_result(tmp_path):
    ew = _load_engine_worker()
    mod = tmp_path / "nj.py"
    mod.write_text("def main():\n    return object()\n", encoding="utf-8")
    out = ew._Target(str(mod)).call({})
    assert out["ok"] is False
    assert out["error"]["type"] == "TypeError"
