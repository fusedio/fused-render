"""The /api/index/* routes and the startup scan scheduler.

See fused_render/index/specs/server-api.md.
"""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from fused_render.index import runner
from fused_render.index.config import IndexConfig, load_config
from fused_render.server import create_app
from fused_render.server.routers import index as index_router
from fused_render.server.routers.index import (
    note_folder_opened as _real_note_folder_opened,
)


class _FakePopen:
    """Stands in for a detached worker: the scan never actually runs."""

    pid = 4242


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway shell home, so the index store lands under it."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    return h


def _tree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha.txt").write_text("a", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "beta.md").write_text("b", encoding="utf-8")
    return src


def _point_home_at(monkeypatch, path):
    """Make `os.path.expanduser("~")` answer `path`, on every platform.

    `monkeypatch.setenv("HOME", ...)` alone only works on POSIX: Windows'
    `ntpath.expanduser` reads `USERPROFILE` (falling back to
    `HOMEDRIVE`+`HOMEPATH`) and never consults `HOME` at all, so a test that
    only sets `HOME` silently keeps pointing `warm_root()`/`config.home` at
    the real machine's profile instead of the tree it built."""
    real_expanduser = os.path.expanduser
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(path) if p == "~" else real_expanduser(p))


# -- guards --------------------------------------------------------------------

@pytest.mark.parametrize("path,body", [
    ("/api/index/scan", {"root": "."}),
    ("/api/index/cancel", {"run_id": "x"}),
    ("/api/index/config", {"roots": []}),
])
def test_mutating_routes_require_the_fused_header(home, tmp_path, path, body):
    resp = _client(tmp_path).post(path, json=body)
    assert resp.status_code == 403
    assert "X-Fused" in resp.json()["error"]


