"""`fused-render://open?file=<.fused path>` (SPEC §26 DL-7, D889): the deep
link Render App's Edit button sends. Parsing (one percent-decode, absolute
`.fused` only), the read-only info preview, and the click-free clone that lands
on the editable copy's entry page — all through the same /clone routes the git
links use.
"""
import os
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from fused_render import appfile, deeplink
from fused_render.deeplink import DeeplinkError, app_file_path_from, parse_open_link
from fused_render.server import create_app

FUSED = {"X-Fused": "1"}
MARKER = '<meta charset="utf-8" />\n<meta name="fused-app" />'


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    # Same isolation as test_appfile_clone: the extract cache lives under the
    # home, the clone lands in the WORKSPACE (never the developer's ~/Fused).
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(appfile, "appfiles_root", lambda: str(tmp_path / "cache"))


def export(tmp_path, name="demo", out_name=None):
    d = tmp_path / name
    d.mkdir()
    (d / "index.html").write_text(
        f"<html><head>{MARKER}<title>Demo</title></head><body>hi</body></html>"
    )
    (d / "data.py").write_text("def main():\n    return {'ok': True}\n")
    out = tmp_path / (out_name or f"{name}.fused")
    appfile.export_app_file(str(d), str(out))
    return out


def link(path) -> str:
    # Render App's side of the contract: quote(path, safe="") once.
    return "fused-render://open?file=" + quote(str(path), safe="")


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


# ---- parsing -----------------------------------------------------------------


def test_file_link_decodes_the_path_once():
    path = "/Users/me/My Apps/a&b #1 100%.fused"
    assert app_file_path_from(link(path)) == path
    assert parse_open_link(link(path)) == {"kind": "file", "path": path}


def test_file_link_tolerates_a_slash_after_open_and_case():
    assert app_file_path_from("FUSED-RENDER://open/?file=%2Ftmp%2Fx.fused") == "/tmp/x.fused"


def test_git_links_and_bare_urls_are_untouched():
    assert app_file_path_from("fused-render://open?git=https://github.com/o/r") is None
    spec = parse_open_link("https://github.com/octocat/sandbox")
    assert spec["kind"] == "git"
    assert spec["repo"] == "sandbox"


@pytest.mark.parametrize("bad", [
    "relative/app.fused",
    "",
    "/tmp/not-an-app.zip",
    "/tmp/folder",
])
def test_file_link_rejects_non_absolute_or_non_fused(bad):
    with pytest.raises(DeeplinkError):
        app_file_path_from("fused-render://open?file=" + quote(bad, safe=""))


# ---- routes ------------------------------------------------------------------


def test_info_previews_the_clone_without_writing(tmp_path):
    fused = export(tmp_path)
    resp = _client(tmp_path).get("/api/clone/info", params={"src": link(fused)})
    assert resp.status_code == 200
    data = resp.json()
    assert data["kind"] == "file"
    assert data["src_file"] == str(fused)
    assert data["name"] == "demo"
    assert data["cloned"] is False
    assert data["path"].endswith("/workspace/local/demo")
    assert not os.path.exists(data["path"])


def test_info_reports_a_missing_file_as_a_400(tmp_path):
    resp = _client(tmp_path).get(
        "/api/clone/info", params={"src": link(tmp_path / "gone.fused")})
    assert resp.status_code == 400
    assert "no such file" in resp.json()["error"]


def test_clone_requires_the_guard(tmp_path):
    fused = export(tmp_path)
    assert _client(tmp_path).post("/api/clone", json={"src": link(fused)}).status_code == 403


def test_clone_lands_on_the_editable_copys_entry_page(tmp_path):
    fused = export(tmp_path)
    resp = _client(tmp_path).post("/api/clone", json={"src": link(fused)}, headers=FUSED)
    assert resp.status_code == 200
    data = resp.json()
    dest = str(tmp_path / "workspace" / "local" / "demo")
    assert data["kind"] == "file"
    assert data["cloned"] is False
    assert data["dest"] == data["target"] == dest
    assert data["view"] == "/explorer/view" + dest + "/index.html"
    assert os.access(os.path.join(dest, "index.html"), os.W_OK)


def test_a_second_link_reuses_the_copy_and_keeps_edits(tmp_path):
    fused = export(tmp_path)
    client = _client(tmp_path)
    first = client.post("/api/clone", json={"src": link(fused)}, headers=FUSED).json()
    edited = os.path.join(first["dest"], "data.py")
    with open(edited, "w") as f:
        f.write("def main():\n    return {'edited': True}\n")
    again = client.post("/api/clone", json={"src": link(fused)}, headers=FUSED).json()
    assert again["cloned"] is True
    assert again["view"] == first["view"]
    with open(edited) as f:
        assert "edited" in f.read()


def test_openurls_target_routes_a_file_link_to_the_clone_page():
    from fused_render._view_url_codec import open_target_path

    raw = link("/tmp/x.fused")
    assert open_target_path(raw) == "/clone?src=" + quote(raw, safe="")


def test_clone_page_still_serves(tmp_path):
    resp = _client(tmp_path).get("/clone?src=" + quote(link("/tmp/x.fused"), safe=""))
    assert resp.status_code == 200
    assert 'data.kind === "file"' in resp.text


def test_deeplink_module_exports(tmp_path):
    # The prefixes stay in lock-step with the git ones (open and open/).
    assert len(deeplink._OPEN_FILE_PREFIXES) == len(deeplink._OPEN_PREFIXES)
