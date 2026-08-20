"""The .fused single-file app export/open (SPEC §43, D385-D387): export walks
the whole app folder into a manifest+payload zip, open extracts it hardened,
read-only, content-addressed, and the shared view-URL codec routes a
double-clicked .fused through the /openfused confirm page."""

import io
import json
import os
import zipfile

import pytest

from fused_render import appfile
from fused_render._view_url_codec import embed_url_path, view_url_path

MARKER = '<meta charset="utf-8" />\n<meta name="fused-app" />'


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Per-test shell home, the same way test_registered_apps.py does it.

    conftest allocates ONE FUSED_RENDER_HOME per process, so without this the
    AF-8 route test's `/render` of an extracted app writes a `linked` entry for
    `<tmp>/cache/demo-<hash>` into a registered_apps.json shared with every
    later test on the same xdist worker — where it surfaced as a phantom
    `demo-<hash>` card in other modules' /api/apps listing assertions."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def make_app(tmp_path, name="demo"):
    d = tmp_path / name
    d.mkdir()
    (d / "index.html").write_text(
        f"<html><head>{MARKER}<title>Demo</title></head><body>hi</body></html>"
    )
    (d / "data.py").write_text("def main():\n    return {'ok': True}\n")
    (d / "assets").mkdir()
    (d / "assets" / "logo.svg").write_text("<svg/>")
    return d


def test_export_then_open_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(appfile, "appfiles_root", lambda: str(tmp_path / "cache"))
    app = make_app(tmp_path)
    out = tmp_path / "demo.fused"
    manifest = appfile.export_app_file(str(app), str(out))
    assert manifest["fused_app_file"] == 1
    assert manifest["entry"] == "index.html"
    assert manifest["name"] == "demo"

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert names == {
        "manifest.json",
        "files/index.html",
        "files/data.py",
        "files/assets/logo.svg",
    }

    result = appfile.open_app_file(str(out))
    assert result["reused"] is False
    assert os.path.isfile(result["entry"])
    assert result["entry"].startswith(result["dir"])
    # RO-7 posture: every extracted file is read-only.
    assert not os.access(result["entry"], os.W_OK)
    assert not os.access(os.path.join(result["dir"], "data.py"), os.W_OK)

    # Same bytes, same content key: the second open re-uses the extract.
    again = appfile.open_app_file(str(out))
    assert again["reused"] is True
    assert again["dir"] == result["dir"]


def test_export_skips_machinery_and_hidden(tmp_path):
    app = make_app(tmp_path)
    (app / ".git").mkdir()
    (app / ".git" / "HEAD").write_text("ref")
    (app / ".env").write_text("SECRET=1")
    (app / "CLAUDE.md").write_text("authoring contract")
    (app / "node_modules").mkdir()
    (app / "node_modules" / "x.js").write_text("x")
    (app / "__pycache__").mkdir()
    (app / "__pycache__" / "d.pyc").write_bytes(b"\x00")
    out = tmp_path / "demo.fused"
    appfile.export_app_file(str(app), str(out))
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert not any(
        ".git" in n or ".env" in n or "CLAUDE" in n or "node_modules" in n or "pycache" in n
        for n in names
    )


def test_export_refuses_non_app_folder(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "index.html").write_text("<html><head></head></html>")  # no marker
    with pytest.raises(appfile.AppFileError, match="not a fused app"):
        appfile.export_app_file(str(d), str(tmp_path / "x.fused"))


def test_export_allows_fused_ai_pages(tmp_path):
    # Unlike the hosted exporter (RH-11), fused.ai() ships: an opened .fused
    # runs in the recipient's full local runtime, where /api/ai exists (D388).
    # A recipient without a claude CLI gets the graceful ai_unavailable state.
    app = make_app(tmp_path)
    (app / "chat.html").write_text("<html><script>fused.ai('hi')</script></html>")
    out = tmp_path / "x.fused"
    appfile.export_app_file(str(app), str(out))
    with zipfile.ZipFile(out) as zf:
        assert "files/chat.html" in zf.namelist()


