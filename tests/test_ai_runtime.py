"""Local inference: the registry, the supervisor and /api/ai/runtime (SPEC §40).

**No model is ever downloaded here, and no runner venv is ever built.** The
supervisor is driven against a FAKE WORKER — a stdlib HTTP script that speaks the
same four routes a real one does — with the venv step stubbed to hand back this
interpreter. That is the whole point of the worker being an HTTP contract rather
than an import: the thing under test is the supervision (spawn, wait, evict,
measure, kill), and supervision is testable without mlx, torch, or a network.

The one thing these tests must never do is depend on the machine they run on:
CI is Linux, MLX is Apple-only, and a suite that only passes on a Mac is a suite
that does not run.
"""
import json
import os
import sys
import textwrap
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import jobs
from fused_render.ai import catalog, registry, supervisor
from fused_render.server import create_app

# A worker that loads instantly, answers /health, streams two chunks and quits.
# Deliberately stdlib-only and tiny: it stands in for mlx_text/worker.py's
# CONTRACT, not its behaviour.
FAKE_WORKER = textwrap.dedent('''
    import argparse, http.server, json, os, socketserver, sys, threading, time

    TOKEN = os.environ.get("FUSED_AI_WORKER_TOKEN", "")
    STATE = {"state": "loading", "model": "", "detail": "", "error": "",
             "resident_bytes": None, "loaded_at": None}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def ok(self):
            if TOKEN and self.headers.get("X-Fused-Worker") == TOKEN:
                return True
            self.send_response(403); self.send_header("Content-Length","0"); self.end_headers()
            return False
        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if not self.ok(): return
            self._json(STATE)
        def do_POST(self):
            if not self.ok(): return
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            if self.path.startswith("/quit"):
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.05), os._exit(0)), daemon=True).start()
                return
            if self.path.startswith("/generate"):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for payload in ({"type":"chunk","text":"he"}, {"type":"chunk","text":"llo"},
                                {"type":"done","ok":True,"tokens":2,"seconds":0.1}):
                    line = (json.dumps(payload) + "\\n").encode()
                    self.wfile.write(("%X\\r\\n" % len(line)).encode() + line + b"\\r\\n")
                    self.wfile.flush()
                self.wfile.write(b"0\\r\\n\\r\\n"); self.wfile.flush()
                return
            self._json({"ok": True})

    class S(socketserver.ThreadingTCPServer):
        daemon_threads = True; allow_reuse_address = True

    p = argparse.ArgumentParser()
    p.add_argument("--model"); p.add_argument("--status", default="")
    p.add_argument("--job", default=""); p.add_argument("--download-only", action="store_true")
    a = p.parse_args()
    if a.download_only:
        time.sleep(float(os.environ.get("FAKE_DOWNLOAD_SECONDS", "0")))
        sys.exit(0 if os.environ.get("FAKE_DOWNLOAD_FAILS") != "1" else 1)
    STATE["model"] = a.model
    srv = S(("127.0.0.1", 0), H)
    with open(a.status, "w") as f:
        json.dump({"port": srv.server_address[1], "pid": os.getpid()}, f)
    def ready():
        time.sleep(float(os.environ.get("FAKE_LOAD_SECONDS", "0.1")))
        STATE.update(state="ready", resident_bytes=1234, loaded_at=time.time())
    threading.Thread(target=ready, daemon=True).start()
    srv.serve_forever()
''')


