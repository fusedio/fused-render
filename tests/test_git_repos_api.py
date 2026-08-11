"""GET /api/git-repos (server/routers/git_repos.py): git repositories on this
machine, for the Explorer homepage's "Repos" tab.

The candidate directory list comes from the index's dirs.parquet (never a fresh
filesystem walk — see the router's docstring), so these tests build a small
dirs.parquet by hand rather than running a scan.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render.index.config import load_config
from fused_render.server import create_app


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway shell home, so the index store lands under it."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    return h


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _write_dirs_index(dirs, *, manifest=True):
    """A minimal index store holding exactly `dirs` in dirs.parquet.

    Same schema the real compaction writes (index/store.schemas) and the same
    `ORDER BY dir` row order, so the endpoint's "natural order is path order"
    assumption is exercised as it is in production.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    from fused_render.index.store import schemas

    cfg = load_config()
    os.makedirs(cfg.dir, exist_ok=True)
    _files, dir_schema = schemas(pa)
    rows = sorted(dirs)
    pq.write_table(
        pa.table({
            "dir": rows,
            "sig": ["" for _ in rows],
            "n_files": [0 for _ in rows],
            "total_size": [0 for _ in rows],
            "mtime_ns": [0 for _ in rows],
            "n_subdirs": [0 for _ in rows],
            "depth": [d.count("/") for d in rows],
        }, schema=dir_schema),
        cfg.dirs_parquet,
    )
    if manifest:
        with open(cfg.partitions_json, "w") as f:
            json.dump({"partitions": [], "rows": 0, "updated": 0.0}, f)
    return cfg


def _repo(root, name):
    d = root / name
    (d / ".git").mkdir(parents=True)
    return d


def test_lists_only_dirs_holding_a_dot_git_directory(home, tmp_path, client):
    repo = _repo(tmp_path, "repo")
    plain = tmp_path / "plain"
    plain.mkdir()
    _write_dirs_index([str(tmp_path), str(repo), str(plain)])
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is True
    assert [r["path"] for r in body["repos"]] == [str(repo)]


def test_a_dot_git_FILE_is_not_a_repo(home, tmp_path, client):
    """A linked worktree and a modern submodule both mark themselves with a
    `.git` FILE, not a directory. Normal repos only — that is the point."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")
    _write_dirs_index([str(tmp_path), str(wt)])
    body = client.get("/api/git-repos").json()
    assert body["repos"] == []


def test_nested_repos_are_both_listed_in_path_order(home, tmp_path, client):
    outer = _repo(tmp_path, "outer")
    inner = _repo(outer, "inner")
    other = _repo(tmp_path, "aaa")
    _write_dirs_index([str(tmp_path), str(outer), str(inner), str(other)])
    body = client.get("/api/git-repos").json()
    assert [r["path"] for r in body["repos"]] == [
        str(other), str(outer), str(inner),
    ]


def test_a_repo_deleted_since_the_scan_is_dropped(home, tmp_path, client):
    gone = tmp_path / "gone"  # never created on disk
    _write_dirs_index([str(tmp_path), str(gone)])
    assert client.get("/api/git-repos").json()["repos"] == []


def test_no_index_reports_not_indexed_rather_than_no_repos(home, tmp_path, client):
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is False
    assert body["repos"] == []
    # The tab has to be able to say "still building" instead of "none found".
    assert "scanning" in body


def test_a_manifest_with_no_dirs_parquet_is_not_indexed(home, tmp_path, client):
    cfg = load_config()
    os.makedirs(cfg.dir, exist_ok=True)
    with open(cfg.partitions_json, "w") as f:
        json.dump({"partitions": [], "rows": 0, "updated": 0.0}, f)
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is False


def test_an_unreadable_index_is_a_502_not_not_indexed(home, tmp_path, client):
    """An index that exists but cannot be queried is a failure. Reporting it as
    "still building" would have the tab promise a list that is never coming."""
    cfg = load_config()
    os.makedirs(cfg.dir, exist_ok=True)
    with open(cfg.partitions_json, "w") as f:
        json.dump({"partitions": [], "rows": 0, "updated": 0.0}, f)
    with open(cfg.dirs_parquet, "wb") as f:
        f.write(b"not a parquet file at all")
    resp = client.get("/api/git-repos")
    assert resp.status_code == 502
    assert "index could not be read" in resp.json()["error"]


def test_hidden_and_machine_managed_checkouts_are_screened_out(home, tmp_path,
                                                               client):
    """A package manager's checkouts (~/.oh-my-zsh, nvim's lazy dir, Claude
    plugin caches) are repos by the .git test but not repos anyone opens. Same
    standard /api/search/files holds its rows to (walk.junk_path)."""
    mine = _repo(tmp_path, "mine")
    hidden = _repo(tmp_path, ".oh-my-zsh")
    nested = _repo(tmp_path / ".local" / "share", "plugin")
    vendored = _repo(tmp_path / "proj" / "node_modules", "dep")
    _write_dirs_index([str(tmp_path), str(mine), str(hidden), str(nested),
                       str(vendored)])
    body = client.get("/api/git-repos").json()
    assert [r["path"] for r in body["repos"]] == [str(mine)]


def test_a_screened_out_row_is_never_statted(home, tmp_path, client, monkeypatch):
    """The screen runs BEFORE the probe, so the majority of rows on a real home
    cost no syscall at all."""
    from fused_render.server.routers import git_repos as mod

    hidden = _repo(tmp_path, ".cache")
    _write_dirs_index([str(tmp_path), str(hidden)])
    probed = []
    real_isdir = os.path.isdir
    monkeypatch.setattr(mod.os.path, "isdir",
                        lambda p: (probed.append(p), real_isdir(p))[1])
    client.get("/api/git-repos")
    assert not any(str(hidden) in p for p in probed)


def test_paths_inside_a_fused_render_home_are_never_probed(home, tmp_path, client,
                                                           monkeypatch):
    """MountGuard territory: a fused-render home holds the mount trees, and a
    stat inside a wedged mount can hang the process forever. Such a candidate
    must be dropped WITHOUT a filesystem probe."""
    from fused_render.server.routers import git_repos as mod

    inside = _repo(home, "mountish")
    outside = _repo(tmp_path, "real")
    _write_dirs_index([str(inside), str(outside)])

    probed = []
    real_isdir = os.path.isdir

    def spy(path):
        probed.append(path)
        return real_isdir(path)

    monkeypatch.setattr(mod.os.path, "isdir", spy)
    body = client.get("/api/git-repos").json()
    assert [r["path"] for r in body["repos"]] == [str(outside)]
    assert not any(str(inside) in p for p in probed)