def test_scan_rejects_a_path_that_is_not_a_directory(home, tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    resp = _client(tmp_path).post("/api/index/scan", json={"root": str(f)},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 400
    assert "not a directory" in resp.json()["error"]


def test_cancel_of_an_unknown_run_is_a_400(home, tmp_path):
    resp = _client(tmp_path).post("/api/index/cancel", json={"run_id": "nope"},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 400


# -- the scan lifecycle, for real ---------------------------------------------

def test_scan_status_and_stats_over_a_real_tree(home, tmp_path):
    """One end-to-end pass: POST a scan, poll status until the detached worker
    finishes, then read the index back through stats and lookup."""
    src = _tree(tmp_path)
    client = _client(tmp_path)
    started = client.post("/api/index/scan", json={"root": str(src)},
                          headers={"X-Fused": "1"})
    assert started.status_code == 200
    run_id = started.json()["run_id"]

    deadline = time.time() + 120
    state = None
    while time.time() < deadline:
        state = client.get("/api/index/status",
                           params={"run_id": run_id}).json()
        if not state["running"]:
            break
        time.sleep(0.2)
    assert state is not None and state["running"] is False, state
    assert state["error"] is None, state["error"]
    # the real `runner.start` records `canonical_root(root)`, not the
    # caller's raw spelling (platform.md §1) — a no-op of that on POSIX.
    assert state["root"] == runner.canonical_root(str(src))

    stats = client.get("/api/index/stats", params={"root": str(src)}).json()
    assert stats["rows"] == 2
    assert stats["empty"] is False

    found = client.get("/api/index/lookup", params={"q": "beta"}).json()
    assert [r["name"] for r in found["rows"]] == ["beta.md"]


def test_status_without_a_run_id_reports_the_latest_run(home, tmp_path):
    cfg = load_config()
    d = os.path.join(cfg.runs_dir, "20260101-000000-aa")
    os.makedirs(d)
    with open(os.path.join(d, "spec.json"), "w") as f:
        json.dump({"root": "/r"}, f)
    with open(os.path.join(d, "events.jsonl"), "w") as f:
        f.write(json.dumps({"type": "progress", "dirs": 2, "files": 7,
                            "current": "/r/x"}) + "\n")
    body = _client(tmp_path).get("/api/index/status").json()
    assert body["running"] is True
    assert body["files"] == 7
    assert body["root"] == "/r"
    assert body["run_id"] == "20260101-000000-aa"


def _write_run(cfg, run_id, root, events):
    d = os.path.join(cfg.runs_dir, run_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "spec.json"), "w") as f:
        json.dump({"root": root}, f)
    with open(os.path.join(d, "events.jsonl"), "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return d


def test_status_without_a_run_id_reports_a_RUNNING_run(home, tmp_path):
    """With several roots the newest run is not the interesting one: a small
    root can finish while the big one still walks, and the newest-first pick
    then froze the panel on the finished run's counts for minutes while
    `scanning` stayed true. The run reported must be one that is running."""
    cfg = load_config()
    _write_run(cfg, "20260101-000000-aa", "/big",
               [{"type": "progress", "dirs": 2, "files": 7, "current": "/big/x"}])
    _write_run(cfg, "20260101-000100-bb", "/small",
               [{"type": "progress", "dirs": 1, "files": 1},
                {"type": "run_end", "summary": {"rows": 1}}])
    body = _client(tmp_path).get("/api/index/status").json()
    assert body["scanning"] is True
    assert body["running"] is True
    assert body["run_id"] == "20260101-000000-aa"
    assert body["root"] == "/big"
    assert body["files"] == 7


def test_status_without_a_run_id_falls_back_to_the_latest_when_none_run(home, tmp_path):
    cfg = load_config()
    _write_run(cfg, "20260101-000000-aa", "/old",
               [{"type": "run_end", "summary": {}}])
    _write_run(cfg, "20260101-000100-bb", "/new",
               [{"type": "progress", "files": 4},
                {"type": "run_end", "summary": {}}])
    body = _client(tmp_path).get("/api/index/status").json()
    assert body["scanning"] is False
    assert body["run_id"] == "20260101-000100-bb"
    assert body["root"] == "/new"


def test_status_with_no_runs_at_all_is_a_quiet_idle(home, tmp_path):
    body = _client(tmp_path).get("/api/index/status").json()
    assert body == {"ok": True, "running": False, "run_id": None, "root": None,
                    "phase": "", "dirs": 0, "files": 0, "reused": 0,
                    "current": "", "summary": None, "cancelled": False,
                    "error": None, "indexed": False, "updated": None,
                    "has_index": False, "scanning": False,
                    "files_indexed": 0, "last_completed_at": None}


def test_status_of_an_unknown_run_id_is_a_400(home, tmp_path):
    resp = _client(tmp_path).get("/api/index/status", params={"run_id": "nope"})
    assert resp.status_code == 400


def test_cancel_writes_the_flag(home, tmp_path):
    cfg = load_config()
    d = os.path.join(cfg.runs_dir, "r1")
    os.makedirs(d)
    open(os.path.join(d, "spec.json"), "w").close()
    resp = _client(tmp_path).post("/api/index/cancel", json={"run_id": "r1"},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 200
    assert os.path.exists(os.path.join(d, "cancel"))


# -- stats / lookup on an empty index -----------------------------------------

def test_stats_on_a_never_built_index(home, tmp_path):
    body = _client(tmp_path).get("/api/index/stats").json()
    assert body["empty"] is True
    assert body["rows"] == 0


def test_lookup_on_a_never_built_index(home, tmp_path):
    body = _client(tmp_path).get("/api/index/lookup", params={"q": "x"}).json()
    assert body["empty"] is True
    assert body["rows"] == []


def test_lookup_limit_is_clamped(home, tmp_path):
    body = _client(tmp_path).get("/api/index/lookup",
                                 params={"q": "x", "limit": 10 ** 9}).json()
    assert body["ok"] is True  # coerced, not rejected


# -- config --------------------------------------------------------------------

def test_config_round_trips_roots_and_ignore(home, tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/index/config",
                       json={"roots": [str(tmp_path)], "ignore": ["node_modules", ""]},
                       headers={"X-Fused": "1"})
    assert resp.status_code == 200
    # "roots" answers with `scan_roots(cfg)` — canonical_root form, not the
    # caller's raw spelling (platform.md §1; routers/index.scan_roots).
    canon = [runner.canonical_root(str(tmp_path))]
    assert resp.json()["roots"] == canon
    assert resp.json()["ignore"] == ["node_modules", ""]  # verbatim
    body = client.get("/api/index/config").json()
    assert body["roots"] == canon
    assert body["defaults"]  # the starting list is reported for a Reset button


def test_saving_a_changed_ignore_list_reconciles_the_index(home, tmp_path, monkeypatch):
    """Editing the rules must not leave the index disagreeing with them. The
    engine's fingerprint turns the next scan into a full rebuild, which purges
    newly-ignored rows and picks up newly-unignored ones — so the save starts
    that scan rather than waiting for the next boot."""
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append((root, full))
                        or {"run_id": "r", "root": root})
    cfg = load_config()
    # pretend an index exists, built under the current rules
    index_router.save_applied_ignore(cfg, index_router.scan_roots(cfg)[0])
    body = _client(tmp_path).post(
        "/api/index/config", json={"ignore": ["node_modules", "target"]},
        headers={"X-Fused": "1"}).json()
    assert body["needs_rescan"] is True
    assert body["rescan_run_id"] == "r"
    assert started and started[0][1] is False  # the fingerprint forces the full one


def test_saving_an_unchanged_ignore_list_starts_nothing(home, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    cfg = load_config()
    index_router.save_applied_ignore(cfg, index_router.scan_roots(cfg)[0])
    body = _client(tmp_path).post("/api/index/config",
                                  json={"ignore": list(cfg.ignore)},
                                  headers={"X-Fused": "1"}).json()
    assert body["needs_rescan"] is False
    assert started == []


def test_saving_rules_with_no_index_yet_starts_nothing(home, tmp_path, monkeypatch):
    """Nothing to reconcile before a first scan — and the startup scheduler
    will pick the new rules up anyway."""
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    body = _client(tmp_path).post("/api/index/config", json={"ignore": ["x"]},
                                  headers={"X-Fused": "1"}).json()
    assert body["needs_rescan"] is False
    assert started == []


def test_saving_the_config_answers_in_the_same_shape_as_reading_it(home, tmp_path):
    """The panel replaces its whole state with the save's response, so the two
    shapes must agree. They did not: GET reports `roots` as the roots actually
    scanned (home, when none are configured) plus `configured_roots`, while
    the save reported the raw configured list and omitted the second field —
    so with no configured roots the panel's "Covers …" line vanished on save
    even though coverage had not changed."""
    client = _client(tmp_path)
    read = client.get("/api/index/config").json()
    saved = client.post("/api/index/config", json={"ignore": ["node_modules"]},
                        headers={"X-Fused": "1"}).json()
    assert saved["roots"] == read["roots"]  # the effective (home) fallback
    assert saved["roots"]  # ...and it is not empty
    assert saved["configured_roots"] == read["configured_roots"] == []


def test_a_search_is_filtered_against_its_enclosing_index_root(home, tmp_path,
                                                              monkeypatch):
    """The gitignore filter pools its verdicts per INDEX ROOT, so the route has
    to tell it which one this folder lives under. Keyed on the REQUESTED folder
    instead (the old behaviour), browsing five folders evicted the first and
    re-paid a full check-ignore sweep of its whole recursive subtree."""
    seen = []
    monkeypatch.setattr(index_router, "filter_corpus",
                        lambda out, index_root=None: seen.append(index_root) or out)
    sub = tmp_path / "sub"
    sub.mkdir()
    client = _client(tmp_path)
    client.post("/api/index/config", json={"roots": [str(tmp_path)]},
                headers={"X-Fused": "1"})
    client.get(f"/api/index/search?root={tmp_path}")
    client.get(f"/api/index/search?root={sub}")
    # Both requests pool under the configured root, whichever folder was asked
    # for. A folder outside every root has no pool to share and gets None.
    client.get(f"/api/index/search?root={tmp_path.parent}")
    # The route hands `filter_corpus` the matched entry from `scan_roots`,
    # which is already in `runner.canonical_root` form — not the caller's
    # raw spelling (platform.md §1) — so the expectation has to be too.
    canon = runner.canonical_root(str(tmp_path))
    assert seen == [canon, canon, None]


def test_saving_the_ignore_list_preserves_comments_and_blank_lines(home, tmp_path):
    """The panel documents `#` comments and round-trips the textarea through
    this response, so cleaning on save silently deleted the user's
    annotations the first time they touched the field."""
    raw = ["# dependency caches", "node_modules", "", ".venv"]
    client = _client(tmp_path)
    saved = client.post("/api/index/config", json={"ignore": raw},
                        headers={"X-Fused": "1"}).json()
    assert saved["ignore"] == raw
    assert client.get("/api/index/config").json()["ignore"] == raw
    # the rules the engine runs still see only the two patterns
    assert load_config().rules.patterns == ["node_modules", ".venv"]


def test_a_comment_only_edit_needs_no_rescan(home, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root)
                        or {"run_id": "r", "root": root})
    client = _client(tmp_path)
    client.post("/api/index/config", json={"ignore": ["node_modules"]},
                headers={"X-Fused": "1"})
    cfg = load_config()
    index_router.save_applied_ignore(cfg, index_router.scan_roots(cfg)[0])
    started.clear()
    body = client.post("/api/index/config",
                       json={"ignore": ["# deps", "node_modules", ""]},
                       headers={"X-Fused": "1"}).json()
    assert body["needs_rescan"] is False
    assert started == []


def test_saving_rules_mid_scan_supersedes_the_running_scan(home, tmp_path, monkeypatch):
    """The reported bug, end to end: a skip-rules save while a scan is in
    flight must not be answered by joining that scan. The running worker
    carries the OLD ignore list and stamps it as applied, so joining it means
    the reconciling rescan the save promised never happens — while the panel
    says the index is being rebuilt."""
    spawned = []
    monkeypatch.setattr(index_router.runner.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(argv) or _FakePopen())
    root = tmp_path / "proj"
    root.mkdir()
    cfg = load_config()
    cfg.roots = [str(root)]
    cfg.ignore = ["node_modules"]
    index_router.save_config(cfg)
    # A scan is running under rules A, and rules A are what the index claims.
    live = index_router.runner.start(load_config(), str(root))
    # `save_applied_ignore` (unlike `runner.start`) does NOT canonicalize its
    # own `root` — it trusts the caller, because its real caller (`scan.
    # run_scan`) only ever gets there with the already-canonical spelling
    # `runner.start` wrote into spec.json. Passing the raw `str(root)` here
    # instead would file the fingerprint under a key `applied_ignore_sig`
    # (looked up via the canonical `scan_roots(cfg)` entries) can never find,
    # reading as "unknown" — which the route's `(sig or current) != current`
    # check treats as "already reconciled", so `needs_rescan` comes back
    # False instead of True.
    index_router.save_applied_ignore(load_config(),
                                     index_router.runner.canonical_root(str(root)))

    body = _client(tmp_path).post(
        "/api/index/config", json={"ignore": ["node_modules", "target"]},
        headers={"X-Fused": "1"}).json()

    assert body["needs_rescan"] is True
    assert body["rescan_run_id"] is not None
    assert body["rescan_run_id"] != live["run_id"]  # not the joined old run
    runs_dir = load_config().runs_dir
    assert os.path.exists(os.path.join(runs_dir, live["run_id"], "cancel"))
    fresh = json.load(open(os.path.join(runs_dir, body["rescan_run_id"], "spec.json")))
    assert fresh["config"]["ignore"] == ["node_modules", "target"]


def test_config_rejects_a_non_list(home, tmp_path):
    resp = _client(tmp_path).post("/api/index/config", json={"roots": "nope"},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 400


def test_default_scan_roots_are_the_users_home(home, tmp_path, monkeypatch):
    """Home, not the project root: a whole-home scan costs seconds with the
    default ignore rules and is what makes search useful everywhere."""
    real_expanduser = os.path.expanduser
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "userhome")
                        if p == "~" else real_expanduser(p))
    # `scan_roots` answers in `runner.canonical_root` form (platform.md §1),
    # not the raw native-separator string `expanduser`/`str(Path)` hand back.
    assert index_router.scan_roots(load_config(), start_dir=str(tmp_path)) == [
        runner.canonical_root(str(tmp_path / "userhome"))]


def test_configured_roots_win_over_the_default(home, tmp_path):
    cfg = load_config()
    cfg.roots = [str(tmp_path / "proj")]
    assert index_router.scan_roots(cfg, start_dir=str(tmp_path)) == [
        runner.canonical_root(str(tmp_path / "proj"))]


def test_scan_roots_are_canonical_so_store_lookups_hit(home, tmp_path, monkeypatch):
    """Roots are KEYS, not just paths: runner.start files every fingerprint,
    debounce entry and freshness record under runner.canonical_root(root), and
    scan_roots' output is compared against those keys (the stale-fingerprint
    rescan, routers/git_repos._usable). A raw configured spelling misses —
    `~/proj` is not `/home/me/proj`, and on Windows `expanduser("~")` gives
    `C:\\Users\\me` against a stored `C:/Users/me`, so every lookup misses there
    and the index reads as permanently unreconciled.

    Asserted as "identical to what runner.start would use", not against a
    hand-written string: the whole bug is two spellings drifting, so the test has
    to pin them together rather than restate one of them (and a separator
    assertion would only ever fire on Windows, where this suite does not run)."""
    real_expanduser = os.path.expanduser
    fake_home = str(tmp_path / "userhome")

    def expand(p):
        if p == "~":
            return fake_home
        if p.startswith("~/"):
            return fake_home + p[1:]
        return real_expanduser(p)

    monkeypatch.setattr(os.path, "expanduser", expand)
    cfg = load_config()
    cfg.roots = ["~/proj", str(tmp_path / "other") + "/"]
    assert index_router.scan_roots(cfg) == [
        runner.canonical_root(r) for r in cfg.roots]
    # ~ really was expanded, so this is not a tautology over two no-ops. Still
    # wrapped in canonical_root: the concatenation above (`fake_home + p[1:]`)
    # is native-separator on Windows, and canonical_root is what scan_roots
    # itself would produce from that same expansion.
    assert index_router.scan_roots(cfg)[0] == runner.canonical_root(
        str(tmp_path / "userhome" / "proj"))
    # and the default root gets the same treatment
    cfg.roots = []
    assert index_router.scan_roots(cfg) == [runner.canonical_root("~")]


# -- manual actions ------------------------------------------------------------

def test_scan_passes_the_full_flag_through(home, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: seen.append(full)
                        or {"run_id": "r", "root": root})
    _client(tmp_path).post("/api/index/scan",
                           json={"root": str(tmp_path), "full": True},
                           headers={"X-Fused": "1"})
    assert seen == [True]


def test_scan_with_no_root_uses_the_configured_one(home, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: seen.append(root)
                        or {"run_id": "r", "root": root})
    cfg = load_config()
    cfg.roots = [str(tmp_path)]
    index_router.save_config(cfg)
    _client(tmp_path).post("/api/index/scan", json={}, headers={"X-Fused": "1"})
    # the route resolves the missing root through `scan_roots`, which answers
    # in canonical_root form, not the configured raw spelling.
    assert seen == [runner.canonical_root(str(tmp_path))]


def test_scan_with_no_root_covers_EVERY_root(home, tmp_path, monkeypatch):
    """Re-index presents itself as rebuilding the index, so it has to mean all
    of it: scanning only roots[0] left every other root stale with nothing in
    the UI to say so."""
    seen = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: seen.append((root, full))
                        or {"run_id": "run-" + os.path.basename(root),
                            "root": root})
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = load_config()
    cfg.roots = [str(a), str(b)]
    index_router.save_config(cfg)
    body = _client(tmp_path).post("/api/index/scan", json={"full": True},
                                  headers={"X-Fused": "1"}).json()
    # every root the route hands to `start` came out of `scan_roots`, in
    # canonical_root form.
    ca, cb = runner.canonical_root(str(a)), runner.canonical_root(str(b))
    assert seen == [(ca, True), (cb, True)]
    assert [r["root"] for r in body["runs"]] == [ca, cb]
    # the single-run fields stay, for a caller that only knows about one
    assert body["run_id"] == "run-a"
    assert body["root"] == ca


def test_scan_with_no_root_skips_roots_that_no_longer_exist(home, tmp_path, monkeypatch):
    """The config outlives the folders it names — one dead root must not fail
    the whole fan-out, exactly as the startup scheduler treats it."""
    def fake_start(cfg, root, full=False):
        if root.endswith("gone"):
            raise ValueError("not a directory: " + root)
        return {"run_id": "r", "root": root}

    monkeypatch.setattr(index_router.runner, "start", fake_start)
    live = tmp_path / "live"
    live.mkdir()
    cfg = load_config()
    cfg.roots = [str(tmp_path / "gone"), str(live)]
    index_router.save_config(cfg)
    resp = _client(tmp_path).post("/api/index/scan", json={},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 200
    assert [r["root"] for r in resp.json()["runs"]] == [
        runner.canonical_root(str(live))]


def test_scan_with_no_root_and_nothing_startable_is_an_error(home, tmp_path, monkeypatch):
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: (_ for _ in ()).throw(
                            ValueError("not a directory: " + root)))
    cfg = load_config()
    cfg.roots = [str(tmp_path / "gone")]
    index_router.save_config(cfg)
    resp = _client(tmp_path).post("/api/index/scan", json={},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 400


def test_delete_requires_the_fused_header(home, tmp_path):
    assert _client(tmp_path).post("/api/index/delete").status_code == 403


def test_delete_removes_the_store_and_search_reverts_to_the_walk(home, tmp_path):
    """After a delete the explorer must degrade, not break: no index means
    covered:false, which is the same silent walk fallback as 'not scanned
    yet'."""
    src = _tree(tmp_path)
    client = _client(tmp_path)
    run_id = client.post("/api/index/scan", json={"root": str(src)},
                         headers={"X-Fused": "1"}).json()["run_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        if not client.get("/api/index/status", params={"run_id": run_id}).json()["running"]:
            break
        time.sleep(0.2)
    assert client.get("/api/index/status").json()["has_index"] is True

    resp = client.post("/api/index/delete", headers={"X-Fused": "1"})
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    status = client.get("/api/index/status").json()
    assert status["has_index"] is False
    assert status["files_indexed"] == 0
    body = client.get("/api/index/search", params={"root": str(src)}).json()
    assert body["covered"] is False and body["entries"] == []


def test_delete_on_an_empty_store_is_not_an_error(home, tmp_path):
    resp = _client(tmp_path).post("/api/index/delete", headers={"X-Fused": "1"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


def test_delete_cancels_a_running_scan(home, tmp_path):
    cfg = load_config()
    d = os.path.join(cfg.runs_dir, "20260101-000000-live")
    os.makedirs(d)
    with open(os.path.join(d, "spec.json"), "w") as f:
        json.dump({"root": "/r"}, f)
    with open(os.path.join(d, "events.jsonl"), "w") as f:
        f.write(json.dumps({"type": "phase", "msg": "scanning"}) + "\n")
    _client(tmp_path).post("/api/index/delete", headers={"X-Fused": "1"})
    # the cancel flag has to survive the delete, or the worker would happily
    # compact a fresh index into the store the user just emptied
    assert os.path.exists(os.path.join(d, "cancel"))


# -- status --------------------------------------------------------------------

def test_status_reports_scanning_and_index_presence_independently(home, tmp_path):
    cfg = load_config()
    d = os.path.join(cfg.runs_dir, "20260101-000000-aa")
    os.makedirs(d)
    with open(os.path.join(d, "spec.json"), "w") as f:
        json.dump({"root": "/r"}, f)
    with open(os.path.join(d, "events.jsonl"), "w") as f:
        f.write(json.dumps({"type": "progress", "files": 12}) + "\n")
    body = _client(tmp_path).get("/api/index/status").json()
    assert body["scanning"] is True
    assert body["has_index"] is False   # nothing compacted yet: walk fallback
    assert body["files"] == 12          # this run's progress
    assert body["files_indexed"] == 0   # rows in a completed index
    assert body["last_completed_at"] is None


# -- the startup scheduler -----------------------------------------------------

def test_startup_schedules_one_scan_per_root(home, tmp_path, monkeypatch):
    src = _tree(tmp_path)
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root)
                        or {"run_id": "x", "root": root})
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert started == [runner.canonical_root(str(src))]


def test_startup_scan_is_debounced(home, tmp_path, monkeypatch):
    src = _tree(tmp_path)
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    # `_record_scan` does not canonicalize its own `root` (only `last_scan`'s
    # read side does — see test_index_freshness's floor test for the same
    # asymmetry), so seeding the debounce record directly like this has to
    # canonicalize first or `run_startup_scan`'s debounce check never finds
    # it and starts a scan anyway.
    runner._record_scan(cfg, runner.canonical_root(str(src)))  # a scan just ran
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert started == []


def test_startup_scan_rescans_once_the_debounce_has_elapsed(home, tmp_path, monkeypatch):
    src = _tree(tmp_path)
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    runner._record_scan(cfg, str(src))
    monkeypatch.setattr(index_router, "SCAN_DEBOUNCE_S", 0)
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert started == [runner.canonical_root(str(src))]


def test_startup_scan_never_raises(home, tmp_path, monkeypatch):
    """Housekeeping must not be able to stop the server from serving."""
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(index_router.runner, "start", boom)
    cfg = load_config()
    cfg.roots = [str(tmp_path)]
    index_router.save_config(cfg)
    index_router.run_startup_scan(start_dir=str(tmp_path))  # no exception


def test_startup_scan_skips_a_root_that_is_gone(home, tmp_path, monkeypatch):
    """A missing root is runner.start's ValueError (raised after its mount
    guard) — the scheduler skips it quietly and still scans the remaining
    roots. Deliberately NO os.path.isdir in the scheduler itself: a kernel
    stat on a path under a wedged mount would hang the startup hook."""
    started = []

    def fake_start(cfg, root, full=False):
        if not os.path.isdir(root):
            raise ValueError(f"not a directory: {root}")
        started.append(root)
        return {"run_id": "r", "root": root}

    monkeypatch.setattr(index_router.runner, "start", fake_start)
    ok = tmp_path / "ok"
    ok.mkdir()
    cfg = load_config()
    cfg.roots = [str(tmp_path / "deleted"), str(ok)]
    index_router.save_config(cfg)
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert started == [runner.canonical_root(str(ok))]


# -- the compact corpus --------------------------------------------------------

def _corpus(n=3):
    """A corpus in the shape search_under answers with, nulls included."""
    entries = [{"rel": "d", "is_dir": True, "size": None, "mtime": None}]
    entries += [{"rel": f"d/f{i}.txt", "is_dir": False, "size": i,
                 "mtime": 1700000000.5 + i} for i in range(n)]
    return {"covered": True, "fresh": True, "updated": 1.0, "age_s": 2.0,
            "root": "/r", "entries": entries, "truncated": False,
            "total": len(entries), "scanned_partitions": 1,
            "of_partitions": 1}


def _stub_corpus(monkeypatch, out):
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: dict(out))
    monkeypatch.setattr(index_router, "filter_corpus",
                        lambda out, index_root=None: out)


def test_search_answers_in_columns_when_asked(home, tmp_path, monkeypatch):
    """The corpus is the home page's whole ranking set — 25.7 MB of
    `{rel,is_dir,size,mtime}` objects on a 164k-entry home, most of it repeated
    key names. `fmt=columns` sends parallel arrays instead."""
    _stub_corpus(monkeypatch, _corpus())
    body = _client(tmp_path).get("/api/index/search",
                                 params={"root": "/r", "fmt": "columns"}).json()
    assert body["fmt"] == "columns"
    assert "entries" not in body
    assert body["rels"] == ["d", "d/f0.txt", "d/f1.txt", "d/f2.txt"]
    assert body["dirs"] == [1, 0, 0, 0]
    # Nulls are entries a directory legitimately has, not missing data.
    assert body["sizes"] == [None, 0, 1, 2]
    assert body["mtimes"] == [None, 1700000000.5, 1700000001.5, 1700000002.5]


def test_the_two_formats_carry_the_same_corpus_and_metadata(home, tmp_path,
                                                            monkeypatch):
    """`fmt` changes the encoding of the entries and nothing else: every
    client decision (covered/fresh/truncated/…) reads the same fields."""
    _stub_corpus(monkeypatch, _corpus())
    client = _client(tmp_path)
    classic = client.get("/api/index/search", params={"root": "/r"}).json()
    columns = client.get("/api/index/search",
                         params={"root": "/r", "fmt": "columns"}).json()
    assert "fmt" not in classic and classic["entries"] == _corpus()["entries"]
    decoded = [{"rel": r, "is_dir": bool(d), "size": s, "mtime": m}
               for r, d, s, m in zip(columns["rels"], columns["dirs"],
                                     columns["sizes"], columns["mtimes"])]
    assert decoded == classic["entries"]
    assert ({k: v for k, v in classic.items() if k != "entries"}
            == {k: v for k, v in columns.items()
                if k not in ("fmt", "rels", "dirs", "sizes", "mtimes")})


def test_an_unknown_fmt_answers_in_the_classic_shape(home, tmp_path, monkeypatch):
    """The bridge (`fused.fileIndex.search`, static/runtime.js) and every other
    caller ask with no `fmt` at all, so anything but the one known value has to
    be the old shape rather than an error."""
    _stub_corpus(monkeypatch, _corpus())
    body = _client(tmp_path).get("/api/index/search",
                                 params={"root": "/r", "fmt": "parquet"}).json()
    assert [e["rel"] for e in body["entries"]] == ["d", "d/f0.txt", "d/f1.txt",
                                                   "d/f2.txt"]


def test_columns_are_several_times_smaller_on_the_wire(home, tmp_path, monkeypatch):
    """The transfer is the third of the three costs of the first search (25.7 MB
    on a 164k-entry home). Content-Length, not the decoded body: the compact
    format is also gzipped, and both halves are the win."""
    _stub_corpus(monkeypatch, _corpus(n=2000))
    client = _client(tmp_path)
    classic = client.get("/api/index/search", params={"root": "/r"})
    columns = client.get("/api/index/search",
                         params={"root": "/r", "fmt": "columns"})
    assert columns.headers["content-encoding"] == "gzip"
    wire = (int(classic.headers["content-length"]),
            int(columns.headers["content-length"]))
    assert wire[0] / wire[1] >= 3.0, wire


@pytest.mark.parametrize("accept,gzipped", [
    ("gzip", True),
    ("gzip, deflate, br", True),
    ("x-gzip", True),
    ("gzip;q=0.5", True),
    ("*", True),
    # `q=0` is the explicit "I cannot take this encoding" spelling, and a
    # substring match read it as consent — handing a client a 5 MB body it
    # just said it could not decode.
    ("gzip;q=0", False),
    ("gzip; q=0.0", False),
    ("*;q=0", False),
    ("identity", False),
    ("", False),
])
def test_gzip_is_negotiated_by_q_value_not_by_substring(home, tmp_path,
                                                        monkeypatch, accept,
                                                        gzipped):
    _stub_corpus(monkeypatch, _corpus())
    resp = _client(tmp_path).get("/api/index/search",
                                 params={"root": "/r", "fmt": "columns"},
                                 headers={"Accept-Encoding": accept})
    assert (resp.headers.get("content-encoding") == "gzip") is gzipped
    # Both bodies live at one URL, so an intermediary keyed on the URL alone
    # would otherwise serve either one to either client.
    assert resp.headers["vary"] == "Accept-Encoding"
    assert resp.json()["rels"] == ["d", "d/f0.txt", "d/f1.txt", "d/f2.txt"]


def test_a_client_that_cannot_gunzip_still_gets_the_columns(home, tmp_path,
                                                            monkeypatch):
    """Encoding is negotiated, not assumed: `Accept-Encoding` decides whether
    the body is compressed, and the document inside it is the same either way."""
    _stub_corpus(monkeypatch, _corpus())
    resp = _client(tmp_path).get("/api/index/search",
                                 params={"root": "/r", "fmt": "columns"},
                                 headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in resp.headers
    assert resp.json()["rels"] == ["d", "d/f0.txt", "d/f1.txt", "d/f2.txt"]


# -- the startup warm ----------------------------------------------------------

def test_startup_warm_runs_the_home_pages_first_search(home, tmp_path, monkeypatch):
    """The warm must ask for exactly what the home page asks for.

    FilesHome searches `config.home` (routers/config.py — `expanduser("~")`),
    not the folder the app was opened on, so a warm aimed anywhere else fills
    a pool the first keystroke never reads."""
    _point_home_at(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: seen.append(("search", root))
                        or {"covered": True, "root": root, "entries": []})
    monkeypatch.setattr(index_router, "filter_corpus",
                        lambda out, index_root=None:
                        seen.append(("filter", index_root)) or out)
    cfg = load_config()
    cfg.roots = [str(tmp_path)]
    index_router.save_config(cfg)
    index_router.run_startup_warm()
    # Same pair of calls the route makes, and pooled under the same index root:
    # a warm that filtered under a different key would fill a second pool.
    # `filter_corpus`'s `index_root` comes from `enclosing_root(scan_roots(...))`
    # — canonical_root form, not the caller's raw spelling.
    assert seen == [("search", index_router.runner.canonical_root("~")),
                    ("filter", index_router.runner.canonical_root(str(tmp_path)))]


def test_startup_warm_never_raises(home, tmp_path, monkeypatch):
    """It runs on a background thread nobody joins: a raise here would be an
    unhandled exception in the log and a permanently cold pool."""
    def boom(*a, **k):
        raise RuntimeError("duckdb on fire")

    monkeypatch.setattr(index_router, "index_search", boom)
    index_router.run_startup_warm()  # no exception


def test_startup_warm_refuses_a_mount_backed_home(home, tmp_path, monkeypatch):
    """The index refuses to scan mounts, so a warm aimed at one could only
    ever answer `covered: false` — after paying kernel I/O on a mount path,
    which is the one thing this codebase never does speculatively."""
    monkeypatch.setattr(index_router.MountGuard, "blocks_root",
                        lambda self, root: True)
    called = []
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: called.append(root) or {})
    index_router.run_startup_warm()
    assert called == []


def test_startup_warm_fills_the_gitignore_verdict_pool(home, tmp_path, monkeypatch):
    """The point of the warm, end to end: after a real scan the first search
    pays a full check-ignore sweep of the whole corpus (~1.5 s on a home dir).
    Once the warm has run, that sweep is already in the pool."""
    from fused_render.server import index_gitignore

    src = _tree(tmp_path)
    (src / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (src / "noise.log").write_text("x", encoding="utf-8")
    client = _client(tmp_path)
    started = client.post("/api/index/scan", json={"root": str(src)},
                          headers={"X-Fused": "1"})
    run_id = started.json()["run_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        if not client.get("/api/index/status",
                          params={"run_id": run_id}).json()["running"]:
            break
        time.sleep(0.2)
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    index_gitignore._cache.clear()
    # HOME is what the warm aims at; point it at the scanned tree so the warm
    # answers `covered` and actually sweeps.
    _point_home_at(monkeypatch, src)
    index_router.run_startup_warm()
    # the pool is keyed by canonical_root(src), not the raw literal.
    pool = index_gitignore._cache.get(runner.canonical_root(str(src)))
    assert pool is not None
    assert "noise.log" in pool.ignored
    assert "alpha.txt" in pool.decider


def test_startup_scan_records_the_run_the_warm_waits_on(home, tmp_path, monkeypatch):
    """The warm waits on the run THIS process started, so the scheduler has to
    hand it over — `run_startup_scan` used to drop `runner.start`'s run id on
    the floor."""
    src = _tree(tmp_path)
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: {"run_id": "r-42",
                                                       "root": root})
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert index_router._startup_runs == {runner.canonical_root(str(src)): "r-42"}


def test_startup_warm_waits_for_the_scan_it_started(home, tmp_path, monkeypatch):
    """The first-ever boot, end to end — the case the warm exists for.

    On a fresh index the warm's first search answers `covered: false` cheaply
    and there is nothing to sweep; the scan this process just spawned finishes
    seconds later. Sampling once left the user's first keystroke paying the
    whole cold cost anyway (2.3 s, observed). Waiting for that one run and
    warming after it is what fills the pool."""
    from fused_render.server import index_gitignore

    src = _tree(tmp_path)
    (src / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (src / "noise.log").write_text("x", encoding="utf-8")
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    index_gitignore._cache.clear()
    # HOME is what the warm aims at, and what the scheduler scans.
    _point_home_at(monkeypatch, src)
    index_router.run_startup_scan(start_dir=str(tmp_path))

    # Force the uncovered branch rather than racing the worker: the first
    # search must answer `covered: false` for this to be the boot being tested.
    real_search = index_router.index_search
    calls = []

    def uncovered_once(cfg, root, **kw):
        calls.append(root)
        if len(calls) == 1:
            return {"covered": False, "root": root}
        return real_search(cfg, root, **kw)

    monkeypatch.setattr(index_router, "index_search", uncovered_once)
    monkeypatch.setattr(index_router, "WARM_WAIT_POLL_S", 0.05)
    monkeypatch.setattr(index_router, "WARM_WAIT_DEADLINE_S", 60.0)
    index_router.run_startup_warm()

    assert len(calls) == 2, "the warm did not search again after the scan"
    # the pool is keyed by canonical_root(src), not the raw literal.
    pool = index_gitignore._cache.get(runner.canonical_root(str(src)))
    assert pool is not None
    assert "noise.log" in pool.ignored
    assert "alpha.txt" in pool.decider


def test_startup_warm_gives_up_when_the_scan_never_finishes(home, tmp_path,
                                                             monkeypatch):
    """A worker alive but wedged — writing nothing, dying never — must not
    leave a thread polling for the process lifetime: the wait is
    deadline-bounded and the warm simply does not happen."""
    monkeypatch.setenv("HOME", str(tmp_path))
    run_dir = tmp_path / "wedged-run"
    run_dir.mkdir()
    calls = []
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: calls.append(root)
                        or {"covered": False, "root": root})
    monkeypatch.setattr(index_router.runner, "_run_dir",
                        lambda cfg, run_id: str(run_dir))
    # Alive, so the dead-worker exit cannot fire: only the deadline can end it.
    monkeypatch.setattr(index_router.runner, "_looks_abandoned",
                        lambda run_dir, now, threshold_s: False)
    monkeypatch.setattr(index_router, "WARM_WAIT_DEADLINE_S", 0.05)
    monkeypatch.setattr(index_router, "WARM_WAIT_POLL_S", 0.01)
    monkeypatch.setitem(index_router._startup_runs, index_router.warm_root(),
                        "run-that-hangs")
    began = time.monotonic()
    index_router.run_startup_warm()
    assert time.monotonic() - began < 5, "the wait is not deadline-bounded"
    # It still searches: how the wait ended says nothing about whether the
    # index covers the root, and an uncovered one costs a cheap `covered:
    # false` — exactly what the original single-shot warm already paid.
    assert len(calls) == 2


def test_startup_warm_stops_waiting_on_a_worker_that_died(home, tmp_path,
                                                          monkeypatch):
    """A worker killed mid-walk never writes `run_end`, so the log alone would
    keep the wait going until the deadline. runner's own mtime liveness check
    is what ends it in seconds instead."""
    monkeypatch.setenv("HOME", str(tmp_path))
    run_dir = tmp_path / "dead-run"
    run_dir.mkdir()
    calls = []
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: calls.append(root)
                        or {"covered": False, "root": root})
    monkeypatch.setattr(index_router.runner, "_run_dir",
                        lambda cfg, run_id: str(run_dir))
    monkeypatch.setattr(index_router.runner, "_looks_abandoned",
                        lambda run_dir, now, threshold_s: True)
    monkeypatch.setitem(index_router._startup_runs, index_router.warm_root(),
                        "run-that-died")
    began = time.monotonic()
    index_router.run_startup_warm()
    assert time.monotonic() - began < 5
    assert len(calls) == 2


def test_startup_warm_searches_again_when_the_run_dir_vanishes_mid_wait(
        home, tmp_path, monkeypatch):
    """`prune_runs` reclaims run DIRS; it never touches the store. So a run
    dir that disappears while the warm is waiting on it says nothing about
    whether the index covers the root — reading it as "the scan died, skip the
    warm" silently left the pool cold for an index that was complete."""
    monkeypatch.setenv("HOME", str(tmp_path))
    run_dir = tmp_path / "pruned-run"
    run_dir.mkdir()
    calls, filtered = [], []

    def search(cfg, root, **kw):
        calls.append(root)
        if len(calls) == 1:
            run_dir.rmdir()  # pruned out from under the wait
            return {"covered": False, "root": root}
        return {"covered": True, "root": root, "entries": []}

    monkeypatch.setattr(index_router, "index_search", search)
    monkeypatch.setattr(index_router, "filter_corpus",
                        lambda out, index_root=None: filtered.append(out) or out)
    monkeypatch.setattr(index_router.runner, "_run_dir",
                        lambda cfg, run_id: str(run_dir))
    monkeypatch.setattr(index_router, "WARM_WAIT_POLL_S", 0.01)
    monkeypatch.setitem(index_router._startup_runs, index_router.warm_root(),
                        "run-that-was-pruned")
    index_router.run_startup_warm()
    assert len(calls) == 2
    assert [out.get("covered") for out in filtered] == [True]


def test_startup_warm_does_not_wait_when_the_root_is_covered(home, tmp_path,
                                                             monkeypatch):
    """The overwhelmingly common boot: an index is already there, so the warm
    is the same two calls it always was, with no wait in the way."""
    monkeypatch.setenv("HOME", str(tmp_path))
    waited, filtered = [], []
    monkeypatch.setattr(index_router, "_wait_for_scan",
                        lambda cfg, run_id: waited.append(run_id) or True)
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: {"covered": True, "root": root,
                                                 "entries": []})
    monkeypatch.setattr(index_router, "filter_corpus",
                        lambda out, index_root=None: filtered.append(out) or out)
    monkeypatch.setitem(index_router._startup_runs, index_router.warm_root(),
                        "run-1")
    index_router.run_startup_warm()
    assert waited == []
    assert len(filtered) == 1