# An image worker: loads instantly, answers /health, and writes a real (tiny)
# PNG where the request tells it to. Stands in for diffusers_image/worker.py's
# CONTRACT — a single JSON reply and a file on disk — not for its pipeline.
FAKE_IMAGE_WORKER = textwrap.dedent('''
    import argparse, http.server, json, os, socketserver, sys, threading, time

    TOKEN = os.environ.get("FUSED_AI_WORKER_TOKEN", "")
    STATE = {"state": "loading", "model": "", "detail": "", "error": "",
             "resident_bytes": None, "loaded_at": None}
    # The 67 bytes of a 1x1 PNG. A real file, so the test can assert the server
    # hands back a path that actually resolves.
    PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d4944415478da63fcffff3f0300050001ff9a9c1c00"
        "00000049454e44ae426082")

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def ok(self):
            if TOKEN and self.headers.get("X-Fused-Worker") == TOKEN:
                return True
            self.send_response(403); self.send_header("Content-Length","0"); self.end_headers()
            return False
        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if not self.ok(): return
            self._json(STATE)
        def do_POST(self):
            if not self.ok(): return
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            body = json.loads(raw or b"{}")
            if self.path.startswith("/quit"):
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.05), os._exit(0)), daemon=True).start()
                return
            if self.path.startswith("/generate"):
                if os.environ.get("FAKE_IMAGE_FAILS") == "1":
                    self._json({"ok": False, "error": "the pipeline exploded"}); return
                out = body["out"]
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "wb") as f: f.write(PNG)
                self._json({"ok": True, "result": {
                    "path": out, "seconds": 0.1, "seed": body.get("seed"),
                    "width": body.get("width"), "height": body.get("height"),
                    "steps": body.get("steps")}})
                return
            self._json({"ok": True})

    class S(socketserver.ThreadingTCPServer):
        daemon_threads = True; allow_reuse_address = True

    p = argparse.ArgumentParser()
    p.add_argument("--model"); p.add_argument("--status", default="")
    p.add_argument("--job", default=""); p.add_argument("--download-only", action="store_true")
    a = p.parse_args()
    if a.download_only:
        sys.exit(0)
    STATE["model"] = a.model
    srv = S(("127.0.0.1", 0), H)
    with open(a.status, "w") as f:
        json.dump({"port": srv.server_address[1], "pid": os.getpid()}, f)
    def ready():
        time.sleep(float(os.environ.get("FAKE_LOAD_SECONDS", "0.1")))
        STATE.update(state="ready", resident_bytes=4321, loaded_at=time.time())
    threading.Thread(target=ready, daemon=True).start()
    srv.serve_forever()
''')


@pytest.fixture()
def fake_runner(tmp_path, monkeypatch):
    """A registry with one runner whose worker is the fake, and whose venv is
    this interpreter — so nothing is downloaded and nothing is built."""
    folder = tmp_path / "fake_runner"
    folder.mkdir()
    (folder / "worker.py").write_text(FAKE_WORKER)
    runner = registry.Runner(
        code="fake-text", capability=registry.TEXT_GENERATION,
        folder=str(folder), label="Fake",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (runner,))
    monkeypatch.setattr(supervisor, "_ensure_venv", lambda r, w, j: sys.executable)
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: "/usr/bin/uv")
    yield runner
    supervisor.unload()
    supervisor.reset()


@pytest.fixture()
def fake_image_runner(tmp_path, monkeypatch):
    """A registry whose ONLY runner serves image generation, with the fake
    worker and this interpreter — so no torch, no weights, no network."""
    folder = tmp_path / "fake_image_runner"
    folder.mkdir()
    (folder / "worker.py").write_text(FAKE_IMAGE_WORKER)
    runner = registry.Runner(
        code="fake-image", capability=registry.IMAGE_GENERATION,
        folder=str(folder), label="Fake image",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (runner,))
    monkeypatch.setattr(supervisor, "_ensure_venv", lambda r, w, j: sys.executable)
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: "/usr/bin/uv")
    yield runner
    supervisor.unload()
    supervisor.reset()


@pytest.fixture(autouse=True)
def _clean_jobs():
    jobs.reset()
    yield
    jobs.reset()


@pytest.fixture()
def client():
    return TestClient(create_app(start_dir="/"))


