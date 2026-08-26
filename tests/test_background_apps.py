"""Background apps (fused_render/background_apps.py): the folder manifest
([tool.fused-render.app] in pyproject.toml), the autostart store persisted
at ~/.fused-render/background_apps.json, engine_id identity, and the version
digest that retires a child when the manifest, daemon file, or interpreter
changes. Also covers engine_host's "background" child kind: validated
against the folder's own manifest (independent of autostart, D511), and
exempt from the warm-app idle reaper.
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
from fused_render.shell import prefs as shell_prefs

FIXTURE_APP = os.path.join(os.path.dirname(__file__), "fixtures", "background_app")
HDRS = {"X-Fused": "1"}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # The autostart store lives in the shell home; isolate it per test the
    # same way test_registered_apps.py does.
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
    # Code-review fix (D499 revised): the exact upgrade-rot case this digest
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


# --------------------------------------------------- interpreter resolution
# D503 (2026-08-26 code review): interpreter_for must NOT walk past the app's
# own folder looking for an ancestor project the way projectenv.project_env_for
# does for a plain .py script — the app folder IS the project boundary.


def test_interpreter_for_manifest_only_app_is_sys_executable(tmp_path, monkeypatch):
    folder = _make_app(tmp_path)  # only [tool.fused-render.app], no [project]
    monkeypatch.setattr(shell_prefs, "effective_engine", lambda: "fused")
    assert background_apps.interpreter_for(str(folder)) == sys.executable


def test_interpreter_for_builtin_engine_is_always_sys_executable(tmp_path, monkeypatch):
    folder = _make_app(tmp_path)
    monkeypatch.setattr(shell_prefs, "effective_engine", lambda: "builtin")
    assert background_apps.interpreter_for(str(folder)) == sys.executable


def test_interpreter_for_manifest_only_app_nested_in_a_dependency_declaring_parent(
        tmp_path, monkeypatch):
    # THE surprising case this decision exists for: a manifest-only app
    # folder sitting inside some unrelated ancestor project must still run on
    # sys.executable, never silently inherit that ancestor's venv — the
    # exact bug the fixture app hit nested inside the fused-render repo
    # itself (a 409, since that ancestor venv isn't in the project-venv
    # store).
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "pyproject.toml").write_text(
        '[project]\nname = "parent"\nversion = "0"\n'
        'dependencies = ["requests"]\n')
    app = parent / "app"
    app.mkdir()
    (app / "pyproject.toml").write_text(
        '[tool.fused-render.app]\nkind = "background"\ndaemon = "daemon.py"\n')
    (app / "daemon.py").write_text("# daemon\n")

    monkeypatch.setattr(shell_prefs, "effective_engine", lambda: "fused")
    assert background_apps.interpreter_for(str(app)) == sys.executable


def test_interpreter_for_app_declaring_its_own_deps_uses_its_own_venv(
        tmp_path, monkeypatch):
    # The other half: an app whose OWN pyproject.toml declares [project] deps
    # (alongside its [tool.fused-render.app] table, same file) gets that
    # folder's own venv — has_project_env checks only the folder handed to
    # it, never an ancestor.
    folder = tmp_path / "app_with_deps"
    folder.mkdir()
    (folder / "pyproject.toml").write_text(
        '[project]\nname = "app_with_deps"\nversion = "0"\n'
        'dependencies = ["requests"]\n\n'
        '[tool.fused-render.app]\nkind = "background"\ndaemon = "daemon.py"\n')
    (folder / "daemon.py").write_text("# daemon\n")

    monkeypatch.setattr(shell_prefs, "effective_engine", lambda: "fused")
    from fused_render import projectenv
    monkeypatch.setattr(projectenv, "interpreter_for",
                        lambda project_dir: f"venv-python-for:{project_dir}")

    result = background_apps.interpreter_for(str(folder))
    assert result == f"venv-python-for:{os.path.abspath(str(folder))}"


# ----------------------------------------------------------- autostart store


def test_autostart_store_round_trip(tmp_path):
    folder = _make_app(tmp_path)
    assert background_apps.autostart_paths() == []
    background_apps.set_autostart(str(folder), True)
    assert os.path.realpath(str(folder)) in background_apps.autostart_paths()
    background_apps.set_autostart(str(folder), False)
    assert os.path.realpath(str(folder)) not in background_apps.autostart_paths()


def test_autostart_store_skips_missing_folder_but_keeps_entry(tmp_path):
    folder = _make_app(tmp_path)
    background_apps.set_autostart(str(folder), True)
    assert os.path.realpath(str(folder)) in background_apps.autostart_paths()

    # Delete the folder: the entry drops out of the live listing...
    import shutil
    shutil.rmtree(folder)
    assert os.path.realpath(str(folder)) not in background_apps.autostart_paths()

    # ...but the store itself was never rewritten, so recreating the folder
    # brings the entry straight back with no re-opt-in.
    folder.mkdir()
    assert os.path.realpath(str(folder)) in background_apps.autostart_paths()


def test_autostart_store_re_opt_in_does_not_duplicate(tmp_path):
    folder = _make_app(tmp_path)
    background_apps.set_autostart(str(folder), True)
    background_apps.set_autostart(str(folder), True)
    assert background_apps.autostart_paths().count(os.path.realpath(str(folder))) == 1


def test_autostart_store_normalizes_via_realpath_like_engine_id_for(tmp_path):
    """D512 (folded in from the deferred half of D509): the store used to
    normalize with `os.path.abspath` while `engine_id_for` keys identity off
    `os.path.realpath` — a symlinked folder and its target could then get
    TWO separate autostart entries for what `engine_id_for` treats as one
    app. Setting autostart through a symlinked alias must read back as set
    through the real path too, and the two must never coexist as separate
    entries."""
    real = _make_app(tmp_path, "realtarget")
    link = tmp_path / "aliaslink"
    link.symlink_to(real)

    background_apps.set_autostart(str(link), True)
    assert os.path.realpath(str(real)) in background_apps.autostart_paths()
    # Setting it again through the REAL path must not create a second entry.
    background_apps.set_autostart(str(real), True)
    assert background_apps.autostart_paths().count(os.path.realpath(str(real))) == 1
    # Turning it off through the real path must clear it for the alias too.
    background_apps.set_autostart(str(real), False)
    assert background_apps.autostart_paths() == []


def test_autostart_is_opt_in_start_alone_never_sets_it():
    """The whole point of the split (D511): `ensure_background`/`start` must
    never persist autostart as a side effect. Uses the real, spawnable
    fixture daemon (same as `test_ensure_background_spawns_and_reuses_the_
    fixture_daemon` above) — `set_autostart` is the only thing that may ever
    add to the store; merely resolving/spawning a background app must never
    call it."""
    assert background_apps.autostart_paths() == []

    engine_id = background_apps.engine_id_for(FIXTURE_APP)
    version = background_apps.version_for(FIXTURE_APP, sys.executable)
    cache = os.path.join(os.environ["FUSED_RENDER_HOME"], "apps", engine_id)
    manifest = background_apps.load_manifest(FIXTURE_APP)

    child = engine_host.ensure_background(
        engine_id, sys.executable, manifest.daemon, cache, version, FIXTURE_APP)
    try:
        assert engine_host._ping(child)
        assert background_apps.autostart_paths() == [], (
            "start (ensure_background) must never persist autostart")
    finally:
        engine_host.stop(engine_id)


def test_start_then_simulated_restart_does_not_resurrect_without_autostart(monkeypatch):
    """End-to-end (module level) proof of the opt-in default: start the
    fixture daemon, never touch autostart, then simulate a server restart by
    calling `resurrect_autostart()` directly — the app must NOT come back."""
    engine_id = background_apps.engine_id_for(FIXTURE_APP)
    version = background_apps.version_for(FIXTURE_APP, sys.executable)
    cache = os.path.join(os.environ["FUSED_RENDER_HOME"], "apps", engine_id)
    manifest = background_apps.load_manifest(FIXTURE_APP)

    child = engine_host.ensure_background(
        engine_id, sys.executable, manifest.daemon, cache, version, FIXTURE_APP)
    try:
        assert engine_host._ping(child)
    finally:
        engine_host.stop(engine_id)

    started = []
    monkeypatch.setattr(engine_host, "ensure_background",
                        lambda *a, **k: started.append(a) or None)
    background_apps.resurrect_autostart()
    assert started == [], (
        "a folder that was only start()ed (never opted into autostart) "
        "must not come back at the next server start")


# ---------------------------------------------- engine_host: background kind


def test_ensure_background_rejects_foreign_interpreter(tmp_path):
    daemon = tmp_path / "daemon.py"
    daemon.write_text("# daemon\n")
    with pytest.raises(engine_host.EngineError):
        engine_host.ensure_background(
            "bg_foreign", "/definitely/not/a/real/python", str(daemon),
            str(tmp_path / "cache"), "v1")


def test_ensure_background_rejects_daemon_whose_folder_has_no_manifest(tmp_path):
    # No pyproject.toml [tool.fused-render.app] table at all in the daemon's
    # own folder: the daemon must be refused regardless of how real it looks
    # on disk. Deliberately NOT gated on the autostart store any more (D511)
    # — `start()` must work for an app that was never opted into autostart,
    # so this validation is now self-contained against the folder's own
    # manifest instead of a "currently enabled" list.
    folder = tmp_path / "no_manifest"
    folder.mkdir()
    daemon = folder / "daemon.py"
    daemon.write_text("# daemon\n")
    with pytest.raises(engine_host.EngineError):
        engine_host.ensure_background(
            "bg_nomanifest", sys.executable, str(daemon),
            str(tmp_path / "cache"), "v1")


def test_ensure_background_rejects_daemon_not_matching_its_own_folders_manifest(tmp_path):
    # The folder DOES have a valid manifest, but the daemon path handed to
    # ensure_background is a different file inside it than the one the
    # manifest declares — must still be refused.
    app = _make_app(tmp_path, "app", daemon="daemon.py")
    wrong = app / "not_the_declared_daemon.py"
    wrong.write_text("# not the manifest's daemon\n")
    with pytest.raises(engine_host.EngineError):
        engine_host.ensure_background(
            "bg_wrongdaemon", sys.executable, str(wrong),
            str(tmp_path / "cache"), "v1")


def test_ensure_background_succeeds_for_a_valid_manifest_without_autostart():
    # The other half of D511: a folder with NO autostart entry at all must
    # still be able to start — autostart is opt-in, not a precondition. Uses
    # the real, spawnable fixture daemon (a plain "# daemon\n" stub file
    # exits immediately once actually spawned, so this needs the fixture,
    # not `_make_app`'s placeholder).
    assert background_apps.autostart_paths() == []
    engine_id = background_apps.engine_id_for(FIXTURE_APP)
    manifest = background_apps.load_manifest(FIXTURE_APP)
    version = background_apps.version_for(FIXTURE_APP, sys.executable)
    cache = os.path.join(os.environ["FUSED_RENDER_HOME"], "apps", engine_id)
    child = engine_host.ensure_background(
        engine_id, sys.executable, manifest.daemon, cache, version, FIXTURE_APP)
    try:
        assert child.kind == "background"
    finally:
        engine_host.stop(engine_id)


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


def test_ensure_background_stores_folder_on_the_child():
    # D505: `folder` flows through ensure_background onto Child.folder the
    # same way cache/version already do, so `_spawn_env` can export
    # FUSED_RENDER_APP_DIR for a daemon to address itself.
    manifest = background_apps.load_manifest(FIXTURE_APP)
    engine_id = background_apps.engine_id_for(FIXTURE_APP)
    version = background_apps.version_for(FIXTURE_APP, sys.executable)
    cache = os.path.join(os.environ["FUSED_RENDER_HOME"], "apps", engine_id)

    child = engine_host.ensure_background(
        engine_id, sys.executable, manifest.daemon, cache, version, FIXTURE_APP)
    try:
        assert child.folder == FIXTURE_APP
    finally:
        engine_host.stop(engine_id)


def test_restart_preserves_folder_so_a_healed_child_keeps_app_dir():
    # Regression: engine_host.restart() rebuilds the replacement Child field
    # by field (the same shape `ensure_app`'s reuse-or-respawn already
    # takes), and the first cut of D505 forgot `folder` in that rebuild —
    # invisible in every ensure_background test above, since none of them
    # go through restart(). This is exactly the path a killed-and-healed
    # background child takes (engine_forward.py's heal-on-proxy) and the
    # one `/api/apps/background/restart` takes when a child IS live
    # (engine_host.current(engine_id) is not None branch) — both would
    # silently drop FUSED_RENDER_APP_DIR from the respawned daemon's env,
    # taking away its only way to call stop()/disable() on itself.
    manifest = background_apps.load_manifest(FIXTURE_APP)
    engine_id = background_apps.engine_id_for(FIXTURE_APP)
    version = background_apps.version_for(FIXTURE_APP, sys.executable)
    cache = os.path.join(os.environ["FUSED_RENDER_HOME"], "apps", engine_id)

    original = engine_host.ensure_background(
        engine_id, sys.executable, manifest.daemon, cache, version, FIXTURE_APP)
    try:
        assert original.folder == FIXTURE_APP  # sanity: the premise holds

        restarted = engine_host.restart(engine_id)
        assert restarted is not original
        assert restarted.folder == FIXTURE_APP, (
            "restart() dropped Child.folder — the respawned daemon would "
            "boot with no FUSED_RENDER_APP_DIR")
        env = engine_host._spawn_env(restarted)
        assert env.get("FUSED_RENDER_APP_DIR") == FIXTURE_APP, (
            "restart()'s replacement child does not carry FUSED_RENDER_APP_DIR "
            "into its own spawn env")
    finally:
        engine_host.stop(engine_id)


def test_ensure_background_without_folder_defaults_to_empty_string():
    # The folder param is optional so existing direct callers that don't
    # care need not pass one.
    manifest = background_apps.load_manifest(FIXTURE_APP)
    engine_id = background_apps.engine_id_for(FIXTURE_APP)
    version = background_apps.version_for(FIXTURE_APP, sys.executable)
    cache = os.path.join(os.environ["FUSED_RENDER_HOME"], "apps", engine_id)

    child = engine_host.ensure_background(
        engine_id, sys.executable, manifest.daemon, cache, version)
    try:
        assert child.folder == ""
    finally:
        engine_host.stop(engine_id)


def test_spawn_env_exports_app_dir_for_background_children_with_a_folder():
    child = engine_host.Child(
        engine_id="bg_envtest", python=sys.executable, daemon="/tmp/d.py",
        cache="c", version="v1", kind="background", folder="/tmp/my-bg-app")
    env = engine_host._spawn_env(child)
    assert env["FUSED_RENDER_APP_DIR"] == "/tmp/my-bg-app"


def test_spawn_env_omits_app_dir_when_folder_is_empty():
    child = engine_host.Child(
        engine_id="bg_envtest2", python=sys.executable, daemon="/tmp/d.py",
        cache="c", version="v1", kind="background", folder="")
    env = engine_host._spawn_env(child)
    assert "FUSED_RENDER_APP_DIR" not in env


def test_spawn_env_omits_app_dir_for_non_background_kinds():
    # Only kind="background" children carry a folder at all — a template or
    # app-worker child spawned with a stray `folder` value (should never
    # happen in production) still must not leak the var.
    child = engine_host.Child(
        engine_id="tmpl_envtest", python=sys.executable, daemon="/tmp/d.py",
        cache="c", version="v1", kind="template", folder="/tmp/should-not-leak")
    env = engine_host._spawn_env(child)
    assert "FUSED_RENDER_APP_DIR" not in env


def test_api_start_passes_folder_through_to_ensure_background(client, tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)

    calls = []
    fake_child = engine_host.Child(
        engine_id="bg_folderfake", python=sys.executable,
        daemon=str(folder / "daemon.py"), cache="c", version="v1",
        kind="background", pid=1)

    def fake_ensure(engine_id, python, daemon, cache, version, folder=""):
        calls.append(folder)
        return fake_child

    monkeypatch.setattr(engine_host, "ensure_background", fake_ensure)

    resp = client.post("/api/apps/background/start", json={"html": html}, headers=HDRS)
    assert resp.status_code == 200, resp.text
    assert calls == [os.path.realpath(str(folder))]


# ---------------------------------------------- router: start/stop/autostart/etc


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


def test_api_start_calls_ensure_background_without_touching_autostart(
        client, tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)

    calls = []
    fake_child = engine_host.Child(
        engine_id="bg_fake", python=sys.executable, daemon=str(folder / "daemon.py"),
        cache="c", version="v1", kind="background", pid=4242)

    def fake_ensure(engine_id, python, daemon, cache, version, folder=""):
        calls.append((engine_id, python, daemon, cache, version))
        return fake_child

    monkeypatch.setattr(engine_host, "ensure_background", fake_ensure)

    resp = client.post("/api/apps/background/start", json={"html": html}, headers=HDRS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["pid"] == 4242
    assert len(calls) == 1
    assert calls[0][2] == str(folder / "daemon.py")
    # D511, the whole point: start() must NEVER persist autostart.
    assert os.path.realpath(str(folder)) not in background_apps.autostart_paths()


def test_api_start_requires_x_fused_header(client, tmp_path):
    folder = _bg_folder(tmp_path)
    resp = client.post("/api/apps/background/start",
                       json={"html": str(folder / "index.html")})
    assert resp.status_code == 403
    assert os.path.realpath(str(folder)) not in background_apps.autostart_paths()


def test_api_start_409_when_project_venv_not_built(client, tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    monkeypatch.setattr(background_apps, "interpreter_for",
                        lambda f: "/definitely/not/a/real/python")

    resp = client.post("/api/apps/background/start", json={"html": html}, headers=HDRS)
    assert resp.status_code == 409
    assert os.path.realpath(str(folder)) not in background_apps.autostart_paths()


def test_api_autostart_sets_the_flag_without_starting_or_stopping_anything(
        client, tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")

    ensured = []
    stopped = []
    monkeypatch.setattr(engine_host, "ensure_background",
                        lambda *a, **k: ensured.append(a) or None)
    monkeypatch.setattr(engine_host, "stop", lambda eid: stopped.append(eid))

    resp = client.post("/api/apps/background/autostart",
                       json={"html": html, "autostart": True}, headers=HDRS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["autostart"] is True
    assert os.path.realpath(str(folder)) in background_apps.autostart_paths()
    assert ensured == []
    assert stopped == []

    resp = client.post("/api/apps/background/autostart",
                       json={"html": html, "autostart": False}, headers=HDRS)
    assert resp.status_code == 200
    assert resp.json()["autostart"] is False
    assert os.path.realpath(str(folder)) not in background_apps.autostart_paths()
    assert ensured == []
    assert stopped == []


def test_api_autostart_requires_x_fused_header(client, tmp_path):
    folder = _bg_folder(tmp_path)
    resp = client.post("/api/apps/background/autostart",
                       json={"html": str(folder / "index.html"), "autostart": True})
    assert resp.status_code == 403
    assert os.path.realpath(str(folder)) not in background_apps.autostart_paths()


def test_api_stop_kills_without_touching_autostart(client, tmp_path, monkeypatch):
    # The whole point of the run-state/autostart split: stop must kill the
    # process but leave autostart exactly where it was.
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    background_apps.set_autostart(str(folder), True)
    engine_id = background_apps.engine_id_for(str(folder))

    stopped = []
    monkeypatch.setattr(engine_host, "stop", lambda eid: stopped.append(eid))

    resp = client.post("/api/apps/background/stop", json={"html": html}, headers=HDRS)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert stopped == [engine_id]
    assert os.path.realpath(str(folder)) in background_apps.autostart_paths()


def test_api_stop_leaves_autostart_off_when_it_was_off(client, tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    assert os.path.realpath(str(folder)) not in background_apps.autostart_paths()
    monkeypatch.setattr(engine_host, "stop", lambda eid: None)

    client.post("/api/apps/background/stop", json={"html": html}, headers=HDRS)
    assert os.path.realpath(str(folder)) not in background_apps.autostart_paths()


def test_api_restart_after_stop_falls_back_to_a_fresh_bring_up(client, tmp_path, monkeypatch):
    # Code-review fix: engine_host.restart() alone raises "has never been
    # started" once stop() has popped the child from _children, which broke
    # the documented stop() -> restart() recovery path with an opaque 502.
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
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

    def fake_ensure(eid, python, daemon, cache, version, folder=""):
        ensured.append(eid)
        return fake_child

    monkeypatch.setattr(engine_host, "ensure_background", fake_ensure)

    resp = client.post("/api/apps/background/restart", json={"html": html}, headers=HDRS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["pid"] == 9191
    assert ensured == [engine_id]  # fell back to a fresh bring-up, not engine_host.restart


def test_api_restart_of_a_live_child_carries_the_freshly_computed_version(
        client, tmp_path, monkeypatch):
    # Code-review finding D: when a child IS live, the endpoint used to call
    # `engine_host.restart(engine_id)` with no version, and `restart()`
    # rebuilds the replacement Child from `existing.version` — the OLD
    # digest. So `fused.daemon.restart()` right after editing daemon.py
    # respawned the new code but tagged it with the stale version string;
    # the next start()/server-start resurrection then computes the current
    # digest, `_matches` fails against it, and the just-restarted child gets
    # torn down and respawned a SECOND time. The endpoint must pass the
    # freshly computed version through to the respawn.
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    engine_id = background_apps.engine_id_for(str(folder))
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)

    stale_version = "stale-version-from-before-the-edit"
    live_child = engine_host.Child(
        engine_id=engine_id, python=sys.executable, daemon=str(folder / "daemon.py"),
        cache="c", version=stale_version, kind="background", pid=1)
    monkeypatch.setattr(engine_host, "current",
                        lambda eid: live_child if eid == engine_id else None)

    captured = {}

    def fake_restart(eid, failed=None, version=None):
        captured["engine_id"] = eid
        captured["version"] = version
        return engine_host.Child(
            engine_id=eid, python=sys.executable, daemon=str(folder / "daemon.py"),
            cache="c", version=version or stale_version, kind="background", pid=2222)

    monkeypatch.setattr(engine_host, "restart", fake_restart)

    fresh_version = background_apps.version_for(str(folder), sys.executable)
    assert fresh_version != stale_version  # sanity: the premise holds

    resp = client.post("/api/apps/background/restart", json={"html": html}, headers=HDRS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["pid"] == 2222
    assert captured["engine_id"] == engine_id
    assert captured["version"] == fresh_version, (
        "restart() of a live child was not given the freshly computed "
        "version — it would carry the stale digest and get torn down and "
        "respawned again on the next start()/server-start resurrection")
    assert body["version"] == fresh_version


def test_api_status_reflects_a_faked_live_child(client, tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    engine_id = background_apps.engine_id_for(str(folder))
    background_apps.set_autostart(str(folder), True)

    fake_child = engine_host.Child(
        engine_id=engine_id, python=sys.executable, daemon="d", cache="c",
        version="v9", kind="background", pid=555)
    monkeypatch.setattr(engine_host, "current",
                        lambda eid: fake_child if eid == engine_id else None)
    monkeypatch.setattr(engine_host, "_alive", lambda c: True)

    resp = client.get("/api/apps/background/status", params={"html": html})
    assert resp.status_code == 200
    body = resp.json()
    assert body["autostart"] is True
    assert body["running"] is True
    assert body["pid"] == 555
    assert body["version"] == "v9"
    assert body["engine_id"] == engine_id


def test_api_status_not_running_and_autostart_false_on_a_never_configured_app(
        client, tmp_path):
    # D511: a folder nobody ever touched must report autostart False, not
    # merely "not running" — the opt-in default has to be visible here.
    folder = _bg_folder(tmp_path)
    resp = client.get("/api/apps/background/status",
                      params={"html": str(folder / "index.html")})
    body = resp.json()
    assert body["autostart"] is False
    assert body["running"] is False
    assert body["pid"] is None


def test_api_start_leaves_autostart_false_on_status(client, tmp_path, monkeypatch):
    # The end-to-end proof of the opt-in default through the real API: start
    # the app, then read status() back — autostart must still read False.
    folder = _bg_folder(tmp_path)
    html = str(folder / "index.html")
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)
    engine_id = background_apps.engine_id_for(str(folder))
    fake_child = engine_host.Child(
        engine_id=engine_id, python=sys.executable, daemon=str(folder / "daemon.py"),
        cache="c", version="v1", kind="background", pid=321)
    monkeypatch.setattr(engine_host, "ensure_background", lambda *a, **kw: fake_child)
    monkeypatch.setattr(engine_host, "current",
                        lambda eid: fake_child if eid == engine_id else None)
    monkeypatch.setattr(engine_host, "_alive", lambda c: True)

    resp = client.post("/api/apps/background/start", json={"html": html}, headers=HDRS)
    assert resp.status_code == 200, resp.text

    status = client.get("/api/apps/background/status", params={"html": html}).json()
    assert status["running"] is True
    assert status["autostart"] is False


def test_status_agrees_on_autostart_and_running_through_a_symlinked_alias(
        client, tmp_path, monkeypatch):
    # Code-review finding C (D509/D512): `autostart` used to compare
    # `_folder_for` against a store normalized differently than
    # `engine_id_for` (and therefore `running`), which keys on realpath. A
    # folder reached through a symlink alias diverged: opted into autostart
    # via the link, `status()`'d via the real path (or vice versa) reported
    # {"autostart": False, "running": True} — a fact that cannot be true,
    # since the two paths name the exact same app and the exact same
    # running engine_id.
    real = _bg_folder(tmp_path, name="real")
    link = tmp_path / "link"
    link.symlink_to(real)

    engine_id = background_apps.engine_id_for(str(real))
    fake_child = engine_host.Child(
        engine_id=engine_id, python=sys.executable, daemon=str(real / "daemon.py"),
        cache="c", version="v9", kind="background", pid=777)
    monkeypatch.setattr(engine_host, "ensure_background",
                        lambda *a, **kw: fake_child)
    monkeypatch.setattr(engine_host, "current",
                        lambda eid: fake_child if eid == engine_id else None)
    monkeypatch.setattr(engine_host, "_alive", lambda c: True)

    resp = client.post("/api/apps/background/autostart",  # opt in via the ALIAS
                       json={"html": str(link / "index.html"), "autostart": True},
                       headers=HDRS)
    assert resp.status_code == 200, resp.text
    client.post("/api/apps/background/start",
               json={"html": str(link / "index.html")}, headers=HDRS)

    # status() through the REAL (non-alias) path must see the same
    # autostart/running facts as status() through the alias it was set
    # through — both name one app.
    resp = client.get("/api/apps/background/status",
                      params={"html": str(real / "index.html")})
    body = resp.json()
    assert body["engine_id"] == engine_id
    assert body["running"] is True
    assert body["autostart"] is True, (
        "autostart/running diverged through a symlinked folder alias")


def test_autostart_set_through_one_alias_and_cleared_through_another_fully_clears(
        client, tmp_path, monkeypatch):
    # The other half of finding C: the store writing folder identity
    # inconsistently with engine_id_for means two aliases of the same folder
    # can each get their OWN store entry. Clearing autostart through one
    # alias must not leave the other alias's entry (i.e. the same app)
    # still opted in.
    real = _bg_folder(tmp_path, name="real2")
    link = tmp_path / "link2"
    link.symlink_to(real)

    html_via_link = str(link / "index.html")
    html_via_real = str(real / "index.html")

    resp = client.post("/api/apps/background/autostart",
                       json={"html": html_via_link, "autostart": True}, headers=HDRS)
    assert resp.status_code == 200, resp.text

    resp = client.post("/api/apps/background/autostart",
                       json={"html": html_via_real, "autostart": False}, headers=HDRS)
    assert resp.status_code == 200, resp.text

    for html in (html_via_link, html_via_real):
        status = client.get("/api/apps/background/status",
                            params={"html": html}).json()
        assert status["autostart"] is False, (
            f"{html} still opted into autostart after clearing it "
            "through its symlinked alias")


# ---------------------------------------------------------------- resurrection


def test_resurrect_autostart_starts_every_app_and_survives_one_raising(tmp_path, monkeypatch):
    good = _bg_folder(tmp_path, "good")
    bad = _bg_folder(tmp_path, "bad")
    background_apps.set_autostart(str(good), True)
    background_apps.set_autostart(str(bad), True)

    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)

    started = []

    def fake_ensure(engine_id, python, daemon, cache, version, folder=""):
        if "bad" in daemon:
            raise engine_host.EngineError("boom")
        started.append(engine_id)
        return engine_host.Child(engine_id=engine_id, python=python, daemon=daemon,
                                 cache=cache, version=version, kind="background")

    monkeypatch.setattr(engine_host, "ensure_background", fake_ensure)

    background_apps.resurrect_autostart()  # must not raise despite "bad" failing

    assert started == [background_apps.engine_id_for(str(good))]


def test_resurrect_autostart_skips_a_folder_with_no_venv_built(tmp_path, monkeypatch):
    folder = _bg_folder(tmp_path)
    background_apps.set_autostart(str(folder), True)
    monkeypatch.setattr(background_apps, "interpreter_for",
                        lambda f: "/definitely/not/a/real/python")
    started = []
    monkeypatch.setattr(engine_host, "ensure_background",
                        lambda *a, **k: started.append(a) or None)

    background_apps.resurrect_autostart()

    assert started == []


def test_resurrect_autostart_stops_before_starting_once_shutdown_is_set(tmp_path, monkeypatch):
    # Code-review fix: the resurrection loop must not start MORE apps once
    # the server has begun shutting down.
    folder = _bg_folder(tmp_path)
    background_apps.set_autostart(str(folder), True)
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)
    started = []
    monkeypatch.setattr(engine_host, "ensure_background",
                        lambda *a, **k: started.append(a) or None)

    shutdown_event = threading.Event()
    shutdown_event.set()  # already shutting down before the loop even starts
    background_apps.resurrect_autostart(shutdown_event)

    assert started == []


def test_resurrect_autostart_stops_a_child_that_finished_spawning_during_shutdown(
        tmp_path, monkeypatch):
    # Code-review fix (the orphan race): engine_host.ensure_background only
    # registers its child into _children AFTER the (possibly slow) spawn
    # returns. If shutdown lands while that spawn is in flight,
    # engine_host.stop_all() can walk an empty/partial _children and miss it
    # entirely — so resurrect_autostart must check the flag again right after
    # its own call returns and clean up anything that landed late.
    folder = _bg_folder(tmp_path)
    background_apps.set_autostart(str(folder), True)
    engine_id = background_apps.engine_id_for(str(folder))
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)

    shutdown_event = threading.Event()
    fake_child = engine_host.Child(
        engine_id=engine_id, python=sys.executable, daemon=str(folder / "daemon.py"),
        cache="c", version="v1", kind="background")

    def fake_ensure(eid, python, daemon, cache, version, folder=""):
        # Simulate shutdown landing WHILE this spawn was still running — by
        # the time it returns (registering the child), the server has
        # already started tearing down.
        shutdown_event.set()
        return fake_child

    monkeypatch.setattr(engine_host, "ensure_background", fake_ensure)
    stopped = []
    monkeypatch.setattr(engine_host, "stop", lambda eid: stopped.append(eid))

    background_apps.resurrect_autostart(shutdown_event)

    assert stopped == [engine_id]  # torn down instead of left running unowned


# --------------------------------------------------------------- end to end


def test_start_through_the_api_reaches_the_fixture_daemon_via_proxy(
        client, tmp_path, monkeypatch):
    """Real spawn, real HTTP: start the fixture app through the actual
    background_apps API (no engine_host mocking) and confirm the daemon it
    started answers through the SAME stable-origin proxy a template daemon
    uses (/api/engines/<id>/proxy) — engine_forward is engine-kind-agnostic,
    so a background app's traffic rides it exactly like a template's."""
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)
    html = os.path.join(FIXTURE_APP, "index.html")  # need not exist on disk

    resp = client.post("/api/apps/background/start", json={"html": html},
                       headers=HDRS)
    assert resp.status_code == 200, resp.text
    engine_id = resp.json()["engine_id"]
    try:
        proxied = client.get(f"/api/engines/{engine_id}/proxy/health")
        assert proxied.status_code == 200
        assert proxied.json()["ok"] is True

        # The documented client API (fused.daemon.call) hardcodes POST — this is
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


def test_proxy_marks_a_background_engine_at_most_once_on_post(client, monkeypatch):
    # Code-review fix: a background app's proxied POST can run side-effecting
    # daemon code (fused.daemon.call), the same shape as the warm /api/engine
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
