"""Background apps (fused_render/background_apps.py): the folder manifest
([tool.fused-render.app] in pyproject.toml), the enabled-store persisted at
~/.fused-render/background_apps.json, engine_id identity, and the version
digest that retires a child when the manifest, daemon file, or interpreter
changes. Also covers engine_host's "background" child kind: validated
against the enabled store, and exempt from the warm-app idle reaper.
"""
import os
import sys
import threading
import time

import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient

from fused_render import background_apps
from fused_render.server import create_app, engine_host

FIXTURE_APP = os.path.join(os.path.dirname(__file__), "fixtures", "background_app")
HDRS = {"X-Fused": "1"}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # The enabled store lives in the shell home; isolate it per test the same
    # way test_registered_apps.py does.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def _make_app(tmp_path, name="app", *, kind="background", daemon="daemon.py",
              daemon_outside=False, table=True):
    folder = tmp_path / name
    folder.mkdir()
    if table:
        lines = ["[tool.fused-render.app]"]
        if kind is not None:
            lines.append(f'kind = "{kind}"')
        if daemon is not None:
            lines.append(f'daemon = "{daemon}"')
        (folder / "pyproject.toml").write_text("\n".join(lines) + "\n")
    else:
        (folder / "pyproject.toml").write_text("[tool.other]\nx = 1\n")
    if daemon_outside:
        outside = tmp_path / "outside_daemon.py"
        outside.write_text("# daemon\n")
    else:
        (folder / (daemon or "daemon.py")).write_text("# daemon\n")
    return folder


# ------------------------------------------------------------- manifest


def test_load_manifest_accepts_valid_background_app(tmp_path):
    folder = _make_app(tmp_path)
    manifest = background_apps.load_manifest(str(folder))
    assert manifest is not None
    assert manifest.folder == os.path.abspath(str(folder))
    assert manifest.daemon == os.path.abspath(str(folder / "daemon.py"))


def test_load_manifest_rejects_missing_table(tmp_path):
    folder = _make_app(tmp_path, table=False)
    assert background_apps.load_manifest(str(folder)) is None


def test_load_manifest_rejects_missing_pyproject(tmp_path):
    folder = tmp_path / "no_pyproject"
    folder.mkdir()
    assert background_apps.load_manifest(str(folder)) is None


def test_load_manifest_rejects_wrong_kind(tmp_path):
    folder = _make_app(tmp_path, kind="template")
    assert background_apps.load_manifest(str(folder)) is None


def test_load_manifest_rejects_daemon_outside_folder(tmp_path):
    folder = _make_app(tmp_path, daemon="../outside_daemon.py",
                       daemon_outside=True)
    assert background_apps.load_manifest(str(folder)) is None


def test_load_manifest_rejects_missing_daemon_key(tmp_path):
    folder = _make_app(tmp_path, daemon=None)
    assert background_apps.load_manifest(str(folder)) is None


def test_load_manifest_rejects_daemon_naming_a_directory(tmp_path):
    # Code-review fix: `daemon = "."` (or any other directory) passed
    # containment trivially — a folder is "inside" itself — and os.stat
    # succeeds on a directory just like a file, so nothing caught it before
    # engine_host tried to spawn `python <folder>` and failed opaquely.
    folder = tmp_path / "dir_daemon"
    folder.mkdir()
    (folder / "pyproject.toml").write_text(
        '[tool.fused-render.app]\nkind = "background"\ndaemon = "."\n')
    assert background_apps.load_manifest(str(folder)) is None


def test_load_manifest_rejects_daemon_naming_a_subdirectory(tmp_path):
    folder = tmp_path / "subdir_daemon"
    folder.mkdir()
    (folder / "notadaemon").mkdir()
    (folder / "pyproject.toml").write_text(
        '[tool.fused-render.app]\nkind = "background"\ndaemon = "notadaemon"\n')
    assert background_apps.load_manifest(str(folder)) is None


# ------------------------------------------------------------- identity


