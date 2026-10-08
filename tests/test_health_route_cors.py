"""`/api/health` is readable from the paired loopback origin only: the shell
probes the other loopback name (127.0.0.1 <-> localhost) so the probe has its
own connection pool, and the body carries pid/version."""
from fastapi.testclient import TestClient

from fused_render.server import create_app

BASE = "http://127.0.0.1:8123"


def _get(origin=None):
    client = TestClient(create_app(start_dir="/"), base_url=BASE)
    headers = {"Origin": origin} if origin else {}
    return client.get("/api/health", headers=headers)


def test_paired_loopback_origins_are_echoed():
    for origin in ("http://127.0.0.1:8123", "http://localhost:8123"):
        r = _get(origin)
        assert r.status_code == 200 and "boot_id" in r.json()
        assert r.headers["access-control-allow-origin"] == origin
        assert "Origin" in r.headers["vary"]


def test_other_origins_get_no_cors_header():
    for origin in (
        "http://evil.example",
        "http://localhost:9999",
        "http://127.0.0.1:9999",
        "https://localhost:8123",
        None,
    ):
        r = _get(origin)
        assert r.status_code == 200 and "boot_id" in r.json()
        assert "access-control-allow-origin" not in r.headers