def test_startup_warm_does_not_wait_when_the_scan_was_debounced(home, tmp_path,
                                                                monkeypatch):
    """No run was started, so there is nothing to wait for — and a debounced
    root was scanned within SCAN_DEBOUNCE_S, so the index is already there.
    Waiting here would block the warm on a run that never comes."""
    src = _tree(tmp_path)
    monkeypatch.setenv("HOME", str(src))
    monkeypatch.setattr(
        index_router.runner, "start",
        lambda cfg, root, full=False: pytest.fail("debounced root was scanned"))
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    runner._record_scan(cfg, runner.canonical_root(str(src)))
    index_router.run_startup_scan(start_dir=str(tmp_path))

    waited, calls = [], []
    monkeypatch.setattr(index_router, "_wait_for_scan",
                        lambda cfg, run_id: waited.append(run_id) or True)
    monkeypatch.setattr(index_router, "index_search",
                        lambda cfg, root, **kw: calls.append(root)
                        or {"covered": False, "root": root})
    index_router.run_startup_warm()
    assert waited == []
    assert len(calls) == 1


def test_a_root_of_slash_survives_the_config_write(home, tmp_path):
    """Roots are paths, not ignore patterns: clean_patterns rstrips '/' into
    the empty string and silently drops the root."""
    body = _client(tmp_path).post("/api/index/config", json={"roots": ["/"]},
                                  headers={"X-Fused": "1"}).json()
    # canonical_root("/") is "/" on POSIX but the drive root ("D:/") on
    # Windows — still a bare root either way, just not the literal "/".
    assert body["roots"] == [runner.canonical_root("/")]


