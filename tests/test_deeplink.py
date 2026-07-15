"""Tests for GitHub deep links (fused_render/deeplink.py, SPEC §26, D110):
URL parsing, sparse clone/pull into the Fused dir, and the /clone routes.

Clone tests use a local file:// remote (uploadpack.allowFilter enabled so the
blob:none partial clone works over the file transport) with `_remote_url`
monkeypatched — the parse layer still sees github.com URLs, only the git
remote is redirected.
"""
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from fused_render import deeplink
from fused_render.deeplink import (
    DeeplinkError,
    clone_or_pull,
    github_url_from,
    parse_github_url,
)
from fused_render.server import create_app

FUSED = {"X-Fused": "1"}

TREE_URL = "https://github.com/fusedlabs/sandbox/tree/main/Max/how_it_works"
DEEPLINK = "fused-render://open/" + TREE_URL


# ---- parsing -----------------------------------------------------------------


def test_parse_tree_url_with_subpath():
    spec = parse_github_url(TREE_URL)
    assert spec == {
        "owner": "fusedlabs",
        "repo": "sandbox",
        "ref": "main",
        "subpath": "Max/how_it_works",
        "name": "how_it_works",
    }


def test_parse_repo_root_url():
    spec = parse_github_url("https://github.com/fusedlabs/sandbox")
    assert spec["ref"] is None
    assert spec["subpath"] == ""
    assert spec["name"] == "sandbox"


def test_parse_tree_url_ref_only():
    spec = parse_github_url("https://github.com/o/r/tree/v1.2")
    assert spec["ref"] == "v1.2"
    assert spec["subpath"] == ""
    assert spec["name"] == "r"


def test_parse_strips_dot_git_suffix():
    assert parse_github_url("https://github.com/o/r.git")["repo"] == "r"


def test_parse_accepts_deeplink_prefix():
    assert parse_github_url(DEEPLINK)["name"] == "how_it_works"


def test_parse_accepts_percent_encoded_deeplink():
    from urllib.parse import quote

    raw = "fused-render://open/" + quote(TREE_URL, safe="")
    assert parse_github_url(raw)["subpath"] == "Max/how_it_works"


def test_github_url_from_passthrough():
    assert github_url_from(TREE_URL) == TREE_URL
    assert github_url_from(DEEPLINK) == TREE_URL


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "https://gitlab.com/o/r",
        "https://github.com/onlyowner",
        "https://github.com/o/r/blob/main/file.py",  # blob URLs unsupported (v1)
        "https://github.com/o/r/tree",  # missing ref
        "https://github.com/o/r/tree/main/../../etc",  # escapes the repo
        "https://github.com/o/r/tree/main/.hidden",  # dot destination name
        "https://github.com/../r",
    ],
)
def test_parse_rejects(bad):
    with pytest.raises(DeeplinkError):
        parse_github_url(bad)


# ---- entry-point helpers (macOS app / Windows winopen) ------------------------


def test_app_clone_url_path_encodes_whole_link():
    from urllib.parse import quote

    from fused_render.app import clone_url_path

    assert clone_url_path(DEEPLINK) == "/clone?src=" + quote(DEEPLINK, safe="")


def test_winopen_clone_url():
    from urllib.parse import quote

    from fused_render.winopen import _clone_url

    assert _clone_url(9000, DEEPLINK) == (
        "http://127.0.0.1:9000/clone?src=" + quote(DEEPLINK, safe="")
    )


# ---- clone / pull ------------------------------------------------------------


def _git(*args, cwd=None):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def source_repo(tmp_path):
    """A local repo shaped like the sandbox example: Max/how_it_works/index.html
    plus an unrelated top-level dir the sparse checkout must NOT materialize."""
    src = tmp_path / "srcrepo"
    sub = src / "Max" / "how_it_works"
    sub.mkdir(parents=True)
    (sub / "index.html").write_text("<h1>hi</h1>")
    other = src / "unrelated"
    other.mkdir()
    (other / "big.txt").write_text("x" * 10)
    _git("-c", "init.defaultBranch=main", "init", str(src))
    _git("add", "-A", cwd=src)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init", cwd=src)
    # file:// partial clone needs the server side to allow the filter
    _git("config", "uploadpack.allowFilter", "true", cwd=src)
    return src