def test_engine_id_for_is_stable_and_bare(tmp_path):
    folder = _make_app(tmp_path)
    a = background_apps.engine_id_for(str(folder))
    b = background_apps.engine_id_for(str(folder))
    assert a == b
    assert a.startswith("bg_")


def test_engine_id_for_differs_per_folder(tmp_path):
    a = _make_app(tmp_path, "one")
    b = _make_app(tmp_path, "two")
    assert (background_apps.engine_id_for(str(a))
            != background_apps.engine_id_for(str(b)))


# ------------------------------------------------------------- version digest


def test_version_for_changes_with_pyproject_bytes(tmp_path):
    folder = _make_app(tmp_path)
    interpreter = sys.executable  # must exist: version_for now os.stats it
    v1 = background_apps.version_for(str(folder), interpreter)
    (folder / "pyproject.toml").write_text(
        (folder / "pyproject.toml").read_text() + "\nextra = 1\n")
    v2 = background_apps.version_for(str(folder), interpreter)
    assert v1 != v2


def test_version_for_changes_with_daemon_mtime(tmp_path):
    folder = _make_app(tmp_path)
    interpreter = sys.executable  # must exist: version_for now os.stats it
    v1 = background_apps.version_for(str(folder), interpreter)
    daemon = folder / "daemon.py"
    new_time = time.time() + 5
    os.utime(daemon, (new_time, new_time))
    v2 = background_apps.version_for(str(folder), interpreter)
    assert v1 != v2


def test_version_for_changes_with_interpreter_path(tmp_path):
    # Two DIFFERENT files at two different paths (version_for now requires
    # the interpreter to actually exist — os.stat, mirroring the daemon).
    # Bogus paths like "/usr/bin/python3" broke this on Linux CI, where
    # /usr/bin/python3 and /usr/bin/python3.12 are symlinks to the identical
    # file and the old realpath-based digest collapsed them to one identity.
    folder = _make_app(tmp_path)
    interp_a = tmp_path / "python_a"
    interp_a.write_bytes(b"cpython")
    interp_b = tmp_path / "python_b"
    interp_b.write_bytes(b"cpython")  # identical bytes, different PATH
    v1 = background_apps.version_for(str(folder), str(interp_a))
    v2 = background_apps.version_for(str(folder), str(interp_b))
    assert v1 != v2


def test_version_for_changes_with_interpreter_mtime_at_the_same_path(tmp_path):
    # Code-review fix (D495 revised): the exact upgrade-rot case this digest
    # exists for is the packaged interpreter rewritten IN PLACE at the same
    # path — same path, same version string, different bytes/mtime. A
    # path-only component (realpath'd or not) cannot see that; the
    # interpreter now gets an os.stat, exactly like the daemon file two lines
    # above it (test_version_for_changes_with_daemon_mtime's identical shape).
    folder = _make_app(tmp_path)
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"cpython")
    v1 = background_apps.version_for(str(folder), str(interpreter))
    new_time = time.time() + 5
    os.utime(interpreter, (new_time, new_time))
    v2 = background_apps.version_for(str(folder), str(interpreter))
    assert v1 != v2


def test_version_for_does_not_collapse_symlinked_venv_pythons_via_realpath(tmp_path):
    # The regression guard against realpath coming back: two different
    # venvs' bin/python are each a symlink to the SAME base CPython (the
    # normal shape of a venv), which is exactly what made the old
    # os.path.realpath(interpreter) component collapse two distinct venvs
    # into one identity on Linux (/usr/bin/python3 -> /usr/bin/python3.12,
    # both -> the same real binary).
    folder = _make_app(tmp_path)
    base = tmp_path / "base_python"
    base.write_bytes(b"cpython")
    venv_a_bin = tmp_path / "venv_a" / "bin"
    venv_a_bin.mkdir(parents=True)
    python_a = venv_a_bin / "python"
    python_a.symlink_to(base)
    venv_b_bin = tmp_path / "venv_b" / "bin"
    venv_b_bin.mkdir(parents=True)
    python_b = venv_b_bin / "python"
    python_b.symlink_to(base)

    v1 = background_apps.version_for(str(folder), str(python_a))
    v2 = background_apps.version_for(str(folder), str(python_b))
    assert v1 != v2


