"""The exported `.fused` file's preview member and the export routes that
carry a caller-supplied capture into it (D396's surviving half). The hub
listing of `.fused` files and the open-recency store were retired by AF-14:
an app file is an installer, and the hub lists the installed copy.
"""
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import appfile, registered_apps
from fused_render.server import create_app


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


@pytest.fixture()
def client(tmp_path, workspace):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _app_folder(root, name):
    d = root / name
    d.mkdir(parents=True)
    (d / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><body>hi</body></html>')
    return d


# ----------------------------------------------------------- preview member

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16


def test_export_bakes_a_capture_only_when_no_authored_preview(tmp_path):
    a = _app_folder(tmp_path / "one", "a")
    out_a = tmp_path / "a.fused"
    appfile.export_app_file(str(a), str(out_a), preview_bytes=PNG)
    assert appfile.read_preview(str(out_a)) == PNG

    b = _app_folder(tmp_path / "two", "b")
    (b / "preview.png").write_bytes(PNG + b"authored")
    out_b = tmp_path / "b.fused"
    appfile.export_app_file(str(b), str(out_b), preview_bytes=PNG)
    assert appfile.read_preview(str(out_b)) == PNG + b"authored"


def test_export_refuses_a_non_png_capture(tmp_path):
    a = _app_folder(tmp_path, "a")
    with pytest.raises(appfile.AppFileError):
        appfile.export_app_file(str(a), str(tmp_path / "a.fused"),
                                preview_bytes=b"GIF89a nope")


def test_preview_route_serves_the_member_or_404s(client, tmp_path):
    a = _app_folder(tmp_path / "one", "a")
    out = tmp_path / "a.fused"
    appfile.export_app_file(str(a), str(out), preview_bytes=PNG)
    r = client.get("/api/appfile/preview", params={"path": str(out)})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG

    bare = _app_folder(tmp_path / "two", "bare")
    out2 = tmp_path / "bare.fused"
    appfile.export_app_file(str(bare), str(out2))
    assert client.get("/api/appfile/preview",
                      params={"path": str(out2)}).status_code == 404
    assert client.get("/api/appfile/preview",
                      params={"path": "relative.fused"}).status_code == 400


def test_post_export_carries_the_capture_into_the_download(client, tmp_path):
    a = _app_folder(tmp_path, "posted")
    r = client.post("/api/appfile/export", data={"path": str(a)},
                    files={"preview": ("preview.png", PNG, "image/png")},
                    headers={"X-Fused": "1"})
    assert r.status_code == 200
    out = tmp_path / "posted.fused"
    out.write_bytes(r.content)
    assert appfile.read_preview(str(out)) == PNG
    # The guard holds: no X-Fused, no export.
    assert client.post("/api/appfile/export",
                       data={"path": str(a)}).status_code == 403


def test_an_over_cap_capture_is_dropped_and_the_export_still_ships(
        client, tmp_path, monkeypatch):
    """The browser side promises that EVERY capture failure exports plain
    (appShot.ts), and a screenshot that came out too big is a capture failure —
    so the route drops it. Raising here instead (which `export_app_file` does,
    correctly, for a caller that meant to supply a still) would spend a failed
    screen grab on the whole download."""
    monkeypatch.setattr(appfile, "MAX_PREVIEW_BYTES", len(PNG))
    a = _app_folder(tmp_path, "big")
    r = client.post("/api/appfile/export", data={"path": str(a)},
                    files={"preview": ("preview.png", PNG + b"over", "image/png")},
                    headers={"X-Fused": "1"})
    assert r.status_code == 200
    out = tmp_path / "big.fused"
    out.write_bytes(r.content)
    assert appfile.read_preview(str(out)) is None
    # Exactly AT the cap still ships — the route reads one byte past it.
    r = client.post("/api/appfile/export", data={"path": str(a)},
                    files={"preview": ("preview.png", PNG, "image/png")},
                    headers={"X-Fused": "1"})
    assert r.status_code == 200
    out2 = tmp_path / "atcap.fused"
    out2.write_bytes(r.content)
    assert appfile.read_preview(str(out2)) == PNG


def test_a_non_png_capture_is_still_an_error_not_a_silent_drop(client, tmp_path):
    """The other half of the split: `canvas.toBlob(…, "image/png")` cannot
    produce a non-PNG, so those bytes are a client bug worth reporting rather
    than a failed grab to swallow."""
    a = _app_folder(tmp_path, "gif")
    r = client.post("/api/appfile/export", data={"path": str(a)},
                    files={"preview": ("preview.png", b"GIF89a nope", "image/png")},
                    headers={"X-Fused": "1"})
    assert r.status_code == 400 and "PNG" in r.json()["error"]


# ------------------------------------- extract-dir double-listing suppression


def test_registered_apps_refuses_the_extract_cache(tmp_path, workspace):
    inside = os.path.join(appfile.appfiles_root(), "demo-abc123", "files")
    os.makedirs(inside)
    (open(os.path.join(inside, "index.html"), "w")).write(
        '<html><head><meta name="fused-app" /></head></html>')
    assert not registered_apps.record_open(inside)
    # A historical entry written by an older build is filtered on read too.
    registered_apps.write_entries([
        {"path": inside, "openedAt": "2026-01-01T00:00:00+00:00"}])
    assert registered_apps.read_entries() == []
