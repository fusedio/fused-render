"""`create_app`'s startup/shutdown contract, after the move off `on_event`.

FastAPI deprecated `@app.on_event`, and `app.py` had 19 of them. Replacing
them is only safe if the replacement keeps what the old decorator actually
did rather than what it looks like it did — so the two details that are easy
to get wrong are pinned here rather than left to a reviewer's memory of
`fastapi/routing.py`.

Driven through `asyncio.run` rather than an async test: nothing else in this
suite needs `pytest-asyncio`, and one file is a poor reason to add a plugin
every test run then has to load.
"""

import asyncio
import os
import time

import pytest

from fused_render.server import create_app
from fused_render.server.app import _lifespan

#: Same real, spawnable fixture daemon test_background_apps.py uses — stdlib
#: only, binds :0, publishes {port, token, pid} (see its own docstring).
FIXTURE_APP = os.path.join(os.path.dirname(__file__), "fixtures", "background_app")


def _drive(startup, shutdown, body=None):
    """Enter and leave one lifespan, optionally raising inside it."""

    async def go():
        async with _lifespan(startup, shutdown)(app=None):
            if body is not None:
                body()

    asyncio.run(go())


def _recorder(log, name, boom=None):
    async def handler():
        log.append(name)
        if boom is not None:
            raise boom

    return handler


def test_startup_handlers_run_in_registration_order():
    log = []
    _drive([_recorder(log, "a"), _recorder(log, "b"), _recorder(log, "c")], [])
    assert log == ["a", "b", "c"]


def test_shutdown_handlers_run_in_registration_order_NOT_reversed():
    """The one most likely to be "tidied up" by a future reader.

    Teardown conventionally unwinds in reverse, and `on_event` did not:
    `_shutdown` iterates `self.on_shutdown` forwards. Reversing it here would
    change the order 7 real handlers close things in, while looking like a
    cleanup — so it is asserted, with the reason attached.
    """
    log = []
    _drive([], [_recorder(log, "a"), _recorder(log, "b"), _recorder(log, "c")])
    assert log == ["a", "b", "c"]
    assert log != ["c", "b", "a"], "shutdown was reversed; on_event never did that"


def test_shutdown_still_runs_when_the_application_raised():
    """`__aexit__` ran regardless once `__aenter__` had returned. A bare
    `yield` with no `finally` would quietly stop doing this, and nothing else
    in the suite would notice — the server would simply stop cleaning up
    after a crash."""
    log = []

    def boom():
        raise RuntimeError("the app fell over")

    with pytest.raises(RuntimeError, match="fell over"):
        _drive([_recorder(log, "up")], [_recorder(log, "down")], body=boom)
    assert log == ["up", "down"]


def test_a_failing_startup_skips_shutdown_entirely():
    """The other side: `__aexit__` was never reached if `__aenter__` raised,
    so a half-started app must not run teardown for things that never came
    up. This is why the `try` opens after the startup loop, not around it."""
    log = []
    handlers = [
        _recorder(log, "first"),
        _recorder(log, "second", boom=RuntimeError("nope")),
    ]
    with pytest.raises(RuntimeError, match="nope"):
        _drive(handlers, [_recorder(log, "down")])
    assert log == ["first", "second"], "shutdown ran for an app that never started"


def test_handlers_registered_after_the_app_is_built_are_still_picked_up():
    """`create_app` hands the lifespan to `FastAPI(...)` at the top and then
    registers handlers for another 450 lines. The lists are read at START,
    which is what makes that work."""
    log = []
    startup: list = []
    lifespan = _lifespan(startup, [])
    startup.append(_recorder(log, "late"))  # …registered after the fact

    async def go():
        async with lifespan(app=None):
            pass

    asyncio.run(go())
    assert log == ["late"]