def test_scanning_reflects_every_run_not_just_the_newest(home, tmp_path):
    """With several roots, a quick second scan can finish (and become the
    newest run) while the first root's is still walking — the status bit
    must keep saying scanning until they all settle."""
    cfg = load_config()
    for rid, events in (("20260101-000000-aa", [{"type": "phase", "msg": "scanning"}]),
                        ("20260102-000000-bb", [{"type": "run_end", "msg": "complete"}])):
        d = os.path.join(cfg.runs_dir, rid)
        os.makedirs(d)
        with open(os.path.join(d, "spec.json"), "w") as f:
            json.dump({"root": "/r"}, f)
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
    body = _client(tmp_path).get("/api/index/status").json()
    assert body["scanning"] is True


def test_a_rules_edit_rescans_every_stale_root_not_just_the_first(home, tmp_path, monkeypatch):
    """The reconciling rescan used to go to roots[0] only; the first root's
    scan then stamped the (global) fingerprint and every other root looked
    reconciled forever — re-included folders stayed permanently missing from
    their slices. Every root whose per-root sig differs gets its own scan."""
    started = []

    def fake_start(cfg, root, full=False):
        started.append(root)
        return {"run_id": f"r{len(started)}", "root": root}

    monkeypatch.setattr(index_router.runner, "start", fake_start)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = load_config()
    cfg.roots = [str(a), str(b)]
    index_router.save_config(cfg)
    index_router.save_applied_ignore(cfg, str(a))
    index_router.save_applied_ignore(cfg, str(b))
    body = _client(tmp_path).post(
        "/api/index/config", json={"ignore": ["node_modules", "target"]},
        headers={"X-Fused": "1"}).json()
    assert body["needs_rescan"] is True
    assert started == [str(a), str(b)]
    assert body["rescan_run_ids"] == ["r1", "r2"]


