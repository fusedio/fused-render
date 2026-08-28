"""Warm-worker app engine (/api/engine): interpreter choice, engine id,
validation, and the worker's warm-state / mtime-reload / error envelope."""
import os
import sys
import time

import pytest

import fused_render
from fused_render import projectenv
from fused_render.server import engine_host


@pytest.fixture(autouse=True)
def _isolate_cwd_and_syspath():
    # _Target._load_locked chdirs to (and puts on sys.path) the module's dir, as
    # the real worker subprocess should; exercised in-process here it would leak
    # into sibling tests, so restore both.
    cwd = os.getcwd()
    path = list(sys.path)
    try:
        yield
    finally:
        os.chdir(cwd)
        sys.path[:] = path


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

    from fused_render.server import engine_forward

    child = engine_host.Child(
        engine_id="app_timeouttest", python=sys.executable,
        daemon=engine_host.APP_WORKER, cache="unused",
        version=engine_host.APP_WORKER_VERSION, module="/tmp/x.py")
    monkeypatch.setattr(engine_host, "current", lambda eid: child)
    healed = []
    monkeypatch.setattr(engine_host, "restart",
                        lambda eid, failed=None: healed.append(eid) or child)

    async def fake_proxy(c, request, path, body, call_timeout=None,
                         at_most_once=False):
        return engine_forward._TIMEOUT

    monkeypatch.setattr(engine_forward, "_proxy", fake_proxy)

    resp = asyncio.run(
        engine_forward._forward("app_timeouttest", None, "/call", b"{}",
                                call_timeout=60.0, at_most_once=True))
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
        module="/tmp/reaper-test/app.py", kind="app")
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


def test_idle_reaper_skips_a_worker_still_running_a_call(monkeypatch):
    # A call that timed out (504) leaves main() running in the worker; the worker
    # reports it as in-flight, so idle-retire must not kill it mid-call even once
    # its host-side busy count has been balanced and it looks idle.
    eid = engine_host.app_engine_id("/tmp/inflight-test/app.py")
    child = engine_host.Child(
        engine_id=eid, python=sys.executable, daemon=engine_host.APP_WORKER,
        cache="unused", version=engine_host.APP_WORKER_VERSION,
        module="/tmp/inflight-test/app.py", kind="app")
    child.last_used = time.monotonic() - (engine_host.APP_IDLE_RETIRE_S + 10)
    reaped = []
    monkeypatch.setattr(engine_host, "_terminate",
                        lambda c: reaped.append(c.engine_id))
    monkeypatch.setattr(engine_host, "_inflight", lambda c: 1)
    engine_host._children[eid] = child
    try:
        assert engine_host.reap_idle_app_workers() == 0  # still running: not reaped
        assert eid in engine_host._children

        monkeypatch.setattr(engine_host, "_inflight", lambda c: 0)  # main() finished
        assert engine_host.reap_idle_app_workers() == 1
        assert reaped == [eid]
    finally:
        engine_host._children.pop(eid, None)


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


# --- _spawn_env: PYTHONHOME survival keyed on interpreter, not kind ----------
# 2026-08-26 code-review fix: the old condition (`child.module and child.python
# == sys.executable`) only preserved the packaged interpreter's own env for an
# app warm worker (`module` is that kind's marker), so a background daemon —
# or a template daemon — running on `sys.executable` was silently stripped of
# PYTHONHOME and died at bootstrap in the packaged app. Pinning all four kinds
# here so the fix can't regress by kind again.

def test_spawn_env_strips_for_a_venv_interpreter(monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "/should/be/stripped")
    child = engine_host.Child(
        engine_id="templ_venv", python="/some/venv/bin/python",
        daemon="/t/daemon.py", cache="c", version="v1", kind="template")
    env = engine_host._spawn_env(child)
    assert "PYTHONHOME" not in env


def test_spawn_env_keeps_pythonhome_for_an_app_worker_on_sys_executable(monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "/keep/me")
    child = engine_host.Child(
        engine_id="app_x", python=sys.executable, daemon=engine_host.APP_WORKER,
        cache="c", version="v1", module="/m.py", kind="app")
    env = engine_host._spawn_env(child)
    assert env.get("PYTHONHOME") == "/keep/me"


def test_spawn_env_keeps_pythonhome_for_a_background_daemon_on_sys_executable(monkeypatch):
    # The blocker this whole block exists to pin: a background app on the
    # packaged interpreter (background_apps.interpreter_for's builtin-engine
    # fallback) must not lose PYTHONHOME.
    monkeypatch.setenv("PYTHONHOME", "/keep/me")
    child = engine_host.Child(
        engine_id="bg_x", python=sys.executable, daemon="/t/daemon.py",
        cache="c", version="v1", kind="background")
    env = engine_host._spawn_env(child)
    assert env.get("PYTHONHOME") == "/keep/me"


def test_spawn_env_keeps_pythonhome_for_a_template_daemon_on_sys_executable(monkeypatch):
    # A deliberate widening from the old module-gated condition: a template
    # daemon with no project venv (running on this app's own interpreter)
    # needs PYTHONHOME for the identical reason an app worker does.
    monkeypatch.setenv("PYTHONHOME", "/keep/me")
    child = engine_host.Child(
        engine_id="templ_own", python=sys.executable, daemon="/t/daemon.py",
        cache="c", version="v1", kind="template")
    env = engine_host._spawn_env(child)
    assert env.get("PYTHONHOME") == "/keep/me"
