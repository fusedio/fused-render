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
DEEPLINK = "fused-render://open?git=" + TREE_URL


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

    raw = "fused-render://open?git=" + quote(TREE_URL, safe="")
    assert parse_github_url(raw)["subpath"] == "Max/how_it_works"


def test_parse_rejects_unknown_deeplink_action():
    with pytest.raises(DeeplinkError, match="expected fused-render://open"):
        parse_github_url("fused-render://frobnicate?git=" + TREE_URL)


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
        "https://github.com/o/r/tree/-f/sub",  # option-injection ref
        "https://github.com/o/r/tree/--stdin",  # option-injection ref
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


def test_view_url_path_normalizes_windows_drive_paths():
    from fused_render.deeplink import _view_url_path

    assert (
        _view_url_path("C:\\Users\\v\\Documents\\Fused\\x\\index.html")
        == "/view/C%3A/Users/v/Documents/Fused/x/index.html"
    )
    assert _view_url_path("/Users/v/a b/index.html") == "/view/Users/v/a%20b/index.html"


def _rev(source_repo, ref="HEAD"):
    out = subprocess.run(
        ["git", "rev-parse", ref], cwd=source_repo, check=True,
        stdout=subprocess.PIPE, text=True,
    )
    return out.stdout.strip()


def test_tag_ref_clones_detached_and_updates(env, source_repo):
    _git("tag", "v1", cwd=source_repo)
    spec = parse_github_url("https://github.com/fusedlabs/sandbox/tree/v1/Max/how_it_works")
    result = clone_or_pull(spec)
    assert result["updated"] is False
    assert (env / "how_it_works" / "Max" / "how_it_works" / "index.html").is_file()
    # re-click on a detached (tag) checkout must not try to pull
    result = clone_or_pull(spec)
    assert result["updated"] is True


def test_commit_sha_ref_clones(env, source_repo):
    sha = _rev(source_repo)
    spec = parse_github_url(f"https://github.com/fusedlabs/sandbox/tree/{sha}/Max/how_it_works")
    result = clone_or_pull(spec)
    assert result["view"].endswith("/index.html")
    # re-click: fetch + re-checkout of the same SHA is a no-op, not a failure
    assert clone_or_pull(spec)["updated"] is True


def test_subpath_segment_with_leading_dash_is_not_an_option(env, source_repo):
    dash = source_repo / "Max" / "-dash"
    dash.mkdir()
    (dash / "index.html").write_text("dash")
    _git("add", "-A", cwd=source_repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "dash", cwd=source_repo)
    spec = parse_github_url("https://github.com/fusedlabs/sandbox/tree/main/Max/-dash")
    result = clone_or_pull(spec)
    assert result["view"].endswith("/index.html")


def test_update_switches_to_link_ref(env, source_repo):
    clone_or_pull(parse_github_url(TREE_URL))
    _git("checkout", "-q", "-b", "feature", cwd=source_repo)
    (source_repo / "Max" / "how_it_works" / "feature.txt").write_text("f")
    _git("add", "-A", cwd=source_repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "feat", cwd=source_repo)
    _git("checkout", "-q", "main", cwd=source_repo)
    result = clone_or_pull(
        parse_github_url("https://github.com/fusedlabs/sandbox/tree/feature/Max/how_it_works")
    )
    assert result["updated"] is True
    assert (env / "how_it_works" / "Max" / "how_it_works" / "feature.txt").is_file()


def test_root_link_after_tag_link_returns_to_default_branch(env, source_repo):
    _git("tag", "v1", cwd=source_repo)
    # tag link without subpath -> dest name is the repo, detached HEAD
    clone_or_pull(parse_github_url("https://github.com/fusedlabs/sandbox/tree/v1"))
    (source_repo / "later.txt").write_text("post-tag")
    _git("add", "-A", cwd=source_repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "later", cwd=source_repo)
    # ref-less root link must land on the default branch tip, not stay detached
    result = clone_or_pull(parse_github_url("https://github.com/fusedlabs/sandbox"))
    assert result["updated"] is True
    assert (env / "sandbox" / "later.txt").is_file()