# -- open-folder freshness -----------------------------------------------------

def test_listing_a_folder_notes_it_for_the_freshness_check(home, tmp_path,
                                                           monkeypatch):
    """The hook is on /api/fs/list because that is what "opened a folder"
    actually is; the check itself must never run on the request thread."""
    seen = []
    monkeypatch.setattr(index_router, "note_folder_opened",
                        lambda p: seen.append(p) or True)
    src = _tree(tmp_path)
    resp = _client(tmp_path).get("/api/fs/list", params={"path": str(src)})
    assert resp.status_code == 200
    assert seen == [str(src)]


@pytest.fixture
def instant_freshness_delay(monkeypatch):
    """FRESHNESS_DELAY_S off the clock. These tests drive `_run_freshness_check`
    synchronously, and the wait is not what they are about — paying it for real
    would add three seconds per call to the suite.

    The CONSTANT is what gets patched, not `time.sleep`: patching the stdlib
    would void every sleep in the process for the duration of the test,
    including store.py's NT-lock poll and this module's warm wait, turning them
    into hot spins."""
    monkeypatch.setattr(index_router, "FRESHNESS_DELAY_S", 0.0)


def _freshness_root(tmp_path):
    """A tree that is a configured scan root, with the check clock cleared."""
    src = _tree(tmp_path)
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    return src


