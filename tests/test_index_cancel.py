"""CancelToken: the plumbing that lets a rank request outlived by its client
actually stop, rather than running to completion on an abandoned connection.

`asyncio.to_thread` (server/routers/index.py) cannot kill the worker thread it
dispatches onto, so cancellation has to be cooperative — these tests are
about the token itself; `search_ranked`'s use of it (the phase-boundary
`check()` calls, the `duckdb.InterruptException` -> `Cancelled` translation)
is covered in tests/test_index_search.py alongside the rest of that
function's tests.
"""
import pytest

from fused_render.index.cancel import CancelToken, Cancelled


class _FakeConnection:
    """Stands in for a duckdb connection: `interrupt()` calls are what these
    tests assert on, and nothing here needs a real database."""

    def __init__(self):
        self.interrupts = 0

    def interrupt(self):
        self.interrupts += 1


def test_check_is_a_no_op_until_cancelled():
    token = CancelToken()
    token.check()  # must not raise
    assert token.cancelled is False


def test_cancel_makes_check_raise():
    token = CancelToken()
    token.cancel()
    assert token.cancelled is True
    with pytest.raises(Cancelled):
        token.check()


def test_cancel_interrupts_a_bound_connection():
    token = CancelToken()
    con = _FakeConnection()
    token.bind(con)
    assert con.interrupts == 0
    token.cancel()
    assert con.interrupts == 1


def test_a_token_cancelled_BEFORE_bind_still_stops_the_connection():
    """Ordering the plan calls out explicitly: a fast abort that lands before
    `bind()` (the connect is still in flight) must not be lost — `bind` has to
    notice the flag is already set and interrupt immediately, or the query
    would run uninterrupted to completion."""
    token = CancelToken()
    token.cancel()
    con = _FakeConnection()
    assert con.interrupts == 0
    token.bind(con)
    assert con.interrupts == 1


def test_cancel_with_nothing_bound_yet_does_not_raise():
    # No connection to interrupt — must be a safe no-op, not an AttributeError.
    token = CancelToken()
    token.cancel()
    assert token.cancelled is True


def test_binding_a_second_connection_only_interrupts_the_current_one():
    # `bind` is called once per connect; the token only ever knows about the
    # LATEST connection, which is the only one search_ranked ever has live.
    token = CancelToken()
    first = _FakeConnection()
    token.bind(first)
    second = _FakeConnection()
    token.bind(second)
    token.cancel()
    assert first.interrupts == 0
    assert second.interrupts == 1


def test_unbind_stops_a_later_cancel_from_touching_the_connection():
    """The disconnect-lands-after-close race (server/routers/index.py):
    `_watch_disconnect` can call `token.cancel()` after `search_ranked` has
    already returned and closed its connection. `unbind()` — called in
    `search_ranked`'s `finally`, right before `con.close()` — is what a later
    `cancel()` needs to see so it does not call `interrupt()` on a connection
    that is closed, or about to be: a real duckdb connection raises
    `ConnectionException` for that, same shape of bug
    guarded_query.py's own `timer.cancel()`-before-`close()` ordering already
    exists to avoid for its Timer."""
    token = CancelToken()
    con = _FakeConnection()
    token.bind(con)
    token.unbind()
    token.cancel()
    assert con.interrupts == 0
    # `cancelled` still flips — `check()` (the between-phase guard) must keep
    # working even once the connection is gone.
    assert token.cancelled is True


def test_unbind_with_nothing_bound_is_a_safe_no_op():
    token = CancelToken()
    token.unbind()  # must not raise
    token.cancel()
    assert token.cancelled is True
