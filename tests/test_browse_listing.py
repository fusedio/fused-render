"""File-explorer directory listing (browse.py).

All 7 templates share the same browse.py logic (only zarr_aoi differs, in the
store-extension classification), so these tests exercise the geotiff copy as the
representative; a couple of assertions pin the zarr_aoi store behaviour too.
"""
import pytest

from fused_render.templates.geotiff import browse as br
from fused_render.templates.zarr_aoi import browse as zbr


def test_local_listing(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.tif").write_bytes(b"x" * 7)
    (tmp_path / "b.txt").write_bytes(b"y" * 3)
    (tmp_path / ".hidden").write_bytes(b"z")
    res = br.main(dir=str(tmp_path), exts=".tif,.tiff")
    assert "error" not in res
    assert [d["name"] for d in res["dirs"]] == ["sub"]
    assert [f["name"] for f in res["files"]] == ["a.tif"]
    assert res["files"][0]["size"] == 7


def test_file_path_descends_to_parent(tmp_path):
    (tmp_path / "a.tif").write_bytes(b"x")
    res = br.main(dir=str(tmp_path / "a.tif"), exts=".tif")
    assert res["dir"] == str(tmp_path).replace("\\", "/")
    assert [f["name"] for f in res["files"]] == ["a.tif"]


def test_zarr_store_classified_as_loadable(tmp_path):
    # zarr_aoi: a directory named *.zarr is a loadable store, not a folder.
    (tmp_path / "data.zarr").mkdir()
    (tmp_path / "plain").mkdir()
    res = zbr.main(dir=str(tmp_path), exts=".zarr", store_exts=".zarr")
    assert [d["name"] for d in res["dirs"]] == ["plain"]
    store = [f for f in res["files"] if f["name"] == "data.zarr"][0]
    assert store["is_dir"] is True and store["loadable"] is True


# --------------------------------------------------------------------------
# breadcrumb roots (both copies of browse.py)
# --------------------------------------------------------------------------
# `main()` derives `dir` from os.path.abspath, so on a POSIX test host it is
# always "/..." — a Windows drive root and a UNC root cannot be produced
# through it at all. _crumbs is a pure string helper for exactly that reason:
# these are the two roots that shipped broken and the only way to pin them here.

@pytest.mark.parametrize("mod", [br, zbr], ids=["geotiff", "zarr_aoi"])
def test_posix_crumbs_are_unchanged(mod):
    assert mod._crumbs("/a/b") == [
        {"label": "a", "path": "/a"},
        {"label": "b", "path": "/a/b"},
    ]
    assert mod._crumbs("/only") == [{"label": "only", "path": "/only"}]
    assert mod._crumbs("/") == []


@pytest.mark.parametrize("mod", [br, zbr], ids=["geotiff", "zarr_aoi"])
def test_a_windows_drive_root_crumbs_to_the_drive_ROOT(mod):
    """"C:" alone is DRIVE-RELATIVE — it means "wherever this process last was
    on C:", not the top of the drive — so the root crumb's path must be "C:/".
    Clicking it has to land on the drive root, not somewhere unpredictable."""
    assert mod._crumbs("C:/Users/foo") == [
        {"label": "C:", "path": "C:/"},
        {"label": "Users", "path": "C:/Users"},
        {"label": "foo", "path": "C:/Users/foo"},
    ]
    assert mod._crumbs("C:/") == [{"label": "C:", "path": "C:/"}]


@pytest.mark.parametrize("mod", [br, zbr], ids=["geotiff", "zarr_aoi"])
def test_a_unc_root_keeps_its_server_and_share(mod):
    """The leading "//" is dropped by the empty-segment filter, and "//server"
    without the share is not a path at all — so server+share are ONE root
    crumb, not two, and the "//" survives."""
    assert mod._crumbs("//server/share/a/b") == [
        {"label": "//server/share", "path": "//server/share"},
        {"label": "a", "path": "//server/share/a"},
        {"label": "b", "path": "//server/share/a/b"},
    ]
    assert mod._crumbs("//server/share") == [
        {"label": "//server/share", "path": "//server/share"}]
