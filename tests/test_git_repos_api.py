"""GET /api/git-repos (server/routers/git_repos.py): git repositories on this
machine, for the Explorer homepage's "Repos" tab.

Repo-ness is an INDEX FACT: `.git` is a leaf dir, so the scan records one dirs
row for it and the endpoint's whole job is "find those rows, take the parent".
These tests therefore build dirs.parquet by hand rather than running a scan, and
mostly do not touch the filesystem at all — the endpoint never stats.

The `.git`-is-recorded-and-not-descended half lives in tests/test_index_scan.py;
the walk/index parity of the rule lives in tests/test_index_ignore.py.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render.index.config import load_config
from fused_render.index.store import save_applied_ignore
from fused_render.server import create_app
from fused_render.server.routers.index import scan_roots


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


def _write_dirs_index(dirs, *, manifest=True, applied=True):
    """A minimal index store holding exactly `dirs` in dirs.parquet.

    Same schema the real compaction writes (index/store.schemas) and the same
    `ORDER BY dir` row order, so the endpoint's "natural order is path order"
    assumption is exercised as it is in production. `applied` stamps the current
    ignore signature — without it the endpoint (rightly) treats the index as
    predating the `.git` leaf rule.
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
    if applied:
        # The roots the endpoint checks are the CONFIGURED ones (scan_roots:
        # cfg.roots, else home), each individually — so stamping some other path
        # would leave the index reading as stale.
        for r in scan_roots(cfg):
            save_applied_ignore(cfg, r)
    return cfg


def _git(path):
    """The dirs row a repo at `path` produces."""
    return str(path) + "/.git"


def _set_roots(roots):
    """Persist the configured scan roots, so scan_roots() reports them instead of
    falling back to the real home directory."""
    from fused_render.index.config import save_config

    cfg = load_config()
    cfg.roots = [str(r) for r in roots]
    save_config(cfg)


# -- the happy path ------------------------------------------------------------

def test_a_repo_is_the_parent_of_a_dot_git_row(home, tmp_path, client):
    repo = tmp_path / "repo"
    _write_dirs_index([str(tmp_path), str(repo), _git(repo)])
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is True
    assert [r["path"] for r in body["repos"]] == [str(repo)]


def test_a_dir_without_a_dot_git_row_is_not_a_repo(home, tmp_path, client):
    """Which also covers linked worktrees and modern submodules: both mark
    themselves with a `.git` FILE, and a file never produces a dirs row."""
    plain = tmp_path / "plain"
    _write_dirs_index([str(tmp_path), str(plain)])
    assert client.get("/api/git-repos").json()["repos"] == []


def test_nested_repos_are_both_listed_in_path_order(home, tmp_path, client):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    other = tmp_path / "aaa"
    _write_dirs_index([str(tmp_path), str(outer), _git(outer), str(inner),
                       _git(inner), str(other), _git(other)])
    body = client.get("/api/git-repos").json()
    assert [r["path"] for r in body["repos"]] == [
        str(other), str(outer), str(inner),
    ]


def test_the_endpoint_does_not_stat_anything(home, tmp_path, client, monkeypatch):
    """The point of sourcing repo-ness from the index: no filesystem syscall on
    a candidate path, so a wedged mount cannot hang the request. None of these
    paths exist on disk at all."""
    from fused_render.server.routers import git_repos as mod

    repo = tmp_path / "nowhere" / "repo"
    _write_dirs_index([str(repo), _git(repo)])
    probed = []
    monkeypatch.setattr(mod.os.path, "isdir",
                        lambda p: (probed.append(p), False)[1])
    body = client.get("/api/git-repos").json()
    assert [r["path"] for r in body["repos"]] == [str(repo)]
    assert not any(str(repo) in p for p in probed)


# -- screening -----------------------------------------------------------------

def test_junk_path_screens_the_PARENT_not_the_dot_git_row(home, tmp_path, client):
    """The trap this endpoint is built around. `.git` is itself a dot-segment, so
    holding the ROW to the explorer's no-hidden-paths standard would reject every
    repository on the machine. The parent is what the user is offered, so the
    parent is what gets screened."""
    mine = tmp_path / "mine"
    hidden = tmp_path / ".oh-my-zsh"
    nested = tmp_path / ".local" / "share" / "plugin"
    vendored = tmp_path / "proj" / "node_modules" / "dep"
    _write_dirs_index([_git(mine), _git(hidden), _git(nested), _git(vendored)])
    body = client.get("/api/git-repos").json()
    assert [r["path"] for r in body["repos"]] == [str(mine)]


