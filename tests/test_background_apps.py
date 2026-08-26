"""Background apps (fused_render/background_apps.py): the folder manifest
([tool.fused-render.app] in pyproject.toml), the enabled-store persisted at
~/.fused-render/background_apps.json, engine_id identity, and the version
digest that retires a child when the manifest, daemon file, or interpreter
changes.
"""
import os
import time

import pytest

from fused_render import background_apps


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