def test_update_widens_sparse_cone_for_new_subpath(env, source_repo):
    other = source_repo / "Other" / "how_it_works"
    other.mkdir(parents=True)
    (other / "other.txt").write_text("o")
    _git("add", "-A", cwd=source_repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "other", cwd=source_repo)
    clone_or_pull(parse_github_url(TREE_URL))
    # same repo + same basename, different subdir -> same dest; cone must widen
    result = clone_or_pull(
        parse_github_url("https://github.com/fusedlabs/sandbox/tree/main/Other/how_it_works")
    )
    assert result["target"].endswith(os.path.join("Other", "how_it_works"))
    assert (env / "how_it_works" / "Other" / "how_it_works" / "other.txt").is_file()
    # the first link's path stays materialized (add, not a replacing set)
    assert (env / "how_it_works" / "Max" / "how_it_works" / "index.html").is_file()


def test_repo_slug_matches_https_and_ssh_forms():
    from fused_render.deeplink import _repo_slug

    forms = [
        "https://github.com/O/R.git",
        "https://www.github.com/o/r/",
        "git@github.com:o/r.git",
        "ssh://git@github.com/o/r",
    ]
    assert {_repo_slug(f) for f in forms} == {"o/r"}


def test_https_auth_failure_falls_back_to_ssh(env, monkeypatch):
    spec = parse_github_url(TREE_URL)
    monkeypatch.setattr(deeplink, "_remote_url", lambda s: "https://github.com/fusedlabs/sandbox.git")
    attempts = []

    def fake_clone(spec_, remote, dest):
        attempts.append(remote)
        if remote.startswith("https://"):
            raise DeeplinkError(
                "git clone failed:\nfatal: could not read Username for "
                "'https://github.com': Device not configured"
            )
        os.makedirs(os.path.join(dest, "Max", "how_it_works"))
        open(os.path.join(dest, "Max", "how_it_works", "index.html"), "w").close()

    monkeypatch.setattr(deeplink, "_clone_into", fake_clone)
    result = clone_or_pull(spec)
    assert attempts == [
        "https://github.com/fusedlabs/sandbox.git",
        "git@github.com:fusedlabs/sandbox.git",
    ]
    assert result["view"].endswith("/index.html")


def test_https_and_ssh_both_failing_reports_both(env, monkeypatch):
    spec = parse_github_url(TREE_URL)
    monkeypatch.setattr(deeplink, "_remote_url", lambda s: "https://github.com/fusedlabs/sandbox.git")

    def fake_clone(spec_, remote, dest):
        if remote.startswith("https://"):
            raise DeeplinkError("fatal: Authentication failed for https remote")
        raise DeeplinkError("git@github.com: Permission denied (publickey).")

    monkeypatch.setattr(deeplink, "_clone_into", fake_clone)
    with pytest.raises(DeeplinkError, match="both\\s+https and ssh"):
        clone_or_pull(spec)


def test_git_env_disables_prompts():
    from fused_render.deeplink import _git_env

    env = _git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert "/opt/homebrew/bin" in env["PATH"]


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
    assert data["updatable"] is False
    assert data["conflict"] is None


def test_api_clone_info_reports_conflict_for_non_git_dir(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    (fdir / "how_it_works").mkdir(parents=True)  # e.g. the seeded example
    resp = _client(tmp_path).get("/api/clone/info", params={"src": DEEPLINK})
    data = resp.json()
    assert data["exists"] is True
    assert data["updatable"] is False
    assert "not a git clone" in data["conflict"]


def test_api_clone_info_updatable_for_matching_clone(tmp_path, env):
    clone_or_pull(parse_github_url(TREE_URL))
    resp = _client(tmp_path).get("/api/clone/info", params={"src": DEEPLINK})
    data = resp.json()
    assert data["updatable"] is True
    assert data["conflict"] is None


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