def test_version_for_raises_on_missing_interpreter(tmp_path):
    folder = _make_app(tmp_path)
    with pytest.raises(OSError):
        background_apps.version_for(str(folder), str(tmp_path / "no_such_python"))


def test_version_for_raises_on_missing_daemon_file(tmp_path):
    folder = _make_app(tmp_path)
    os.remove(folder / "daemon.py")
    with pytest.raises(OSError):
        background_apps.version_for(str(folder), "/usr/bin/python3")


# ------------------------------------------------------------- enabled store


def test_enabled_store_round_trip(tmp_path):
    folder = _make_app(tmp_path)
    assert background_apps.enabled_paths() == []
    background_apps.set_enabled(str(folder), True)
    assert os.path.abspath(str(folder)) in background_apps.enabled_paths()
    background_apps.set_enabled(str(folder), False)
    assert os.path.abspath(str(folder)) not in background_apps.enabled_paths()


def test_enabled_store_skips_missing_folder_but_keeps_entry(tmp_path):
    folder = _make_app(tmp_path)
    background_apps.set_enabled(str(folder), True)
    assert os.path.abspath(str(folder)) in background_apps.enabled_paths()

    # Delete the folder: the entry drops out of the live listing...
    import shutil
    shutil.rmtree(folder)
    assert os.path.abspath(str(folder)) not in background_apps.enabled_paths()

    # ...but the store itself was never rewritten, so recreating the folder
    # brings the entry straight back with no re-enable.
    folder.mkdir()
    assert os.path.abspath(str(folder)) in background_apps.enabled_paths()


def test_enabled_store_re_enable_does_not_duplicate(tmp_path):
    folder = _make_app(tmp_path)
    background_apps.set_enabled(str(folder), True)
    background_apps.set_enabled(str(folder), True)
    assert background_apps.enabled_paths().count(os.path.abspath(str(folder))) == 1


# ---------------------------------------------- engine_host: background kind


def test_ensure_background_rejects_foreign_interpreter(tmp_path):
    daemon = tmp_path / "daemon.py"
    daemon.write_text("# daemon\n")
    with pytest.raises(engine_host.EngineError):
        engine_host.ensure_background(
            "bg_foreign", "/definitely/not/a/real/python", str(daemon),
            str(tmp_path / "cache"), "v1")


def test_ensure_background_rejects_daemon_of_non_enabled_folder(tmp_path):
    # A valid interpreter but no enabled apps at all: the daemon must be
    # refused regardless of how real it looks on disk.
    folder = _make_app(tmp_path)
    daemon = folder / "daemon.py"
    with pytest.raises(engine_host.EngineError):
        engine_host.ensure_background(
            "bg_notenabled", sys.executable, str(daemon),
            str(tmp_path / "cache"), "v1")


def test_ensure_background_rejects_daemon_not_matching_enabled_manifest(tmp_path):
    # One folder IS enabled, but the daemon path handed to ensure_background
    # belongs to a different, non-enabled folder — must still be refused.
    enabled = _make_app(tmp_path, "enabled")
    other = _make_app(tmp_path, "other")
    background_apps.set_enabled(str(enabled), True)
    with pytest.raises(engine_host.EngineError):
        engine_host.ensure_background(
            "bg_wrongdaemon", sys.executable, str(other / "daemon.py"),
            str(tmp_path / "cache"), "v1")


def test_reap_idle_app_workers_skips_background_kind():
    # A background child idle far past APP_IDLE_RETIRE_S must never be
    # reaped — only kind == "app" is eligible (pattern: test_engine_app.py's
    # idle-reaper tests).
    eid = "bg_reapskip"
    child = engine_host.Child(
        engine_id=eid, python=sys.executable, daemon="/tmp/bg-daemon.py",
        cache="unused", version="v1", kind="background")
    child.last_used = time.monotonic() - (engine_host.APP_IDLE_RETIRE_S + 10)
    engine_host._children[eid] = child
    try:
        assert engine_host.reap_idle_app_workers() == 0
        assert eid in engine_host._children
    finally:
        engine_host._children.pop(eid, None)


