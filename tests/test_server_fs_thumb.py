"""`GET /api/fs/thumb` — a small JPEG of a picture the browser could not show
itself (an iPhone's HEIC), cached per source path + mtime + size."""
import os
import sys

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from fused_render.server import create_app
from fused_render.server.routers import fs_read


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(fs_read, "_thumb_dir", lambda: str(tmp_path / "thumbs"))
    return TestClient(create_app(start_dir=str(tmp_path)))


def _png(path, size=(800, 600), colour=(200, 30, 30)):
    Image.new("RGB", size, colour).save(path)
    return str(path)


def test_a_png_comes_back_as_a_small_jpeg(client, tmp_path):
    src = _png(tmp_path / "big.png")
    resp = client.get("/api/fs/thumb", params={"path": src, "size": 160})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    import io
    with Image.open(io.BytesIO(resp.content)) as thumb:
        assert max(thumb.size) == 160
        assert thumb.size == (160, 120)


def test_the_size_snaps_to_a_fixed_ladder_and_the_file_is_cached(client, tmp_path):
    src = _png(tmp_path / "a.png")
    client.get("/api/fs/thumb", params={"path": src, "size": 150})
    client.get("/api/fs/thumb", params={"path": src, "size": 170})
    files = os.listdir(tmp_path / "thumbs")
    assert len(files) == 1, files  # both rounded to 160, one cache entry
    assert files[0].endswith(".jpg")


def test_an_edited_picture_gets_a_new_thumbnail(client, tmp_path):
    src = _png(tmp_path / "a.png")
    first = client.get("/api/fs/thumb", params={"path": src}).content
    _png(tmp_path / "a.png", colour=(0, 0, 255))
    os.utime(src, (1, 1))  # mtime is part of the key
    second = client.get("/api/fs/thumb", params={"path": src}).content
    assert first != second


def test_a_sideways_phone_photo_is_rotated_upright(client, tmp_path):
    src = tmp_path / "rot.jpg"
    image = Image.new("RGB", (400, 200), (1, 2, 3))
    exif = image.getexif()
    exif[0x0112] = 6  # orientation: rotate 90 CW to display
    image.save(src, exif=exif)
    resp = client.get("/api/fs/thumb", params={"path": str(src), "size": 160})
    import io
    with Image.open(io.BytesIO(resp.content)) as thumb:
        assert thumb.size == (80, 160)


def test_refusals_name_the_reason(client, tmp_path):
    assert client.get("/api/fs/thumb", params={"path": "relative.png"}).status_code == 400
    assert client.get("/api/fs/thumb", params={"path": str(tmp_path / "x.mp3")}).status_code == 415
    assert client.get("/api/fs/thumb", params={"path": str(tmp_path / "gone.png")}).status_code == 404
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png")
    resp = client.get("/api/fs/thumb", params={"path": str(bad)})
    assert resp.status_code == 415
    assert "bad.png" in resp.json()["error"]


@pytest.mark.skipif(sys.platform != "darwin", reason="sips is macOS")
def test_a_heic_is_decoded_by_sips_on_a_mac(client, tmp_path):
    import subprocess
    src = _png(tmp_path / "src.png", size=(640, 480))
    heic = tmp_path / "photo.HEIC"
    made = subprocess.run(["sips", "-s", "format", "heic", src, "--out", str(heic)],
                          capture_output=True)
    if made.returncode != 0:
        pytest.skip("this Mac's sips cannot write HEIC")
    resp = client.get("/api/fs/thumb", params={"path": str(heic), "size": 160})
    assert resp.status_code == 200, resp.text
    import io
    with Image.open(io.BytesIO(resp.content)) as thumb:
        assert thumb.size == (160, 120)


def test_heic_off_a_mac_is_a_clear_415(client, tmp_path, monkeypatch):
    monkeypatch.setattr(fs_read, "_sips_here", lambda: False)
    heic = tmp_path / "photo.heic"
    heic.write_bytes(b"\x00\x00\x00\x18ftypheic")
    resp = client.get("/api/fs/thumb", params={"path": str(heic)})
    assert resp.status_code == 415
    assert "macOS" in resp.json()["error"]


def test_a_transparent_png_lands_on_white_not_black(client, tmp_path):
    import io
    src = tmp_path / "t.png"
    Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(src)
    resp = client.get("/api/fs/thumb", params={"path": str(src), "size": 64})
    with Image.open(io.BytesIO(resp.content)) as thumb:
        assert thumb.getpixel((5, 5))[0] > 240


def test_no_temp_file_is_left_behind_on_a_failure(client, tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png")
    client.get("/api/fs/thumb", params={"path": str(bad)})
    assert not [f for f in os.listdir(tmp_path / "thumbs") if f.endswith(".tmp")]


def test_a_decompression_bomb_is_a_415_not_a_500(client, tmp_path, monkeypatch):
    from PIL import Image as _Image
    src = _png(tmp_path / "huge.png", size=(10, 10))
    monkeypatch.setattr(_Image, "MAX_IMAGE_PIXELS", 10)
    resp = client.get("/api/fs/thumb", params={"path": src})
    assert resp.status_code == 415


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="needs a mode bit the reader cannot override")
def test_a_cache_hit_still_needs_read_access_to_the_source(client, tmp_path):
    """A thumbnail made while the photo was readable must stop answering
    once it is not: the cached JPEG is served only after the source opens,
    exactly as `/api/fs/raw` probes before every send."""
    src = _png(tmp_path / "private.png")
    assert client.get("/api/fs/thumb", params={"path": src}).status_code == 200
    os.chmod(src, 0)
    try:
        resp = client.get("/api/fs/thumb", params={"path": src})
        assert resp.status_code == 403, resp.text
    finally:
        os.chmod(src, 0o644)
    assert client.get("/api/fs/thumb", params={"path": src}).status_code == 200