def test_export_refuses_existing_out_path(tmp_path):
    app = make_app(tmp_path)
    out = tmp_path / "demo.fused"
    out.write_text("occupied")
    with pytest.raises(appfile.AppFileError, match="overwrite"):
        appfile.export_app_file(str(app), str(out))
    assert out.read_text() == "occupied"


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_open_rejects_zip_slip(tmp_path, monkeypatch):
    monkeypatch.setattr(appfile, "appfiles_root", lambda: str(tmp_path / "cache"))
    bad = tmp_path / "evil.fused"
    bad.write_bytes(
        _zip_bytes(
            {
                "manifest.json": json.dumps(
                    {"fused_app_file": 1, "root": "files", "name": "evil", "entry": "index.html"}
                ),
                "files/index.html": f"<html><head>{MARKER}</head></html>",
                "../escape.txt": "outside",
            }
        )
    )
    with pytest.raises(appfile.AppFileError, match="escape|rejected"):
        appfile.open_app_file(str(bad))
    assert not (tmp_path / "escape.txt").exists()


def test_open_rejects_manifest_traversal_entry(tmp_path):
    bad = tmp_path / "evil.fused"
    bad.write_bytes(
        _zip_bytes(
            {
                "manifest.json": json.dumps(
                    {"fused_app_file": 1, "root": "files", "name": "e", "entry": "../../x.html"}
                )
            }
        )
    )
    with pytest.raises(appfile.AppFileError, match="invalid entry"):
        appfile.read_manifest(str(bad))


def test_open_rejects_markerless_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(appfile, "appfiles_root", lambda: str(tmp_path / "cache"))
    bad = tmp_path / "nomarker.fused"
    bad.write_bytes(
        _zip_bytes(
            {
                "manifest.json": json.dumps(
                    {"fused_app_file": 1, "root": "files", "name": "n", "entry": "index.html"}
                ),
                "files/index.html": "<html><head></head></html>",
            }
        )
    )
    with pytest.raises(appfile.AppFileError, match="fused-app"):
        appfile.open_app_file(str(bad))


def test_open_rejects_backslash_entry(tmp_path):
    # `..\..\x.html` passes a `/`-split ".." check but joins as traversal on
    # Windows; backslashes are rejected outright (the exporter never writes them).
    bad = tmp_path / "evil.fused"
    bad.write_bytes(
        _zip_bytes(
            {
                "manifest.json": json.dumps(
                    {"fused_app_file": 1, "root": "files", "name": "e",
                     "entry": "..\\..\\x.html"}
                )
            }
        )
    )
    with pytest.raises(appfile.AppFileError, match="invalid entry"):
        appfile.read_manifest(str(bad))


def test_manifest_read_is_size_capped(tmp_path):
    # read_manifest runs before the capped extractor; a crafted zip declaring
    # a huge manifest must not be decompressed unbounded into memory.
    bomb = tmp_path / "bomb.fused"
    bomb.write_bytes(
        _zip_bytes({"manifest.json": " " * (appfile._MANIFEST_CAP_BYTES + 100)})
    )
    with pytest.raises(appfile.AppFileError, match="too large"):
        appfile.read_manifest(str(bomb))


def test_open_rejects_non_fused_zip(tmp_path):
    plain = tmp_path / "plain.fused"
    plain.write_bytes(_zip_bytes({"readme.txt": "hello"}))
    with pytest.raises(appfile.AppFileError, match="manifest"):
        appfile.read_manifest(str(plain))


def test_sweep_never_evicts_live_extracts(tmp_path, monkeypatch):
    # The staging sweep runs on every open against dirs whose mtime never
    # advances — it must only ever see .staging/, or a day-old extracted app
    # would be rmtree'd out from under its hub card and open tabs.
    monkeypatch.setattr(appfile, "appfiles_root", lambda: str(tmp_path / "cache"))
    app = make_app(tmp_path)
    out = tmp_path / "demo.fused"
    appfile.export_app_file(str(app), str(out))
    first = appfile.open_app_file(str(out))
    # Age the extract (and a leftover staging dir) past the TTL.
    old = 1_000_000_000
    os.utime(first["dir"], (old, old))
    stale = os.path.join(str(tmp_path / "cache"), ".staging", "open-stale")
    os.makedirs(stale)
    os.utime(stale, (old, old))

    other = make_app(tmp_path, name="other")
    out2 = tmp_path / "other.fused"
    appfile.export_app_file(str(other), str(out2))
    appfile.open_app_file(str(out2))

    assert os.path.isfile(first["entry"])  # the live extract survived
    assert not os.path.isdir(stale)  # the stale staging dir did not