def test_a_repo_inside_a_fused_render_home_is_excluded(home, tmp_path, client):
    """MountGuard territory: a fused-render home holds the mount trees. The scan
    already refuses them, so this is the layer that holds if an index written by
    an older build carries such a row."""
    inside = home / "mountish"
    outside = tmp_path / "real"
    _write_dirs_index([_git(inside), _git(outside)])
    body = client.get("/api/git-repos").json()
    assert [r["path"] for r in body["repos"]] == [str(outside)]


# -- states that are not an answer ---------------------------------------------

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
    assert client.get("/api/git-repos").json()["indexed"] is False


def test_an_index_built_under_OLD_ignore_rules_is_not_indexed(home, tmp_path,
                                                              client):
    """THE migration case, and the one way this endpoint could ship a silent lie.

    `.git` moving out of the ignore list and into the leaf rules changed
    IgnoreRules.sig(), which forces a full rescan — but an index already on disk
    has no `.git` rows until that rescan lands. Answering `indexed: true` with an
    empty list would tell the user, with total confidence, that they have no
    repositories."""
    repo = tmp_path / "repo"
    # A real index (rows, manifest) whose applied signature is a stale one.
    cfg = _write_dirs_index([str(tmp_path), str(repo)], applied=False)
    with open(cfg.applied_ignore_json, "w") as f:
        json.dump({"roots": {"/": "a-signature-from-before-the-leaf-rule"}}, f)
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is False
    assert body["repos"] == []


def test_an_index_with_no_applied_signature_at_all_is_not_indexed(home, tmp_path,
                                                                  client):
    """None means "predates the applied-ignore file", which cannot be assumed to
    have been built under the current rules either."""
    repo = tmp_path / "repo"
    _write_dirs_index([str(tmp_path), str(repo), _git(repo)], applied=False)
    assert client.get("/api/git-repos").json()["indexed"] is False


def test_a_partially_rescanned_multi_root_index_is_not_indexed(home, tmp_path,
                                                               client):
    """Two configured roots, only one rescanned under the current rules: every
    repo under the other is still missing, so the honest answer is "not ready"."""
    a, b = tmp_path / "a", tmp_path / "b"
    repo = a / "repo"
    _set_roots([a, b])
    cfg = _write_dirs_index([str(repo), _git(repo)], applied=False)
    save_applied_ignore(cfg, str(a))   # only /a reconciled
    assert client.get("/api/git-repos").json()["indexed"] is False
    save_applied_ignore(cfg, str(b))
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is True
    assert [r["path"] for r in body["repos"]] == [str(repo)]


def test_a_legacy_sig_root_is_stale_even_once_another_root_is_stamped(
        home, tmp_path, client):
    """Bugbot's multi-root hole, pinned. On a store migrated from the pre-per-root
    applied-ignore format, stamping the FIRST root leaves
    `{"roots": {a: current}, "legacy_sig": old}` — and the ROOTLESS
    applied_ignore_sig() reads only the `roots` values, so it answers "everything
    matches" while root `b` is still described by nothing but the stale legacy sig
    and still holds no `.git` rows. Checking each root individually is what sees
    it: the per-root form falls back to `legacy_sig` for `b`."""
    from fused_render.index.store import applied_ignore_sig

    a, b = tmp_path / "a", tmp_path / "b"
    repo = a / "repo"
    _set_roots([a, b])
    cfg = _write_dirs_index([str(repo), _git(repo)], applied=False)
    # the pre-per-root file, then one root migrated onto the new format
    with open(cfg.applied_ignore_json, "w") as f:
        json.dump({"sig": "a-pre-leaf-rule-global-sig"}, f)
    save_applied_ignore(cfg, str(a))

    # the rootless form is exactly as misleading as reported...
    assert applied_ignore_sig(cfg) == cfg.rules.sig()
    # ...and the endpoint must not be fooled by it
    assert client.get("/api/git-repos").json()["indexed"] is False


def test_an_unreadable_index_is_a_502_not_not_indexed(home, tmp_path, client):
    """An index that exists but cannot be queried is a failure. Reporting it as
    "still building" would have the tab promise a list that is never coming."""
    _write_dirs_index([str(tmp_path)])
    cfg = load_config()
    with open(cfg.dirs_parquet, "wb") as f:
        f.write(b"not a parquet file at all")
    resp = client.get("/api/git-repos")
    assert resp.status_code == 502
    assert "index could not be read" in resp.json()["error"]
