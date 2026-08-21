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
    # `.as_posix()`, not `str()`: the endpoint canonicalizes every path it
    # returns to forward slashes (`_view_url_codec.canonical_fs_path`), while a
    # bare `str(Path)` on Windows is backslashed — comparing against that raw
    # spelling fails on Windows for no reason the app got wrong.
    assert [r["path"] for r in body["repos"]] == [repo.as_posix()]


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
        other.as_posix(), outer.as_posix(), inner.as_posix(),
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
    assert [r["path"] for r in body["repos"]] == [repo.as_posix()]
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
    assert [r["path"] for r in body["repos"]] == [mine.as_posix()]


def test_a_repo_inside_a_fused_render_home_is_excluded(home, tmp_path, client):
    """MountGuard territory: a fused-render home holds the mount trees. The scan
    already refuses them, so this is the layer that holds if an index written by
    an older build carries such a row."""
    inside = home / "mountish"
    outside = tmp_path / "real"
    _write_dirs_index([_git(inside), _git(outside)])
    body = client.get("/api/git-repos").json()
    assert [r["path"] for r in body["repos"]] == [outside.as_posix()]


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


def test_an_index_built_under_OLD_rules_with_NO_rows_is_not_indexed(home, tmp_path,
                                                                    client):
    """THE migration case, and the one way this endpoint could ship a silent lie.

    `.git` moving out of the ignore list and into the leaf rules changed
    IgnoreRules.sig(), which forces a full rescan — but an index already on disk
    has no `.git` rows until that rescan lands. Answering `indexed: true` with an
    empty list would tell the user, with total confidence, that they have no
    repositories. Zero rows under OLD rules is missing data, not an answer."""
    repo = tmp_path / "repo"
    # A real index (rows, manifest) whose applied signature is a stale one.
    cfg = _write_dirs_index([str(tmp_path), str(repo)], applied=False)
    with open(cfg.applied_ignore_json, "w") as f:
        json.dump({"roots": {"/": "a-signature-from-before-the-leaf-rule"}}, f)
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is False
    assert body["reason"] == "outdated"
    assert body["repos"] == []


# -- a stale index still answers -----------------------------------------------

def test_a_stale_index_WITH_rows_serves_them_marked_stale(home, tmp_path, client):
    """The principle: a stale index is still a useful index. Rows that can answer
    the question are served even though the rules signature is a generation behind
    — refusing would hide a list that is almost certainly right, and an index is
    ALWAYS somewhat behind the filesystem."""
    repo = tmp_path / "repo"
    cfg = _write_dirs_index([str(repo), _git(repo)], applied=False)
    with open(cfg.applied_ignore_json, "w") as f:
        json.dump({"roots": {"/": "an-older-signature"}}, f)
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is True
    assert body["stale"] is True
    assert [r["path"] for r in body["repos"]] == [repo.as_posix()]


def test_a_fresh_index_with_no_repos_is_a_real_empty_answer(home, tmp_path, client):
    """The other side of the same coin: the rule DID run and found nothing, so the
    empty list is an answer and must not be dressed up as "still building"."""
    _write_dirs_index([str(tmp_path), str(tmp_path / "plain")])
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is True
    assert body["stale"] is False
    assert body["repos"] == []


def test_rows_screened_down_to_zero_is_still_a_real_answer(home, tmp_path, client):
    """The zero-row test is on RAW rows, before screening. A machine whose every
    repo sits inside a dotted directory screens to an empty list — but the rule ran,
    so that is an answer, not a migration. Getting this backwards would report
    "outdated" forever on such a machine."""
    hidden = tmp_path / ".oh-my-zsh"
    cfg = _write_dirs_index([_git(hidden)], applied=False)
    with open(cfg.applied_ignore_json, "w") as f:
        json.dump({"roots": {"/": "an-older-signature"}}, f)
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is True   # rows existed; screening emptied them
    assert body["repos"] == []


def test_a_scan_in_flight_over_a_usable_index_serves_it_marked_stale(
        home, tmp_path, client, monkeypatch):
    """A rescan keeps serving the last completed generation (index-store.md §4),
    so a scan in flight is a `stale` note on a live list, never a reason to hide
    it."""
    from fused_render.server.routers import git_repos as mod

    repo = tmp_path / "repo"
    _write_dirs_index([str(repo), _git(repo)])
    monkeypatch.setattr(mod, "_scanning", lambda cfg: True)
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is True
    assert body["scanning"] is True
    assert body["stale"] is True
    assert [r["path"] for r in body["repos"]] == [repo.as_posix()]


def test_no_index_at_all_says_so_distinctly(home, tmp_path, client):
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is False
    assert body["reason"] == "no-index"
    assert body["stale"] is False


