"""The macOS quit teardown (app.py `_do_quit` -> `quit_teardown`/`start_quit`).

Three defects were funnelling through the old one-liner quit (INCIDENT
2026-07-29, crash report FusedRender-2026-07-29-135823.ips):

  A. rcd was SIGTERM/SIGKILLed with live kernel NFS mounts still attached, so
     the NFS server vanished under its own client and macOS raised "server
     connection interrupted / disks not ejected properly". The quit path was
     the one mount teardown in the tree that skipped the rc-unmount ->
     force-unmount ladder every other path goes through.
  B. the whole reap ran synchronously inside a menu-item action, i.e. with the
     AppKit run loop blocked — up to ~13s of beachball by construction.
  C. `rumps.quit_application()` -> `NSApplication.terminate:` -> C `exit()`
     runs C++ static destructors with the GIL RELEASED (pyobjc drops it for the
     duration of the ObjC call), and the duckdb reader's cached
     `DuckDBPyConnection` destructs there, touching the Python C-API ->
     Py_FatalError -> abort().

These tests pin the fix: the ordering (server drain -> duckdb stash close ->
unmount -> reap rcd), the non-blocking entry point with its hard deadline, and
per-mount isolation. Nothing macOS-only is exercised — rumps/AppKit are never
imported (app.py imports them lazily inside `main()`), and the mount ladder is
faked at the rc/subprocess boundary exactly like tests/test_shell_mounts.py and
tests/test_mounts_rcd_owner.py do, so no real rclone, mount or `umount` runs.
"""
import importlib.util
import os
import sys
import types

import pytest

import fused_render.app as app_mod


# --------------------------------------------------------------- duckdb stash
# Defect C. The stash lives on the *duckdb module* (see reader._http_connection),
# which is why quit can reach it at all: the reader module object itself is
# transient (executor._run_inprocess never puts it in sys.modules), so the
# connection outlives every reader run and nothing ever closed it.


def _load_reader():
    """The duckdb template's reader.py, loaded by path (it isn't importable —
    templates/ is deliberately not a package). Same loader as
    tests/test_duckdb_reader.py."""
    path = os.path.join(os.path.dirname(__file__), "..", "fused_render",
                        "templates", "duckdb", "reader.py")
    spec = importlib.util.spec_from_file_location("duckdb_reader_quit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeCon:
    def __init__(self, raises=False):
        self.closed = 0
        self._raises = raises

    def close(self):
        self.closed += 1
        if self._raises:
            raise RuntimeError("connection already invalidated")


@pytest.fixture()
def reader(monkeypatch):
    """reader.py with a guaranteed-clean stash slot on the real duckdb module.
    monkeypatch.delattr/setattr restores whatever was there, so a stash a
    previous test in this worker created can't leak in (or out)."""
    pytest.importorskip("duckdb")
    mod = _load_reader()
    monkeypatch.delattr(mod.duckdb, mod._HTTP_CON_KEY, raising=False)
    return mod


def test_close_http_connection_closes_and_clears_the_stash(reader):
    con = _FakeCon()
    setattr(reader.duckdb, reader._HTTP_CON_KEY, con)

    assert reader.close_http_connection() is True

    assert con.closed == 1
    # Cleared, not merely closed: a closed-but-reachable DuckDBPyConnection is
    # still a live object for exit()'s static destructors to trip over, and
    # _http_connection would hand out cursors from it.
    assert not hasattr(reader.duckdb, reader._HTTP_CON_KEY)


def test_close_http_connection_is_a_no_op_without_a_stash(reader):
    # Never previewed a mounted parquet file this run — the common case.
    assert reader.close_http_connection() is False


def test_close_http_connection_survives_a_raising_close(reader):
    con = _FakeCon(raises=True)
    setattr(reader.duckdb, reader._HTTP_CON_KEY, con)

    assert reader.close_http_connection() is False

    # The stash is dropped either way: quit must not leave the connection
    # reachable just because close() complained.
    assert not hasattr(reader.duckdb, reader._HTTP_CON_KEY)


def test_http_connection_stashes_under_the_shared_key(reader, monkeypatch):
    """_http_connection and close_http_connection must agree on the key by
    construction (one constant), not by two matching string literals."""
    made = _FakeCon()
    made.cursor = lambda: "cursor"
    monkeypatch.setattr(reader.duckdb, "connect", lambda *a, **k: made,
                        raising=False)
    made.execute = lambda sql: None

    assert reader._http_connection() == "cursor"
    assert getattr(reader.duckdb, reader._HTTP_CON_KEY) is made

    assert reader.close_http_connection() is True


# ---- the app-side call: never blocks, never raises, never loads duckdb itself


def test_quit_close_of_the_stash_reaches_the_reader(monkeypatch):
    called = []
    stub = types.ModuleType("__fused_duckdb_reader_stub__")
    stub.close_http_connection = lambda: called.append(True) or True
    monkeypatch.setitem(sys.modules, "duckdb", types.ModuleType("duckdb"))
    monkeypatch.setattr(app_mod, "_load_duckdb_reader", lambda: stub)

    app_mod._close_duckdb_stash()

    assert called == [True]


def test_quit_close_skips_everything_when_duckdb_was_never_imported(monkeypatch):
    # No duckdb in sys.modules == no connection can exist, so quit must not pay
    # a (multi-hundred-ms) duckdb import just to find nothing.
    loaded = []
    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    monkeypatch.setattr(app_mod, "_load_duckdb_reader",
                        lambda: loaded.append(True))

    app_mod._close_duckdb_stash()

    assert loaded == []


def test_quit_close_swallows_a_reader_that_cannot_be_loaded(monkeypatch, tmp_path):
    # duckdb not installed / a mangled reader.py: an ImportError here must not
    # take the quit path down with it.
    monkeypatch.setitem(sys.modules, "duckdb", types.ModuleType("duckdb"))
    monkeypatch.setattr(app_mod, "_DUCKDB_READER_PATH",
                        str(tmp_path / "does-not-exist.py"))

    app_mod._close_duckdb_stash()  # must not raise


def test_quit_close_swallows_a_raising_reader_hook(monkeypatch):
    stub = types.ModuleType("__fused_duckdb_reader_stub__")

    def _boom():
        raise RuntimeError("duckdb is wedged")

    stub.close_http_connection = _boom
    monkeypatch.setitem(sys.modules, "duckdb", types.ModuleType("duckdb"))
    monkeypatch.setattr(app_mod, "_load_duckdb_reader", lambda: stub)

    app_mod._close_duckdb_stash()  # must not raise
