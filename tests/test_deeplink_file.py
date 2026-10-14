"""`fused-render://open?file=<.fused path>` (SPEC §26 DL-7): the deep link
Render App's Edit button sends. Parsing (one percent-decode, absolute `.fused`
only) and the `GET /clone` redirect that lands it on the file's own INSTALL
PAGE (AF-14) — no page of its own here, nothing written on the GET; a
malformed payload goes to Home for the shell to report.
"""
import os
from urllib.parse import quote, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from fused_render import appfile, deeplink
from fused_render._view_url_codec import canonical_fs_path
from fused_render.deeplink import (
    EDIT_APPFILE_PARAM,
    DeeplinkError,
    app_file_path_from,
    edit_appfile_redirect,
    file_payload_from,
)
from fused_render.server import create_app

FUSED = {"X-Fused": "1"}
MARKER = '<meta charset="utf-8" />\n<meta name="fused-app" />'


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "workspace"))


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


def _embed(p) -> str:
    return "/explorer/embed/" + canonical_fs_path(str(p)).lstrip("/")


def _redirect(tmp_path, src):
    resp = _client(tmp_path).get("/clone", params={"src": src}, follow_redirects=False)
    assert resp.status_code == 303
    parts = urlsplit(resp.headers["location"])
    q = dict(kv.split("=", 1) for kv in parts.query.split("&")) if parts.query else {}
    return unquote(parts.path), {k: unquote(v) for k, v in q.items()}


# ---- parsing -----------------------------------------------------------------


def test_file_link_decodes_the_path_once():
    path = "/Users/me/My Apps/a&b #1 100%.fused"
    assert app_file_path_from(link(path)) == os.path.normpath(path)


def test_file_link_tolerates_a_slash_after_open_and_case():
    assert app_file_path_from("FUSED-RENDER://open/?file=%2Ftmp%2Fx.fused") == os.path.normpath("/tmp/x.fused")


def test_git_links_and_bare_urls_are_not_file_links():
    assert app_file_path_from("fused-render://open?git=https://github.com/o/r") is None
    assert file_payload_from("https://github.com/o/r") is None


@pytest.mark.parametrize("bad", [
    "relative/app.fused",
    "",
    "/tmp/not-an-app.zip",
    "/tmp/folder",
])
def test_file_link_rejects_non_absolute_or_non_fused(bad):
    with pytest.raises(DeeplinkError):
        app_file_path_from("fused-render://open?file=" + quote(bad, safe=""))


# ---- the redirect -----------------------------------------------------------


def test_a_file_link_lands_on_the_files_install_page(tmp_path):
    fused = export(tmp_path)
    path, q = _redirect(tmp_path, link(fused))
    assert path == _embed(fused)
    assert q == {}
    # Read-only: the GET installed nothing (the page does, behind X-Fused).
    assert not (tmp_path / "workspace" / "app" / "demo").exists()


def test_an_installed_app_still_lands_on_the_install_page(tmp_path):
    """The page is where "already installed / update / diverged" is said; the
    GET never decides for the user."""
    fused = export(tmp_path)
    _client(tmp_path).post("/api/appfile/install", json={"file": str(fused)}, headers=FUSED)
    path, _q = _redirect(tmp_path, link(fused))
    assert path == _embed(fused)


def test_a_missing_file_still_lands_on_its_page_which_reports_it(tmp_path):
    gone = tmp_path / "gone.fused"
    path, _q = _redirect(tmp_path, link(gone))
    assert path == _embed(gone)


def test_a_malformed_payload_is_ferried_verbatim_to_home(tmp_path):
    path, q = _redirect(tmp_path, "fused-render://open?file=relative.zip")
    assert path == "/"
    assert q == {EDIT_APPFILE_PARAM: "relative.zip"}


def test_the_redirect_round_trips_a_spicy_path(tmp_path):
    from fused_render._view_url_codec import view_url_path

    spicy = "/Users/me/My Apps/a&b #1 100%.fused"
    assert edit_appfile_redirect(spicy) == view_url_path(os.path.abspath(spicy))
    assert unquote(edit_appfile_redirect(spicy)).endswith("My Apps/a&b #1 100%.fused")


def test_git_links_still_get_the_confirm_page(tmp_path):
    resp = _client(tmp_path).get(
        "/clone", params={"src": "fused-render://open?git=https://github.com/o/r"},
        follow_redirects=False)
    assert resp.status_code == 200
    assert "Clone" in resp.text


def test_the_clone_api_stays_git_only(tmp_path):
    fused = export(tmp_path)
    assert _client(tmp_path).get("/api/clone/info", params={"src": link(fused)}).status_code == 400
    resp = _client(tmp_path).post("/api/clone", json={"src": link(fused)}, headers=FUSED)
    assert resp.status_code == 400


def test_openurls_target_routes_a_file_link_to_the_clone_route():
    from fused_render._view_url_codec import open_target_path

    raw = link("/tmp/x.fused")
    assert open_target_path(raw) == "/clone?src=" + quote(raw, safe="")


def test_prefixes_stay_in_lock_step():
    assert len(deeplink._OPEN_FILE_PREFIXES) == len(deeplink._OPEN_PREFIXES)