def test_the_freshness_check_defers_before_it_stamps_the_check_clock(
        home, tmp_path, monkeypatch):
    """The check waits out the page's opening burst before it commits.

    /home lists one folder per Claude session card, so a plain refresh lands a
    handful of /api/fs/list inside the first second and the scan one of them may
    start piles onto the paint.

    The ordering against the STAMP is the assertion. `_freshness_due` records the
    check the moment it decides one is due, and stamping and then waiting would
    record a check three seconds before it actually happened — and, worse, would
    make the wait unskippable for checks that are about to refuse. So the wait
    must sit after the free gates and before the stamp. Nothing here waits on a
    real clock; the seam is patched, not the stdlib."""
    events = []
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    # The stamp map, snapshotted as the wait begins. Still empty is what says
    # the wait ran BEFORE _freshness_due stamped.
    monkeypatch.setattr(index_router, "_freshness_wait", lambda s: events.append(
        ("waited", s, dict(index_router._freshness_checked))))
    monkeypatch.setattr(index_router.freshness, "note_folder_opened",
                        lambda cfg, path, roots: events.append(("checked", path)))
    src = _freshness_root(tmp_path)
    index_router._run_freshness_check(str(src))
    assert events == [("waited", index_router.FRESHNESS_DELAY_S, {}),
                      ("checked", str(src))]
    # ...and the stamp did land, on the far side of the wait.
    assert str(src) in index_router._freshness_checked


