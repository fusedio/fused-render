"""The env contract that decouples templates from the fused_render package.

`fused_render/templates/shared/appenv.py` is how a template learns where the
shell home is and what origin the server is on — reading only env vars,
importing only the stdlib. It exists because the fused local execution backend
strips PYTHONPATH from child processes: a template's guarded
`from fused_render.shell import ...` silently takes its fallback branch there.

DECOUPLING is the contract pinned here: the module imports and answers
correctly in an interpreter that CANNOT see `fused_render` at all (spawned
subprocess, scrubbed sys.path/env), which is the situation it was written for.

FUSED_RENDER_HOME is redirected per test so nothing touches the real
~/.fused-render.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPENV_PATH = os.path.join(REPO_ROOT, "fused_render", "templates", "shared",
                           "appenv.py")


def _load_appenv():
    # Loaded by path, exactly the way a template loads it (sys.path.insert on
    # ../shared/ then import) — never as `fused_render.templates.shared.appenv`,
    # which would hide an accidental package-relative dependency.
    spec = importlib.util.spec_from_file_location("_appenv", APPENV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def appenv():
    return _load_appenv()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway shell home, with the branch ref cleared so the app-side
    home_dir() and the exported var agree on the unnested layout."""
    h = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    h.mkdir(parents=True)
    return h


@pytest.fixture()
def exported(home, monkeypatch):
    """Run the startup export against the tmp home, as the server does."""
    from fused_render import server

    monkeypatch.delenv("FUSED_RENDER_HOME_DIR", raising=False)
    server.export_app_env()
    return home


# ----------------------------------------------------------------- fallbacks

def test_dirs_fall_back_to_the_unbranched_baseline(appenv, monkeypatch):
    """Absent vars => a standalone copy of a template still resolves something
    sane, with no exception: FUSED_RENDER_HOME if set, else ~/.fused-render."""
    monkeypatch.delenv("FUSED_RENDER_HOME_DIR", raising=False)
    monkeypatch.setenv("FUSED_RENDER_HOME", "/tmp/fr-home")
    assert appenv.home_dir() == "/tmp/fr-home"

    monkeypatch.delenv("FUSED_RENDER_HOME", raising=False)
    assert appenv.home_dir() == os.path.expanduser("~/.fused-render")


def test_home_dir_var_is_taken_verbatim(appenv, monkeypatch):
    """The var is exported ALREADY branch-resolved, so appenv must not re-derive
    the nesting on top of it (that would yield branches/<ref>/branches/<ref>)."""
    monkeypatch.setenv("FUSED_RENDER_HOME", "/tmp/fr-home")
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "feature-x")
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", "/tmp/fr-home/branches/feature-x")
    assert appenv.home_dir() == "/tmp/fr-home/branches/feature-x"


def test_empty_vars_behave_as_absent(appenv, monkeypatch):
    """An exported-but-empty var must not win and produce ""."""
    monkeypatch.setenv("FUSED_RENDER_HOME", "/tmp/fr-home")
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", "")
    assert appenv.home_dir() == "/tmp/fr-home"