#: Exactly what `@app.on_event` registered, in order, on the commit before the
#: migration — read off `app.router.on_startup` / `.on_shutdown` there rather
#: than transcribed from the decorators. This is the ground truth the move had
#: to preserve, and the only thing that checks the real 19 handlers rather than
#: the mechanism driving them.
EXPECTED_STARTUP = [
    "_startup_prewarm_ai",
    "_startup_warm_engine",
    "_startup_resurrect_background_apps",
    "_startup_sync_user_plugin",
    # Added 2026-10-16: kicks the workbench skills fetch for Fused Web's
    # `~/Fused/canvas`, if it already exists, so a task created there right
    # after a restart usually finds the clone already on disk.
    "_startup_kick_fused_web_workbench_skills",
    # Added 2026-09-17 (PR 2): the project queue's factory is registered here
    # explicitly. Also claims the machine-duties lease (B2) and, only for the
    # process that wins it, starts the scheduler and reconciles once so a
    # restart resumes every folder's line — folded in rather than left as a
    # separate `_startup_schedule` hook, since both duties now share one
    # lease and deciding that only once, in one place, is the whole point.
    "_startup_queue_manager",
    "_startup_tasks_watch",
    "_startup_tasks_warm",
    "_startup_ai_hub_metadata_refresh",
    "_startup_ai_hub_catalog_refresh",
    "_startup_resource_trail",
    "_startup_gc_project_venvs",
    # TEMPORARY upgrade shim (legacy_mounts_cleanup.py); a startup hook so lean
    # and bare create_app() never run it.
    "_startup_legacy_mounts_cleanup",
    # Added 2026-10-06: the Bots sub-app's scheduler + iMessage bridge
    # (fused_render/bots/registry.py), registered beside its router include.
    "_startup_bots",
    # Added: code review finding 1 — nothing else ever called the shim's
    # `rules` action, so share_file's catalog cache was never built and
    # every extension but the built-in `.fused` refused to share.
    "_startup_warm_share_rules",
    "_startup_index_scan",
    # Added 2026-09-21: the live filesystem watcher (index_watch.py) that
    # replaces time-based staleness guessing with an actual change feed.
    "_startup_index_watch",
    "_startup_update_dev_manager",
]

#: `_startup_shutdown_ai` is a SHUTDOWN handler despite the name — read the
#: decorator, not the name. Kept as-is so this list stays a faithful record of
#: what ran before rather than a tidied version of it.
EXPECTED_SHUTDOWN = [
    "_shutdown_background_apps_resurrection",
    "_startup_shutdown_ai",
    "_shutdown_server_json",
    "_shutdown_resource_trail",
    "_shutdown_captures",
    "_shutdown_ai_workers",
    "_shutdown_engines",
    # Status-bar terminal (PLAN-status-bar-terminal.md): registered where
    # `terminal_router` is included in `create_app`, well after the handlers
    # above — reaps every live pty session (pty_session.py's
    # PtySessionRegistry.shutdown_all) so a server restart never leaves an
    # orphaned shell running.
    "_shutdown_terminal_sessions",
    # Added 2026-10-06: every loaded bot's task and Chrome (bots/registry.py).
    "_shutdown_bots",
    "_shutdown_index_watch",
]


def test_every_handler_is_collected_in_the_order_on_event_had():
    """The wiring, not the mechanism.

    Every other test here drives `_lifespan` with fake handlers, which proves
    the contract but not that the real hooks reach it. A decorator swapped to
    the wrong collector, or a handler moved, would pass all of them and change
    what the server does at startup.
    """
    app = create_app(start_dir=".")
    assert [f.__name__ for f in app.state.startup_handlers] == EXPECTED_STARTUP
    assert [f.__name__ for f in app.state.shutdown_handlers] == EXPECTED_SHUTDOWN


def test_create_app_registers_nothing_on_the_deprecated_path():
    """The regression guard for the migration itself: one `@app.on_event`
    creeping back in would re-open the deprecation and go unnoticed, since
    the handler would still run."""
    app = create_app(start_dir=".")
    assert app.router.on_startup == []
    assert app.router.on_shutdown == []


