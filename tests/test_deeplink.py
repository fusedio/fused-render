"""Tests for deep links (fused_render/deeplink.py, SPEC §26): the launch /
relaunch matchers, the `open?git=` payload (DL-9) and the `/clone` link
router. `file=` lives in test_deeplink_file.py; the repo build itself
(appfetch.download_github_app) is exercised through a monkeypatch here.
"""
from urllib.parse import quote, unquote

import pytest
from fastapi.testclient import TestClient

from fused_render import appfetch
from fused_render.deeplink import (
    git_payload_from,
    is_fda_relaunch_url,
    is_launch_url,
    is_relaunch_url,
)
from fused_render.server import create_app

FUSED = {"X-Fused": "1"}

REPO_URL = "https://github.com/octocat/sandbox"
DEEPLINK = "fused-render://open?git=" + REPO_URL


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


# ---- git= payload (DL-9) -------------------------------------------------------


def test_git_payload_verbatim_and_percent_encoded():
    assert git_payload_from(DEEPLINK) == REPO_URL
    assert git_payload_from("FUSED-RENDER://open/?git=" + REPO_URL) == REPO_URL
    assert git_payload_from("fused-render://open?git=" + quote(REPO_URL, safe="")) == REPO_URL
    assert git_payload_from("fused-render://open?url=" + REPO_URL) is None


@pytest.mark.parametrize(
    "url",
    [REPO_URL, REPO_URL + "/", REPO_URL + ".git", "https://www.github.com/octocat/sandbox"],
)
def test_parse_github_repo_accepts(url):
    assert appfetch.parse_github_repo(url) == ("octocat", "sandbox")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "https://gitlab.com/o/r",
        "https://github.com/onlyowner",
        REPO_URL + "/tree/main",  # owner/repo only: no refs, no subfolders
        REPO_URL + "/tree/main/examples/app",
        REPO_URL + "/blob/main/index.html",
        "https://github.com/../r",
        "https://github.com/-o/r",
    ],
)
def test_parse_github_repo_rejects(bad):
    with pytest.raises(appfetch.FetchError):
        appfetch.parse_github_repo(bad)


# ---- /clone link router --------------------------------------------------------


def _route(tmp_path, src):
    resp = _client(tmp_path).get("/clone", params={"src": src}, follow_redirects=False)
    assert resp.status_code == 303
    path, _, query = resp.headers["location"].partition("?")
    return path, query


def test_git_link_hands_repo_to_shell(tmp_path):
    path, query = _route(tmp_path, DEEPLINK)
    assert path == "/"
    assert query == "_fetch_appgit=" + quote(REPO_URL, safe="")


def test_url_link_hands_link_to_shell(tmp_path):
    link = "https://example.com/a.fused?x=1&y=2"
    path, query = _route(tmp_path, "fused-render://open?url=" + quote(link, safe=""))
    assert path == "/"
    assert unquote(query.removeprefix("_fetch_appfile=")) == link


@pytest.mark.parametrize(
    "src", ["whatever", "fused-render://open?page=x", "fused-render://launch"],
)
def test_unknown_link_is_a_400(tmp_path, src):
    resp = _client(tmp_path).get("/clone", params={"src": src}, follow_redirects=False)
    assert resp.status_code == 400


def test_old_clone_routes_are_gone(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/clone", json={"src": DEEPLINK}, headers=FUSED).status_code in (404, 405)
    assert client.get("/api/clone/info", params={"src": DEEPLINK}).status_code == 404


def test_fetch_git_builds_through_appfetch(tmp_path, monkeypatch):
    seen = []

    def fake(url):
        seen.append(url)
        return str(tmp_path / "sandbox-12345678.fused")

    monkeypatch.setattr(appfetch, "download_github_app", fake)
    resp = _client(tmp_path).post("/api/appfile/fetch", json={"git": REPO_URL}, headers=FUSED)
    assert resp.status_code == 200
    assert resp.json()["file"].endswith("sandbox-12345678.fused")
    assert seen == [REPO_URL]


def test_fetch_git_reports_build_error(tmp_path):
    resp = _client(tmp_path).post(
        "/api/appfile/fetch", json={"git": REPO_URL + "/tree/main"}, headers=FUSED)
    assert resp.status_code == 400
    assert "github.com/<owner>/<repo>" in resp.json()["error"]


def test_view_url_path_encodes_posix_paths():
    from fused_render.deeplink import _view_url_path

    assert _view_url_path("/Users/v/a b/index.html") == "/explorer/view/Users/v/a%20b/index.html"



# ---- launch action (D128) ------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "fused-render://launch",
        "fused-render://launch/",
        "fused-render:launch",  # some carriers strip the empty authority
        "FUSED-RENDER://LAUNCH",
        "  fused-render://launch  ",
    ],
)
def test_is_launch_url_accepts(src):
    assert is_launch_url(src) is True