def test_a_check_that_will_refuse_anyway_never_waits(home, tmp_path,
                                                     monkeypatch):
    """The wait holds the one-at-a-time slot, so only a check that is going to do
    something may pay it.

    Both refusals here are the common case, not the corner: a folder on screen
    re-lists about once a second, so a wait before the interval gate would mean
    the slot is held essentially forever, dropping listings of other roots that
    ARE stale. And on /home, a card sitting under no configured root would burn
    the window doing nothing while every sibling card is turned away — the page
    load would get no check at all, which is the opposite of the point."""
    waits = []
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    monkeypatch.setattr(index_router, "_freshness_wait", waits.append)
    monkeypatch.setattr(index_router.freshness, "note_folder_opened",
                        lambda cfg, path, roots: None)
    src = _freshness_root(tmp_path)
    # Under no configured root at all.
    index_router._run_freshness_check(str(tmp_path.parent))
    assert waits == []
    # Under a root that was checked moments ago.
    index_router._freshness_checked[str(src)] = time.time()
    index_router._run_freshness_check(str(src))
    assert waits == []
    # ...and the wait IS paid once the root is genuinely due, so the two
    # assertions above are about due-ness and not about a wait that never runs.
    index_router._freshness_checked[str(src)] -= index_router.FRESHNESS_CHECK_S + 1
    index_router._run_freshness_check(str(src))
    assert waits == [index_router.FRESHNESS_DELAY_S]


def test_the_freshness_slot_is_freed_after_a_deferred_check(
        home, tmp_path, monkeypatch, instant_freshness_delay):
    """The slot is held across the wait, so it must still be released on every
    way out of it — including the early refusals, which are the common case.
    A leak would block the check for the rest of the process's life, so the next
    refresh a minute later would silently never check anything."""
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    monkeypatch.setattr(index_router.freshness, "note_folder_opened",
                        lambda cfg, path, roots: None)
    src = _freshness_root(tmp_path)
    # The slot is a module global: a failed assertion below must not leave it
    # held, or every later test that touches the real hook fails for an
    # unrelated reason and hides this one.
    for target in (str(tmp_path.parent), str(src)):
        assert index_router._freshness_slot.acquire(blocking=False)
        try:
            # Two ways out: `tmp_path.parent` is under no root and refuses
            # before the gates, `src` runs the check all the way through.
            index_router._run_freshness_check(target)
            assert not index_router._freshness_slot.locked()
        finally:
            if index_router._freshness_slot.locked():
                index_router._freshness_slot.release()


def test_the_freshness_check_runs_at_most_one_at_a_time(
        home, tmp_path, monkeypatch, instant_freshness_delay):
    """A folder being watched re-lists on every mtime tick, so the hook fires
    far more often than a check costs. Overlapping checks would each open
    duckdb over dirs.parquet for nothing."""
    threads = []
    monkeypatch.setattr(index_router.threading, "Thread",
                        lambda **kw: threads.append(kw) or _FakeThread())
    # The REAL function, bound at import: conftest's _no_startup_index_scan
    # replaces the module attribute for every test, so that this hook cannot
    # spawn a home scan from a suite that merely lists a directory.
    assert _real_note_folder_opened(str(tmp_path)) is True
    assert _real_note_folder_opened(str(tmp_path)) is False
    assert len(threads) == 1
    # The slot frees once the check finishes, so the next open is checked again.
    threads[0]["target"](*threads[0]["args"])
    assert _real_note_folder_opened(str(tmp_path)) is True


class _FakeThread:
    def start(self):
        pass


def test_a_stale_open_folder_gets_its_configured_root_rescanned(
        home, tmp_path, monkeypatch, instant_freshness_delay):
    """The glue end to end, synchronously: the persisted config supplies the
    roots and the check fires the ordinary incremental scan of the enclosing
    one."""
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root)
                        or {"run_id": "r1", "root": root})
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    src = _tree(tmp_path)
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    sub = src / "sub"
    # An index that recorded `sub` long before its current mtime.
    _write_dirs_index(load_config(), {str(src): 1, str(sub): 1})
    monkeypatch.setattr(index_router.freshness, "QUIET_S", 0.0)
    index_router._run_freshness_check(str(sub))
    assert started == [str(src)]


def test_a_root_checked_moments_ago_is_not_checked_again(
        home, tmp_path, monkeypatch, instant_freshness_delay):
    """The in-flight lock drops OVERLAPPING checks, not the ones that follow —
    so browsing folder to folder checked (and could rescan) on every open, and
    every scan that completed invalidated every corpus the client had fetched.
    A root is now checked at most once per FRESHNESS_CHECK_S."""
    checked = []
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    monkeypatch.setattr(index_router.freshness, "note_folder_opened",
                        lambda cfg, path, roots: checked.append(path) or None)
    src = _tree(tmp_path)
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    index_router._run_freshness_check(str(src / "sub"))
    # A second folder UNDER THE SAME ROOT is the same question: a scan is per
    # root, so checking again buys nothing.
    index_router._run_freshness_check(str(src))
    assert checked == [str(src / "sub")]
    # ...and it comes back round once the window has passed.
    index_router._freshness_checked[str(src)] -= index_router.FRESHNESS_CHECK_S + 1
    index_router._run_freshness_check(str(src))
    assert checked == [str(src / "sub"), str(src)]


def test_a_folder_outside_every_root_never_reaches_the_index(
        home, tmp_path, monkeypatch, instant_freshness_delay):
    """Cheapest gate first: no enclosing root means no scan is possible, so the
    duckdb lookup behind note_folder_opened must not be paid at all."""
    checked = []
    monkeypatch.setattr(index_router, "_freshness_checked", {})
    monkeypatch.setattr(index_router.freshness, "note_folder_opened",
                        lambda cfg, path, roots: checked.append(path) or None)
    src = _tree(tmp_path)
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    index_router._run_freshness_check(str(tmp_path.parent))
    assert checked == []