#: The startup handlers registered even in `lean` (`on_startup_always` in
#: app.py).
EXPECTED_STARTUP_LEAN = [
    # Task scheduling must work in lean apps, and the machine-duties lease only
    # hands over promptly if every live process is parked on it from startup.
    "_startup_queue_manager",
    "_startup_tasks_watch",
]

#: The shutdown handlers registered even in `lean` — anything that can start
#: from an ordinary request (an engine, a local AI worker, a terminal
#: session, a capture) rather than only from the
#: `@on_startup` warm-up `lean` skips. Order matches their position in
#: EXPECTED_SHUTDOWN above. `_startup_shutdown_ai` moved in here too: the
#: `_AI_SESSION` it tears down is constructed at import time and can be
#: started by an ordinary `/api/ai` request regardless of `lean` — only its
#: `@on_startup` PREWARM is lean-skipped, not the session object itself.
EXPECTED_SHUTDOWN_LEAN = [
    "_startup_shutdown_ai",
    "_shutdown_captures",
    "_shutdown_ai_workers",
    "_shutdown_engines",
    "_shutdown_terminal_sessions",
    # A request can start a bot's Chrome in a lean app too.
    "_shutdown_bots",
]


def test_lean_registers_no_startup_handlers_but_always_runs_cleanup():
    """`fused-render open`'s server (`create_app(..., lean=True)`): every one
    of the startup handlers is still DEFINED (so a lean build stays
    otherwise identical code), and all but the `on_startup_always` ones are
    never collected — `_lifespan` then iterates a near-empty startup list and
    starts nothing in the background.

    Shutdown is NOT symmetric: `lean` only ever skips STARTUP (eager warm-up)
    work. An engine, a local AI worker, a terminal session or a capture can
    all still start ON DEMAND from an ordinary
    request in a lean app even though their `@on_startup` warm-up never ran
    — so their cleanup (`on_shutdown_always`) is registered regardless of
    `lean`, and everything else (paired only with a startup hook that lean
    skips, so nothing of theirs can have started) stays skipped."""
    app = create_app(start_dir=".", lean=True)
    assert [f.__name__ for f in app.state.startup_handlers] == EXPECTED_STARTUP_LEAN
    assert [f.__name__ for f in app.state.shutdown_handlers] == EXPECTED_SHUTDOWN_LEAN


def test_lean_still_serves_ordinary_routes():
    """The routers are wired regardless of `lean` — only the background
    side effects are skipped — so a request against a lean app still gets a
    real response rather than a 404."""
    from starlette.testclient import TestClient

    app = create_app(start_dir=".", lean=True)
    with TestClient(app) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200


def _alive(p: int) -> bool:
    """Is this pid a live process? `tasks_watch._pid_alive`, the production
    probe."""
    from fused_render import tasks_watch

    return tasks_watch._pid_alive(p)


