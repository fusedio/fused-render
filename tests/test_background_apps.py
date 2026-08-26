"""Background apps (fused_render/background_apps.py): the folder manifest
([tool.fused-render.app] in pyproject.toml), the enabled-store persisted at
~/.fused-render/background_apps.json, engine_id identity, and the version
digest that retires a child when the manifest, daemon file, or interpreter
changes. Also covers engine_host's "background" child kind: validated
against the enabled store, and exempt from the warm-app idle reaper.
"""
import os
import sys
import time

import pytest

from fused_render import background_apps
from fused_render.server import engine_host

FIXTURE_APP = os.path.join(os.path.dirname(__file__), "fixtures", "background_app")


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
    interpreter = "/usr/bin/python3"
    v1 = background_apps.version_for(str(folder), interpreter)
    (folder / "pyproject.toml").write_text(
        (folder / "pyproject.toml").read_text() + "\nextra = 1\n")
    v2 = background_apps.version_for(str(folder), interpreter)
    assert v1 != v2


def test_version_for_changes_with_daemon_mtime(tmp_path):
    folder = _make_app(tmp_path)
    interpreter = "/usr/bin/python3"
    v1 = background_apps.version_for(str(folder), interpreter)
    daemon = folder / "daemon.py"
    new_time = time.time() + 5
    os.utime(daemon, (new_time, new_time))
    v2 = background_apps.version_for(str(folder), interpreter)
    assert v1 != v2


def test_version_for_changes_with_interpreter(tmp_path):
    folder = _make_app(tmp_path)
    v1 = background_apps.version_for(str(folder), "/usr/bin/python3")
    v2 = background_apps.version_for(str(folder), "/usr/bin/python3.12")
    assert v1 != v2


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