def test_origin_absent_is_none(appenv, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    assert appenv.origin() is None
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:32953")
    assert appenv.origin() == "http://127.0.0.1:32953"




# ------------------------------------------------------------ server exports

def test_export_app_env_sets_the_home_var(exported, monkeypatch):
    """The startup hook must leave the home var set."""
    assert os.environ["FUSED_RENDER_HOME_DIR"] == str(exported)
    # The skill plugin root (D216) rides the same export. It is the one var here
    # that may legitimately be ABSENT — a sync with nothing to copy, or one that
    # failed — so this asserts only that
    # it never carries a path to a root that isn't a plugin; details live in
    # test_skill_plugin.py.
    published = os.environ.get("FUSED_RENDER_SKILL_PLUGIN_DIR")
    assert published is None or os.path.isfile(
        os.path.join(published, ".claude-plugin", "plugin.json"))


def test_the_bundled_uv_is_on_the_path_every_child_inherits(home, tmp_path, monkeypatch):
    """A packaged build's own uv must be findable by `shutil.which("uv")`.

    Five templates set up their daemon's venv with uv and resolve it exactly that
    way (`geotiff/tile_server.py`, `zarr_aoi/tile_server.py`,
    `netcdf/grid_tile_server.py`, `las/las_reader.py`,
    `pyramid/overview_pyramid.py`) — they must, because a template may not branch
    on how the app was installed. The macOS bundle ships uv at
    `Contents/Resources/bin/uv`, which is NOT beside the interpreter and was on
    nobody's PATH: only `envinstall._worker_env()` put it there, and only for the
    install worker. So on a DMG with no user-installed uv, `_daemon_python()` fell
    back to the app interpreter — which has neither `imagecodecs`/`pyproj` (geotiff
    loses LZW and JPEG tiles) nor `s3fs`/`gcsfs`/`crc32c` (every remote zarr store
    fails to open), and las/pyramid raised advice the user cannot follow. The
    Linux and Windows supervisors already prepend their payload's bin dir
    (`supervisor/paths.py`), so this is the same mechanism, not a second one.

    Asserted through `shutil.which` rather than any fused_render helper, because
    `shutil.which` is what the templates actually call.
    """
    from fused_render import server

    contents = tmp_path / "Fused.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources" / "bin").mkdir(parents=True)
    interp = contents / "MacOS" / "python"
    interp.write_text("")
    uv = contents / "Resources" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    uv.write_text("")
    os.chmod(uv, 0o755)
    monkeypatch.setattr(sys, "executable", str(interp))
    monkeypatch.delenv("FUSED_RENDER_UV_BIN", raising=False)
    # A PATH with no uv anywhere on it: the bundled one is the only uv there is,
    # which is precisely the DMG-without-a-dev-toolchain case.
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    server.export_app_env()
    # normcase, not a bare ==: shutil.which() on Windows matches "uv" against
    # PATHEXT (.COM;.EXE;...) and returns cmd + THAT extension's case, e.g.
    # "uv.EXE", regardless of the actual on-disk filename's case ("uv.exe"
    # here) — a case difference on a filesystem where it is not a different
    # file. Same idiom as claude_agent/agent.py's containment check.
    assert os.path.normcase(shutil.which("uv")) == os.path.normcase(str(uv))


# --------------------------------------------------------------- decoupling

def test_appenv_works_without_fused_render_importable(tmp_path):
    """The whole point: import + answer in an interpreter that cannot see the
    package. Run with cwd outside the repo, PYTHONPATH cleared, and every
    sys.path entry that can reach `fused_render` stripped — the fused local
    backend's child environment, reproduced.
    """
    shared = os.path.dirname(APPENV_PATH)

    script = textwrap.dedent(f"""
        import os, sys
        # Drop anything that can import fused_render, then prove it.
        sys.path = [p for p in sys.path
                    if not os.path.isdir(os.path.join(p, "fused_render"))]
        try:
            import fused_render
            raise SystemExit("fused_render was importable; test setup is wrong")
        except ImportError:
            pass

        sys.path.insert(0, {shared!r})
        import appenv

        assert appenv.home_dir() == {str(tmp_path / "home")!r}, appenv.home_dir()
        assert appenv.origin() == "http://127.0.0.1:32953"
        print("ok")
    """)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["FUSED_RENDER_HOME_DIR"] = str(tmp_path / "home")
    env["FUSED_RENDER_ORIGIN"] = "http://127.0.0.1:32953"

    out = subprocess.run([sys.executable, "-c", script], cwd=str(tmp_path),
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_appenv_imports_only_the_stdlib():
    """Guard the constraint at the AST level: nothing but `os` is
    imported, so neither fused_render nor a third-party package can creep in
    (either would break the standalone / no-PYTHONPATH case the module exists
    for)."""
    import ast
    with open(APPENV_PATH, encoding="utf-8") as f:
        src = f.read()
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names <= {"os"}, names