def test_lean_app_leaves_no_engine_process_after_a_request_starts_one(tmp_path, monkeypatch):
    """The proof for the shutdown-leak fix: `_startup_resurrect_background_apps`
    (`@on_startup`) never runs in a lean server — nothing is brought back at
    boot — but `/api/apps/background/start` is an ORDINARY request, wired
    whether or not `lean`, and it spawns a real child process the same way a
    template daemon or a `runPython`/`fused.run` call against a `main =` app
    would. That child must not survive lifespan shutdown just because `lean`
    skipped the warm-up that would have started it a different way — this is
    exactly the leak `_shutdown_engines` moving to `on_shutdown_always` fixes.

    Drives the real ASGI lifespan (TestClient's context manager), not a fake
    handler list, so a regression in the real registration — not just in the
    unit-level handler-list assertions above — would be caught here too."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    app = create_app(start_dir=str(tmp_path), lean=True)

    with TestClient(app) as client:
        resp = client.post(
            "/api/apps/background/start",
            json={"html": os.path.join(FIXTURE_APP, "index.html")},
            headers={"X-Fused": "1"},
        )
        assert resp.status_code == 200, resp.text
        pid = resp.json()["pid"]

        assert _alive(pid), "the fixture daemon never actually started"

    # TestClient's `with` block has now run ASGI shutdown for real. Reaping a
    # just-killed child can lag a beat behind SIGTERM, so poll briefly rather
    # than asserting instantly and risking a flaky false failure.
    deadline = time.monotonic() + 5.0
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid), (
        f"pid {pid} (the fixture background-app daemon) outlived lifespan "
        "shutdown in a lean app")


def test_lean_shutdown_is_a_noop_when_no_ai_session_ever_started(tmp_path, monkeypatch):
    """`_startup_shutdown_ai` now runs in every lean app (EXPECTED_SHUTDOWN_LEAN
    above), including the common case where nothing ever touched `/api/ai`.
    `_AiSession.shutdown()` must tolerate that — no process, no spawn task —
    rather than raising and breaking an otherwise-ordinary lean server's
    teardown."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    app = create_app(start_dir=str(tmp_path), lean=True)

    with TestClient(app) as client:
        resp = client.get("/api/config")
        assert resp.status_code == 200
    # No AssertionError/exception escaping the `with` block is the assertion:
    # lifespan shutdown (including _startup_shutdown_ai) completed cleanly.


def test_lean_teardown_never_touches_the_module_global_ai_lock(tmp_path, monkeypatch):
    """A lean app never sets `app.state.ai_session`, so teardown used to fall
    through to the module-global `_AI_SESSION` and acquire its `asyncio.Lock`
    on every shutdown. That lock binds to whichever event loop contends it, so
    a later TestClient on a new loop hit "is bound to a different event loop".
    A global session nothing ever started has nothing to tear down: skip it."""
    from starlette.testclient import TestClient

    from fused_render.server import ai as ai_mod

    acquired = []

    class _TrackingLock:
        async def __aenter__(self):
            acquired.append(True)

        async def __aexit__(self, *exc):
            return False

    session = ai_mod._AiSession()
    session.lock = _TrackingLock()
    monkeypatch.setattr(ai_mod, "_AI_SESSION", session)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    app = create_app(start_dir=str(tmp_path), lean=True)
    assert getattr(app.state, "ai_session", None) is None

    with TestClient(app) as client:
        assert client.get("/api/config").status_code == 200

    assert acquired == [], "lean teardown acquired the module-global AI lock"


def test_lean_app_shuts_down_an_ai_session_started_by_an_ordinary_request(tmp_path, monkeypatch):
    """The AI-cleanup analogue of
    `test_lean_app_leaves_no_engine_process_after_a_request_starts_one`:
    `_startup_prewarm_ai` (`@on_startup`) never runs in a lean server, but
    `app.state.ai_session` can still come alive from an ordinary request —
    here simulated directly, since driving a real `/api/ai` call would spawn
    an actual `claude` CLI process. Lifespan shutdown must reach and close it
    even though its warm-up hook never ran."""
    from starlette.testclient import TestClient

    from fused_render.server.ai import _AiSession

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    app = create_app(start_dir=str(tmp_path), lean=True)

    shutdown_calls = []
    session = _AiSession()

    async def fake_shutdown():
        shutdown_calls.append(True)

    session.shutdown = fake_shutdown

    with TestClient(app) as client:
        app.state.ai_session = session
        resp = client.get("/api/config")
        assert resp.status_code == 200

    assert shutdown_calls, (
        "lean shutdown never reached the on-demand AI session's shutdown()")


