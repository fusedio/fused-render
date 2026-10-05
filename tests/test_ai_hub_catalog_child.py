"""Pool builds run in a REAL child process (no faked subprocess: a canned
`CompletedProcess` cannot catch pipe-inheritance hangs or a worker that never
starts). The Hub is a local HTTP server reached through `HF_ENDPOINT`, so the
child exercises its own httpx stack with no egress.
"""
import http.server
import json
import os
import sys
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from fused_render.ai import hub_catalog
from fused_render.ai import hub_catalog_builder as builder
from fused_render.ai.hub_catalog_config import load_config

CAP = "automatic-speech-recognition"
PAGES = 3
PAGE_ROWS = 5


class _Runner:
    hub_filter_tags = ("gguf",)
    hub_pool_tags = ("gguf",)


class _Hub:
    mode = "ok"
    requests = 0


def _make_handler(hub):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hub.requests += 1
            parts = urlsplit(self.path)
            q = parse_qs(parts.query)
            tag = q["filter"][0]
            page = int(q.get("page", ["0"])[0])
            if hub.mode == "429" and hub.requests >= 2:
                self.send_response(429)
                self.send_header("RateLimit", "limit=1, remaining=0, reset=120")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            rows = [{"id": f"shared/{page}-{i}" if i == 0 else f"{tag}/{page}-{i}",
                     "downloads": 5, "likes": 1,
                     "lastModified": "2026-02-01T00:00:00.000Z",
                     "library_name": "x", "tags": [tag]} for i in range(PAGE_ROWS)]
            body = json.dumps(rows).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if page + 1 < PAGES:
                base = f"http://127.0.0.1:{self.server.server_port}/api/models"
                nxt = f"{base}?filter={tag}&filter=gguf&page={page + 1}"
                self.send_header("Link", f'<{nxt}>; rel="next"')
            self.end_headers()
            self.wfile.write(body)

    return Handler


@pytest.fixture
def hub(tmp_path, monkeypatch):
    state = _Hub()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HF_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(builder, "available_runners", lambda cap: (_Runner(),))
    builder._building.clear()
    yield state
    server.shutdown()
    server.server_close()


def _expected_ids():
    from fused_render.ai import tasks as ai_tasks
    ids = set()
    for tag in ai_tasks.tags_for_capability(CAP):
        for p in range(PAGES):
            for i in range(PAGE_ROWS):
                ids.add(f"shared/{p}-{i}" if i == 0 else f"{tag}/{p}-{i}")
    return ids


def _spy_popen(monkeypatch):
    calls = []
    real = builder.subprocess.Popen

    def spy(cmd, **kw):
        proc = real(cmd, **kw)
        calls.append((cmd, kw, proc.pid))
        return proc

    monkeypatch.setattr(builder.subprocess, "Popen", spy)
    return calls


def test_build_runs_in_a_child_process_and_server_never_holds_rows(hub, monkeypatch):
    calls = _spy_popen(monkeypatch)
    writers = []
    real_init = hub_catalog.PoolWriter.__init__
    monkeypatch.setattr(hub_catalog.PoolWriter, "__init__",
                        lambda self, *a, **k: (writers.append(1), real_init(self, *a, **k))[1])
    cfg = load_config()

    assert builder.ensure_build_started(CAP, cfg=cfg) is True
    thread = builder._building[CAP]
    thread.join(timeout=60)
    assert not thread.is_alive()

    assert writers == []  # the server process itself wrote no pool
    (cmd, kw, pid), = calls
    assert pid != os.getpid()
    assert cmd[0] == sys.executable and os.path.isabs(cmd[0])
    assert cmd[1:3] == ["-m", builder.WORKER_MODULE]
    assert kw.get("close_fds") is False if os.name != "nt" else True
    assert "cwd" not in kw
    assert all(len(a) < 1000 for a in cmd)  # small args only, never rows/token
    assert hub_catalog.pool_exists(cfg, CAP)
    rows = hub_catalog.query_pool(cfg, CAP)
    assert {r["id"] for r in rows} == _expected_ids()
    assert len(rows) == len(_expected_ids())  # deduped across tags
    entry = hub_catalog.pool_entry(cfg, CAP)
    assert entry["rows"] == len(rows) and entry["formats"] == ["gguf"]
    assert entry["pages"] == hub.requests
    assert entry["generation"] == 1


