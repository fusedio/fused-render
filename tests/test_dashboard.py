"""health.py's resource-trail reader and GET /api/system/dashboard: sections are
isolated, the verdict rides along, and the activity payload carries a compact
verdict."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import health
from fused_render.server import create_app, dashboard


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_trail_reader_returns_recent_rows_and_caches(tmp_path, monkeypatch):
    p = tmp_path / "resources.jsonl"
    now = time.time()
    _write(p, [{"t": now - 7200, "load": 1.0}, {"t": now - 60, "load": 2.0}, {"t": now - 5, "load": 3.0}])
    monkeypatch.setattr(health, "resources_path", lambda: str(p))
    health._trail_cache.clear()
    rows = health.read_trail(3600)
    assert [r["load"] for r in rows] == [2.0, 3.0]
    _write(p, [{"t": now, "load": 9.0}])
    assert [r["load"] for r in health.read_trail(3600)] == [2.0, 3.0]   # cached 30 s
    health._trail_cache.clear()
    assert [r["load"] for r in health.read_trail(3600)] == [9.0]


def test_trail_reader_skips_garbage_and_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "resources_path", lambda: str(tmp_path / "none.jsonl"))
    health._trail_cache.clear()
    assert health.read_trail(60) == []
    p = tmp_path / "r.jsonl"
    p.write_text('not json\n{"t": %f, "load": 1}\n' % time.time())
    monkeypatch.setattr(health, "resources_path", lambda: str(p))
    health._trail_cache.clear()
    assert len(health.read_trail(60)) == 1


def test_swap_growth_over_window():
    now = 1000.0
    rows = [{"t": now - 700, "swap_used": 100}, {"t": now - 500, "swap_used": 200},
            {"t": now - 5, "swap_used": 900}]
    assert dashboard.swap_growth(rows, now, 600) == 700
    assert dashboard.swap_growth([], now, 600) is None
    assert dashboard.swap_growth([{"t": now, "swap_used": None}], now, 600) is None


def test_sections_are_isolated(monkeypatch):
    def boom():
        raise RuntimeError("nope")
    monkeypatch.setitem(dashboard.SECTIONS, "disk", boom)
    out = dashboard.build()
    assert out["disk"] == {"error": "RuntimeError: nope"}
    for key in ("net", "server", "memory", "cpu", "claude", "index", "jobs", "errors"):
        assert key in out
    assert out["verdict"]["level"] in ("ok", "warn", "bad")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_dashboard_endpoint_and_watch(client):
    from fused_render import netlog
    r = client.get("/api/system/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert {"net", "server", "verdict", "claude", "index"} <= set(body)
    assert netlog.get().watched()


def test_activity_payload_carries_compact_verdict(client):
    body = client.get("/api/system/activity").json()
    v = body["verdict"]
    assert set(v) == {"level", "headline"}