def test_an_index_with_no_applied_signature_reads_stale_but_still_answers(
        home, tmp_path, client):
    """None means "predates the applied-ignore file", so the rows cannot be assumed
    current — but they exist, and rows beat signatures. Served, marked stale."""
    repo = tmp_path / "repo"
    _write_dirs_index([str(tmp_path), str(repo), _git(repo)], applied=False)
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is True
    assert body["stale"] is True
    assert [r["path"] for r in body["repos"]] == [repo.as_posix()]


def test_a_partially_rescanned_multi_root_index_serves_what_it_has(home, tmp_path,
                                                                  client):
    """Two configured roots, only one reconciled under the current rules. The repos
    under the reconciled root are real and get served; `stale` says the picture is
    incomplete. Hiding them would be the "refuse to answer while behind" mistake —
    an index is essentially always behind on at least one root."""
    from fused_render.index import runner

    a, b = tmp_path / "a", tmp_path / "b"
    repo = a / "repo"
    _set_roots([a, b])
    cfg = _write_dirs_index([str(repo), _git(repo)], applied=False)
    # `_fresh` looks up `applied_ignore_sig(cfg, r)` for `r in scan_roots(cfg)`,
    # which hands back `runner.canonical_root` spellings — so stamping the RAW
    # `str(a)` key would never match that lookup on Windows (`C:\...\a` vs the
    # stored `C:/.../a`), and this root would read as never-reconciled forever.
    save_applied_ignore(cfg, runner.canonical_root(str(a)))   # only /a reconciled
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is True
    assert body["stale"] is True
    assert [r["path"] for r in body["repos"]] == [repo.as_posix()]
    save_applied_ignore(cfg, runner.canonical_root(str(b)))
    body = client.get("/api/git-repos").json()
    assert body["indexed"] is True
    assert body["stale"] is False      # every root now reconciled
    assert [r["path"] for r in body["repos"]] == [repo.as_posix()]


def test_a_root_configured_in_a_NON_canonical_spelling_still_reads_usable(
        home, tmp_path, client, monkeypatch):
    """The fingerprint is stamped under runner.canonical_root(root), so looking it
    up with the user's raw configured spelling misses and the tab would sit in the
    not-indexed empty state forever — even after a successful scan. On Windows that
    is EVERY root (`expanduser("~")` -> `C:\\Users\\me` vs a stored `C:/Users/me`),
    which is strictly worse than the multi-root staleness the per-root check fixed.

    Exercised through `~` rather than through separators: expansion is a
    normalization every platform performs, so this fails on POSIX too if the
    lookup key is raw."""
    from fused_render.index import runner

    real_expanduser = os.path.expanduser

    def expand(p):
        if p == "~":
            return str(tmp_path)
        if p.startswith("~/"):
            return str(tmp_path) + p[1:]
        return real_expanduser(p)

    monkeypatch.setattr(os.path, "expanduser", expand)
    repo = tmp_path / "proj" / "repo"
    _set_roots(["~/proj"])                       # raw, un-expanded spelling
    cfg = _write_dirs_index([_git(repo)], applied=False)
    save_applied_ignore(cfg, runner.canonical_root("~/proj"))   # what a scan writes

    body = client.get("/api/git-repos").json()
    assert body["indexed"] is True
    assert [r["path"] for r in body["repos"]] == [repo.as_posix()]


def test_a_legacy_sig_root_is_stale_even_once_another_root_is_stamped(
        home, tmp_path, client):
    """Bugbot's multi-root hole, pinned. On a store migrated from the pre-per-root
    applied-ignore format, stamping the FIRST root leaves
    `{"roots": {a: current}, "legacy_sig": old}` — and the ROOTLESS
    applied_ignore_sig() reads only the `roots` values, so it answers "everything
    matches" while root `b` is still described by nothing but the stale legacy sig
    and is unreconciled. Checking each root individually is what sees it: the
    per-root form falls back to `legacy_sig` for `b`.

    The CONSEQUENCE moved — a mismatch now marks the answer `stale` instead of
    withholding it — but the detection must not: `stale: false` here would promise a
    complete picture of a machine half of which was never scanned under these
    rules."""
    from fused_render.index import runner
    from fused_render.index.store import applied_ignore_sig

    a, b = tmp_path / "a", tmp_path / "b"
    repo = a / "repo"
    _set_roots([a, b])
    cfg = _write_dirs_index([str(repo), _git(repo)], applied=False)
    # the pre-per-root file, then one root migrated onto the new format
    with open(cfg.applied_ignore_json, "w") as f:
        json.dump({"sig": "a-pre-leaf-rule-global-sig"}, f)
    # canonical, matching what scan_roots()/save_applied_ignore's real caller
    # stamps — a raw `str(a)` key would just never match `a`'s lookup either,
    # which would hide the very distinction ("a" migrated, "b" is not) this
    # test is about.
    save_applied_ignore(cfg, runner.canonical_root(str(a)))

    # the rootless form is exactly as misleading as reported...
    assert applied_ignore_sig(cfg) == cfg.rules.sig()
    # ...and the endpoint must not be fooled into claiming freshness
    body = client.get("/api/git-repos").json()
    assert body["stale"] is True
    assert [r["path"] for r in body["repos"]] == [repo.as_posix()]


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