def test_ensure_background_spawns_and_reuses_the_fixture_daemon():
    background_apps.set_enabled(FIXTURE_APP, True)
    manifest = background_apps.load_manifest(FIXTURE_APP)
    assert manifest is not None
    engine_id = background_apps.engine_id_for(FIXTURE_APP)
    version = background_apps.version_for(FIXTURE_APP, sys.executable)
    cache = os.path.join(
        os.environ["FUSED_RENDER_HOME"], "apps", engine_id)

    child = engine_host.ensure_background(
        engine_id, sys.executable, manifest.daemon, cache, version)
    try:
        assert child.kind == "background"
        assert engine_host._ping(child)

        # A second call with the same identity reuses the live child rather
        # than spawning a new process.
        again = engine_host.ensure_background(
            engine_id, sys.executable, manifest.daemon, cache, version)
        assert again is child
    finally:
        engine_host.stop(engine_id)
        background_apps.set_enabled(FIXTURE_APP, False)


# ------------------------------------------------- router: enable/disable/etc


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


@pytest.fixture()
def client(tmp_path, workspace):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _bg_folder(tmp_path, name="app"):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "pyproject.toml").write_text(
        '[tool.fused-render.app]\nkind = "background"\ndaemon = "daemon.py"\n')
    (folder / "daemon.py").write_text("# daemon\n")
    (folder / "index.html").write_text("<html></html>")
    return folder


