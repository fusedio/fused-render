"""`fused-render://open?file=<.fused path>` (SPEC §26 DL-7, D889): the deep
link Render App's Edit button sends. Parsing (one percent-decode, absolute
`.fused` only) and the `GET /clone` redirect that lands it INSIDE the shell —
no page of its own: the shell clones through the guarded /api/appfile routes
and asks in a modal when a local copy already exists.
"""
import os
from urllib.parse import quote, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from fused_render import appfile, deeplink
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
    # Same isolation as test_appfile_clone: the extract cache lives under the
    # home, a clone lands in the WORKSPACE (never the developer's ~/Fused).
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


def _redirect(tmp_path, src):
    resp = _client(tmp_path).get("/clone", params={"src": src}, follow_redirects=False)
    assert resp.status_code == 303
    parts = urlsplit(resp.headers["location"])
    q = dict(kv.split("=", 1) for kv in parts.query.split("&"))
    return parts.path, {k: unquote(v) for k, v in q.items()}


# ---- parsing -----------------------------------------------------------------


def test_file_link_decodes_the_path_once():
    path = "/Users/me/My Apps/a&b #1 100%.fused"
    assert app_file_path_from(link(path)) == path


def test_file_link_tolerates_a_slash_after_open_and_case():
    assert app_file_path_from("FUSED-RENDER://open/?file=%2Ftmp%2Fx.fused") == "/tmp/x.fused"


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


def test_no_copy_yet_lands_on_home_with_the_file(tmp_path):
    fused = export(tmp_path)
    path, q = _redirect(tmp_path, link(fused))
    assert path == "/"
    assert q == {EDIT_APPFILE_PARAM: str(fused)}
    # Read-only: the GET cloned nothing (the shell does, behind X-Fused).
    assert not os.path.exists(tmp_path / "workspace" / "local" / "demo")


def test_an_existing_copy_lands_on_its_entry_page_with_the_file(tmp_path):
    fused = export(tmp_path)
    _client(tmp_path).post("/api/appfile/clone", json={"file": str(fused)}, headers=FUSED)
    path, q = _redirect(tmp_path, link(fused))
    assert path == "/explorer/view" + str(tmp_path / "workspace" / "local" / "demo" / "index.html")
    assert q == {EDIT_APPFILE_PARAM: str(fused)}


def test_a_copy_without_an_entry_page_lands_on_the_folder(tmp_path):
    fused = export(tmp_path)
    dest = tmp_path / "workspace" / "local" / "demo"
    dest.mkdir(parents=True)
    (dest / "notes.txt").write_text("not an app any more")
    path, q = _redirect(tmp_path, link(fused))
    assert path == "/explorer/view" + str(dest)


def test_a_missing_file_still_lands_on_home_for_the_shell_to_report(tmp_path):
    gone = tmp_path / "gone.fused"
    path, q = _redirect(tmp_path, link(gone))
    assert path == "/"
    assert q == {EDIT_APPFILE_PARAM: str(gone)}


def test_a_malformed_payload_is_ferried_verbatim(tmp_path):
    path, q = _redirect(tmp_path, "fused-render://open?file=relative.zip")
    assert path == "/"
    assert q == {EDIT_APPFILE_PARAM: "relative.zip"}


def test_the_param_round_trips_a_spicy_path(tmp_path):
    spicy = "/Users/me/My Apps/a&b #1 100%.fused"
    assert unquote(edit_appfile_redirect(spicy).split("=", 1)[1]) == spicy


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