@pytest.fixture
def env(tmp_path, monkeypatch, source_repo):
    fdir = tmp_path / "Fused"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    monkeypatch.setattr(deeplink, "_remote_url", lambda spec: f"file://{source_repo}")
    return fdir


def test_clone_subdir_opens_index(env, source_repo):
    result = clone_or_pull(parse_github_url(TREE_URL))
    dest = env / "how_it_works"
    assert result["dest"] == str(dest)
    assert result["updated"] is False
    assert (dest / "Max" / "how_it_works" / "index.html").is_file()
    # sparse checkout: the unrelated tree must not be materialized
    assert not (dest / "unrelated").exists()
    assert result["view"].startswith("/view/")
    assert result["view"].endswith("/index.html")


def test_clone_dir_without_index_opens_folder(env, source_repo):
    (source_repo / "Max" / "how_it_works" / "index.html").unlink()
    # keep the dir non-empty: git tracks files, an empty dir would vanish
    (source_repo / "Max" / "how_it_works" / "readme.md").write_text("no index")
    _git("add", "-A", cwd=source_repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "rm", cwd=source_repo)
    result = clone_or_pull(parse_github_url(TREE_URL))
    assert result["view"].endswith("/how_it_works")


def test_reclick_pulls_updates(env, source_repo):
    spec = parse_github_url(TREE_URL)
    clone_or_pull(spec)
    (source_repo / "Max" / "how_it_works" / "new.txt").write_text("v2")
    _git("add", "-A", cwd=source_repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "v2", cwd=source_repo)
    result = clone_or_pull(spec)
    assert result["updated"] is True
    assert (env / "how_it_works" / "Max" / "how_it_works" / "new.txt").is_file()


def test_existing_non_git_dir_refused(env):
    dest = env / "how_it_works"
    dest.mkdir(parents=True)
    (dest / "keep.txt").write_text("mine")
    with pytest.raises(DeeplinkError, match="not a git clone"):
        clone_or_pull(parse_github_url(TREE_URL))
    assert (dest / "keep.txt").is_file()  # never clobbered


def test_existing_clone_of_other_remote_refused(env, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "f").write_text("x")
    _git("-c", "init.defaultBranch=main", "init", str(other))
    _git("add", "-A", cwd=other)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "i", cwd=other)
    env.mkdir(parents=True, exist_ok=True)
    _git("clone", str(other), str(env / "how_it_works"))
    with pytest.raises(DeeplinkError, match="different repository"):
        clone_or_pull(parse_github_url(TREE_URL))


def test_missing_subpath_cleans_up_dest(env):
    spec = parse_github_url("https://github.com/fusedlabs/sandbox/tree/main/no/such/dir")
    with pytest.raises(DeeplinkError, match="does not exist"):
        clone_or_pull(spec)
    # a failed first clone must be retryable: nothing left behind
    assert not (env / "dir").exists()


# ---- routes ------------------------------------------------------------------


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_clone_page_serves_html(tmp_path):
    resp = _client(tmp_path).get("/clone?src=whatever")
    assert resp.status_code == 200
    assert "Clone" in resp.text


def test_api_clone_requires_guard(tmp_path):
    resp = _client(tmp_path).post("/api/clone", json={"src": TREE_URL})
    assert resp.status_code == 403


def test_api_clone_info(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    resp = _client(tmp_path).get("/api/clone/info", params={"src": DEEPLINK})
    assert resp.status_code == 200
    data = resp.json()
    assert data["owner"] == "fusedlabs"
    assert data["dest"] == str(fdir / "how_it_works")
    assert data["exists"] is False


def test_api_clone_info_rejects_bad_url(tmp_path):
    resp = _client(tmp_path).get("/api/clone/info", params={"src": "https://evil.example/x/y"})
    assert resp.status_code == 400
    assert "github.com" in resp.json()["error"]


def test_api_clone_end_to_end(tmp_path, env):
    resp = _client(tmp_path).post("/api/clone", json={"src": DEEPLINK}, headers=FUSED)
    assert resp.status_code == 200
    data = resp.json()
    assert data["updated"] is False
    assert data["view"].endswith("/index.html")
    assert os.path.isfile(os.path.join(data["target"], "index.html"))
