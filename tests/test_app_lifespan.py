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

import pytest

from fused_render.server import create_app
from fused_render.server.app import _lifespan


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
    "_startup_pooled_client",
    "_startup_prewarm_ai",
    "_startup_warm_engine",
    "_startup_resurrect_background_apps",
    "_startup_sync_user_plugin",
    "_startup_schedule",
    "_startup_tasks_watch",
    "_startup_ai_idle_reaper",
    "_startup_ai_hardware_refresh",
    "_startup_ai_hub_metadata_refresh",
    "_startup_gc_project_venvs",
    "_startup_index_scan",
]

#: `_startup_shutdown_ai` is a SHUTDOWN handler despite the name — read the
#: decorator, not the name. Kept as-is so this list stays a faithful record of
#: what ran before rather than a tidied version of it.
EXPECTED_SHUTDOWN = [
    "_shutdown_pooled_client",
    "_shutdown_background_apps_resurrection",
    "_startup_shutdown_ai",
    "_shutdown_server_json",
    "_shutdown_captures",
    "_shutdown_ai_workers",
    "_shutdown_engines",
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
