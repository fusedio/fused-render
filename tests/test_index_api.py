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
    assert state["root"] == str(src)

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


def test_status_with_no_runs_at_all_is_a_quiet_idle(home, tmp_path):
    body = _client(tmp_path).get("/api/index/status").json()
    assert body == {"ok": True, "running": False, "run_id": None, "root": None,
                    "phase": "", "dirs": 0, "files": 0, "reused": 0,
                    "current": "", "summary": None, "cancelled": False,
                    "error": None, "indexed": False, "updated": None}


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
    assert resp.json()["roots"] == [str(tmp_path)]
    assert resp.json()["ignore"] == ["node_modules"]
    body = client.get("/api/index/config").json()
    assert body["roots"] == [str(tmp_path)]
    assert body["defaults"]  # the starting list is reported for a Reset button


def test_config_rejects_a_non_list(home, tmp_path):
    resp = _client(tmp_path).post("/api/index/config", json={"roots": "nope"},
                                  headers={"X-Fused": "1"})
    assert resp.status_code == 400


def test_default_scan_roots_are_the_users_home(home, tmp_path, monkeypatch):
    """Home, not the project root: a whole-home scan costs seconds with the
    default ignore rules and is what makes search useful everywhere."""
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "userhome")
                        if p == "~" else os.path.expanduser(p))
    assert index_router.scan_roots(load_config(), start_dir=str(tmp_path)) == [
        str(tmp_path / "userhome")]


def test_configured_roots_win_over_the_default(home, tmp_path):
    cfg = load_config()
    cfg.roots = [str(tmp_path / "proj")]
    assert index_router.scan_roots(cfg, start_dir=str(tmp_path)) == [
        str(tmp_path / "proj")]


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
    assert started == [str(src)]


def test_startup_scan_is_debounced(home, tmp_path, monkeypatch):
    src = _tree(tmp_path)
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    cfg = load_config()
    cfg.roots = [str(src)]
    index_router.save_config(cfg)
    runner._record_scan(cfg, str(src))  # a scan just ran
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
    assert started == [str(src)]


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
    started = []
    monkeypatch.setattr(index_router.runner, "start",
                        lambda cfg, root, full=False: started.append(root))
    cfg = load_config()
    cfg.roots = [str(tmp_path / "deleted")]
    index_router.save_config(cfg)
    index_router.run_startup_scan(start_dir=str(tmp_path))
    assert started == []