def test_codec_routes_os_opened_fused_to_embed():
    # A Finder/Explorer double-click lands on the file's own EMBED url — the
    # fusedapp template renders the app chrome-free (D390). In-explorer clicks
    # use the ordinary view prefix and need no special case.
    assert view_url_path("/tmp/My App.fused") == "/explorer/embed/tmp/My%20App.fused"
    # Case-insensitive like .bookmark's check.
    assert view_url_path("/a/B.FUSED") == "/explorer/embed/a/B.FUSED"
    # And the embed path the template iframes the entry into is the same codec.
    assert embed_url_path("/x/demo/index.html") == "/explorer/embed/x/demo/index.html"


def test_routes_export_and_gateless_open(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fused_render.server.app import create_app

    monkeypatch.setattr(appfile, "appfiles_root", lambda: str(tmp_path / "cache"))
    # Isolated home: the open below records into appfile_recents.json (D396),
    # and a write into the session-shared home would leak an "exported" row
    # into every later /api/apps assertion.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    client = TestClient(create_app(start_dir=str(tmp_path)))
    app_dir = make_app(tmp_path)

    r = client.get("/api/appfile/export", params={"path": str(app_dir)})
    assert r.status_code == 200
    assert "demo.fused" in r.headers["content-disposition"]
    fused_path = tmp_path / "demo.fused"
    fused_path.write_bytes(r.content)

    # D390: no user-facing open route — the fusedapp template calls the
    # X-Fused-guarded POST and iframes the answered embed URL.
    r = client.post("/api/appfile/open", json={"file": str(fused_path)})
    assert r.status_code == 403
    r = client.post(
        "/api/appfile/open", json={"file": str(fused_path)}, headers={"X-Fused": "1"}
    )
    assert r.status_code == 200
    assert r.json()["view"].startswith("/explorer/embed/")
    extracted = appfile.open_app_file(str(fused_path))
    assert extracted["reused"] is True  # the POST above already extracted

    # A bad export target answers 400 with the reason.
    r = client.get("/api/appfile/export", params={"path": str(tmp_path / "nope")})
    assert r.status_code == 400
    assert "error" in r.json()

    # A junk .fused answers the error the template renders as its fail state.
    junk = tmp_path / "junk.fused"
    junk.write_bytes(b"not a zip")
    r = client.post(
        "/api/appfile/open", json={"file": str(junk)}, headers={"X-Fused": "1"}
    )
    assert r.status_code == 400
    assert "error" in r.json()

    # The .fused extension resolves to the fusedapp template on stat, which is
    # what makes /explorer/view|embed/<path>.fused render the app at all.
    r = client.get("/api/fs/stat", params={"path": str(fused_path)}, headers={"X-Fused": "1"})
    assert r.status_code == 200
    modes = [t["mode"] for t in r.json().get("templates") or []]
    assert "fusedapp" in modes

    # AF-8 (revised by D396): the hub identity of an opened app file is the
    # .fused FILE — POST /api/appfile/open recorded it into appfile_recents —
    # and the extract dir is refused by the registered-apps store, so
    # rendering the extracted entry does NOT register the cache dir.
    r = client.get("/render", params={"path": extracted["entry"]})
    assert r.status_code == 200
    from fused_render import exported_apps, registered_apps

    assert not any(
        os.path.abspath(e["path"]) == extracted["dir"]
        for e in registered_apps.read_entries()
    )
    assert any(
        os.path.abspath(e["path"]) == str(fused_path)
        for e in exported_apps.read_recents()
    )
