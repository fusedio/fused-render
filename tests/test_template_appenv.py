"""The env contract that decouples templates from the fused_render package.

`fused_render/templates/shared/appenv.py` is how a template learns where the
shell home / mounts dirs are, which mountpoints are read-only, and what origin
the server is on — reading only env vars, importing only the stdlib. It exists
because the fused local execution backend strips PYTHONPATH from child
processes: a template's guarded `from fused_render.shell.mounts import ...`
silently takes its fallback branch there and a mount-backed path gets treated as
local.

Two things must hold and are pinned here:
  1. PARITY — appenv's answers match `shell.mounts` for the same filesystem,
     including the symlink-into-mounts case a pure string check would miss.
  2. DECOUPLING — the module imports and answers correctly in an interpreter
     that CANNOT see `fused_render` at all (spawned subprocess, scrubbed
     sys.path/env), which is the situation it was written for.

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

import fused_render.shell.mounts as mounts_mod

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
    # The read-only list is cached on the in-process write counter, which knows
    # nothing about a test re-pointing the home dir underneath it — clear it so a
    # previous test's mountpoints can't be served for this test's home.
    monkeypatch.setattr(mounts_mod, "_ro_cache", None)
    (h / "mounts").mkdir(parents=True)
    return h


@pytest.fixture()
def exported(home, monkeypatch):
    """Run the startup export against the tmp home, as the server does."""
    from fused_render import server

    monkeypatch.delenv("FUSED_RENDER_HOME_DIR", raising=False)
    monkeypatch.delenv("FUSED_RENDER_MOUNTS_DIR", raising=False)
    monkeypatch.delenv("FUSED_RENDER_RO_MOUNTS", raising=False)
    server.export_app_env()
    return home


def _write_store(home, mounts):
    """Write mounts.json the way the shell does, bumping the generation so the
    read-only cache (keyed on it) can't serve a stale list."""
    (home / "mounts.json").write_text(json.dumps(mounts))
    mounts_mod._mounts_generation += 1


# ----------------------------------------------------------------- fallbacks

def test_dirs_fall_back_to_the_unbranched_baseline(appenv, monkeypatch):
    """Absent vars => a standalone copy of a template still resolves something
    sane, with no exception: FUSED_RENDER_HOME if set, else ~/.fused-render."""
    monkeypatch.delenv("FUSED_RENDER_HOME_DIR", raising=False)
    monkeypatch.delenv("FUSED_RENDER_MOUNTS_DIR", raising=False)
    monkeypatch.setenv("FUSED_RENDER_HOME", "/tmp/fr-home")
    assert appenv.home_dir() == "/tmp/fr-home"
    assert appenv.mounts_dir() == os.path.normpath("/tmp/fr-home/mounts")

    monkeypatch.delenv("FUSED_RENDER_HOME", raising=False)
    assert appenv.home_dir() == os.path.expanduser("~/.fused-render")


def test_home_dir_var_is_taken_verbatim(appenv, monkeypatch):
    """The var is exported ALREADY branch-resolved, so appenv must not re-derive
    the nesting on top of it (that would yield branches/<ref>/branches/<ref>)."""
    monkeypatch.setenv("FUSED_RENDER_HOME", "/tmp/fr-home")
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "feature-x")
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", "/tmp/fr-home/branches/feature-x")
    assert appenv.home_dir() == "/tmp/fr-home/branches/feature-x"
    assert appenv.mounts_dir() == os.path.normpath(
        "/tmp/fr-home/branches/feature-x/mounts")


def test_empty_vars_behave_as_absent(appenv, monkeypatch):
    """An exported-but-empty var must not win and produce "" / "mounts"."""
    monkeypatch.setenv("FUSED_RENDER_HOME", "/tmp/fr-home")
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", "")
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", "")
    assert appenv.home_dir() == "/tmp/fr-home"
    assert appenv.mounts_dir() == os.path.normpath("/tmp/fr-home/mounts")