def test_build_in_child_reports_429_and_leaves_no_pool(hub):
    hub.mode = "429"
    cfg = load_config()
    result = builder.build_capability_pool_in_child(cfg, CAP)
    assert result["rateLimited"] is True
    assert not hub_catalog.pool_exists(cfg, CAP)
    assert hub_catalog.is_blocked(cfg, CAP)
    assert not [n for n in os.listdir(cfg.pools_dir) if n.endswith((".tmp", ".parquet"))]


def test_delta_in_child_widens_existing_pool(hub, monkeypatch):
    cfg = load_config()
    builder.build_capability_pool_in_child(cfg, CAP)
    before = hub_catalog.pool_entry(cfg, CAP)
    calls = _spy_popen(monkeypatch)
    result = builder.refresh_capability_pool_delta(cfg, CAP)
    assert "changedIds" not in result
    assert result["rows"] == before["rows"]  # nothing newer than the watermark
    assert len(calls) == 1
    assert hub_catalog.pool_entry(cfg, CAP)["generation"] == 2
    assert {r["id"] for r in hub_catalog.query_pool(cfg, CAP)} == _expected_ids()


def test_delta_with_no_pool_does_not_spawn(hub, monkeypatch):
    calls = _spy_popen(monkeypatch)
    assert builder.refresh_capability_pool_delta(load_config(), CAP) == {"skipped": "no-pool"}
    assert calls == []


def test_child_crash_is_logged_and_leaves_manifest_and_pool_untouched(
        hub, tmp_path, monkeypatch, caplog):
    cfg = load_config()
    builder.build_capability_pool_in_child(cfg, CAP)
    before_entry = hub_catalog.pool_entry(cfg, CAP)
    before_rows = hub_catalog.query_pool(cfg, CAP)

    crash_dir = tmp_path / "crashmods"
    crash_dir.mkdir()
    (crash_dir / "crash_worker.py").write_text(
        "import os, signal, sys\n"
        "from fused_render.ai.hub_catalog import PoolWriter\n"
        "from fused_render.ai.hub_catalog_config import HubCatalogConfig\n"
        "w = PoolWriter(HubCatalogConfig(dir=sys.argv[3]), sys.argv[2])\n"
        "w.append([{'capability': 'c', 'format': '', 'raw': {'id': 'a/b'}}])\n"
        "print('about to die', flush=True)\n"
        "os.kill(os.getpid(), signal.SIGKILL if os.name != 'nt' else signal.SIGTERM)\n")
    monkeypatch.setenv("PYTHONPATH", str(crash_dir) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setattr(builder, "WORKER_MODULE", "crash_worker")

    with caplog.at_level("ERROR", logger="fused_render.ai.hub_catalog_builder"):
        with pytest.raises(RuntimeError):
            builder.build_capability_pool_in_child(cfg, CAP)
    assert "about to die" in caplog.text

    assert hub_catalog.pool_entry(cfg, CAP) == before_entry
    assert hub_catalog.query_pool(cfg, CAP) == before_rows
    parquets = [n for n in os.listdir(cfg.pools_dir) if n.endswith(".parquet")]
    assert parquets == [before_entry["file"]]


def test_ensure_build_started_logs_child_failure_and_clears_in_flight(
        hub, monkeypatch, caplog):
    monkeypatch.setattr(builder, "WORKER_MODULE", "no_such_worker_module_xyz")
    cfg = load_config()
    with caplog.at_level("ERROR", logger="fused_render.ai.hub_catalog_builder"):
        assert builder.ensure_build_started(CAP, cfg=cfg) is True
        builder._building[CAP].join(timeout=60)
    assert "hub-catalog build failed" in caplog.text
    assert not hub_catalog.pool_exists(cfg, CAP)
    assert hub_catalog.pool_entry(cfg, CAP) is None
    assert builder.build_status(CAP, cfg=cfg)["state"] == "none"