def test_a_slow_startup_handler_is_named_in_the_log(monkeypatch, caplog):
    """The desktop app quits itself when startup outlasts its readiness
    cutoff; the one fact that incident needs is WHICH handler was still
    running (2026-10-13: six silent self-quits on a busy Mac). Every handler
    at or over `STARTUP_SLOW_HANDLER_S` gets a line with its name, the fast
    ones stay quiet, and one summary line always closes the startup."""
    import logging

    from fused_render.server import app as server_app

    monkeypatch.setattr(server_app, "STARTUP_SLOW_HANDLER_S", 0.3)

    async def _startup_quick():
        pass

    async def _startup_slow():
        await asyncio.sleep(0.6)

    with caplog.at_level(logging.INFO, logger=server_app.logger.name):
        _drive([_startup_quick, _startup_slow], [])

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("startup:")]
    # The line that matters for the incident: written while the handler is
    # still running, so a handler that never returns is still named.
    assert "startup: _startup_slow still running after 0s" in lines, lines
    assert any(l.startswith("startup: _startup_slow took ") for l in lines), lines
    assert not any("_startup_quick" in l for l in lines), lines
    assert lines[-1].startswith("startup: 2 handler(s) in "), lines


def test_relauncher_outlives_a_slow_but_healthy_successor():
    """The relauncher's deadline covers a relaunch teardown plus a successor
    that takes every second of SERVER_READY_TIMEOUT_S, and still ends before
    the page gives up on the restart (RESTART_GIVE_UP_MS, 120 s)."""
    from fused_render import app as app_mod

    assert (app_mod.RELAUNCH_DEADLINE_S
            >= app_mod.QUIT_FAST_HARD_DEADLINE_S + app_mod.SERVER_READY_TIMEOUT_S + 5)
    assert app_mod.RELAUNCH_DEADLINE_S < 120


def _grab_startup_handler(tmp_path, monkeypatch, name: str):
    """The real handler `create_app` registers under `name`, via the same spy
    `test_agent_backends_plumbing.py`'s `_startup_sync_user_plugin` test uses:
    `_lifespan` is intercepted at the point `create_app` calls it, which is
    after every `@on_startup` decorator ran, so the closures it was handed are
    the real ones — not a copy a unit test re-derives."""
    import asyncio

    from fused_render.server import app as app_mod

    captured = {}
    real = app_mod._lifespan

    def spy(startup, shutdown):
        captured["startup"] = startup
        return real(startup, shutdown)

    monkeypatch.setattr(app_mod, "_lifespan", spy)
    app_mod.create_app(start_dir=str(tmp_path))
    handler, = [h for h in captured["startup"] if h.__name__ == name]
    return handler, asyncio.run


def test_startup_kicks_the_fused_web_workbench_fetch_when_the_folder_exists(
        tmp_path, monkeypatch):
    """Server startup covers the case `api_task_create`'s own kick cannot: a
    `~/Fused/canvas` folder that already existed BEFORE this process came up,
    so the first Fused Web task after a restart usually finds the clone
    already on disk rather than paying for a cold one."""
    from fused_render import canvases as canvases_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / "Fused" / "canvas").mkdir(parents=True)

    kicked = []
    monkeypatch.setattr(canvases_mod, "_kick_workbench_skills",
                        lambda: kicked.append(1))
    handler, run = _grab_startup_handler(
        tmp_path, monkeypatch, "_startup_kick_fused_web_workbench_skills")
    run(handler())
    assert kicked == [1]


def test_startup_does_not_kick_when_fused_web_canvas_is_absent(tmp_path, monkeypatch):
    from fused_render import canvases as canvases_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # No ~/Fused/canvas folder at all.

    kicked = []
    monkeypatch.setattr(canvases_mod, "_kick_workbench_skills",
                        lambda: kicked.append(1))
    handler, run = _grab_startup_handler(
        tmp_path, monkeypatch, "_startup_kick_fused_web_workbench_skills")
    run(handler())
    assert kicked == []


def test_startup_kick_failure_never_fails_startup(tmp_path, monkeypatch):
    from fused_render import canvases as canvases_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / "Fused" / "canvas").mkdir(parents=True)

    def boom():
        raise RuntimeError("no thread for you")
    monkeypatch.setattr(canvases_mod, "_kick_workbench_skills", boom)
    handler, run = _grab_startup_handler(
        tmp_path, monkeypatch, "_startup_kick_fused_web_workbench_skills")
    run(handler())  # must not raise
