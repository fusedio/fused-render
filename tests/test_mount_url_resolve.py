"""Tests for cloud-URL resolution in the path bar (shell/mounts.py
resolve_cloud_url + GET /api/mounts/resolve): s3:// / gs:// / gcs:// typed into
the shell's Ctrl/Cmd+L path bar turn into the local path under the mount that
covers them, or fail with a message naming the missing mount.

FUSED_RENDER_HOME is redirected per test (same isolation as
test_shell_mounts.py) and rclone is never invoked — the remote configs come
from a stubbed `rclone config dump`.
"""
import os

import pytest

import fused_render.shell.mounts as mounts_mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture()
def rclone(monkeypatch):
    """Stub the remote configs. Returns the dict tests fill with
    {remote_name: {"type": ...}}; rcd is forced unavailable so a miss can't
    silently fall through to a live daemon."""
    configs: dict = {}
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/usr/bin/rclone")
    monkeypatch.setattr(mounts_mod, "_rclone_config_dump", lambda _bin: configs)
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda **kw: None)
    return configs


def _mp(name: str) -> str:
    return os.path.join(mounts_mod.mounts_dir(), name)


# -- resolution ------------------------------------------------------------


def test_bucket_mount_resolves_key(home, rclone):
    rclone["aws"] = {"type": "s3"}
    mounts_mod.add_mount("data", "aws:my-bucket")
    assert mounts_mod.resolve_cloud_url("s3://my-bucket/a/b.tif") == \
        os.path.join(_mp("data"), "a", "b.tif")


def test_bucket_root_resolves_to_mountpoint(home, rclone):
    rclone["aws"] = {"type": "s3"}
    mounts_mod.add_mount("data", "aws:my-bucket")
    assert mounts_mod.resolve_cloud_url("s3://my-bucket") == _mp("data")
    assert mounts_mod.resolve_cloud_url("s3://my-bucket/") == _mp("data")


def test_bucketless_remote_puts_bucket_under_mountpoint(home, rclone):
    rclone["aws"] = {"type": "s3"}
    mounts_mod.add_mount("aws-all", "aws:")
    assert mounts_mod.resolve_cloud_url("s3://b/k.json") == \
        os.path.join(_mp("aws-all"), "b", "k.json")


def test_prefix_mount_strips_its_prefix(home, rclone):
    rclone["aws"] = {"type": "s3"}
    mounts_mod.add_mount("tiles", "aws:my-bucket/tiles")
    assert mounts_mod.resolve_cloud_url("s3://my-bucket/tiles/z/x.png") == \
        os.path.join(_mp("tiles"), "z", "x.png")
    # The prefix itself is the mount root.
    assert mounts_mod.resolve_cloud_url("s3://my-bucket/tiles") == _mp("tiles")


def test_key_outside_the_prefix_is_not_covered(home, rclone):
    rclone["aws"] = {"type": "s3"}
    mounts_mod.add_mount("tiles", "aws:my-bucket/tiles")
    # "tiles-old" must not match the "tiles" prefix on a raw string prefix test.
    with pytest.raises(mounts_mod.CloudUrlError):
        mounts_mod.resolve_cloud_url("s3://my-bucket/tiles-old/x.png")


def test_most_specific_mount_wins(home, rclone):
    rclone["aws"] = {"type": "s3"}
    mounts_mod.add_mount("everything", "aws:")
    mounts_mod.add_mount("bucket", "aws:my-bucket")
    mounts_mod.add_mount("tiles", "aws:my-bucket/tiles")
    assert mounts_mod.resolve_cloud_url("s3://my-bucket/tiles/x.png") == \
        os.path.join(_mp("tiles"), "x.png")
    assert mounts_mod.resolve_cloud_url("s3://my-bucket/other/x.png") == \
        os.path.join(_mp("bucket"), "other", "x.png")
    assert mounts_mod.resolve_cloud_url("s3://elsewhere/x.png") == \
        os.path.join(_mp("everything"), "elsewhere", "x.png")


@pytest.mark.parametrize("scheme", ["gs", "gcs", "GS"])
def test_gcs_schemes_resolve_through_a_gcs_mount(home, rclone, scheme):
    rclone["g"] = {"type": "google cloud storage"}
    mounts_mod.add_mount("gcs", "g:my-bucket")
    assert mounts_mod.resolve_cloud_url(f"{scheme}://my-bucket/k") == \
        os.path.join(_mp("gcs"), "k")


def test_backend_type_decides_not_remote_name(home, rclone):
    """A GCS remote that happens to be NAMED "s3" must not serve s3:// URLs."""
    rclone["s3"] = {"type": "google cloud storage"}
    mounts_mod.add_mount("gcs", "s3:my-bucket")
    with pytest.raises(mounts_mod.CloudUrlError) as e:
        mounts_mod.resolve_cloud_url("s3://my-bucket/k")
    assert "no s3:// mount is connected" in str(e.value)


# -- errors ----------------------------------------------------------------


def test_no_mount_of_that_kind(home, rclone):
    with pytest.raises(mounts_mod.CloudUrlError) as e:
        mounts_mod.resolve_cloud_url("s3://b/k")
    assert "no s3:// mount is connected" in str(e.value)
    assert e.value.status == 404


def test_mounts_exist_but_none_covers_the_bucket(home, rclone):
    rclone["aws"] = {"type": "s3"}
    mounts_mod.add_mount("data", "aws:my-bucket")
    with pytest.raises(mounts_mod.CloudUrlError) as e:
        mounts_mod.resolve_cloud_url("s3://other-bucket/k")
    assert "no mount covers s3://other-bucket" in str(e.value)
    assert e.value.status == 404


@pytest.mark.parametrize("url,fragment", [
    ("/home/me/x", "not a URL"),
    ("https://example.com/x", "https:// URLs can't be opened"),
    ("s3://", "names no bucket"),
])
def test_malformed_urls_are_400(home, rclone, url, fragment):
    with pytest.raises(mounts_mod.CloudUrlError) as e:
        mounts_mod.resolve_cloud_url(url)
    assert fragment in str(e.value)
    assert e.value.status == 400


# -- endpoint --------------------------------------------------------------


def test_endpoint_returns_path_and_errors(home, rclone):
    rclone["aws"] = {"type": "s3"}
    mounts_mod.add_mount("data", "aws:my-bucket")
    assert mounts_mod.resolve_url_endpoint("s3://my-bucket/k") == \
        {"path": os.path.join(_mp("data"), "k")}

    miss = mounts_mod.resolve_url_endpoint("s3://nope/k")
    assert miss.status_code == 404
    assert b"no mount covers" in miss.body

    bad = mounts_mod.resolve_url_endpoint("ftp://h/x")
    assert bad.status_code == 400