def test_api_enable_persists_and_calls_ensure_background(client, tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)

    calls = []
    fake_child = engine_host.Child(
        engine_id="bg_fake", python=sys.executable, daemon=str(folder / "daemon.py"),
        cache="c", version="v1", kind="background", pid=4242)

    def fake_ensure(engine_id, python, daemon, cache, version):
        calls.append((engine_id, python, daemon, cache, version))
        return fake_child

    monkeypatch.setattr(engine_host, "ensure_background", fake_ensure)

    resp = client.post("/api/apps/background/enable", json={"html": html}, headers=HDRS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["pid"] == 4242
    assert len(calls) == 1
    assert calls[0][2] == str(folder / "daemon.py")
    assert os.path.abspath(str(folder)) in background_apps.enabled_paths()


def test_api_enable_requires_x_fused_header(client, tmp_path):
    folder = _bg_folder(tmp_path)
    resp = client.post("/api/apps/background/enable",
                       json={"html": str(folder / "index.html")})
    assert resp.status_code == 403
    assert os.path.abspath(str(folder)) not in background_apps.enabled_paths()


def test_api_enable_409_when_project_venv_not_built(client, tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    monkeypatch.setattr(background_apps, "interpreter_for",
                        lambda f: "/definitely/not/a/real/python")

    resp = client.post("/api/apps/background/enable", json={"html": html}, headers=HDRS)
    assert resp.status_code == 409
    # A 409 must not persist — the app is not enabled until it can actually start.
    assert os.path.abspath(str(folder)) not in background_apps.enabled_paths()


def test_api_disable_stops_and_unpersists(client, tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    background_apps.set_enabled(str(folder), True)
    engine_id = background_apps.engine_id_for(str(folder))

    stopped = []
    monkeypatch.setattr(engine_host, "stop", lambda eid: stopped.append(eid))

    resp = client.post("/api/apps/background/disable", json={"html": html}, headers=HDRS)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert stopped == [engine_id]
    assert os.path.abspath(str(folder)) not in background_apps.enabled_paths()


def test_api_stop_kills_without_disabling(client, tmp_path, monkeypatch):
    # The whole point of the stop/disable split: stop must kill the process
    # but leave the folder enabled, so the startup hook brings it back.
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    background_apps.set_enabled(str(folder), True)
    engine_id = background_apps.engine_id_for(str(folder))

    stopped = []
    monkeypatch.setattr(engine_host, "stop", lambda eid: stopped.append(eid))

    resp = client.post("/api/apps/background/stop", json={"html": html}, headers=HDRS)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert stopped == [engine_id]
    assert os.path.abspath(str(folder)) in background_apps.enabled_paths()


def test_api_stop_vs_disable_distinguished(client, tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    background_apps.set_enabled(str(folder), True)
    monkeypatch.setattr(engine_host, "stop", lambda eid: None)

    client.post("/api/apps/background/stop", json={"html": html}, headers=HDRS)
    assert os.path.abspath(str(folder)) in background_apps.enabled_paths()

    client.post("/api/apps/background/disable", json={"html": html}, headers=HDRS)
    assert os.path.abspath(str(folder)) not in background_apps.enabled_paths()


def test_api_restart_after_stop_falls_back_to_a_fresh_bring_up(client, tmp_path, monkeypatch):
    # Code-review fix: engine_host.restart() alone raises "has never been
    # started" once stop() has popped the child from _children, which broke
    # the documented stop() -> restart() recovery path with an opaque 502.
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    background_apps.set_enabled(str(folder), True)
    engine_id = background_apps.engine_id_for(str(folder))
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)
    monkeypatch.setattr(engine_host, "current", lambda eid: None)  # stopped: no live child

    def fail_restart(eid, failed=None):
        raise engine_host.EngineError(f"the {eid} engine has never been started")

    monkeypatch.setattr(engine_host, "restart", fail_restart)

    ensured = []
    fake_child = engine_host.Child(
        engine_id=engine_id, python=sys.executable, daemon=str(folder / "daemon.py"),
        cache="c", version="v1", kind="background", pid=9191)

    def fake_ensure(eid, python, daemon, cache, version):
        ensured.append(eid)
        return fake_child

    monkeypatch.setattr(engine_host, "ensure_background", fake_ensure)

    resp = client.post("/api/apps/background/restart", json={"html": html}, headers=HDRS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["pid"] == 9191
    assert ensured == [engine_id]  # fell back to a fresh bring-up, not engine_host.restart


def test_api_status_reflects_a_faked_live_child(client, tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    engine_id = background_apps.engine_id_for(str(folder))
    background_apps.set_enabled(str(folder), True)

    fake_child = engine_host.Child(
        engine_id=engine_id, python=sys.executable, daemon="d", cache="c",
        version="v9", kind="background", pid=555)
    monkeypatch.setattr(engine_host, "current",
                        lambda eid: fake_child if eid == engine_id else None)
    monkeypatch.setattr(engine_host, "_alive", lambda c: True)

    resp = client.get("/api/apps/background/status", params={"html": html})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["running"] is True
    assert body["pid"] == 555
    assert body["version"] == "v9"
    assert body["engine_id"] == engine_id


def test_api_status_not_running_when_no_live_child(client, tmp_path):
    folder = _bg_folder(tmp_path)
    resp = client.get("/api/apps/background/status",
                      params={"html": str(folder / "index.html")})
    body = resp.json()
    assert body["enabled"] is False
    assert body["running"] is False
    assert body["pid"] is None


# ---------------------------------------------------------------- resurrection


def test_resurrect_enabled_starts_every_app_and_survives_one_raising(tmp_path, monkeypatch):
    good = _bg_folder(tmp_path, "good")
    bad = _bg_folder(tmp_path, "bad")
    background_apps.set_enabled(str(good), True)
    background_apps.set_enabled(str(bad), True)

    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)

    started = []

    def fake_ensure(engine_id, python, daemon, cache, version):
        if "bad" in daemon:
            raise engine_host.EngineError("boom")
        started.append(engine_id)
        return engine_host.Child(engine_id=engine_id, python=python, daemon=daemon,
                                 cache=cache, version=version, kind="background")

    monkeypatch.setattr(engine_host, "ensure_background", fake_ensure)

    background_apps.resurrect_enabled()  # must not raise despite "bad" failing

    assert started == [background_apps.engine_id_for(str(good))]


def test_resurrect_enabled_skips_a_folder_with_no_venv_built(tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    background_apps.set_enabled(str(folder), True)
    monkeypatch.setattr(background_apps, "interpreter_for",
                        lambda f: "/definitely/not/a/real/python")
    started = []
    monkeypatch.setattr(engine_host, "ensure_background",
                        lambda *a, **k: started.append(a) or None)

    background_apps.resurrect_enabled()

    assert started == []


def test_resurrect_enabled_stops_before_starting_once_shutdown_is_set(tmp_path, monkeypatch):
    # Code-review fix: the resurrection loop must not start MORE apps once
    # the server has begun shutting down.
    folder = _bg_folder(tmp_path)
    background_apps.set_enabled(str(folder), True)
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)
    started = []
    monkeypatch.setattr(engine_host, "ensure_background",
                        lambda *a, **k: started.append(a) or None)

    shutdown_event = threading.Event()
    shutdown_event.set()  # already shutting down before the loop even starts
    background_apps.resurrect_enabled(shutdown_event)

    assert started == []


def test_resurrect_enabled_stops_a_child_that_finished_spawning_during_shutdown(
        tmp_path, monkeypatch):
    # Code-review fix (the orphan race): engine_host.ensure_background only
    # registers its child into _children AFTER the (possibly slow) spawn
    # returns. If shutdown lands while that spawn is in flight,
    # engine_host.stop_all() can walk an empty/partial _children and miss it
    # entirely — so resurrect_enabled must check the flag again right after
    # its own call returns and clean up anything that landed late.
    folder = _bg_folder(tmp_path)
    background_apps.set_enabled(str(folder), True)
    engine_id = background_apps.engine_id_for(str(folder))
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)

    shutdown_event = threading.Event()
    fake_child = engine_host.Child(
        engine_id=engine_id, python=sys.executable, daemon=str(folder / "daemon.py"),
        cache="c", version="v1", kind="background")

    def fake_ensure(eid, python, daemon, cache, version):
        # Simulate shutdown landing WHILE this spawn was still running — by
        # the time it returns (registering the child), the server has
        # already started tearing down.
        shutdown_event.set()
        return fake_child

    monkeypatch.setattr(engine_host, "ensure_background", fake_ensure)
    stopped = []
    monkeypatch.setattr(engine_host, "stop", lambda eid: stopped.append(eid))

    background_apps.resurrect_enabled(shutdown_event)

    assert stopped == [engine_id]  # torn down instead of left running unowned


# --------------------------------------------------------------- end to end


def test_enable_through_the_api_reaches_the_fixture_daemon_via_proxy(
        client, tmp_path, monkeypatch):
    """Real spawn, real HTTP: enable the fixture app through the actual
    background_apps API (no engine_host mocking) and confirm the daemon it
    started answers through the SAME stable-origin proxy a template daemon
    uses (/api/engines/<id>/proxy) — engine_forward is engine-kind-agnostic,
    so a background app's traffic rides it exactly like a template's."""
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)
    html = os.path.join(FIXTURE_APP, "index.html")  # need not exist on disk

    resp = client.post("/api/apps/background/enable", json={"html": html},
                       headers=HDRS)
    assert resp.status_code == 200, resp.text
    engine_id = resp.json()["engine_id"]
    try:
        proxied = client.get(f"/api/engines/{engine_id}/proxy/health")
        assert proxied.status_code == 200
        assert proxied.json()["ok"] is True

        # The documented client API (fused.app.call) hardcodes POST — this is
        # what a real call from the runtime actually exercises, not just the
        # GET path above. Code-review fix: the fixture used to answer every
        # POST with a 501 (do_GET only), so this path was never under test.
        called = client.post(f"/api/engines/{engine_id}/proxy/count",
                             json={"n": 1}, headers=HDRS)
        assert called.status_code == 200, called.text
        body = called.json()
        assert body["ok"] is True
        assert body["count"] == 1
        assert body["echo"] == {"n": 1}

        status = client.get("/api/apps/background/status", params={"html": html})
        assert status.json()["running"] is True
    finally:
        engine_host.stop(engine_id)
        background_apps.set_enabled(FIXTURE_APP, False)


def test_proxy_marks_a_background_engine_at_most_once_on_post(client, monkeypatch):
    # Code-review fix: a background app's proxied POST can run side-effecting
    # daemon code (fused.app.call), the same shape as the warm /api/engine
    # worker's own /call — which already guards a heal-restart from silently
    # re-sending it via at_most_once=True. Pin the routing decision directly
    # rather than relying on flaky real-network-failure simulation.
    from fused_render.server.routers import engines as engines_router_mod

    captured = {}

    async def fake_forward(engine_id, request, path, body, call_timeout=None,
                           at_most_once=False):
        captured["at_most_once"] = at_most_once
        return Response(content=b"{}", status_code=200,
                        media_type="application/json")

    monkeypatch.setattr(engines_router_mod, "_forward", fake_forward)
    bg_child = engine_host.Child(
        engine_id="bg_pin", python=sys.executable, daemon="/d.py",
        cache="c", version="v1", kind="background")
    monkeypatch.setattr(engine_host, "current", lambda eid: bg_child)

    resp = client.post("/api/engines/bg_pin/proxy/count", json={}, headers=HDRS)
    assert resp.status_code == 200
    assert captured["at_most_once"] is True


def test_proxy_does_not_mark_a_template_engine_at_most_once_on_post(client, monkeypatch):
    # The other half of the same fix: a TEMPLATE daemon's POST traffic stays
    # pooled/retry-friendly — the fix is scoped to background apps, not a
    # blanket policy change for every engine kind.
    from fused_render.server.routers import engines as engines_router_mod

    captured = {}

    async def fake_forward(engine_id, request, path, body, call_timeout=None,
                           at_most_once=False):
        captured["at_most_once"] = at_most_once
        return Response(content=b"{}", status_code=200,
                        media_type="application/json")

    monkeypatch.setattr(engines_router_mod, "_forward", fake_forward)
    template_child = engine_host.Child(
        engine_id="map_tiles", python=sys.executable, daemon="/d.py",
        cache="c", version="v1", kind="template")
    monkeypatch.setattr(engine_host, "current", lambda eid: template_child)

    resp = client.post("/api/engines/map_tiles/proxy/describe", json={}, headers=HDRS)
    assert resp.status_code == 200
    assert captured["at_most_once"] is False


# --------------------------------------------------- Python 3.10 compatibility


def test_background_apps_has_no_bare_tomllib_import():
    """Code-review fix (CI regression): `requires-python = ">=3.10"`, but
    tomllib is 3.11+ stdlib — a bare module-level `import tomllib` raises
    ImportError on 3.10, and since server/app.py imports the background_apps
    router which imports this module, that took the whole server down on the
    `test-python (3.10)` CI lane, not just this feature.

    A clean-import test alone is weak here — this pytest run is always on the
    dev venv's 3.12+, so it can't reproduce a 3.10-only failure. This is a
    static pin on the SHAPE of the fix (the try tomllib / except tomli
    fallback `projectenv._load_manifest` already uses) instead, plus this
    module having actually been re-verified by hand against a real 3.10 venv
    with only `tomli` installed (no tomllib) — `uv venv --python 3.10` +
    `uv pip install -e .[dev]`, then `from fused_render import
    background_apps` and `background_apps.load_manifest(...)` both succeed."""
    import inspect

    src = inspect.getsource(background_apps)
    top_level_lines = [
        line for line in src.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    assert not any("tomllib" in line for line in top_level_lines), (
        "background_apps.py must not import tomllib at module level — "
        "it needs the try/except tomli fallback (see load_manifest)"
    )
    assert "import tomllib" in src and "import tomli as tomllib" in src, (
        "background_apps.py's tomllib fallback pattern (projectenv.py's "
        "shape) appears to have been removed"
    )
