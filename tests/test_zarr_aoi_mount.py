"""zarr_aoi daemon must recognize branch-isolated mount paths.

A per-branch dev server nests all state (mounts included) under
``~/.fused-render/branches/<ref>/`` (see fused_render.shell.storage.home_dir +
fused_render._branch). The daemon runs in its own venv with no fused_render, so
it cannot call that resolution — if it gets the home dir wrong, a mount path
fails the ``mroot`` prefix check in ``resolve_source`` and is misread as
``kind: local``, meaning the daemon reads the mount through the KERNEL instead
of the server's ``/api/fs/raw`` — the exact NFS-wedge risk the mount routing
exists to avoid.

It used to inline the branch nesting + the ``_branch.sanitize`` rule, and these
tests pinned that copy against the original. The copy is gone: the server now
exports the ALREADY-RESOLVED dir as ``FUSED_RENDER_HOME_DIR`` and the daemon
reads it through ``templates/shared/appenv.py``, so there is no second
implementation of the rule to drift. What still needs pinning is the handoff:
the exported value must equal ``storage.home_dir()`` for every branch, and a
branch mount path must land under the mounts root the daemon computes from it.
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
    # A stray FUSED_RENDER_* in the runner env would skew the defaults.
    monkeypatch.delenv("FUSED_RENDER_HOME", raising=False)
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    monkeypatch.delenv("FUSED_RENDER_HOME_DIR", raising=False)
    monkeypatch.delenv("FUSED_RENDER_MOUNTS_DIR", raising=False)


def _export(monkeypatch, branch=None, home="/tmp/fr"):
    """Run the server's startup export, as a real server does before serving."""
    from fused_render import server
    import fused_render._branch as _b

    monkeypatch.setenv("FUSED_RENDER_HOME", home)
    if branch is None:
        monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    else:
        monkeypatch.setenv("FUSED_RENDER_BRANCH", branch)
    # storage.home_dir caches the branch ref per process via _branch._CACHED_REF;
    # reset it so each call re-resolves from the env.
    _b._CACHED_REF = None
    server.export_app_env()


def test_baseline_home_is_unnested(ts, monkeypatch):
    _export(monkeypatch, branch=None)
    assert ts.appenv.home_dir() == "/tmp/fr"


def test_branch_nests_under_branches_dir(ts, monkeypatch):
    _export(monkeypatch, branch="fix/template-kernel-listing")
    # sanitize (lowercase, collapse non-[a-z0-9] to '-', trim, truncate to 12)
    # happens ONCE, app-side, and arrives already applied.
    #
    # branch_dir() nests with a plain os.path.join, never normalized before
    # FUSED_RENDER_HOME_DIR is exported — on Windows that mixes the "/tmp/fr"
    # literal's forward slashes with the newly-joined segments' backslashes,
    # so the expected value has to go through the very same join rather than
    # assume it stays all-forward-slash.
    assert ts.appenv.home_dir() == os.path.join("/tmp/fr", "branches", "fix-template")


def test_default_branch_names_are_baseline(ts, monkeypatch):
    for name in ("main", "master", "HEAD", "Main"):
        _export(monkeypatch, branch=name)
        assert ts.appenv.home_dir() == "/tmp/fr", name


def test_matches_shell_storage_home_dir(ts, monkeypatch):
    """The daemon's answer must equal the authoritative resolution."""
    from fused_render.shell import storage

    for branch in ("fix/template-kernel-listing", "My Feature #2", "main", None):
        _export(monkeypatch, branch=branch)
        assert ts.appenv.home_dir() == storage.home_dir(), branch
        assert ts.appenv.mounts_dir() == os.path.normpath(
            os.path.join(storage.home_dir(), "mounts")), branch


def test_branch_mount_path_is_under_mounts_root(ts, monkeypatch):
    """The real symptom: a branch mount path lands under the computed mroot."""
    _export(monkeypatch, branch="fix/template-kernel-listing",
            home=os.path.expanduser("~/.fused-render"))
    mroot = ts.appenv.mounts_dir() + os.sep
    # normpath, matching mounts_dir()'s own: expanduser("~/...") only expands
    # the "~" segment and concatenates the rest of this forward-slash literal
    # verbatim, so on Windows the result is userhome (backslashed) + a
    # forward-slashed tail — a mixed string mroot (normpath'd) never prefixes.
    wsf = os.path.normpath(os.path.expanduser(
        "~/.fused-render/branches/fix-template/mounts/source.coop/x.zarr"))
    assert wsf.startswith(mroot)


def test_home_dir_falls_back_when_no_server_exported_it(ts, monkeypatch):
    """A standalone copy of the template (no server around) must still resolve
    something, not raise: the un-branched baseline."""
    monkeypatch.setenv("FUSED_RENDER_HOME", "/tmp/fr")
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "fix/whatever")
    assert ts.appenv.home_dir() == "/tmp/fr"