def _wait_ready(model, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker = supervisor.ready_worker(registry.TEXT_GENERATION, model)
        if worker is not None:
            return worker
        time.sleep(0.05)
    raise AssertionError(f"{model} never became ready: {supervisor.describe()}")


def _wait_downloading(model, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = [r for r in supervisor.describe()["downloading"] if r["model"] == model]
        if rows:
            return rows
        time.sleep(0.05)
    raise AssertionError(f"{model} never reported downloading: {supervisor.describe()}")


def _drain_downloads(timeout=20.0):
    """Let in-flight fetches finish before the test ends.

    Not politeness: the fetch thread reports to the job registry when it lands,
    and the autouse `jobs.reset()` runs the moment the test returns — so a test
    that walked away from a running download would drop a stray row into
    whichever test came next.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and supervisor.describe()["downloading"]:
        time.sleep(0.05)


# -- the registry ---------------------------------------------------------------


def test_a_runner_that_cannot_run_here_says_why(monkeypatch):
    # The reason is the product: "needs Apple Silicon" tells a Windows user what
    # happened, where a bare False sends them to the issue tracker.
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    mlx = registry.by_code("mlx-text")
    status = mlx.available()
    assert status.ok is False
    assert "Apple Silicon" in status.reason and "windows" in status.reason


def test_resolution_skips_a_runner_that_cannot_run(monkeypatch):
    # Picking an unavailable runner and failing at load time would report "the
    # model failed to load" for a machine that was never going to load it.
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    assert registry.for_capability(registry.TEXT_GENERATION) is None
    # …and the same runner resolves on the platform it was built for.
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    resolved = registry.for_capability(registry.TEXT_GENERATION)
    assert resolved is not None and resolved.code == "mlx-text"


def test_every_suggested_model_names_a_capability_with_a_runner():
    # A suggestion for a capability nothing serves is a dead card on the page.
    for capability in catalog.SUGGESTIONS:
        assert any(r.capability == capability for r in registry.all_runners()), capability


# -- the supervisor -------------------------------------------------------------


def test_a_model_loads_and_reports_its_memory(fake_runner):
    supervisor.load("org/small", registry.TEXT_GENERATION)
    worker = _wait_ready("org/small")
    assert worker.resident_bytes == 1234
    described = supervisor.describe()
    assert described["loaded"][0]["model"] == "org/small"
    assert described["totalResidentBytes"] == 1234


def test_loading_a_second_text_model_evicts_the_first(fake_runner):
    # The whole memory policy, in one test: an 8GB model and another 8GB model
    # on a 16GB machine is a swap storm, which reads to the user as a hang.
    supervisor.load("org/first", registry.TEXT_GENERATION)
    first = _wait_ready("org/first")
    supervisor.load("org/second", registry.TEXT_GENERATION)
    _wait_ready("org/second")
    described = supervisor.describe()
    assert [row["model"] for row in described["loaded"]] == ["org/second"]
    # …and the evicted process is really gone, not merely forgotten.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and supervisor._alive(first):
        time.sleep(0.05)
    assert not supervisor._alive(first)


def test_loading_the_same_model_twice_joins_rather_than_restarting(fake_runner):
    first = supervisor.load("org/same", registry.TEXT_GENERATION)
    worker = _wait_ready("org/same")
    pid = worker.pid
    second = supervisor.load("org/same", registry.TEXT_GENERATION)
    assert second["jobId"] == first["jobId"]
    assert supervisor.ready_worker(registry.TEXT_GENERATION).pid == pid


def test_unload_stops_the_process_and_frees_the_table(fake_runner):
    supervisor.load("org/bye", registry.TEXT_GENERATION)
    worker = _wait_ready("org/bye")
    assert supervisor.unload("org/bye") is True
    assert supervisor.describe()["loaded"] == []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and supervisor._alive(worker):
        time.sleep(0.05)
    assert not supervisor._alive(worker)


def test_a_worker_that_dies_is_error_not_ready(fake_runner):
    # The one thing the supervisor knows better than the worker: whether it is
    # alive. A ready row for a dead process is the lie that matters most.
    supervisor.load("org/doomed", registry.TEXT_GENERATION)
    worker = _wait_ready("org/doomed")
    os.kill(worker.pid, 9)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and supervisor._alive(worker):
        time.sleep(0.05)
    assert supervisor.describe()["loaded"] == []


def test_a_load_on_a_machine_without_a_runner_says_why(monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    with pytest.raises(supervisor.SupervisorError, match="Apple Silicon"):
        supervisor.load("org/x", registry.TEXT_GENERATION)


def test_generation_streams_from_the_resident_model(fake_runner):
    supervisor.load("org/chat", registry.TEXT_GENERATION)
    _wait_ready("org/chat")
    events = list(supervisor.generate_text("org/chat", {"messages": []}))
    assert "".join(e.get("text", "") for e in events if e["type"] == "chunk") == "hello"
    assert events[-1] == {"type": "done", "ok": True, "tokens": 2, "seconds": 0.1}


def test_generating_with_an_unloaded_model_starts_the_load(fake_runner):
    # "Not loaded" is never just "no": the call that needed the model is the one
    # that starts it, and the reply carries the job to watch.
    with pytest.raises(supervisor.ModelNotReady) as caught:
        list(supervisor.generate_text("org/cold", {"messages": []}))
    assert caught.value.job_id == supervisor.job_id_for("org/cold")
    _wait_ready("org/cold")  # …and it really is loading, not just claimed to be


# -- the download-manager join --------------------------------------------------


def test_a_load_opens_a_server_owned_job_row(fake_runner):
    started = supervisor.load("org/rowed", registry.TEXT_GENERATION)
    _wait_ready("org/rowed")
    row = next(j for j in jobs.list_jobs() if j["id"] == started["jobId"])
    assert row["owner"] == jobs.OWNER_SERVER
    assert row["title"] == "org/rowed"
    assert row["id"].startswith(jobs.SERVER_ID_PREFIX)


def test_a_page_cannot_post_to_a_server_owned_id(client):
    # The ids are deterministic, so without this a page could post state:"done"
    # for a download still running and the manager would believe it.
    forged = supervisor.job_id_for("org/whatever")
    response = client.post("/api/jobs", json={"id": forged, "title": "mine"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "reserved" in response.json()["error"]


def test_a_worker_can_report_to_its_own_reserved_row(client, fake_runner):
    """The other side of the reserved prefix, and the reason it needs a key.

    The worker is the process doing the downloading, so it is the only thing
    that knows the byte counts — but its row is under `sys:`, which pages may
    not write. Without a way to tell a worker from a page, every progress tick
    from a multi-GB download is silently rejected and the bar never moves.
    """
    supervisor.load("org/reports", registry.TEXT_GENERATION)
    worker = _wait_ready("org/reports")
    row_id = supervisor.job_id_for("org/reports")

    response = client.post(
        "/api/jobs",
        json={"id": row_id, "title": "org/reports", "done": 5, "total": 10},
        headers={"X-Fused": "1", "X-Fused-Worker": worker.token},
    )
    assert response.status_code == 200
    assert response.json()["done"] == 5

    # …and only an EXACT live token opens it. Not a prefix, not any truthy string.
    for bogus in ("", "nope", worker.token[:-1], worker.token + "x"):
        refused = client.post(
            "/api/jobs", json={"id": row_id, "title": "x"},
            headers={"X-Fused": "1", "X-Fused-Worker": bogus})
        assert refused.status_code == 400, bogus


def test_a_stopped_workers_token_stops_working(client, fake_runner):
    # The token is only good while the worker it belongs to is alive.
    supervisor.load("org/expires", registry.TEXT_GENERATION)
    worker = _wait_ready("org/expires")
    token = worker.token
    supervisor.unload("org/expires")
    refused = client.post(
        "/api/jobs", json={"id": supervisor.job_id_for("org/expires"), "title": "x"},
        headers={"X-Fused": "1", "X-Fused-Worker": token})
    assert refused.status_code == 400


def test_a_page_owned_job_still_works(client):
    response = client.post("/api/jobs", json={"id": "my-page-job", "title": "mine"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 200
    assert response.json()["owner"] == jobs.OWNER_PAGE


def test_download_only_never_becomes_resident(fake_runner):
    supervisor.load("org/justfiles", registry.TEXT_GENERATION, weights_only=True)
    deadline = time.monotonic() + 20
    row = None
    while time.monotonic() < deadline:
        row = next((j for j in jobs.list_jobs()
                    if j["id"] == supervisor.job_id_for("org/justfiles")), None)
        if row and row["state"] != "running":
            break
        time.sleep(0.05)
    assert row and row["state"] == "done"
    # A download fills the cache; it does not replace what someone is using.
    assert supervisor.describe()["loaded"] == []
    # …and it is no longer claimed as in flight, which is what lets the page
    # re-walk the cache and draw the checkmark.
    assert supervisor.describe()["downloading"] == []


def test_a_download_in_flight_is_reported_as_downloading(fake_runner, monkeypatch):
    """A weights-only pull holds no memory and evicts nothing — but it IS
    something this machine is doing, and leaving it out of the runtime made it
    invisible: the page polls job rows only while the runtime says something is
    happening, so a Download reported progress nobody read, and the Discover
    card it came from claimed "✓ downloaded" over a pull on its first byte."""
    monkeypatch.setenv("FAKE_DOWNLOAD_SECONDS", "2")
    supervisor.load("org/slowfiles", registry.TEXT_GENERATION, weights_only=True)
    rows = _wait_downloading("org/slowfiles")
    assert rows[0]["jobId"] == supervisor.job_id_for("org/slowfiles")
    # Downloading is NOT residency: nothing is holding memory.
    assert supervisor.describe()["loaded"] == []
    _drain_downloads()


def test_a_second_download_of_the_same_model_joins_the_first(fake_runner, monkeypatch):
    """Two `snapshot_download` runs over one cache directory is not a faster
    download, it is a race for the same `.incomplete` files."""
    monkeypatch.setenv("FAKE_DOWNLOAD_SECONDS", "2")
    started = supervisor.load("org/twice", registry.TEXT_GENERATION, weights_only=True)
    _wait_downloading("org/twice")
    again = supervisor.load("org/twice", registry.TEXT_GENERATION, weights_only=True)
    assert again["jobId"] == started["jobId"]
    assert len(supervisor.describe()["downloading"]) == 1
    _drain_downloads()


def test_two_bring_ups_do_not_share_a_status_file(fake_runner):
    """The port handshake is per BRING-UP, not per capability.

    Two workers for one capability really do overlap — an eviction's
    replacement starts while the old one is still being killed, a Download runs
    beside a Load. When they shared `<capability>.json`, the second one's
    `unlink` deleted the port the first had just published, and the first sat
    out its entire 120-second bootstrap timeout waiting for a file that was
    never coming back.
    """
    one = supervisor.Worker(model="org/a", capability=registry.TEXT_GENERATION,
                            runner_code="fake-text")
    two = supervisor.Worker(model="org/b", capability=registry.TEXT_GENERATION,
                            runner_code="fake-text")
    assert supervisor._status_path(one) != supervisor._status_path(two)
    assert supervisor._log_path(one) != supervisor._log_path(two)


# -- the endpoints --------------------------------------------------------------


def test_a_runner_whose_folder_is_missing_is_not_advertised(tmp_path, monkeypatch):
    """Advertising a capability is a claim; the folder existing is what makes it
    true. A registry that lists a runner it has not built yet hands the user a
    Download button that fails at spawn while the API calls the capability
    ready — which is how the image runner looked between its registration and
    its worker being written."""
    ghost = registry.Runner(
        code="ghost", capability=registry.IMAGE_GENERATION,
        folder=str(tmp_path / "not-written-yet"), label="Ghost",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (ghost,))
    status = ghost.available()
    assert status.ok is False and "not built yet" in status.reason
    assert registry.for_capability(registry.IMAGE_GENERATION) is None


def test_the_worker_stderr_never_goes_to_an_undrained_pipe():
    """A pipe nobody reads holds ~64KB and then BLOCKS the child's next write.

    Workers log while they download (hf and torch both do, at length), and the
    supervisor only reads stderr after exit — so a pipe would wedge the load
    mid-flight while the process still looked alive, and the read that would
    have revealed it never happens. Pinned as source, because reproducing a
    64KB-buffer deadlock in a test is slower than the bug is subtle.
    """
    source = open(supervisor.__file__, encoding="utf-8").read()
    assert "stderr=subprocess.PIPE" not in source
    assert 'stderr=open(log, "w")' in source


def test_the_runtime_endpoint_reports_runners_and_nothing_loaded(client):
    body = client.get("/api/ai/runtime").json()
    assert {r["code"] for r in body["runners"]} == {"mlx-text", "diffusers-image"}
    assert body["loaded"] == []


def test_every_mutating_route_carries_the_guard(client):
    for path in ("/api/ai/runtime/load", "/api/ai/runtime/unload",
                 "/api/ai/runtime/download", "/api/ai/image"):
        assert client.post(path, json={"model": "org/x", "prompt": "x"}).status_code == 403


def test_the_catalog_explains_a_capability_this_machine_cannot_serve(client, monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    rows = {row["capability"]: row for row in client.get("/api/ai/catalog").json()["capabilities"]}
    text = rows[registry.TEXT_GENERATION]
    # Shown, not hidden: hiding it leaves a user wondering where it went.
    assert text["available"] is False and "Apple Silicon" in text["reason"]
    assert text["models"], "the suggestions are still listed"


def test_an_unknown_capability_is_refused(client):
    response = client.post("/api/ai/runtime/load",
                           json={"model": "org/x", "capability": "telepathy"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 400


def test_ai_without_a_model_still_means_claude(client, monkeypatch):
    # The back-compat promise: every page written against fused.ai() keeps its
    # meaning, because only a slash-bearing id is local.
    from fused_render.server import ai as ai_mod

    monkeypatch.setattr(ai_mod, "_claude_bin", lambda: None)
    body = client.post("/api/ai", json={"prompt": "hi"}, headers={"X-Fused": "1"}).json()
    assert body["error"]["type"] == "ai_unavailable"
    assert "claude" in body["error"]["message"].lower()


def test_a_slash_bearing_model_goes_local(client, monkeypatch):
    # …and the same call with a repo id does NOT reach for the CLI at all.
    from fused_render.server import ai as ai_mod

    monkeypatch.setattr(ai_mod, "_claude_bin",
                        lambda: pytest.fail("a local model reached the Claude CLI"))
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    response = client.post("/api/ai", json={"prompt": "hi", "model": "mlx-community/x"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 502
    assert "Apple Silicon" in response.json()["error"]["message"]


# -- image generation (SPEC AI-9) -----------------------------------------------
# The image half of the API. Everything the caller needs comes back from the
# POST — path and seed included — so nothing has to look the result up later,
# and the job row carries only progress.


def _wait_job(job_id, timeout=20.0):
    """The record, once it stops running."""
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = next((j for j in jobs.list_jobs() if j["id"] == job_id), None)
        if row and row["state"] != "running":
            return row
        time.sleep(0.05)
    raise AssertionError(f"{job_id} never finished: {row}")


def test_an_image_renders_to_disk_and_the_job_finishes(client, fake_image_runner):
    response = client.post("/api/ai/image", json={"prompt": "a red square"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 200
    started = response.json()

    row = _wait_job(started["jobId"])
    assert row["state"] == "done", row
    # The path the POST promised is the path that exists — no second lookup.
    assert os.path.isfile(started["path"])
    assert open(started["path"], "rb").read(8) == b"\x89PNG\r\n\x1a\n"


def test_the_reply_carries_the_seed_even_when_none_was_asked_for(client, fake_image_runner):
    """A seed invented inside the worker and never surfaced would make every
    unseeded image unrepeatable — "make that one again" has to be possible."""
    first = client.post("/api/ai/image", json={"prompt": "x"},
                        headers={"X-Fused": "1"}).json()
    assert isinstance(first["seed"], int) and first["seed"] >= 0
    # And an explicit seed is honoured rather than replaced.
    second = client.post("/api/ai/image", json={"prompt": "x", "seed": 1234},
                         headers={"X-Fused": "1"}).json()
    assert second["seed"] == 1234


def test_the_reply_describes_the_render_that_will_actually_happen(client, fake_image_runner):
    """Clamped and snapped, not echoed. A caller that trusts its own request
    would mislabel the picture it gets."""
    body = {"prompt": "x", "width": 99999, "height": 1000, "steps": 5000, "guidance": 99}
    reply = client.post("/api/ai/image", json=body, headers={"X-Fused": "1"}).json()
    assert reply["width"] == 2048          # clamped to the ceiling
    assert reply["height"] == 992          # 1000 snapped down to a multiple of 16
    assert reply["steps"] == 100           # clamped
    assert reply["guidance"] == 20.0       # clamped


def test_two_renders_are_two_rows_and_two_files(client, fake_image_runner):
    """One row per RENDER, not per model: a shared id would have the second
    overwrite the first's progress mid-flight."""
    one = client.post("/api/ai/image", json={"prompt": "a"}, headers={"X-Fused": "1"}).json()
    two = client.post("/api/ai/image", json={"prompt": "b"}, headers={"X-Fused": "1"}).json()
    assert one["jobId"] != two["jobId"]
    assert one["path"] != two["path"]
    _wait_job(one["jobId"])
    _wait_job(two["jobId"])


def test_an_image_row_is_server_owned_and_reserved(client, fake_image_runner):
    started = client.post("/api/ai/image", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    assert started["jobId"].startswith(jobs.SERVER_ID_PREFIX)
    row = _wait_job(started["jobId"])
    assert row["owner"] == jobs.OWNER_SERVER
    # And a page cannot post to it — the same rule that stops a page faking a
    # finished download (BG-4a).
    refused = client.post("/api/jobs", json={"id": started["jobId"], "state": "done"},
                          headers={"X-Fused": "1"})
    assert refused.status_code == 400
    assert "reserved" in refused.json()["error"]


def test_an_image_needs_a_prompt(client, fake_image_runner):
    for body in ({}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 7}):
        response = client.post("/api/ai/image", json=body, headers={"X-Fused": "1"})
        assert response.status_code == 400, body


def test_a_failing_render_reports_the_reason_on_the_row(client, fake_image_runner,
                                                        monkeypatch):
    monkeypatch.setenv("FAKE_IMAGE_FAILS", "1")
    started = client.post("/api/ai/image", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    row = _wait_job(started["jobId"])
    assert row["state"] == "error"
    assert "exploded" in row["message"]


def test_an_image_on_a_machine_with_no_image_runner_says_why(client, monkeypatch):
    """Refused UP FRONT, with the platform's reason — not a job row that opens
    and immediately dies, which gives the caller a bar to watch instead of an
    error to show."""
    ghost = registry.Runner(
        code="ghost", capability=registry.IMAGE_GENERATION,
        folder="/nowhere", label="Ghost",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (ghost,))
    response = client.post("/api/ai/image", json={"prompt": "x"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 409
    assert "not built yet" in response.json()["error"]
    # No row was opened for work that never started.
    assert not [j for j in jobs.list_jobs() if j["id"].startswith(supervisor.IMAGE_JOB_PREFIX)]


def test_an_image_waits_for_its_model_rather_than_failing_fast(client, fake_image_runner,
                                                               monkeypatch):
    """The difference from the text path, and it is deliberate. A chat box must
    not hang for a cold load, so `generate_text` fails fast with the job id. An
    image caller already has a job to watch — rendering is minutes either way —
    so the wait belongs inside it."""
    monkeypatch.setenv("FAKE_LOAD_SECONDS", "1.5")
    assert supervisor.describe()["loaded"] == []
    started = client.post("/api/ai/image", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    row = _wait_job(started["jobId"], timeout=40)
    assert row["state"] == "done", row
    assert os.path.isfile(started["path"])


# -- history and cancel, the two things a chat client needs ---------------------


def test_history_reaches_the_worker_as_prior_turns(client, fake_runner, monkeypatch):
    """A chat is a conversation, and `prompt` alone cannot express one. The
    turns arrive as messages so the model's OWN chat template formats them —
    flattening them into one string is how you get output that looks almost
    right."""
    sent = {}

    def capture(model, request):
        sent["request"] = request
        yield {"type": "chunk", "text": "ok"}
        yield {"type": "done", "ok": True, "tokens": 1}

    monkeypatch.setattr(supervisor, "generate_text", capture)
    response = client.post("/api/ai", json={
        "prompt": "and in French?",
        "model": "org/chat",
        "history": [{"role": "user", "content": "say hello"},
                    {"role": "assistant", "content": "Hello!"}],
    }, headers={"X-Fused": "1"})
    assert response.status_code == 200, response.text
    assert sent["request"]["messages"] == [
        {"role": "user", "content": "say hello"},
        {"role": "assistant", "content": "Hello!"},
        # The prompt is still the thing being asked NOW, and it goes last.
        {"role": "user", "content": "and in French?"},
    ]


@pytest.mark.parametrize("history,expected", [
    ("not a list", "must be a list"),
    ([{"role": "user"}], "content"),
    ([{"role": "system", "content": "x"}], "role"),
    ([{"role": "user", "content": 7}], "content"),
    (["hello"], "must be an object"),
])
def test_a_malformed_history_says_which_turn_is_wrong(client, history, expected):
    response = client.post("/api/ai", json={
        "prompt": "hi", "model": "org/chat", "history": history,
    }, headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert expected in response.json()["error"]["message"]


def test_history_is_refused_for_claude_rather_than_dropped(client):
    """Silently ignoring it would answer a follow-up as if it were the first
    question — which reads as the model having forgotten, not as the API having
    declined."""
    response = client.post("/api/ai", json={
        "prompt": "and in French?",
        "history": [{"role": "user", "content": "say hello"}],
    }, headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "local model" in response.json()["error"]["message"]


def test_cancel_stops_the_generation_without_unloading(client, fake_runner):
    """Not the same as unloading: the weights stay, so the next message starts
    answering immediately."""
    supervisor.load("org/chat", registry.TEXT_GENERATION)
    _wait_ready("org/chat")

    response = client.post("/api/ai/cancel", json={}, headers={"X-Fused": "1"})
    assert response.status_code == 200
    assert response.json() == {"cancelled": True}
    # Still resident — cancel is not unload.
    assert [m["model"] for m in supervisor.describe()["loaded"]] == ["org/chat"]


def test_cancelling_nothing_is_false_not_an_error(client, fake_runner):
    """A Stop pressed just as the last token lands should be a no-op."""
    response = client.post("/api/ai/cancel", json={}, headers={"X-Fused": "1"})
    assert response.status_code == 200
    assert response.json() == {"cancelled": False}


def test_cancel_carries_the_guard_and_checks_the_capability(client):
    assert client.post("/api/ai/cancel", json={}).status_code == 403
    bad = client.post("/api/ai/cancel", json={"capability": "telepathy"},
                      headers={"X-Fused": "1"})
    assert bad.status_code == 400


def test_a_streamed_local_reply_carries_its_result(client, fake_runner, monkeypatch):
    """`fused.ai` resolves with the done frame's `result`, so a local model that
    omitted it handed every streaming page `undefined` — and the page threw on
    the first property it read. The shapes only LOOKED alike because the chunks
    matched.
    """
    def fake(model, request):
        yield {"type": "chunk", "text": "he"}
        yield {"type": "chunk", "text": "llo"}
        yield {"type": "done", "ok": True, "tokens": 2, "seconds": 0.1}

    monkeypatch.setattr(supervisor, "generate_text", fake)
    response = client.post("/api/ai", json={
        "prompt": "hi", "model": "org/chat", "stream": True,
    }, headers={"X-Fused": "1"})
    assert response.status_code == 200
    frames = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    done = frames[-1]
    assert done["type"] == "done" and done["ok"] is True
    # The whole completion, accumulated server-side: a page streaming into a DOM
    # node has no string of its own to fall back on.
    assert done["result"]["text"] == "hello"
    assert done["result"]["model"] == "org/chat"
    assert done["result"]["usage"]["output_tokens"] == 2


def test_both_ai_paths_close_a_stream_the_same_way(client, fake_runner, monkeypatch):
    """One reader parses both, so the frame they finish with is a contract —
    pinned here rather than only described in a docstring, which is what let the
    two drift apart."""
    def fake(model, request):
        yield {"type": "chunk", "text": "x"}
        yield {"type": "done", "ok": True, "tokens": 1, "seconds": 0.1}

    monkeypatch.setattr(supervisor, "generate_text", fake)
    local = client.post("/api/ai", json={"prompt": "hi", "model": "org/chat",
                                         "stream": True},
                        headers={"X-Fused": "1"})
    done = [json.loads(line) for line in local.text.splitlines() if line.strip()][-1]
    # The same keys the Claude path's success frame carries (see _ai_relay's
    # `{"type": "done", "ok": True, "result": payload}`), and the same shape of
    # payload the NON-streaming reply returns.
    assert set(done) == {"type", "ok", "result"}
    assert set(done["result"]) == {"text", "model", "usage"}