def test_ro_mounts_absent_or_empty_is_no_mounts(appenv, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_RO_MOUNTS", raising=False)
    assert appenv.read_only_mountpoints() == []
    monkeypatch.setenv("FUSED_RENDER_RO_MOUNTS", "")
    assert appenv.read_only_mountpoints() == []
    # A stray trailing separator must not yield a "" entry — that would
    # prefix-match every path and mark the whole filesystem read-only.
    monkeypatch.setenv("FUSED_RENDER_RO_MOUNTS", "/a/b" + os.pathsep)
    assert appenv.read_only_mountpoints() == ["/a/b"]


def test_origin_absent_is_none(appenv, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    assert appenv.origin() is None
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:32953")
    assert appenv.origin() == "http://127.0.0.1:32953"


# --------------------------------------------------- is_mount_backed parity

def test_is_mount_backed_parity(appenv, exported, tmp_path):
    home = exported
    mroot = home / "mounts"
    cases = [
        str(mroot),                       # the mounts root itself
        str(mroot / "s3" / "a" / "f.tif"),  # under a mount
        str(tmp_path / "local.txt"),      # plainly local
        str(home / "mounts-sibling"),     # prefix-adjacent, must NOT match
    ]
    for p in cases:
        assert appenv.is_mount_backed(p) == mounts_mod.is_mount_backed(p), p
    assert appenv.is_mount_backed(str(mroot / "s3" / "a" / "f.tif"))
    assert not appenv.is_mount_backed(str(home / "mounts-sibling"))


def test_is_mount_backed_follows_a_symlink_into_the_mounts_dir(
        appenv, exported, tmp_path):
    """The case a pure string check gets WRONG: the link's own path is local, so
    only the realpath retry classifies it as mount-backed."""
    target = exported / "mounts" / "s3" / "deep"
    target.mkdir(parents=True)
    link = tmp_path / "shortcut"
    link.symlink_to(target)

    assert appenv.is_mount_backed(str(link))
    assert appenv.is_mount_backed(str(link / "f.tif"))
    assert appenv.is_mount_backed(str(link)) == mounts_mod.is_mount_backed(str(link))
    # Sanity: the naive string check really would have said "local" here.
    assert not str(link).startswith(str(exported / "mounts"))


# --------------------------------------------------- mount_read_only parity

def test_mount_read_only_parity(appenv, exported):
    home = exported
    _write_store(home, [
        {"id": "1", "name": "ro", "remote": "pub:", "read_only": True},
        {"id": "2", "name": "rw", "remote": "priv:"},
    ])
    mounts_mod.export_ro_mounts_env()

    ro = home / "mounts" / "ro"
    rw = home / "mounts" / "rw"
    local = home / "elsewhere.txt"

    # not mount-backed => False (a local file is never read-only for THIS reason)
    assert appenv.mount_read_only(str(local)) is False
    # mount-backed but not flagged read_only => False
    assert appenv.mount_read_only(str(rw / "f.tif")) is False
    # under a read-only mountpoint => True
    assert appenv.mount_read_only(str(ro / "a" / "f.tif")) is True
    # the exact mountpoint => True
    assert appenv.mount_read_only(str(ro)) is True
    # a prefix-adjacent sibling of the ro mountpoint must NOT match
    assert appenv.mount_read_only(str(home / "mounts" / "ro-extra")) is False

    for p in (local, rw / "f.tif", ro / "a" / "f.tif", ro,
              home / "mounts" / "ro-extra"):
        assert appenv.mount_read_only(str(p)) == mounts_mod.mount_read_only(str(p)), p


def test_ro_list_is_reread_per_call(appenv, exported):
    """Nothing is cached at import time: a long-lived template daemon must see a
    mount that becomes read-only after it started."""
    home = exported
    f = home / "mounts" / "later" / "f.tif"
    assert appenv.mount_read_only(str(f)) is False

    _write_store(home, [{"id": "1", "name": "later", "remote": "pub:",
                         "read_only": True}])
    mounts_mod.export_ro_mounts_env()
    assert appenv.mount_read_only(str(f)) is True


# ------------------------------------------------------------ server exports

def test_export_app_env_sets_all_three_vars(exported, monkeypatch):
    """The startup hook must leave every var set — including an EMPTY
    FUSED_RENDER_RO_MOUNTS for a server with no read-only mount, so a child can
    tell "no read-only mounts" from "nobody told me"."""
    assert os.environ["FUSED_RENDER_HOME_DIR"] == str(exported)
    assert os.environ["FUSED_RENDER_MOUNTS_DIR"] == str(exported / "mounts")
    assert os.environ["FUSED_RENDER_RO_MOUNTS"] == ""
    # The skill plugin root (D212) rides the same export. It is the one var here
    # that may legitimately be ABSENT — a `claude` whose `--help` has no
    # `--plugin-dir`, or a sync with nothing to copy — so this asserts only that
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
    assert shutil.which("uv") == str(uv)


def test_ro_mounts_env_tracks_a_store_write(home, monkeypatch):
    """A read-only mount added through the store (not by hand) shows up in the
    var — the _write hook, not just the startup export."""
    from fused_render import server

    monkeypatch.delenv("FUSED_RENDER_RO_MOUNTS", raising=False)
    server.export_app_env()
    assert os.environ["FUSED_RENDER_RO_MOUNTS"] == ""

    mounts_mod.add_mount("pubdata", "pub:bucket", read_only=True)
    assert os.environ["FUSED_RENDER_RO_MOUNTS"] == str(home / "mounts" / "pubdata")

    appenv = _load_appenv()
    assert appenv.mount_read_only(str(home / "mounts" / "pubdata" / "f.tif")) is True

    # ...and drops back out when the mount is deleted (same hook, other way).
    cid = mounts_mod.list_mounts()[0]["id"]
    _write_store(home, [c for c in mounts_mod.list_mounts() if c["id"] != cid])
    mounts_mod.export_ro_mounts_env()
    assert os.environ["FUSED_RENDER_RO_MOUNTS"] == ""


# --------------------------------------------------------------- decoupling

def test_appenv_works_without_fused_render_importable(tmp_path):
    """The whole point: import + answer in an interpreter that cannot see the
    package. Run with cwd outside the repo, PYTHONPATH cleared, and every
    sys.path entry that can reach `fused_render` stripped — the fused local
    backend's child environment, reproduced.
    """
    mroot = tmp_path / "home" / "mounts"
    (mroot / "ro").mkdir(parents=True)
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
        assert appenv.mounts_dir() == {str(mroot)!r}, appenv.mounts_dir()
        assert appenv.is_mount_backed({str(mroot / "ro" / "f.tif")!r})
        assert not appenv.is_mount_backed({str(tmp_path / "local.txt")!r})
        assert appenv.mount_read_only({str(mroot / "ro" / "f.tif")!r})
        assert not appenv.mount_read_only({str(mroot / "rw" / "f.tif")!r})
        assert appenv.origin() == "http://127.0.0.1:32953"
        print("ok")
    """)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["FUSED_RENDER_HOME_DIR"] = str(tmp_path / "home")
    env["FUSED_RENDER_MOUNTS_DIR"] = str(mroot)
    env["FUSED_RENDER_RO_MOUNTS"] = str(mroot / "ro")
    env["FUSED_RENDER_ORIGIN"] = "http://127.0.0.1:32953"

    out = subprocess.run([sys.executable, "-c", script], cwd=str(tmp_path),
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_appenv_imports_only_the_stdlib():
    """Guard the constraint at the AST level: nothing but `os`/`ntpath` is
    imported, so neither fused_render nor a third-party package can creep in
    (either would break the standalone / no-PYTHONPATH case the module exists
    for). `ntpath` is stdlib (pure path classification, no OS calls even on
    non-Windows hosts) - sidecar_path's Windows-shaped/UNC mapping needs it,
    for the same reason _view_url_codec.py builds its own path classification
    on pathlib.PureWindowsPath rather than a live os.path."""
    import ast
    with open(APPENV_PATH, encoding="utf-8") as f:
        src = f.read()
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names <= {"os", "ntpath"}, names