@pytest.mark.parametrize(
    "src",
    [
        "",
        None,
        "fused-render://launch?x=1",  # launch is payload-free by definition
        "fused-render://launch/extra",
        "fused-render://launchpad",
        "fused-render://open?git=" + REPO_URL,
        DEEPLINK,
        "https://github.com/o/r",
    ],
)
def test_is_launch_url_rejects(src):
    assert is_launch_url(src) is False


# ---- relaunch action -----------------------------------------------------------
# Same strict, payload-free grammar as launch: the running app quits itself and
# respawns from the (possibly newer) bundle on disk.


@pytest.mark.parametrize(
    "src",
    [
        "fused-render://relaunch",
        "fused-render://relaunch/",
        "fused-render:relaunch",  # some carriers strip the empty authority
        "FUSED-RENDER://RELAUNCH",
        "  fused-render://relaunch  ",
    ],
)
def test_is_relaunch_url_accepts(src):
    assert is_relaunch_url(src) is True


@pytest.mark.parametrize(
    "src",
    [
        "",
        None,
        "fused-render://relaunch?x=1",  # relaunch is payload-free by definition
        "fused-render://relaunch/extra",
        "fused-render://relaunchpad",
        "fused-render://launch",  # the sibling action must not match
        DEEPLINK,
        "https://github.com/o/r",
    ],
)
def test_is_relaunch_url_rejects(src):
    assert is_relaunch_url(src) is False


# The one relaunch payload (D110: a query param on the same action): reason=fda
# respawns the SAME version so a fresh Full Disk Access grant takes effect.


@pytest.mark.parametrize(
    "src",
    [
        "fused-render://relaunch?reason=fda",
        "fused-render://relaunch/?reason=fda",
        "fused-render:relaunch?reason=fda",
        "FUSED-RENDER://RELAUNCH?REASON=FDA",
    ],
)
def test_is_fda_relaunch_url_accepts(src):
    assert is_fda_relaunch_url(src) is True
    assert is_relaunch_url(src) is False  # the two actions never both match


@pytest.mark.parametrize(
    "src",
    [
        "",
        None,
        "fused-render://relaunch",  # bare relaunch is the update swap, not this
        "fused-render://relaunch?reason=update",
        "fused-render://relaunch?reason=fda&x=1",
        "fused-render://launch?reason=fda",
    ],
)
def test_is_fda_relaunch_url_rejects(src):
    assert is_fda_relaunch_url(src) is False

# ---- entry-point helpers (macOS app) -------------------------------------------


def test_app_clone_url_path_encodes_whole_link():
    from urllib.parse import quote

    from fused_render.app import clone_url_path

    assert clone_url_path(DEEPLINK) == "/clone?src=" + quote(DEEPLINK, safe="")


def test_openurls_target_path_routes_deeplink_to_clone():
    from fused_render.app import clone_url_path, openurls_target_path

    assert openurls_target_path(DEEPLINK) == clone_url_path(DEEPLINK)


def test_openurls_target_path_routes_plain_file_to_view():
    from fused_render.app import openurls_target_path, view_url_path

    raw = "file:///tmp/notes.txt"
    assert openurls_target_path(raw) == view_url_path("/tmp/notes.txt")


# ---- clone / pull ------------------------------------------------------------