# -- the freshness nudge -------------------------------------------------------

def _reset_rotation(monkeypatch):
    """Start the round-robin over the scan roots at its first root.

    The counter is module state and deliberately never resets in production, so a
    test that did not pin it would assert on whichever turn earlier tests in the
    same process happened to leave behind."""
    import itertools

    from fused_render.server.routers import git_repos as mod

    monkeypatch.setattr(mod, "_root_turn", itertools.count())


def test_opening_the_tab_fires_a_freshness_check_on_the_scan_roots(
        home, tmp_path, client, monkeypatch):
    """The tab is served entirely from the index, so without this it is the one
    surface in the app that can never notice the index is behind — /api/fs/list
    fires the same check for every folder the explorer opens."""
    from fused_render.index import runner
    from fused_render.server.routers import index as index_mod

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _set_roots([a, b])
    repo = a / "repo"
    _write_dirs_index([str(a), str(repo), _git(repo)])
    checked = []
    monkeypatch.setattr(index_mod, "note_folder_opened",
                        lambda p: (checked.append(p), True)[1])
    _reset_rotation(monkeypatch)

    body = client.get("/api/git-repos").json()
    assert [r["path"] for r in body["repos"]] == [repo.as_posix()]
    # A configured root, and nothing else: the tab is machine-wide, so a root is
    # the only path it can name. `scan_roots()` hands `note_folder_opened` the
    # `runner.canonical_root` spelling, not the raw configured one — so the
    # expectation has to be built the same way, or this compares canonical
    # against raw on Windows for no reason the app got wrong.
    assert checked == [runner.canonical_root(str(a))]


def test_successive_tab_opens_check_each_root_in_turn(home, tmp_path, client,
                                                      monkeypatch):
    """ONE root per request, round-robin — and the point is that the later roots
    are genuinely reached.

    Offering every root in a loop looked equivalent and was not: the checker's
    slot is taken by the first call and released by its own background thread a
    config read later, so every subsequent root in the same request fails its
    non-blocking acquire. The first root would win every request forever and the
    second would never be checked at all."""
    from fused_render.index import runner
    from fused_render.server.routers import index as index_mod

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _set_roots([a, b])
    _write_dirs_index([str(a)])
    checked = []
    monkeypatch.setattr(index_mod, "note_folder_opened",
                        lambda p: (checked.append(p), True)[1])
    _reset_rotation(monkeypatch)

    for _ in range(4):
        assert client.get("/api/git-repos").status_code == 200
    assert checked == [runner.canonical_root(str(a)), runner.canonical_root(str(b)),
                       runner.canonical_root(str(a)), runner.canonical_root(str(b))]


def test_a_freshness_check_that_explodes_does_not_fail_the_tab(
        home, tmp_path, client, monkeypatch):
    """Housekeeping, hung off a read endpoint. The repo list is the answer; the
    nudge is a side effect and must never become the response.

    Two roots, the first one raising, because a bad root must not take the
    rotation down with it: the roots are independent questions, and a config
    where one of them wedges the check for every other one is the failure that
    hides itself."""
    from fused_render.index import runner
    from fused_render.server.routers import index as index_mod

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _set_roots([a, b])
    repo = a / "repo"
    _write_dirs_index([str(a), str(repo), _git(repo)])
    checked = []
    # `boom` is handed whatever `note_folder_opened` is actually called with —
    # the `runner.canonical_root` spelling `scan_roots()` returns, not the raw
    # configured one — so it has to recognise "root a" in that same spelling or
    # it never raises on Windows and this test silently stops exercising the
    # "a bad root must not take the rotation down with it" scenario it exists
    # to pin.
    canonical_a = runner.canonical_root(str(a))

    def boom(path):
        checked.append(path)
        if path == canonical_a:
            raise RuntimeError("freshness exploded")
        return True

    monkeypatch.setattr(index_mod, "note_folder_opened", boom)
    _reset_rotation(monkeypatch)

    for _ in range(2):
        resp = client.get("/api/git-repos")
        assert resp.status_code == 200
        assert [r["path"] for r in resp.json()["repos"]] == [repo.as_posix()]
    # the throwing root took its turn and the next request moved on regardless
    assert checked == [canonical_a, runner.canonical_root(str(b))]
