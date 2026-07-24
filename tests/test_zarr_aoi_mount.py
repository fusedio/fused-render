"""zarr_aoi daemon must recognize branch-isolated mount paths.

A per-branch dev server nests all state (mounts included) under
``~/.fused-render/branches/<ref>/`` (see fused_render.shell.storage.home_dir +
fused_render._branch). The daemon runs in its own venv with no fused_render, so
it inlines the branch-aware home dir as ``_home_dir()``. If that resolution is
wrong, a mount path fails the ``mroot`` prefix check in ``resolve_source`` and
is misread as ``kind: local`` — meaning the daemon reads the mount through the
KERNEL instead of the server's ``/api/fs/raw``, the exact NFS-wedge risk the
mount routing exists to avoid.

These tests pin ``_home_dir()`` against the real resolution in
fused_render.shell.storage so the two can't silently drift.
"""
import importlib.util
import os

import pytest

TS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "fused_render", "templates", "zarr_aoi", "tile_server.py",
)


def _load_tile_server():
    spec = importlib.util.spec_from_file_location("_zarr_aoi_ts", TS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ts():
    return _load_tile_server()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # A stray FUSED_RENDER_HOME/BRANCH in the runner env would skew the defaults.
    monkeypatch.delenv("FUSED_RENDER_HOME", raising=False)
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)


def test_baseline_home_is_unnested(ts, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", "/tmp/fr")
    assert ts._home_dir() == "/tmp/fr"


def test_branch_nests_under_branches_dir(ts, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", "/tmp/fr")
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "fix/template-kernel-listing")
    # sanitize: lowercase, collapse non-[a-z0-9] to '-', trim, truncate to 12
    assert ts._home_dir() == "/tmp/fr/branches/fix-template"


def test_default_branch_names_are_baseline(ts, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", "/tmp/fr")
    for name in ("main", "master", "HEAD", "Main"):
        monkeypatch.setenv("FUSED_RENDER_BRANCH", name)
        assert ts._home_dir() == "/tmp/fr", name


def test_matches_shell_storage_home_dir(ts, monkeypatch):
    """The inlined daemon logic must equal the authoritative resolution."""
    from fused_render.shell import storage

    monkeypatch.setenv("FUSED_RENDER_HOME", "/tmp/fr")
    for branch in ("fix/template-kernel-listing", "My Feature #2", "main", ""):
        monkeypatch.setenv("FUSED_RENDER_BRANCH", branch)
        # storage.home_dir caches branch ref per process via _branch._CACHED_REF;
        # reset it so each iteration re-resolves from the env.
        import fused_render._branch as _b
        _b._CACHED_REF = None
        assert ts._home_dir() == storage.home_dir(), branch


def test_branch_mount_path_is_under_mounts_root(ts, monkeypatch):
    """The real symptom: a branch mount path lands under the computed mroot."""
    monkeypatch.setenv("FUSED_RENDER_HOME", os.path.expanduser("~/.fused-render"))
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "fix/template-kernel-listing")
    mroot = os.path.join(ts._home_dir(), "mounts") + os.sep
    wsf = os.path.expanduser(
        "~/.fused-render/branches/fix-template/mounts/source.coop/x.zarr")
    assert wsf.startswith(mroot)