# -- guarded SQL ---------------------------------------------------------------

def _indexed_client(tmp_path, dirs=None):
    """A client whose index holds one real dirs row, so SQL has something to
    read."""
    cfg = load_config()
    _write_dirs_index(cfg, dirs or {str(tmp_path): 1})
    return _client(tmp_path)


@pytest.mark.parametrize("path,body", [
    ("/api/index/query", {"sql": "SELECT 1"}),
    ("/api/index/ask", {"prompt": "how many files"}),
])
def test_the_query_routes_require_the_fused_header(home, tmp_path, path, body):
    """Read-only, but they execute a caller-shaped statement — so they are
    POST-only and guarded, unlike GET /search."""
    resp = _client(tmp_path).post(path, json=body)
    assert resp.status_code == 403


def test_query_answers_a_select_over_the_index(home, tmp_path):
    client = _indexed_client(tmp_path)
    body = client.post("/api/index/query",
                       json={"sql": "SELECT count(*) AS n FROM dirs"},
                       headers={"X-Fused": "1"}).json()
    assert body["ok"] is True
    assert body["columns"] == ["n"]
    assert body["rows"] == [[1]]
    assert body["truncated"] is False


def test_query_rejects_a_mutation_with_a_400(home, tmp_path):
    resp = _indexed_client(tmp_path).post(
        "/api/index/query", json={"sql": "DELETE FROM files"},
        headers={"X-Fused": "1"})
    assert resp.status_code == 400
    assert "read-only" in resp.json()["error"]


def test_query_rejects_a_missing_or_non_string_sql(home, tmp_path):
    client = _indexed_client(tmp_path)
    for body in ({}, {"sql": 5}, {"sql": "   "}):
        resp = client.post("/api/index/query", json=body,
                           headers={"X-Fused": "1"})
        assert resp.status_code == 400


def test_query_enforces_the_row_cap_server_side(home, tmp_path):
    """The client's `limit` is a request, not an instruction."""
    client = _indexed_client(tmp_path)
    body = client.post("/api/index/query",
                       json={"sql": "SELECT * FROM range(50) t(i)",
                             "limit": 10 ** 9},
                       headers={"X-Fused": "1"}).json()
    assert len(body["rows"]) == 50  # answered, and under the server cap
    body = client.post("/api/index/query",
                       json={"sql": "SELECT * FROM range(50) t(i)",
                             "limit": 3},
                       headers={"X-Fused": "1"}).json()
    assert len(body["rows"]) == 3
    assert body["truncated"] is True


def test_a_duckdb_runtime_error_is_a_400_not_a_500(home, tmp_path):
    resp = _indexed_client(tmp_path).post(
        "/api/index/query", json={"sql": "SELECT nope FROM dirs"},
        headers={"X-Fused": "1"})
    assert resp.status_code == 400
    assert "nope" in resp.json()["error"]


# -- natural language ----------------------------------------------------------

def _fake_relay(answer, seen=None):
    """Stands in for server.ai._ai_relay: one non-streaming completion."""
    from fastapi.responses import JSONResponse

    async def relay(body):
        if seen is not None:
            seen.append(body)
        return JSONResponse({"ok": True, "result": {"text": answer,
                                                    "model": "m", "usage": {}}})

    return relay


def test_ask_runs_the_sql_the_model_returned_and_echoes_it(home, tmp_path,
                                                           monkeypatch):
    seen = []
    monkeypatch.setattr(index_router._server_ai, "_ai_relay",
                        _fake_relay("```sql\nSELECT count(*) AS n FROM dirs;\n```",
                                    seen))
    body = _indexed_client(tmp_path).post(
        "/api/index/ask", json={"prompt": "how many folders"},
        headers={"X-Fused": "1"}).json()
    assert body["ok"] is True
    assert body["sql"] == "SELECT count(*) AS n FROM dirs;"
    assert body["rows"] == [[1]]
    # The schemas have to reach the model, or it cannot write a valid statement.
    assert "files" in seen[0]["system_prompt"]
    assert "mtime_ns" in seen[0]["system_prompt"]
    assert seen[0]["stream"] is False


def test_ask_does_not_execute_a_mutation_the_model_wrote(home, tmp_path,
                                                        monkeypatch):
    """The guard is the boundary, not the prompt: a model that answers with a
    DELETE is refused by the same gate a user's DELETE hits."""
    monkeypatch.setattr(index_router._server_ai, "_ai_relay",
                        _fake_relay("DELETE FROM files"))
    resp = _indexed_client(tmp_path).post(
        "/api/index/ask", json={"prompt": "delete everything"},
        headers={"X-Fused": "1"})
    assert resp.status_code == 400
    assert "read-only" in resp.json()["error"]
    # The SQL is echoed even when refused, so the user can see what was tried.
    assert resp.json()["sql"] == "DELETE FROM files"


def test_ask_passes_an_ai_failure_through_unchanged(home, tmp_path, monkeypatch):
    from fastapi.responses import JSONResponse

    async def broken(_body):
        return JSONResponse({"ok": False, "error": {"type": "ai_unavailable",
                                                    "message": "no claude"}},
                            status_code=502)

    monkeypatch.setattr(index_router._server_ai, "_ai_relay", broken)
    resp = _indexed_client(tmp_path).post(
        "/api/index/ask", json={"prompt": "anything"},
        headers={"X-Fused": "1"})
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "ai_unavailable"


def test_ask_runs_the_guarded_query_off_the_event_loop(home, tmp_path, monkeypatch):
    """`ask` is an async handler, so anything it calls directly runs ON the
    event loop — and the guarded query is duckdb plus disk, bounded only by
    TIMEOUT_S (10s). Blocking there freezes every other request in the server,
    including the scan-status polling the same panel is doing. `query` next
    door is safe only because it is a plain `def` handler, which FastAPI runs
    in a threadpool; this one has to ask for that explicitly."""
    import asyncio

    seen = {}
    real = index_router.run_guarded

    def spy(*a, **kw):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return real(*a, **kw)

    monkeypatch.setattr(index_router, "run_guarded", spy)
    monkeypatch.setattr(index_router._server_ai, "_ai_relay",
                        _fake_relay("SELECT count(*) AS n FROM dirs"))
    body = _indexed_client(tmp_path).post(
        "/api/index/ask", json={"prompt": "how many folders"},
        headers={"X-Fused": "1"}).json()
    assert body["ok"] is True
    assert seen["on_loop"] is False


def test_ask_rejects_an_empty_prompt(home, tmp_path):
    resp = _indexed_client(tmp_path).post(
        "/api/index/ask", json={"prompt": "  "}, headers={"X-Fused": "1"})
    assert resp.status_code == 400


@pytest.mark.parametrize("answer,expected", [
    ("```sql\nSELECT 1\n```", "SELECT 1"),
    ("```\nSELECT 1\n```", "SELECT 1"),
    ("SELECT 1", "SELECT 1"),
    ("Here you go:\n```sql\nSELECT 1\n```\nHope that helps.", "SELECT 1"),
])
def test_the_models_fencing_is_stripped(answer, expected):
    assert index_router._sql_from_answer(answer) == expected


def _write_dirs_index(cfg, dirs):
    """A minimal real index whose dirs.parquet holds {dir: mtime_ns}."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from fused_render.index.store import Sink, compact
    shards = os.path.join(cfg.dir, "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    for d, mtime_ns in dirs.items():
        sink.add(d, "s", ("sig", [], 0, mtime_ns, 0))
    sink.close()
    compact(cfg, next(iter(dirs)), shards, pa, pq)
