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
import re
import sys
import textwrap
import threading
import time
import types

import pytest
from fastapi.testclient import TestClient

from fused_render import jobs
from fused_render.ai import catalog, fit, footprints, registry, speed, supervisor
from fused_render.ai import tasks as ai_tasks
from fused_render.ai.runners import formats, partial
from fused_render.server import create_app
from fused_render.ai import hub_cache as ai_models
from fused_render.ai.hub_cache import CachedModel
from fused_render.server.routers import ai_runtime
# `no_egress` is imported for its SIDE EFFECT: it is an autouse fixture, so
# binding the name in this module installs it for every test here, including
# the model-mirror tests below. See its docstring — Windows CI proved that
# "every test stubs the Hub" is not the same claim as "no test reaches the
# network".
from test_ai_hub_fetch import no_egress  # noqa: F401
from _big_files import sparse_file

# os.geteuid is POSIX-only; a bare call below would crash collection of this
# whole module on Windows, before any skipif could act on it.
_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

#: The real `_ensure_venv`, captured at import — before any fixture replaces it.
#: The runner fixtures stub it (nothing is ever built in these tests), so a test
#: about what it DOES has no other way back to it.
_REAL_ENSURE_VENV = supervisor._ensure_venv

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
        STATE.update(state="ready", resident_bytes=1234, peak_resident_bytes=9999, loaded_at=time.time())
    threading.Thread(target=ready, daemon=True).start()
    srv.serve_forever()
''')


# An image worker: loads instantly, answers /health, and writes a real (tiny)
# PNG where the request tells it to. Stands in for runners/torch_image.py's
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


FAKE_VIDEO_WORKER = textwrap.dedent('''
    import argparse, http.server, json, os, socketserver, sys, threading, time

    TOKEN = os.environ.get("FUSED_AI_WORKER_TOKEN", "")
    STATE = {"state": "loading", "model": "", "detail": "", "error": "",
             "resident_bytes": None, "loaded_at": None}
    # A tiny placeholder mp4 -- not a real container, just enough bytes for the
    # test to assert the server hands back a path that actually resolves.
    MP4 = b"\\x00\\x00\\x00\\x18ftypmp42fake video bytes"

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
                if os.environ.get("FAKE_VIDEO_FAILS") == "1":
                    self._json({"ok": False, "error": "the renderer exited nonzero"}); return
                out = body["out"]
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "wb") as f: f.write(MP4)
                self._json({"ok": True, "result": {
                    "path": out, "seconds": 0.1, "seed": body.get("seed"),
                    "width": body.get("width"), "height": body.get("height"),
                    "frames": body.get("frames"), "steps": body.get("steps")}})
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
        STATE.update(state="ready", resident_bytes=6543, loaded_at=time.time())
    threading.Thread(target=ready, daemon=True).start()
    srv.serve_forever()
''')


# A transcription worker: loads instantly, answers /health, and writes the two
# transcript files where the request tells it to. Stands in for
# faster_whisper/worker.py's CONTRACT — a single JSON reply and files on disk —
# not for Whisper.
FAKE_TRANSCRIBE_WORKER = textwrap.dedent('''
    import argparse, http.server, json, os, socketserver, sys, threading, time

    TOKEN = os.environ.get("FUSED_AI_WORKER_TOKEN", "")
    STATE = {"state": "loading", "model": "", "detail": "", "error": "",
             "resident_bytes": None, "loaded_at": None}
    SEGMENTS = [{"start": 0.0, "end": 1.5, "text": "hello"},
                {"start": 1.5, "end": 3.0, "text": "world"}]

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
                if os.environ.get("FAKE_TRANSCRIBE_FAILS") == "1":
                    self._json({"ok": False, "error": "the decoder exploded"}); return
                out, out_text = body["out"], body["outText"]
                os.makedirs(os.path.dirname(out), exist_ok=True)
                text = " ".join(s["text"] for s in SEGMENTS)
                result = {"path": body["path"], "output": out, "outputText": out_text,
                          "task": body.get("task"), "language": "en", "duration": 3.0,
                          "seconds": 0.1}
                with open(out, "w") as f:
                    json.dump({**result, "segments": SEGMENTS, "text": text}, f)
                with open(out_text, "w") as f: f.write(text + "\\n")
                self._json({"ok": True, "result": {**result, "segments": len(SEGMENTS)}})
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
        STATE.update(state="ready", resident_bytes=2222, loaded_at=time.time())
    threading.Thread(target=ready, daemon=True).start()
    srv.serve_forever()
''')


@pytest.fixture()
def fake_runner(tmp_path, monkeypatch):
    """A registry with one runner whose worker is the fake, and whose venv is
    this interpreter — so nothing is downloaded and nothing is built."""
    folder = tmp_path / "fake_runner"
    folder.mkdir()
    (folder / "worker.py").write_text(FAKE_WORKER, encoding="utf-8")
    runner = registry.Runner(
        code="fake-text", capability=registry.TEXT_GENERATION,
        folder=str(folder), label="Fake",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (runner,))
    monkeypatch.setattr(supervisor, "_ensure_venv", lambda r, w, j: sys.executable)
    # The prerequisites (`uv`, the `fused` package) are a fact about the
    # MACHINE, and these tests are about the supervisor. Stubbed rather than
    # faked piecemeal so the suite passes identically on a host that has
    # neither — the check itself is tested on its own, below.
    monkeypatch.setattr(supervisor, "_require_build_tools", lambda: None)
    yield runner
    supervisor.unload()
    supervisor.reset()


@pytest.fixture()
def fake_image_runner(tmp_path, monkeypatch):
    """A registry whose ONLY runner serves image generation, with the fake
    worker and this interpreter — so no torch, no weights, no network."""
    folder = tmp_path / "fake_image_runner"
    folder.mkdir()
    (folder / "worker.py").write_text(FAKE_IMAGE_WORKER, encoding="utf-8")
    runner = registry.Runner(
        code="fake-image", capability=registry.IMAGE_GENERATION,
        folder=str(folder), label="Fake image",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (runner,))
    # A shortlist for the fake runner, because since D293 the catalog is keyed
    # by RUNNER rather than by capability — so "the default image model" is
    # whatever the runner that will actually load it suggests, and a runner with
    # no list has no default. That is the right production behaviour (a default
    # the resolving backend cannot load is worse than none), and it means a
    # fixture that swaps the registry has to swap the curation with it.
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-image", [
        {"id": "org/fake-image", "label": "Fake image", "size_gb": None, "note": ""},
    ])
    monkeypatch.setattr(supervisor, "_ensure_venv", lambda r, w, j: sys.executable)
    # The prerequisites (`uv`, the `fused` package) are a fact about the
    # MACHINE, and these tests are about the supervisor. Stubbed rather than
    # faked piecemeal so the suite passes identically on a host that has
    # neither — the check itself is tested on its own, below.
    monkeypatch.setattr(supervisor, "_require_build_tools", lambda: None)
    yield runner
    supervisor.unload()
    supervisor.reset()


@pytest.fixture()
def fake_video_runner(tmp_path, monkeypatch):
    """A registry whose ONLY runner serves video generation, with the fake
    worker and this interpreter — so no weights and no ffmpeg.

    Deliberately does NOT gate on Apple Silicon — the fake runner's own
    `_available` always says yes, which is what lets these tests exercise the
    ROUTE's behaviour (clamping, job shape, error surfacing) on any machine
    running the suite. The platform gate itself is `test_ai_registry.py`'s
    job, tested directly against `_apple_silicon` rather than through this
    fixture.
    """
    folder = tmp_path / "fake_video_runner"
    folder.mkdir()
    (folder / "worker.py").write_text(FAKE_VIDEO_WORKER, encoding="utf-8")
    runner = registry.Runner(
        code="fake-video", capability=registry.VIDEO_GENERATION,
        folder=str(folder), label="Fake video",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (runner,))
    # See `fake_image_runner`: the catalog is keyed by runner since D293, so
    # the fake backend brings its own default.
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-video", [
        {"id": "org/fake-video", "label": "Fake video", "size_gb": None, "note": ""},
    ])
    monkeypatch.setattr(supervisor, "_ensure_venv", lambda r, w, j: sys.executable)
    monkeypatch.setattr(supervisor, "_require_build_tools", lambda: None)
    yield runner
    supervisor.unload()
    supervisor.reset()


def _only_video_runner(tmp_path, monkeypatch, code):
    """A registry whose ONLY runner serves video generation, registered
    under `code` — the mirror `fake_video_runner` needs to exercise
    `registry.video_traits_for` per engine (Task 5): that fixture's own
    `code="fake-video"` is deliberately NOT one of `registry.VIDEO_TRAITS`'
    keys, so it always exercises the FALLBACK rather than letting a test pick
    which row it wants. This one takes the code as a
    parameter for exactly that reason — see `_only_transcribe_runner` above,
    which the same argument already justifies for that capability.
    """
    folder = tmp_path / ("fake_runner_" + code.replace("-", "_"))
    folder.mkdir()
    (folder / "worker.py").write_text(FAKE_VIDEO_WORKER, encoding="utf-8")
    runner = registry.Runner(
        code=code, capability=registry.VIDEO_GENERATION,
        folder=str(folder), label=f"Fake {code}",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (runner,))
    monkeypatch.setitem(catalog.SUGGESTIONS, code, [
        {"id": f"org/{code}", "label": f"Fake {code}", "size_gb": None, "note": ""},
    ])
    monkeypatch.setattr(supervisor, "_ensure_venv", lambda r, w, j: sys.executable)
    monkeypatch.setattr(supervisor, "_require_build_tools", lambda: None)
    yield runner
    supervisor.unload()
    supervisor.reset()


@pytest.fixture()
def fake_ltx_video_runner(tmp_path, monkeypatch):
    """Registered under the REAL `ltx-video` code, so the route resolves
    `registry.VIDEO_TRAITS["ltx-video"]` rather than the fallback."""
    yield from _only_video_runner(tmp_path, monkeypatch, "ltx-video")


def _only_transcribe_runner(tmp_path, monkeypatch, code):
    """A registry whose ONLY runner transcribes, under `code`, with the fake
    worker and this interpreter — so no CTranslate2, no weights, no audio.

    The code is a parameter because it is not decoration: the endpoint asks
    `runners/engine_options.py` what the RESOLVED runner cannot do, so a test
    about that answer has to be able to say which runner resolved.
    """
    folder = tmp_path / ("fake_runner_" + code.replace("-", "_"))
    folder.mkdir()
    (folder / "worker.py").write_text(FAKE_TRANSCRIBE_WORKER, encoding="utf-8")
    runner = registry.Runner(
        code=code, capability=registry.SPEECH_TO_TEXT,
        folder=str(folder), label="Fake whisper",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (runner,))
    # See `fake_image_runner`: the catalog is keyed by runner since D293, so the
    # fake backend brings its own default.
    monkeypatch.setitem(catalog.SUGGESTIONS, code, [
        {"id": "org/fake-whisper", "label": "Fake whisper", "size_gb": None, "note": ""},
    ])
    monkeypatch.setattr(supervisor, "_ensure_venv", lambda r, w, j: sys.executable)
    monkeypatch.setattr(supervisor, "_require_build_tools", lambda: None)
    return runner


@pytest.fixture()
def fake_transcribe_runner(tmp_path, monkeypatch):
    yield _only_transcribe_runner(tmp_path, monkeypatch, "fake-whisper")
    supervisor.unload()
    supervisor.reset()


@pytest.fixture()
def fake_refusing_runner(tmp_path, monkeypatch):
    """The same fake worker, resolving under a code this fixture gives a
    temporary `engine_options.UNSUPPORTED` entry — which is what makes the
    endpoint's per-engine refusals reachable from a test.

    Until D406, this used the real `parakeet-mlx` code and its real table
    entry; that engine was withdrawn and no registered TRANSCRIBE runner
    refuses anything today (the table itself is no longer empty overall —
    D432 gave the diffusers image engines a real `image` row — just still
    empty on this capability), so the refusal PATH is exercised here with a
    fake entry instead of a real one — the mechanism, not any particular
    engine, is what this file pins."""
    from fused_render.ai.runners import engine_options
    code = "fake-refusing-engine"
    monkeypatch.setitem(engine_options.UNSUPPORTED, code, {
        "task": (
            "the fake engine only transcribes — it has no translate task. "
            "Ask for task: 'transcribe'."),
        "language": (
            "the fake engine has no 'language' option — it detects the "
            "language itself."),
        "initialPrompt": (
            "the fake engine has no 'initialPrompt' — it has no text to "
            "condition on."),
    })
    yield _only_transcribe_runner(tmp_path, monkeypatch, code)
    supervisor.unload()
    supervisor.reset()


@pytest.fixture()
def recording(tmp_path):
    """A file to point the route at. Its BYTES are never read — the fake worker
    stands in for the decoder — but its existence is what the route checks."""
    path = tmp_path / "meeting.m4a"
    path.write_bytes(b"not really audio")
    return str(path)


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
    #
    # Linux used to prove this by resolving to NOTHING, which stopped being the
    # observation the day a cross-platform text runner landed below MLX (D293):
    # a None then meant "nobody serves this" rather than "the unavailable one
    # was skipped". Skipping is now visible as a HANDOVER, which is the stronger
    # statement of the same rule — and the ordering is what carries it, so the
    # test names both sides.
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    resolved = registry.for_capability(registry.TEXT_GENERATION)
    assert resolved is not None and resolved.code == "llamacpp-text"
    # …and the runner that was skipped is still registered ahead of it, which is
    # what makes this a skip rather than an absence.
    assert registry.all_runners()[0].code == "mlx-text"
    assert registry.by_code("mlx-text").available().ok is False

    # The same capability resolves to MLX on the platform it was built for.
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    resolved = registry.for_capability(registry.TEXT_GENERATION)
    assert resolved is not None and resolved.code == "mlx-text"


def test_speech_to_text_prefers_MLX_on_a_mac_and_CTranslate2_everywhere_else(
        monkeypatch):
    """The ordering the table was built for, finally used by a second capability.

    Apple Silicon transcribed on its CPU cores until D302 because CTranslate2
    has no Metal backend. The MLX row sits ABOVE the CT2 one, so a Mac takes it
    and no other platform loses anything — which is the property that had to
    hold before this could ship, since speech to text was deliberately the first
    capability that worked everywhere (AI-10).
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    resolved = registry.for_capability(registry.SPEECH_TO_TEXT)
    assert resolved is not None and resolved.code == "mlx-whisper"

    for system, machine in (("Windows", "AMD64"), ("Linux", "x86_64"),
                            ("Darwin", "x86_64")):
        monkeypatch.setattr(registry.platform, "system", lambda s=system: s)
        monkeypatch.setattr(registry.platform, "machine", lambda m=machine: m)
        resolved = registry.for_capability(registry.SPEECH_TO_TEXT)
        assert resolved is not None and resolved.code == "faster-whisper", (
            f"{system}/{machine} lost speech to text")


def test_image_generation_takes_MFLUX_on_apple_silicon_and_diffusers_elsewhere(
        monkeypatch):
    """Image generation is arranged like the other two: MLX takes the Macs (D310).

    One 4.6GB repo instead of the ~10.1GB two-repo split, ~8x quicker to load
    and ~15-20% quicker per image. The memory ceiling behind the old inversion
    is a known accepted risk, not a resolved one — a ~23.6GB allocator pool on a
    34GB machine, and nothing run on a 16GB Mac — and the way back is the engine
    preference, which is why the Diffusers half of this test matters as much as
    the MLX half.

    Pinned as a test because the ordering is invisible in a diff of the table:
    it decides what every Mac's image generation does, and nothing else fails
    when a row moves.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")

    resolved = registry.for_capability(registry.IMAGE_GENERATION)
    assert resolved is not None and resolved.code == "mflux-image"
    # …and Diffusers is still AVAILABLE on the same machine, which is what makes
    # the preference below a way BACK rather than a setting with nowhere to go.
    assert registry.by_code("diffusers-image").available().ok is True

    _prefer(monkeypatch, registry.IMAGE_GENERATION, "diffusers-image")
    resolution = registry.resolve(registry.IMAGE_GENERATION)
    assert resolution.runner.code == "diffusers-image" and resolution.honoured
    # Switching also moves the suggestion list, since a repo belongs to a
    # backend: the MLX conversion is unloadable by diffusers and vice versa.
    # Smallest first: the tiny-sd row is 0.6GB, then the SDNQ repo at 5.5GB
    # (which is still the recommended one), then the int8 split's 8.2GB.
    assert [m["id"] for m in catalog.for_capability(registry.IMAGE_GENERATION)] == [
        "segmind/tiny-sd",
        "Disty0/FLUX.2-klein-4B-SDNQ-4bit-dynamic",
        "tonera/FLUX.2-klein-4B-int8-diffusers"]

    # Windows and Linux never see the MLX row at all, preference or none.
    for system, machine in (("Windows", "AMD64"), ("Linux", "x86_64")):
        monkeypatch.setattr(registry.platform, "system", lambda s=system: s)
        monkeypatch.setattr(registry.platform, "machine", lambda m=machine: m)
        assert registry.for_capability(
            registry.IMAGE_GENERATION).code == "diffusers-image"


def test_the_mflux_preference_is_dropped_off_apple_silicon(monkeypatch):
    """The same synced-prefs.json rule as the whisper runners: an image
    preference set on a Mac must not take image generation away on a PC."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    _prefer(monkeypatch, registry.IMAGE_GENERATION, "mflux-image")

    resolution = registry.resolve(registry.IMAGE_GENERATION)
    assert resolution.runner.code == "diffusers-image"
    assert "Apple Silicon" in resolution.ignored_reason


# -- the engine preference (D302) ------------------------------------------------
#
# `prefs.engine_for_capability` is patched rather than a prefs.json written,
# because what is under test here is the RESOLUTION — which preference wins,
# and what happens to one that cannot. The file half is
# `tests/test_shell_prefs.py`'s, driven through the endpoint.


def _prefer(monkeypatch, capability, code):
    monkeypatch.setattr(
        registry, "preferred_code",
        lambda asked, cap=capability, chosen=code: chosen if asked == cap
        else registry.AUTO)


def test_an_engine_preference_overrides_the_registry_order(monkeypatch):
    """The whole point of the feature: a Mac that would resolve to MLX can be
    told to use CTranslate2 instead — for a language it handles better, or to
    compare the two — and that choice is what LOADS, not just what is stored."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    _prefer(monkeypatch, registry.SPEECH_TO_TEXT, "faster-whisper")

    resolution = registry.resolve(registry.SPEECH_TO_TEXT)
    assert resolution.runner.code == "faster-whisper"
    assert resolution.honoured and resolution.ignored_reason == ""
    # And it is the same answer every consumer gets — the supervisor, the
    # catalog and the API all go through this one call (D293's fix, which a
    # second copy of the preference logic would undo).
    assert registry.for_capability(registry.SPEECH_TO_TEXT).code == "faster-whisper"
    assert catalog._runner_for(registry.SPEECH_TO_TEXT).code == "faster-whisper"


def test_a_preference_for_a_runner_that_cannot_run_HERE_is_ignored(monkeypatch):
    """The rule that makes this safe to store at all.

    prefs.json travels: it is a plain file in a home directory people sync,
    copy between machines and restore from backups. A preference for MLX
    Whisper set on a Mac and honoured on a Windows box would take speech to
    text away entirely — a capability silently gone is a bug report, while a
    preference that quietly does nothing is recoverable. So the ordering
    decides instead, and the REASON comes back so the page can say so.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    _prefer(monkeypatch, registry.SPEECH_TO_TEXT, "mlx-whisper")

    resolution = registry.resolve(registry.SPEECH_TO_TEXT)
    assert resolution.runner.code == "faster-whisper", "the capability survived"
    assert resolution.requested == "mlx-whisper", "the choice is not rewritten"
    assert not resolution.honoured
    # The registry's own words, not a second copy written for the page.
    assert "Apple Silicon" in resolution.ignored_reason
    assert "windows" in resolution.ignored_reason


def test_a_preference_naming_something_that_is_not_a_runner_is_ignored(monkeypatch):
    """A prefs.json written by a NEWER build and opened by an older one, or
    hand-edited. Not an assert: an unreadable preference must cost the
    preference, never the capability."""
    _prefer(monkeypatch, registry.SPEECH_TO_TEXT, "whisper-9000")
    resolution = registry.resolve(registry.SPEECH_TO_TEXT)
    assert resolution.runner is not None
    assert "not a runner this build knows" in resolution.ignored_reason


def test_a_removed_engine_code_in_prefs_degrades_to_the_ordering(monkeypatch):
    """The three codes D416 deleted, arriving in a prefs.json that outlived them.

    This is not the same case as the test above even though it takes the same
    branch, and the difference is who is holding the file. "whisper-9000" is a
    value nobody's app ever wrote — a newer build's vocabulary, or a typo. These
    three were REGISTERED, offered in the Engines picker, and stored by users who
    made a deliberate choice; `prefs.json` is a plain file in a home directory
    people sync and restore from backup, so upgrading is not the only way one
    arrives. The failure mode being ruled out is therefore not exotic: it is the
    ordinary experience of anyone who had picked Transformers before upgrading.

    Every one of the three is checked rather than one standing for the family.
    `resolve()`'s unknown-code branch is code-agnostic, but a future build that
    "helpfully" mapped one stale code onto a live engine would want to be forced
    to think about all three, and a test that only names the CPU row would let
    two of them keep a silent special case.

    What must NOT happen: an error, or a capability with no engine. What DOES
    happen is the ordering deciding, with the reason carried out so the Engines
    tab can say the stored choice is not in force — and the stored value is left
    exactly as written, because a preference silently corrected on read is one
    the user can neither see nor undo (`describe_engines`' `selected`).
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    for stale in ("transformers-text", "transformers-text-cuda",
                  "transformers-text-rocm"):
        assert registry.by_code(stale) is None, stale
        _prefer(monkeypatch, registry.TEXT_GENERATION, stale)

        resolution = registry.resolve(registry.TEXT_GENERATION)
        assert resolution.runner is not None, stale
        assert resolution.runner.code == "llamacpp-text", stale
        assert resolution.honoured is False, stale
        assert "not a runner this build knows" in resolution.ignored_reason, stale
        # The ordering's answer and nothing else: an ignored preference must
        # resolve exactly as "auto" would, or the drop is not really a drop.
        _prefer(monkeypatch, registry.TEXT_GENERATION, registry.AUTO)
        assert registry.for_capability(registry.TEXT_GENERATION).code == \
            resolution.runner.code, stale
        _prefer(monkeypatch, registry.TEXT_GENERATION, stale)

        # …and what the Engines tab is handed: the stale value still shown as
        # SELECTED (never rewritten), a live effective engine beside it, a reason,
        # and no option in the list to match — which is the state
        # `engines.strandedSelection` exists to render.
        row = next(r for r in registry.describe_engines()
                   if r["capability"] == registry.TEXT_GENERATION)
        assert row["selected"] == stale
        assert row["effective"] == "llamacpp-text"
        assert row["ignoredReason"]
        assert stale not in {c["code"] for c in row["choices"]}
        # WITHDRAWN, not merely stranded: `by_code(stale)` is None, so there is
        # no label to give — `strandedLabel` stays null and the Engines tab
        # falls back to the bare code (`EngineSelect`'s stranded option).
        assert row["strandedLabel"] is None, stale


def test_a_preference_for_the_WRONG_capabilitys_runner_is_ignored(monkeypatch):
    """Runner codes are global and capabilities are not, so a stale or
    hand-edited file can pair them wrongly. Loading a Whisper runner for text
    generation would fail at the first `/generate` with something unreadable."""
    _prefer(monkeypatch, registry.TEXT_GENERATION, "faster-whisper")
    resolution = registry.resolve(registry.TEXT_GENERATION)
    assert resolution.runner is not None
    assert resolution.runner.capability == registry.TEXT_GENERATION
    assert "does not do" in resolution.ignored_reason


def test_a_stranded_wrong_capability_preference_carries_its_own_display_name(
        monkeypatch):
    """The OTHER half of the code review's finding: a stranded code that IS
    registered (just for a different capability) has a real label to give,
    unlike a withdrawn one — and the label `describe_engines` sends has to be
    the SAME name `resolve()` already put in `ignoredReason` (`runner.short`,
    D416's "MLX Whisper does not do text-generation" shape), or the Engines
    tab's substring dedup (`ignoredWarning`) can't recognise its own reason and
    prints the name twice: "faster-whisper is not used here — Faster Whisper
    does not do text-generation."

    `describe_engines` is the only place that can compute this: it has
    `by_code`, which the frontend payload does not, and the frontend must not
    paraphrase a registry sentence to extract a name from it.
    """
    _prefer(monkeypatch, registry.TEXT_GENERATION, "faster-whisper")
    row = next(r for r in registry.describe_engines()
               if r["capability"] == registry.TEXT_GENERATION)
    assert row["selected"] == "faster-whisper"
    assert "faster-whisper" not in {c["code"] for c in row["choices"]}
    assert row["strandedLabel"] == registry.by_code("faster-whisper").short
    assert row["strandedLabel"] in row["ignoredReason"]


def test_a_broken_preferences_file_costs_the_preference_and_nothing_else(
        monkeypatch):
    """`preferred_code` is on the path of every load, download and page render.
    A preferences store that cannot be read must not make local inference
    unavailable — the capability is a property of the machine, not of a JSON
    file."""
    def _explode(capability):
        raise OSError("prefs.json is a directory")

    monkeypatch.setattr("fused_render.shell.prefs.engine_for_capability", _explode)
    assert registry.preferred_code(registry.SPEECH_TO_TEXT) == registry.AUTO
    assert registry.for_capability(registry.SPEECH_TO_TEXT) is not None


def test_auto_is_honoured_by_definition(monkeypatch):
    """"Ignored" has to mean something, so the default must not report itself as
    overruled — a page that showed "your preference was ignored" on every fresh
    machine would teach the user to ignore the message."""
    resolution = registry.resolve(registry.SPEECH_TO_TEXT)
    assert resolution.requested == registry.AUTO
    assert resolution.honoured and resolution.ignored_reason == ""


def test_a_resident_worker_of_the_WRONG_ENGINE_is_not_served(monkeypatch):
    """Resolution moves under models that are already loaded, and until now the
    only thing that noticed was `evict_stale_engines` — which has exactly ONE
    caller, the prefs PUT handler.

    That is not enough, because `preferred_code` re-reads prefs.json on every
    resolution with no cache: a file edited by hand, restored from a backup or
    synced into the home directory moves the answer with no PUT ever running.
    The result was a page reporting CTranslate2 while every transcription was
    still served by the resident MLX worker — the exact "which engine served
    me?" confusion the whole feature exists to remove.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(supervisor, "_terminate", lambda worker: None)
    _prefer(monkeypatch, registry.SPEECH_TO_TEXT, registry.AUTO)
    worker = supervisor.Worker(model="org/w", capability=registry.SPEECH_TO_TEXT,
                               runner_code="mlx-whisper", token="t", state="ready")
    monkeypatch.setitem(supervisor._workers, registry.SPEECH_TO_TEXT, worker)

    assert supervisor.ready_worker(registry.SPEECH_TO_TEXT) is worker

    _prefer(monkeypatch, registry.SPEECH_TO_TEXT, "faster-whisper")

    assert supervisor.ready_worker(registry.SPEECH_TO_TEXT) is None
    # …and it is GONE, not merely refused. A worker nothing will ever route to
    # again, holding gigabytes, is the precise waste `evict_stale_engines`
    # exists to prevent; declining to serve it without unloading it would leak
    # that memory until a PUT that may never come.
    assert supervisor._workers.get(registry.SPEECH_TO_TEXT) is None


def test_a_load_JOINING_one_in_flight_checks_the_engine_too(monkeypatch, fake_runner):
    """The other place a worker is reused without being started.

    Same model, different backend, is a real pair — `openai/whisper-large-v3`
    exists as both an MLX conversion and a CTranslate2 one — so "the model
    already loading is the model you asked for" is not the same question as
    "the worker loading it is the one that should serve you".
    """
    monkeypatch.setattr(supervisor, "_terminate", lambda worker: None)
    stale = supervisor.Worker(model="m", capability=registry.TEXT_GENERATION,
                              runner_code="some-other-runner", token="t",
                              state="loading")
    monkeypatch.setitem(supervisor._workers, registry.TEXT_GENERATION, stale)

    _reply, worker = supervisor._start_resident("m", registry.TEXT_GENERATION)

    assert worker is not stale, "it joined a bring-up of the wrong engine"
    assert worker.runner_code == fake_runner.code


def test_describe_tells_AVAILABLE_apart_from_ACTIVE(monkeypatch):
    """The distinction the public API needed (`fused.ai.models.list()`).

    Availability is a fact about the hardware; active is a fact about this
    capability right now. They were the same answer while resolution was purely
    first-available, so every reader took "available" to mean "this is what
    serves me". On an Apple Silicon machine BOTH whisper runners are available
    and exactly one is active — a page that cannot tell them apart cannot say
    which engine transcribed for it.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    rows = {row["code"]: row for row in registry.describe()}
    assert rows["mlx-whisper"]["available"] and rows["faster-whisper"]["available"]
    assert rows["mlx-whisper"]["active"] is True
    assert rows["faster-whisper"]["active"] is False

    _prefer(monkeypatch, registry.SPEECH_TO_TEXT, "faster-whisper")
    rows = {row["code"]: row for row in registry.describe()}
    assert rows["mlx-whisper"]["active"] is False
    assert rows["faster-whisper"]["active"] is True
    # Availability did not move — it is not the thing the preference changes.
    assert rows["mlx-whisper"]["available"] is True


def test_describe_engines_carries_every_choice_with_its_own_reason(monkeypatch):
    """What the Preferences page renders. The greyed-out control's explanation
    comes from the registry (`available().reason`) rather than being written
    again in the UI, because the UI cannot know it — it is a fact about this
    machine and this backend."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    rows = {row["capability"]: row for row in registry.describe_engines()}
    speech = rows[registry.SPEECH_TO_TEXT]

    assert speech["selected"] == registry.AUTO
    assert speech["effective"] == "faster-whisper"
    assert speech["ignoredReason"] is None
    choices = {choice["code"]: choice for choice in speech["choices"]}
    assert set(choices) == {"mlx-whisper", "faster-whisper"}
    assert choices["mlx-whisper"]["available"] is False
    assert "Apple Silicon" in choices["mlx-whisper"]["reason"]
    assert choices["faster-whisper"]["reason"] is None
    # Every capability is listed, servable here or not — a preference the user
    # cannot see is one they cannot fix.
    assert set(rows) == set(registry.capabilities())


def test_the_unavailable_reason_names_EVERY_runner_not_just_the_first(monkeypatch):
    """A capability with two runners must not answer for only the first of them.

    `mlx-text` is registered first, so a Linux machine whose cross-platform text
    worker was missing — a state `Runner.available` documents, since a runner is
    registered before its folder is written — was told text generation "needs
    Apple Silicon": the one backend that was never going to serve it, with the
    one that would have gone unmentioned. Reported by review on the PR that
    added the second runner, and the fix is that all three copies of this
    lookup (registry, `_runner_or_raise`, `start_image`) became one.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    # The cross-platform runner present but unbuilt, which is what makes the
    # whole capability unservable on a machine MLX has already turned down.
    ghost = registry.Runner(
        code="llamacpp-text", capability=registry.TEXT_GENERATION,
        folder="/nowhere", label="llama.cpp (CPU)",
        short_label="llama.cpp (CPU)")
    monkeypatch.setattr(
        registry, "_RUNNERS", (registry.by_code("mlx-text"), ghost))

    reason = registry.unavailable_reason(registry.TEXT_GENERATION)
    assert "not built yet" in reason, reason
    # The SHORT name. This sentence is read wherever a capability has to
    # explain itself — a card, a job row, an API error — and none of those is
    # the engine picker, which is the one surface that keeps a PLATFORM
    # qualifier. On a hardware variant the two names are equal, because the
    # accelerator is part of the short name too (a variant is not identifiable
    # without it), so what this pins is that the sentence names the engine at
    # all and names it the way the rest of the app does.
    assert "llama.cpp (CPU)" in reason, reason
    assert "(GGUF)" not in reason, reason
    # The supervisor raises the same sentence rather than deriving its own.
    with pytest.raises(supervisor.SupervisorError) as caught:
        supervisor.load("org/x", registry.TEXT_GENERATION)
    assert str(caught.value) == reason


def test_one_runner_per_capability_reads_exactly_as_before(monkeypatch):
    """Joining the reasons must not have changed the common case into a list.

    Every capability but text generation has a single runner, so its message is
    that runner's sentence and nothing else — no separator, no second clause.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    ghost = registry.Runner(
        code="ghost", capability=registry.IMAGE_GENERATION,
        folder="/nowhere", label="Ghost")
    monkeypatch.setattr(registry, "_RUNNERS", (ghost,))
    assert registry.unavailable_reason(registry.IMAGE_GENERATION) == (
        "the Ghost runner is not built yet")


def test_a_capability_nothing_can_serve_still_resolves_to_nothing(monkeypatch):
    """The other half of the rule, now that no real capability demonstrates it.

    Every capability has a runner that runs everywhere since D293, so the
    "skipped everything and found nobody" branch has no platform left to be
    reached on — and an unreachable branch is one nobody notices breaking. A
    registry holding only the Metal-only runner puts it back within reach.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(registry, "_RUNNERS", (registry.by_code("mlx-text"),))
    assert registry.for_capability(registry.TEXT_GENERATION) is None


def test_every_runner_has_both_names_and_they_differ_only_by_the_qualifier():
    """Two names per backend, and a registered runner must set both.

    `Runner.short` falls back to the long name so a stand-in built in a test
    renders as something, and that fallback is exactly what would hide a
    forgotten `short_label` on a real row — the tag would silently go back to
    "MLX FLUX (Apple Silicon)" with nothing failing. So it is required here.

    The shape is also pinned: the short name is a PREFIX of the long one and
    the remainder is a parenthetical. That is not how the value is derived —
    deriving it would make the name a side effect of someone's punctuation —
    but it is what makes the pair one backend rather than two, and a row whose
    two names disagree about what the thing is called is worth a failure.
    """
    for runner in registry.all_runners():
        assert runner.short_label, f"{runner.code} has no short name"
        assert runner.short == runner.short_label
        rest = runner.label[len(runner.short_label):].strip()
        assert runner.label.startswith(runner.short_label) and (
            rest == "" or (rest.startswith("(") and rest.endswith(")"))), (
            f"{runner.code}: {runner.label!r} is not {runner.short_label!r} "
            f"plus a qualifier")


#: The runners whose SHORT name keeps a bracketed qualifier, and why each does.
#:
#: A PLATFORM qualifier ("(Apple Silicon)", "(CTranslate2)") is dropped outside
#: the picker: it tells someone sitting at the machine nothing they do not know.
#: A HARDWARE qualifier is kept, because it is the only thing that tells two or
#: three builds of one library apart, and the short name is what the Local card
#: and `servingLine` print — three engines all reading "Diffusers" would render
#: as one engine everywhere but the picker.
#:
#: An allow-list rather than a rule about brackets, so adding a row with a
#: qualifier in its short name is a decision somebody writes down here.
_QUALIFIED_SHORT_NAMES = {
    "diffusers-image": "(CPU)",
    "diffusers-image-cuda": "(CUDA)",
    "diffusers-image-rocm": "(ROCm)",
    "llamacpp-text": "(CPU)",
    "llamacpp-text-vulkan": "(Vulkan)",
    "onnx-embed": "(CPU)",
    "onnx-embed-directml": "(DirectML)",
    "onnx-embed-cuda": "(CUDA)",
    "onnx-embed-rocm": "(ROCm)",
}

#: Every qualifier this app uses to name a BUILD rather than a platform.
#:
#: The vocabulary is closed on purpose: it is what
#: `test_no_engine_name_advertises_the_format_its_sibling_also_reads` matches a
#: name against, and what Task B's family test forbids in a family name.
#: `(DirectML)` joined the vocabulary with the ONNX embedding family: it names a
#: BUILD (`onnxruntime-directml`) exactly as `(Vulkan)` does, and it is the
#: Windows GPU path — vendor-neutral, so one row covers NVIDIA, AMD and Intel
#: there rather than a folder per vendor.
_HARDWARE_QUALIFIERS = ("(CPU)", "(CUDA)", "(ROCm)", "(Vulkan)", "(DirectML)")


def test_the_picker_keeps_the_platform_qualifier_and_everything_else_drops_it():
    """A PLATFORM qualifier lives on the picker; a HARDWARE one lives everywhere.

    The original split: on the picker the reader is CHOOSING between backends and
    "(Apple Silicon)" is the difference between two options, while everywhere else
    they are being told what is happening on a machine they are already sitting
    at. The per-hardware torch rows are the exception that proves what the rule
    was about — "(CUDA)" is not a fact about the reader's machine, it is the
    engine's IDENTITY, and dropping it makes three rows print the same name on
    the card, the job row and the serving line.
    """
    engines = registry.describe_engines()
    choices = [c for row in engines for c in row["choices"]]
    assert any("(" in c["label"] for c in choices), choices
    for row in registry.describe():
        expected = _QUALIFIED_SHORT_NAMES.get(row["code"])
        if expected is None:
            assert "(" not in row["shortLabel"], row
        else:
            assert row["shortLabel"].endswith(expected), row
        assert row["label"].startswith(row["shortLabel"])
    # …and the summary line under the picker reads whatever that runner's short
    # name is, which is the same string the card shows.
    for row in engines:
        if row["effective"]:
            runner = registry.by_code(row["effective"])
            assert row["effectiveShortLabel"] == runner.short


def test_every_runner_names_its_family_with_no_hardware_in_it():
    """A THIRD name per runner, and the card's tag is what it exists for.

    The tag on a Local card is a FORMAT claim — "Diffusers" is exactly the
    statement that these weights are safetensors a Diffusers pipeline opens —
    and all three Diffusers rows read the identical file, so "(ROCm)" on that
    tag answers a question nobody asked of a file on disk and leaks which
    machine happens to be reading it. `family_label` is that claim with the
    hardware taken out; the hardware-qualified `short_label` stays on the
    tag's hover, so the full truth is one hover away rather than gone.

    A prefix of `short_label` and never a regex over it, for `short_label`'s
    own reason: derived names make the value a side effect of somebody's
    punctuation. The prefix check is what keeps the pair one engine.
    """
    for runner in registry.all_runners():
        assert runner.family_label, f"{runner.code} has no family name"
        assert runner.family == runner.family_label
        assert runner.short_label.startswith(runner.family_label), (
            f"{runner.code}: {runner.family_label!r} is not the start of "
            f"{runner.short_label!r}")
        for qualifier in _HARDWARE_QUALIFIERS + ("(Apple Silicon)",):
            assert qualifier not in runner.family_label, (
                f"{runner.code}: {runner.family_label!r} carries {qualifier} — "
                f"a tag that is a format claim must not name the hardware")
    # And the field earns its existence: on a hardware variant it is genuinely
    # a different string from the short name. Without a row like that the test
    # above would pass on a registry that simply copied `short_label` across.
    assert registry.by_code("diffusers-image-rocm").family_label == "Diffusers"


def test_no_engine_name_advertises_the_format_its_sibling_also_reads():
    """Sibling rows of one library are told apart by HARDWARE, never by format.

    The regression this locks out shipped once: the first llama.cpp row was
    called "llama.cpp (GGUF)" beside "llama.cpp (Vulkan)", and GGUF is not what
    tells those two apart — both load it through the same
    `runners/llama_text.py`. A qualifier naming something a sibling also does is
    a qualifier that answers nothing, and it cost that row a hardware name it
    needed: the Vulkan row's own note has to point at "the CPU build", which
    only reads as a cross-reference once the row is CALLED that.

    So the rule, per library: where two rows share a `label` stem, each
    qualifier must come from `_HARDWARE_QUALIFIERS`, they must differ, and no
    name may repeat a format tag both rows declare in `hub_filter_tags`.
    """
    families: dict[str, list] = {}
    for runner in registry.all_runners():
        families.setdefault(runner.label.split(" (")[0], []).append(runner)
    siblings = {stem: rows for stem, rows in families.items() if len(rows) > 1}
    # If this is ever empty the test has stopped testing anything — every
    # hardware-variant family could have been renamed out from under it.
    assert "llama.cpp" in siblings, sorted(families)
    for stem, rows in siblings.items():
        qualifiers = [runner.label[len(stem):].strip() for runner in rows]
        assert len(set(qualifiers)) == len(qualifiers), (stem, qualifiers)
        for runner, qualifier in zip(rows, qualifiers):
            assert qualifier in _HARDWARE_QUALIFIERS, (runner.code, qualifier)
            # And the hardware is in BOTH names, the shape the torch rows set:
            # the short name is what the Local card and the job row print, so a
            # family whose short names collide renders as one engine there.
            assert runner.short_label == runner.label, runner.code
        shared = set.intersection(*(set(r.hub_filter_tags) for r in rows))
        for runner in rows:
            for tag in shared:
                assert tag.lower() not in runner.label.lower(), (
                    f"{runner.code}: {runner.label!r} names {tag!r}, which "
                    f"every {stem} row reads — it distinguishes nothing")


def test_every_suggested_model_names_a_runner_that_exists():
    # A suggestion list under a runner nobody registered is a dead card on the
    # page — and since D293 the table is keyed by RUNNER rather than capability,
    # because `mlx-community/…` and `Qwen/…` serve the same capability and are
    # unloadable on each other's machines.
    for code in catalog.SUGGESTIONS:
        assert registry.by_code(code) is not None, code


def test_every_runner_that_can_run_here_suggests_something(monkeypatch):
    """A capability with a runner and no shortlist is an empty Discover heading.

    Checked on the platform where it would actually bite: text generation
    resolves to a DIFFERENT runner on Windows than on a Mac, so a list added for
    one and forgotten for the other is invisible to anyone developing on the
    other machine.
    """
    for system, machine in (("Darwin", "arm64"), ("Windows", "AMD64"), ("Linux", "x86_64")):
        monkeypatch.setattr(registry.platform, "system", lambda s=system: s)
        monkeypatch.setattr(registry.platform, "machine", lambda m=machine: m)
        for capability in registry.capabilities():
            if registry.for_capability(capability) is None:
                continue
            assert catalog.for_capability(capability), (
                f"{capability} resolves to a runner on {system} and suggests nothing")


#: Every (system, machine) pair this app is DISTRIBUTED to, and the local text
#: engine each one resolves to with no preference set.
#:
#: A literal table rather than a loop over `_RUNNERS`, because the property being
#: pinned is not "the registry is self-consistent" — it is "the set of platforms
#: we ship to is covered", and a registry cannot know that set. The three rows
#: are the three artefacts this repo builds: the macOS DMG (`skills/making-a-release`),
#: and the Windows and Linux builds under `windows/` and `.github/workflows`.
#:
#: The `code` column is asserted and not merely non-None on purpose. "Something
#: serves text generation here" would still pass if a removal silently moved
#: every non-Apple machine onto a DIFFERENT engine than the one this app's
#: catalog, notes and docs describe — which is exactly what D416 did do, and it
#: is a decision that must show up as a diff in this table rather than as
#: nothing.
_TEXT_ENGINE_PER_SHIPPED_PLATFORM = (
    ("Darwin", "arm64", "mlx-text"),
    ("Windows", "AMD64", "llamacpp-text"),
    ("Linux", "x86_64", "llamacpp-text"),
)


def test_every_shipped_platform_keeps_a_local_text_engine(monkeypatch):
    """No platform this app ships to may be left with no local text generation.

    D293 stated this first: text generation was Apple-Silicon-only, which made
    the app's flagship local capability something a Windows or Linux user could
    read about and not use. D416 is why it is now written as an INVARIANT over an
    enumerated platform list rather than as three incidental assertions — that
    change removed three of the four text rows, and the thing that made it safe
    to do was being able to show that no shipped platform was stranded. A future
    removal must have to argue with this test.

    **The remaining coverage is thin, and the test says so rather than implying
    otherwise.** Apple Silicon holds `mlx-text` and `llamacpp-text` both; Windows
    and Linux hold `llamacpp-text` alone, with `llamacpp-text-vulkan` as an
    opt-in registered BELOW it — which is why `auto` never reaches the Vulkan row
    and why it does not widen this coverage — and no second FAMILY behind
    either. So on those two
    platforms this assertion is one runner deep, and `_llamacpp_platform`'s own
    docstring carries what that costs (a Windows ARM64 box, or a Linux machine
    outside the three architectures its wheel index publishes, has no local text
    engine at all and falls back to `claude-cli`).
    """
    for system, machine, code in _TEXT_ENGINE_PER_SHIPPED_PLATFORM:
        monkeypatch.setattr(registry.platform, "system", lambda s=system: s)
        monkeypatch.setattr(registry.platform, "machine", lambda m=machine: m)
        runner = registry.for_capability(registry.TEXT_GENERATION)
        assert runner is not None and runner.code == code, (system, machine)
        # A resolved engine with an empty shortlist is a Discover tab with a
        # heading and nothing under it, which is not "covered".
        assert catalog.for_capability(registry.TEXT_GENERATION), (system, machine)


def test_intel_macos_is_not_advertised_as_a_supported_text_platform(monkeypatch):
    """Availability controls the catalog and Load button, so it is a support claim.

    Both text runners registered for Darwin have to say no here, and each is
    checked BY NAME rather than through `for_capability` alone: "nothing
    resolves" would still pass if one gate regressed while the other covered
    for it. `mlx-text` is Metal-only, and `llamacpp-text` refuses because the
    maintainer's wheel index publishes no macOS x86_64 build at all — pinned
    against the index's own tag list in
    `test_llamacpp_texts_platform_gate_matches_its_published_wheel_tags` below.

    Intel macOS was never covered: the removed `transformers-text` row refused
    it too (as a distribution decision rather than a packaging one), so D416
    took nothing away here.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")

    assert registry.for_capability(registry.TEXT_GENERATION) is None
    status = registry.by_code("mlx-text").available()
    assert status.ok is False
    assert "Apple Silicon" in status.reason
    llamacpp_status = registry.by_code("llamacpp-text").available()
    assert llamacpp_status.ok is False
    assert "Apple Silicon" in llamacpp_status.reason


def test_llamacpp_texts_platform_gate_matches_its_published_wheel_tags(monkeypatch):
    """`_llamacpp_platform`, pinned directly rather than only through
    `for_capability` — the maintainer's CPU index publishes wheels for
    `win_amd64`, Linux x86_64/aarch64/riscv64, and macOS arm64, and NOTHING
    else (verified against the index listing for the pinned 0.3.29, D411).

    **The gate is checked by ARCHITECTURE, and the refusals are asserted, not
    just the admissions.** This docstring used to say "Windows (any arch it runs
    on), Linux (any arch)", which contradicted both `_llamacpp_platform` and the
    loop two lines below it — and a half-checked gate is what
    `test_the_hardware_probes_stop_refusing_machines_that_work`'s whole family
    exists to prevent. D416 makes it load-bearing rather than cosmetic: this row
    is now the ONLY local text engine on Windows and Linux, so an arch this gate
    wrongly ADMITS gets a Load button whose `uv sync` has nothing to install, and
    one it wrongly REFUSES has no local text generation at all —
    `_llamacpp_platform`'s own docstring cites this test for exactly that reason.
    """
    for system, machine in (
        ("Windows", "AMD64"), ("Linux", "x86_64"), ("Linux", "aarch64"),
        ("Linux", "riscv64"), ("Darwin", "arm64"),
    ):
        monkeypatch.setattr(registry.platform, "system", lambda s=system: s)
        monkeypatch.setattr(registry.platform, "machine", lambda m=machine: m)
        status = registry.by_code("llamacpp-text").available()
        assert status.ok is True, (system, machine, status.reason)

    # …and every architecture the index does NOT publish, refused with a reason
    # that names the missing tag rather than the OS. Each `needle` is checked
    # because "ok is False" alone would pass on a gate that refused for the
    # wrong reason, which is the failure mode a user reads off the disabled row.
    for system, machine, needle in (
        ("Windows", "ARM64", "no win_arm64"),
        ("Linux", "ppc64le", "no ppc64le build for Linux"),
        ("Darwin", "x86_64", "macOS x86_64"),
    ):
        monkeypatch.setattr(registry.platform, "system", lambda s=system: s)
        monkeypatch.setattr(registry.platform, "machine", lambda m=machine: m)
        status = registry.by_code("llamacpp-text").available()
        assert status.ok is False, (system, machine)
        assert needle in status.reason, (system, machine, status.reason)


def test_apple_silicon_falls_back_to_llamacpp_when_mlx_is_unavailable(
        monkeypatch):
    """A Mac keeps text generation even with the MLX row gone.

    The fallback used to be `transformers-text`, whose `whl/cpu` pin resolved to
    an MPS-capable macOS wheel. Since D416 it is `llamacpp-text`, whose CPU-index
    wheel links `libggml-metal.dylib` — so a Mac still falls back onto its GPU
    rather than onto a CPU path, which is the property this test is really about
    and the reason the row below MLX is allowed to be the only one.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        registry, "_RUNNERS",
        tuple(runner for runner in registry.all_runners() if runner.code != "mlx-text"),
    )

    runner = registry.for_capability(registry.TEXT_GENERATION)
    assert runner is not None and runner.code == "llamacpp-text"


# -- the accelerator probes -----------------------------------------------------
#
# Hardware is monkeypatched the way PLATFORM is monkeypatched everywhere above: a
# fake sysfs and /dev on a tmp_path, with the module's path constants repointed
# at it. That is why those constants exist — a probe that hard-coded
# "/sys/class/kfd/…" could only be tested on a machine that happened to have the
# hardware, which is the same reason `registry.platform` is asked at call time
# rather than at import.


def _fake_amd(monkeypatch, tmp_path, *, gpus=(120000,), kfd=True, render=True,
              amd_card=False, topology=True, kfd_mode=0o666, render_mode=0o666,
              foreign_render=None):
    """A machine as the amdkfd driver would describe it, on tmp_path.

    `gpus` is the raw `gfx_target_version` of each GPU node. **Node 0 is always
    written as a CPU** (`cpu_cores_count 6, simd_count 0, gfx_target_version 0`),
    because that is what a real machine with a working GPU reports and reading
    only node 0 — decoding gfx0 and concluding nothing is supported — is the bug
    this fixture exists to catch. A fixture whose first node were a GPU would let
    that bug pass.

    A render node is written in TWO places, because that is where a real one
    lives: the device under `DRI_DIR` (what HIP opens, and what `os.access` is
    asked about, so `render_mode` belongs to it) and the DRM class entry
    `renderD*/device/vendor` that says which card it belongs to. `foreign_render`
    adds a SECOND, world-openable node under another PCI vendor — `"0x8086"` for
    an Intel iGPU — which is the hybrid machine the probe must not be satisfied
    by.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")

    nodes = tmp_path / "sys" / "kfd" / "topology" / "nodes"
    if topology:
        properties = ["cpu_cores_count 6\nsimd_count 0\ngfx_target_version 0\n"]
        properties += [
            f"cpu_cores_count 0\nsimd_count 64\ngfx_target_version {raw}\n"
            for raw in gpus
        ]
        for index, text in enumerate(properties):
            node = nodes / str(index)
            node.mkdir(parents=True)
            (node / "properties").write_text(text)
    monkeypatch.setattr(registry, "KFD_NODES_DIR", str(nodes))

    dev = tmp_path / "dev"
    dev.mkdir(exist_ok=True)
    kfd_path = dev / "kfd"
    if kfd:
        kfd_path.write_text("")
        os.chmod(kfd_path, kfd_mode)
    monkeypatch.setattr(registry, "KFD_DEVICE", str(kfd_path))

    dri = dev / "dri"
    dri.mkdir()
    monkeypatch.setattr(registry, "DRI_DIR", str(dri))

    drm = tmp_path / "sys" / "drm"
    drm.mkdir(parents=True)
    if amd_card:
        device = drm / "card1" / "device"
        device.mkdir(parents=True)
        (device / "vendor").write_text("0x1002\n")
    nodes_to_write = []
    if render:
        nodes_to_write.append(("renderD128", "0x1002", render_mode))
    if foreign_render:
        nodes_to_write.append(("renderD129", foreign_render, 0o666))
    for name, vendor, mode in nodes_to_write:
        (dri / name).write_text("")
        os.chmod(dri / name, mode)
        device = drm / name / "device"
        device.mkdir(parents=True)
        (device / "vendor").write_text(f"{vendor}\n")
    monkeypatch.setattr(registry, "DRM_CLASS_DIR", str(drm))


def _fake_nvidia(monkeypatch, tmp_path, *, control=True, gpus=("nvidia0",),
                 uvm=True, unreadable=(), wsl=False):
    """A Linux machine with (or without) NVIDIA's three device nodes.

    `unreadable` names devices to create and then `chmod 0o000` — the container
    started without `--gpus all`, which has the nodes and not the access. Named
    per device rather than as one mode, because each of the three is a separate
    `os.access` call and a mutation that deleted one of them would otherwise be
    caught by another.

    `wsl` writes WSL2's shape instead: `/dev/dxg` and a `libcuda.so.1` in the
    guest's `/usr/lib/wsl/lib`, which is all a WSL2 machine has — none of the
    nodes above exist there. The two WSL constants are repointed at tmp_path
    unconditionally, so a test that does not ask for WSL cannot accidentally read
    the host's `/dev`.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    dev = tmp_path / "dev"
    dev.mkdir(exist_ok=True)
    for name, present in [("nvidiactl", control), *[(g, True) for g in gpus],
                          ("nvidia-uvm", uvm)]:
        if present:
            (dev / name).write_text("")
            os.chmod(dev / name, 0o000 if name in unreadable else 0o666)
    monkeypatch.setattr(registry, "NVIDIA_DEVICE_DIR", str(dev))
    monkeypatch.setattr(registry, "NVIDIA_CONTROL_DEVICE", str(dev / "nvidiactl"))
    monkeypatch.setattr(registry, "NVIDIA_UVM_DEVICE", str(dev / "nvidia-uvm"))

    wsl_lib = tmp_path / "usr" / "lib" / "wsl" / "lib"
    if wsl:
        (dev / "dxg").write_text("")
        wsl_lib.mkdir(parents=True)
        (wsl_lib / "libcuda.so.1").write_text("")
    monkeypatch.setattr(registry, "WSL_DXG_DEVICE", str(dev / "dxg"))
    monkeypatch.setattr(registry, "WSL_CUDA_LIBRARY", str(wsl_lib / "libcuda.so.1"))


def test_the_gfx_target_version_decoder_reads_minor_and_step_as_HEX_digits():
    """`major * 10000 + minor * 100 + step`, with minor and step as HEX digits.

    The one fact the whole ROCm probe rests on, and the one a reader gets wrong:
    90010 is `gfx90a`, not `gfx9010`. A decimal render produces names that match
    nothing in `ROCM_TARGETS`, so every AMD GPU would be refused as unsupported
    — a failure that looks like a policy decision rather than a bug, which is
    why the table is pinned rather than left to the code to imply.
    """
    assert {raw: registry.decode_gfx_target(raw) for raw in
            (90000, 90010, 90402, 100300, 110501, 120000, 0)} == {
        90000: "gfx900",
        90010: "gfx90a",
        90402: "gfx942",
        100300: "gfx1030",
        110501: "gfx1151",
        120000: "gfx1200",
        # A CPU node, which every machine has as node 0 — not a GPU called gfx0.
        0: None,
    }


def test_every_supported_rocm_target_is_a_name_the_decoder_can_produce():
    """The guard on the SET, which no other test can give.

    `ROCM_TARGETS` is compared against the decoder's output, so a member the
    decoder can never emit — `"gfx9010"` for the card that is really `gfx90a`,
    the exact typo a decimal reading invites — is a GPU that is silently never
    supported on any machine. Nothing else fails: the probe works, the reason
    string is grammatical, and the card is simply always refused.

    Producibility is asked over the plausible ISA majors rather than by
    round-tripping each string, because a round-trip cannot see this: "gfx9010"
    parses back to major 90 and returns itself. Major 90 is what makes it wrong.
    """
    producible = {
        registry.decode_gfx_target(major * 10000 + minor * 100 + step)
        for major in range(6, 13) for minor in range(16) for step in range(16)
    }
    assert registry.ROCM_TARGETS <= producible, (
        sorted(registry.ROCM_TARGETS - producible))


def test_a_supported_amd_gpu_is_offered_the_rocm_engines(monkeypatch, tmp_path):
    _fake_amd(monkeypatch, tmp_path, gpus=(120000,))
    for code in ("diffusers-image-rocm",):
        status = registry.by_code(code).available()
        assert status.ok is True, (code, status.reason)


def test_an_amd_gpu_the_rocm_wheel_cannot_target_is_refused(monkeypatch, tmp_path):
    """The 6GB download that would die inside HIP, refused with a sentence.

    gfx1010 (raw 100100) is a real card — an RX 5700 — and it is not in the
    target list the pinned ROCm wheel was built for. Installing anyway costs the
    user a multi-gigabyte fetch and then fails with "no kernel image is
    available for execution", several frames below anything this app wrote.
    """
    _fake_amd(monkeypatch, tmp_path, gpus=(100100,))
    status = registry.by_code("diffusers-image-rocm").available()
    assert status.ok is False
    assert "gfx1010" in status.reason
    assert "not supported by the ROCm build" in status.reason


def test_one_supported_gpu_among_several_is_enough(monkeypatch, tmp_path):
    """A machine can hold two AMD GPUs and ROCm only needs one it can target —
    an integrated gfx1010-era part beside a discrete card being the ordinary
    shape of that. This is also the case a node-0-only probe gets wrong twice
    over, since node 0 is neither of them."""
    _fake_amd(monkeypatch, tmp_path, gpus=(100100, 120000))
    assert registry.by_code("diffusers-image-rocm").available().ok is True


def test_a_kfd_reporting_no_gpu_nodes_names_the_container_case(monkeypatch, tmp_path):
    """`/dev/kfd` present, topology readable, and every node a CPU.

    That is a container started without `--device /dev/kfd --device /dev/dri`
    passthrough (or with the device and no GPU behind it), and it is a different
    sentence from "no AMD GPU" because the fix is on the outside of the
    container.
    """
    _fake_amd(monkeypatch, tmp_path, gpus=())
    status = registry.by_code("diffusers-image-rocm").available()
    assert status.ok is False
    assert "CPU nodes only" in status.reason
    assert "--device /dev/kfd" in status.reason


def test_an_amd_gpu_with_no_kfd_device_says_the_driver_is_not_loaded(
        monkeypatch, tmp_path):
    """Two causes for one missing file, and the reasons must differ: an AMD GPU
    with no `/dev/kfd` is an ACTION (`modprobe amdgpu`, or a reboot after a
    driver update), which is why the probe falls back to the DRM class rather
    than concluding there is no GPU."""
    _fake_amd(monkeypatch, tmp_path, kfd=False, amd_card=True)
    status = registry.by_code("diffusers-image-rocm").available()
    assert status.ok is False
    assert "amdgpu kernel driver" in status.reason
    assert "modprobe amdgpu" in status.reason


def test_a_machine_with_no_amd_gpu_at_all_says_so(monkeypatch, tmp_path):
    """…and the other cause is a FACT, with nothing to do about it. An NVIDIA
    or Intel machine must not be told to load a driver for a card it does not
    have."""
    _fake_amd(monkeypatch, tmp_path, kfd=False, amd_card=False)
    status = registry.by_code("diffusers-image-rocm").available()
    assert status.ok is False
    assert "needs an AMD GPU" in status.reason
    assert "modprobe" not in status.reason


@pytest.mark.skipif(_IS_ROOT, reason="os.access ignores mode bits for root")
def test_a_kfd_this_user_cannot_open_asks_for_permission(monkeypatch, tmp_path):
    """PERMISSION IS ASKED OF THE KERNEL, never modelled — `os.access`.

    A group-membership check gets real machines wrong in both directions: this
    was written on a box whose `/dev/kfd` is world-writable while the user is in
    neither `render` nor `video` (a group check refuses a working machine), and
    whose `card1` carries a POSIX ACL that mode arithmetic cannot see (it
    refuses a machine the ACL permits).

    Skipped as root, where `os.access` returns True regardless of the mode and
    the test would assert nothing while still passing.
    """
    _fake_amd(monkeypatch, tmp_path, kfd_mode=0o000)
    status = registry.by_code("diffusers-image-rocm").available()
    assert status.ok is False
    assert "needs permission" in status.reason
    assert "render" in status.reason


def test_a_render_node_that_is_ABSENT_is_not_a_permission_problem(
        monkeypatch, tmp_path):
    """Two states, two sentences — the fix for one cannot fix the other.

    A render node that does not EXIST is a container started without
    `--device /dev/dri`, or a `/dev/dri` the driver never populated. Telling that
    reader to join the `render` group and log out and back in is advice that
    cannot work, and it was the only sentence this branch had: the container case
    lived further down in the `not targets` branch, which this return makes
    unreachable whenever the devices are missing rather than the topology.
    """
    _fake_amd(monkeypatch, tmp_path, render=False)
    status = registry.by_code("diffusers-image-rocm").available()
    assert status.ok is False
    assert "renderD*" in status.reason
    assert "--device /dev/dri" in status.reason
    assert "render` group" not in status.reason


@pytest.mark.skipif(_IS_ROOT, reason="os.access ignores mode bits for root")
def test_a_render_node_that_is_CLOSED_asks_for_permission(monkeypatch, tmp_path):
    """…and the other state IS the group case, which keeps that advice.

    HIP opens `/dev/kfd` AND the card's render node, so a readable kfd is not
    enough — a user added to `video` but not `render` has exactly this
    half-working state, and here `usermod` is the whole fix.
    """
    _fake_amd(monkeypatch, tmp_path, render_mode=0o000)
    status = registry.by_code("diffusers-image-rocm").available()
    assert status.ok is False
    assert "needs permission" in status.reason
    assert "renderD128" in status.reason
    assert "render` group" in status.reason


@pytest.mark.skipif(_IS_ROOT, reason="os.access ignores mode bits for root")
def test_an_open_INTEL_render_node_does_not_admit_rocm(monkeypatch, tmp_path):
    """The render node has to belong to the AMD CARD, not merely to open.

    The hybrid machine, and it is the ordinary desktop rather than an exotic
    one: an Intel iGPU's `renderD128` is world-openable on most distributions,
    so a probe that accepted ANY readable `renderD*` passed on a device HIP will
    never touch while the AMD card's own node stayed shut. The user then paid a
    ~6GB download for a row that failed the moment HIP opened the node it
    actually needed — the exact outcome the hard gate exists to prevent, reached
    through the gate.
    """
    _fake_amd(monkeypatch, tmp_path, render_mode=0o000, foreign_render="0x8086")
    status = registry.by_code("diffusers-image-rocm").available()
    assert status.ok is False
    assert "needs permission" in status.reason
    # …named as the AMD card's node, not as the Intel one that happened to open.
    assert "renderD128" in status.reason and "renderD129" not in status.reason


def test_a_render_node_belonging_to_another_vendor_is_not_the_amd_one(
        monkeypatch, tmp_path):
    """The same rule with the AMD node absent altogether: an open Intel node is
    not evidence of anything ROCm can use, and the reason must say the AMD card
    has none rather than asking for a permission the user already has."""
    _fake_amd(monkeypatch, tmp_path, render=False, foreign_render="0x8086")
    status = registry.by_code("diffusers-image-rocm").available()
    assert status.ok is False
    assert "belongs to an AMD card" in status.reason


def test_an_unreadable_topology_is_its_own_reason(monkeypatch, tmp_path):
    """`/dev/kfd` there and `/sys/class/kfd` not — a container with the devices
    passed through and no sysfs. The GPU cannot be IDENTIFIED, which is neither
    "no GPU" nor "unsupported GPU", and saying either would send the reader
    after the wrong thing."""
    _fake_amd(monkeypatch, tmp_path, topology=False)
    status = registry.by_code("diffusers-image-rocm").available()
    assert status.ok is False
    assert "topology" in status.reason


def test_rocm_is_not_offered_off_linux(monkeypatch, tmp_path):
    """ROCm publishes no macOS or Windows wheels, so these rows must not be
    selectable there however the devices look."""
    _fake_amd(monkeypatch, tmp_path)
    for system, machine in (("Windows", "AMD64"), ("Darwin", "arm64")):
        monkeypatch.setattr(registry.platform, "system", lambda s=system: s)
        monkeypatch.setattr(registry.platform, "machine", lambda m=machine: m)
        status = registry.by_code("diffusers-image-rocm").available()
        assert status.ok is False
        assert "needs Linux" in status.reason
        assert system.lower() in status.reason


def test_an_nvidia_machine_is_offered_the_cuda_engines(monkeypatch, tmp_path):
    _fake_nvidia(monkeypatch, tmp_path)
    for code in ("diffusers-image-cuda",):
        status = registry.by_code(code).available()
        assert status.ok is True, (code, status.reason)


def test_cuda_is_a_HARD_GATE_on_a_machine_with_no_nvidia_gpu(monkeypatch, tmp_path):
    """The policy, stated as a test: an accelerated row is offerable only where
    it can actually run.

    Informational availability was the alternative — offer the row, let torch
    explain later — and it is wrong for the same reason the whole `Availability`
    type exists: selecting it buys a multi-gigabyte wheel and a load that fails
    with a message about a CUDA driver, on a machine that has no NVIDIA GPU to
    put a driver on.
    """
    _fake_nvidia(monkeypatch, tmp_path, control=False, gpus=(), uvm=False)
    status = registry.by_code("diffusers-image-cuda").available()
    assert status.ok is False
    assert "needs an NVIDIA GPU" in status.reason
    # …and the capability is untouched: the unaccelerated row above it still
    # serves. Asserted on IMAGE generation rather than on text, because since
    # D416 the CUDA row under test is an image row — text generation's own
    # accelerated variant is `llamacpp-text-vulkan`, gated by `_vulkan`, and
    # would not have been moved by this NVIDIA probe either way.
    monkeypatch.setattr(registry, "preferred_code", lambda capability: registry.AUTO)
    assert registry.for_capability(registry.IMAGE_GENERATION).code == "diffusers-image"


def test_a_MISSING_uvm_node_is_not_a_refusal_because_it_is_created_LAZILY(
        monkeypatch, tmp_path):
    """The freshly booted NVIDIA desktop, which the hard gate used to refuse.

    `nvidia-modprobe` loads `nvidia_uvm` and makes `/dev/nvidia-uvm` the first
    time a process creates a CUDA context; the display path needs only `nvidia`
    and `nvidia_drm`. So a Linux box with the proprietary driver that has not run
    a CUDA program since boot has `/dev/nvidiactl`, `/dev/nvidia0`, no
    `/dev/nvidia-uvm` — and a perfectly working `torch.cuda`. Both CUDA rows were
    greyed out there, and the reason blamed "a driver update without a reboot",
    which is the OPPOSITE of what had happened; with no override, the feature was
    simply unreachable. Docker with `NVIDIA_DRIVER_CAPABILITIES` short of
    `compute` is the same class of false refusal.

    The absence therefore proves nothing and says nothing. The genuine
    driver-mismatch case it used to catch is not visible from here either — a
    `modprobe` of the new module against the old running `nvidia` fails at load,
    not at `os.path.exists` — and torch's own error reports it, on the same
    argument the probe already makes about a driver-version floor.
    """
    _fake_nvidia(monkeypatch, tmp_path, uvm=False)
    for code in ("diffusers-image-cuda",):
        status = registry.by_code(code).available()
        assert status.ok is True, (code, status.reason)


def test_wsl2_can_pick_cuda_with_none_of_the_linux_device_nodes(
        monkeypatch, tmp_path):
    """WSL2 has no `/dev/nvidiactl` and no `/dev/nvidia0`, and torch.cuda works.

    GPU-PV projects the Windows driver into the guest: `/dev/dxg` is the device,
    and the CUDA driver library is bind-mounted at `/usr/lib/wsl/lib`. A WSL2
    user was told "there is no /dev/nvidiactl or /dev/nvidia0 on this machine" —
    true, irrelevant, and it left the CUDA engine unselectable on a machine where
    it is the whole point of the setup.

    **UNVERIFIED ON REAL HARDWARE.** There is no WSL2 and no NVIDIA GPU on the
    machine this was written on, so what this pins is the shape of the evidence
    and the fact that the Linux nodes are not required to accompany it. Both
    checks are `os.path.exists`: a dlopen of `libcuda.so.1` would initialise a
    driver on a page render, which AI-6 bars for `nvidia-smi`'s reasons.
    """
    _fake_nvidia(monkeypatch, tmp_path, control=False, gpus=(), uvm=False, wsl=True)
    assert registry.by_code("diffusers-image-cuda").available().ok is True
    # …and it takes BOTH: a `/dev/dxg` with no CUDA library is a WSL2 guest whose
    # host driver does not carry one, which is not a CUDA machine.
    os.remove(str(registry.WSL_CUDA_LIBRARY))
    status = registry.by_code("diffusers-image-cuda").available()
    assert status.ok is False
    assert "needs an NVIDIA GPU" in status.reason


@pytest.mark.skipif(_IS_ROOT, reason="os.access ignores mode bits for root")
@pytest.mark.parametrize("device", ["nvidiactl", "nvidia0", "nvidia-uvm"])
def test_nvidia_nodes_this_user_cannot_open_ask_for_permission(
        monkeypatch, tmp_path, device):
    """A container run without `--gpus all` has the nodes and not the access,
    and `os.access` is what tells the two apart — the same kernel-answers-it
    rule the ROCm probe follows.

    All THREE nodes, one case each, because each is its own `os.access` and a
    single case would be caught by whichever check happened to survive: this
    started as one test with every device unreadable, and dropping the
    control-and-GPU check entirely still passed it on the unified-memory one.
    """
    _fake_nvidia(monkeypatch, tmp_path, unreadable=(device,))
    status = registry.by_code("diffusers-image-cuda").available()
    assert status.ok is False
    assert "needs permission" in status.reason
    assert device in status.reason


def test_cuda_is_not_offered_on_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    status = registry.by_code("diffusers-image-cuda").available()
    assert status.ok is False
    assert "Windows and Linux only" in status.reason


def test_windows_gates_cuda_on_the_drivers_own_cuda_library(monkeypatch, tmp_path):
    """Windows has no device nodes to ask, so the gate is the weaker one the
    constant documents: `nvcuda.dll` is a HINT (the display driver installs it),
    and proving CUDA would mean loading the DLL and calling `cuInit` on a page
    render. What it does buy is the ordinary case — a machine that has never had
    an NVIDIA driver does not offer the row."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(registry, "NVCUDA_DLL", str(tmp_path / "nvcuda.dll"))
    status = registry.by_code("diffusers-image-cuda").available()
    assert status.ok is False
    assert "nvcuda.dll" in status.reason

    (tmp_path / "nvcuda.dll").write_text("")
    assert registry.by_code("diffusers-image-cuda").available().ok is True


def test_AUTO_STAYS_ON_THE_UNACCELERATED_ROW_EVEN_WITH_AN_ACCELERATOR(
        monkeypatch, tmp_path):
    """The whole user-facing decision of the per-hardware split, in one test.

    A machine with a working NVIDIA GPU and a working AMD GPU has both
    accelerated image rows available and still resolves to the unaccelerated
    one, because that is the default and the accelerated rows are OPT-IN from the
    Engines tab. That is a choice, not an accident of ordering: the accelerated
    wheels are much larger downloads with a hardware requirement, and a default
    that silently required one would fail hardest on the machines least able to
    explain why. Anyone who wants the GPU says so once, and `prefs.json`
    remembers.

    **Renamed from `test_AUTO_STAYS_ON_THE_CPU_ROW_EVEN_WITH_AN_ACCELERATOR` at
    D416, and the text-generation half of it went with the transformers rows.**
    The rule is unchanged and still holds on both capabilities — text
    generation's accelerated variant is now `llamacpp-text-vulkan`, which sits
    below `llamacpp-text` for this very reason — but Vulkan is gated by
    `_vulkan`, which needs a loader library and a registered ICD rather than the
    `/dev` nodes `_fake_amd`/`_fake_nvidia` fake here. Pinning text generation
    through those two fakes would have asserted a property of the fixtures, so
    the text side is pinned by
    `test_llamacpp_text_vulkan_is_registered_immediately_below_llamacpp_text`
    instead, on the ordering itself.

    Pinned because the ordering is invisible in a diff of the table and nothing
    else fails when a row moves — the same argument the mflux ordering test
    makes, applied to the decision it was written for.
    """
    _fake_amd(monkeypatch, tmp_path)
    _fake_nvidia(monkeypatch, tmp_path)
    monkeypatch.setattr(registry, "preferred_code", lambda capability: registry.AUTO)

    for code in ("diffusers-image-cuda", "diffusers-image-rocm"):
        assert registry.by_code(code).available().ok is True, code
    assert registry.for_capability(registry.IMAGE_GENERATION).code == "diffusers-image"

    # …and opting in is honoured, which is what makes the default a default
    # rather than a restriction.
    _prefer(monkeypatch, registry.IMAGE_GENERATION, "diffusers-image-cuda")
    resolution = registry.resolve(registry.IMAGE_GENERATION)
    assert resolution.runner.code == "diffusers-image-cuda" and resolution.honoured


def test_an_engine_row_is_serialised_from_ONE_probe(monkeypatch):
    """A row's `available` and its `reason` come from the SAME probe call.

    They were two calls — `runner.available().ok` and `runner.available().reason`
    — which was harmless while every probe was a `platform` fact and stopped
    being harmless the moment a probe read live device state (AI-6): the two can
    now straddle a `modprobe`, a container restart or an eGPU being unplugged and
    disagree. Both disagreements are user-visible and neither is a crash: a row
    can serialise as `available: false` with `reason: null`, which the `<select>`
    renders as a disabled option with NOTHING saying why (`choiceReason` returns
    null and the page has no copy of its own), or as `available: true` still
    carrying the refusal that has just stopped being true.

    Driven with a probe that flips on every call, which is the strongest form of
    the race and cannot be satisfied by luck: whichever parity the two calls land
    on, the pair is inconsistent.
    """
    flapping = {"ok": False}

    def probe():
        flapping["ok"] = not flapping["ok"]
        return (registry.Availability(True) if flapping["ok"]
                else registry.Availability(False, "the device just went away"))

    runner = registry.Runner(
        code="flapping-text",
        capability=registry.TEXT_GENERATION,
        # A real folder, so `available()` gets past its is-the-worker-built check
        # and reaches the probe this test is about.
        folder=os.path.join(registry.RUNNERS_DIR, "llamacpp_text"),
        label="Flapping", short_label="Flapping",
        _available=probe,
    )
    monkeypatch.setattr(registry, "_RUNNERS", (runner,))
    monkeypatch.setattr(registry, "preferred_code", lambda capability: registry.AUTO)

    for row in registry.describe_engines():
        for choice in row["choices"]:
            assert (choice["available"] is False) == (choice["reason"] is not None), choice


def test_the_cpu_rows_name_the_apple_silicon_GPU_they_run_on(monkeypatch):
    """The Engines tab must not contradict the card beside it.

    `torch_image._place()` moves the pipeline to `mps` and `llama_text.load()`
    reports device "gpu" when the Metal backend takes the layers — which is the
    whole point of a `whl/cpu`-style pin resolving darwin to a wheel with Metal
    in it: these rows are what a Mac falls back to when MLX is unavailable
    (AI-2b). So a note reading "Runs on the CPU on any machine, at a few words a
    second" printed a CPU speed claim under the picker while the loaded card
    reported device `mps`, on the exact machine the fallback exists for.

    The `code`, the `label` and the `short_label` are deliberately NOT what
    changed: a stored engine preference keys on `code` (D381), and "(CPU)" names
    the BUILD — the install with no accelerator libraries in it — which is the
    identity AI-2c requires a hardware variant to carry in both names.

    `transformers-text` was the third row here and the one this test was written
    for (D382); it went at D416, and the property outlived it because it was
    never about torch. `llamacpp-text` took its place in the list, which is the
    check that matters: it is now the row a Mac with no MLX falls back to.
    """
    for code, cap in (("llamacpp-text", 90), ("diffusers-image", 100)):
        runner = registry.by_code(code)
        assert runner.label == runner.short_label
        assert "(CPU)" in runner.label
        note = runner.note
        assert "Apple Silicon" in note, (code, note)
        # ONE OR TWO SHORT SENTENCES is the constraint the field documents.
        # The caps track the current notes (68 and 82 chars) with roughly 20
        # chars of headroom each — generous enough that a small rewording
        # doesn't trip the test, tight enough to still catch a note that
        # grows back into a paragraph.
        assert len(note) <= cap, (code, len(note), note)


def test_the_rocm_image_row_warns_that_a_render_can_stall_the_desktop():
    """The ROCm note names the desktop risk within its two-sentence budget.

    Observed rather than theorised (D383): a sustained submission on an RX 9060
    XT (gfx1200) starved `gfx_0.0.0` until the driver reset the ring, and the
    process the kernel named was the COMPOSITOR — the desktop died while the GPU
    itself recovered without a reboot. Compute and display share that ring on a
    single-GPU machine, so "seconds per image" was true and incomplete: the row
    promised the speed and said nothing about what paying for it can cost.

    Pinned because it is the kind of clause a later tidy-up deletes as hedging.
    130 gives the current 109-char note real headroom (the CPU rows' caps
    above are the same idea, ~20 chars over their own notes) rather than the
    old 110 cap, which left this note ONE character of room — not a budget,
    a trip wire.
    """
    runner = registry.by_code("diffusers-image-rocm")
    assert "desktop" in runner.note, runner.note
    assert len(runner.note) <= 130, (len(runner.note), runner.note)


def test_every_test_this_module_cites_by_name_exists(monkeypatch):
    """The registry's comments name tests as their evidence, and a rename is silent.

    `_RUNNERS` explains its own ordering by pointing at the test that pins it —
    which is the right way to write that comment and the reason it rots: the
    citation was `test_auto_stays_on_the_cpu_row_even_with_an_accelerator` while
    the test is `test_AUTO_STAYS_ON_THE_CPU_ROW_EVEN_WITH_AN_ACCELERATOR`, so a
    case-sensitive grep for the evidence found nothing and a reader had to take
    the comment's word for it. Cheap to check, and it makes the citation a link
    rather than a claim.
    """
    import glob as _glob
    import inspect

    cited = set(re.findall(r"`(test_[A-Za-z0-9_]+)`", inspect.getsource(registry)))
    assert cited, "the citation style went away — delete this test with it"
    # Every test module, not this one: a registry comment may cite the test that
    # actually holds the fact wherever it lives (the loaded-card names live
    # in a test of their own), and a citation is only worth checking if the check
    # looks where the reader would.
    defined = set()
    for path in _glob.glob(os.path.join(os.path.dirname(__file__), "test_*.py")):
        with open(path, encoding="utf-8") as handle:
            defined.update(re.findall(r"^def (test_[A-Za-z0-9_]+)", handle.read(),
                                      re.MULTILINE))
    assert not sorted(cited - defined), sorted(cited - defined)


def test_the_accelerated_engines_share_their_siblings_suggestions(monkeypatch, tmp_path):
    """A variant's shortlist is its CPU sibling's, BY CONSTRUCTION.

    The lists in `catalog.py` are per runner because a repo belongs to a
    BACKEND — an MLX conversion is unloadable by torch. A CUDA build of torch
    reads exactly what the CPU build reads, so these rows must not have their own
    list to drift: `_SHARED_SUGGESTIONS` aliases them, and this is the assertion
    that the alias is wired rather than the lists merely being equal today.
    """
    assert catalog.for_runner("llamacpp-text-vulkan") == catalog.SUGGESTIONS["llamacpp-text"]
    assert catalog.for_runner("diffusers-image-cuda") == catalog.SUGGESTIONS["diffusers-image"]
    assert catalog.for_runner("diffusers-image-rocm") == catalog.SUGGESTIONS["diffusers-image"]
    assert catalog.for_runner("onnx-embed-cuda") == (
        catalog.SUGGESTIONS["onnx-embed"])
    assert catalog.for_runner("onnx-embed-rocm") == (
        catalog.SUGGESTIONS["onnx-embed"])
    # And through the resolution, which is how the page actually reaches it.
    _fake_nvidia(monkeypatch, tmp_path)
    _prefer(monkeypatch, registry.IMAGE_GENERATION, "diffusers-image-cuda")
    assert catalog.for_capability(registry.IMAGE_GENERATION) == (
        catalog.SUGGESTIONS["diffusers-image"])
    assert catalog.default_for(registry.IMAGE_GENERATION) == (
        catalog.SUGGESTIONS["diffusers-image"][0]["id"])


def test_no_hardware_variant_holds_its_own_suggestion_list():
    """The other half of the alias, and the one that catches a "fix".

    Somebody adding a fifth variant will reach for a copied literal — it is the
    obvious thing to do, and it passes every other test in this file. What it
    breaks is silent: the two lists agree until one is edited, and then one
    engine recommends a model the other does not, for no reason a reader of the
    page could work out.
    """
    for variant, source in catalog._SHARED_SUGGESTIONS.items():
        assert variant not in catalog.SUGGESTIONS, (
            f"{variant} holds its own copy of {source}'s list — alias it in "
            f"_SHARED_SUGGESTIONS instead, so the two cannot drift")
        assert source in catalog.SUGGESTIONS, source
        assert registry.by_code(variant) is not None, variant
        assert registry.by_code(variant).capability == registry.by_code(source).capability


def test_llamacpp_and_whisper_suggestions_show_snapshot_size_estimates():
    """The `size_gb` column, pinned by value on the two lists whose numbers came
    from summing real Hub blob metadata rather than from a parameter count.

    It read the `transformers-text` list until D416 — the bf16 Qwen entries at
    9.3 to 19.3GB — and now reads `llamacpp-text`, whose figures are the single
    GGUF file each id resolves to. That the numbers are a quarter to a third of
    what they replaced is the measurement D416 rests on, sitting here as data.

    The llamacpp figures moved again with the four-family refresh, and the one
    worth reading twice is position 0: 0.7GB, against the 9.3GB a bare call
    fetched two changes ago.
    """
    expected = {
        "LFM2.5-1.2B-Instruct-Q4_K_M.gguf": 0.7,
        "Qwen3.5-4B-Q4_K_M.gguf": 2.7,
        "gemma-4-E4B-it-Q4_K_M.gguf": 5.0,
        "LFM2.5-8B-A1B-Q4_K_M.gguf": 5.2,
        "Qwen3.8-27B-UD-Q3_K_XL.gguf": 13.1,
        "deepdml/faster-whisper-large-v3-turbo-ct2": 1.6,
        "Systran/faster-whisper-tiny.en": 0.08,
        "Systran/faster-whisper-small": 0.5,
    }
    actual = {
        model["id"]: model["size_gb"]
        for runner in ("llamacpp-text", "faster-whisper")
        for model in catalog.SUGGESTIONS[runner]
    }
    assert actual == expected


def test_every_suggestion_list_is_ordered_smallest_first():
    """One ordering rule, and the default is whatever it puts at position 0.

    The user was shown the trade — a bare `fused.ai.transcribe()` now loads
    `Systran/faster-whisper-tiny.en` rather than the turbo model — and chose one
    rule over a separate default field. So this is the rule, asserted rather
    than left to the eye: sorted by ascending `size_gb`, with an entry that has
    no size sorting LAST (an unknown download must never lead a list, since
    leading it means being what a no-model call silently starts).
    """
    for code, entries in catalog.SUGGESTIONS.items():
        keys = [(e["size_gb"] is None, e["size_gb"] or 0.0) for e in entries]
        assert keys == sorted(keys), (
            f"{code} is not smallest-first: "
            f"{[(e['id'], e['size_gb']) for e in entries]}")


def test_every_suggestion_list_recommends_exactly_one_model():
    """One per list, which is one per capability AND engine (D425).

    Both bounds are load-bearing and they fail differently. NONE leaves the
    Playground's group empty on a machine that has downloaded nothing — the one
    outcome that filter must not be able to produce. TWO puts a comparison back
    in front of the reader who came to type a sentence, which is the whole thing
    the flag was cut down to prevent.

    Asserted per runner rather than in total, because a list is what ONE machine
    sees: a Mac reads `mlx-text` and nothing else, and a total would let a
    Windows-only list go unmarked behind a well-marked Apple one.
    """
    for code, entries in catalog.SUGGESTIONS.items():
        marked = [e["id"] for e in entries if e.get("recommended")]
        assert len(marked) == 1, (
            f"{code} recommends {len(marked)} models ({marked}); the Playground "
            f"offers exactly one per engine, out of {[e['id'] for e in entries]}")


def test_recommended_is_written_opt_in_and_never_as_a_false():
    """`recommended` is present-and-True or absent, never `False` in the source.

    Two ways to write "no" is how a curator ends up believing one of them means
    something else — the route normalises absence to `False` on the wire
    (`_catalog_with_downloads`), which is where the bool a consumer filters on
    comes from, so the literal in this file has exactly one job.
    """
    for code, entries in catalog.SUGGESTIONS.items():
        for entry in entries:
            if "recommended" in entry:
                assert entry["recommended"] is True, (
                    f"{code}/{entry['id']} writes recommended={entry['recommended']!r}; "
                    "leave the key out instead")


def test_the_default_is_the_smallest_model_the_active_runner_offers(monkeypatch):
    """`default_for` is position 0, and position 0 is the smallest — end to end.

    Named per capability because these are the ids a no-model call reaches for,
    and the whole point of the change is that they are now the SMALL ones.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    assert catalog.default_for(registry.TEXT_GENERATION) == \
        "mlx-community/LFM2.5-1.2B-Instruct-4bit"
    # tiny.en is English-only, and it still leads: the one-rule trade above was
    # chosen with its cost in view, and an entry is added to the list because
    # it is worth OFFERING, not because it should be default-proof.
    assert catalog.default_for(registry.SPEECH_TO_TEXT) == \
        "mlx-community/whisper-tiny.en-8bit"

    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    assert catalog.default_for(registry.TEXT_GENERATION) == \
        "LFM2.5-1.2B-Instruct-Q4_K_M.gguf"
    assert catalog.default_for(registry.SPEECH_TO_TEXT) == \
        "Systran/faster-whisper-tiny.en"


def test_the_catalog_follows_the_runner_that_would_actually_load(monkeypatch):
    """A Windows machine must not be shown MLX repos, or told it has no runner.

    Both halves were one bug: `describe()` took the FIRST runner registered for
    a capability regardless of whether it could run, so with MLX listed above the
    cross-platform row a Windows box would have read "needs Apple Silicon" under
    a heading whose four suggestions were all Metal-packed checkpoints it could
    not load — while a runner sat ready to serve it.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    text = next(row for row in catalog.describe()
                if row["capability"] == registry.TEXT_GENERATION)
    assert text["available"] is True and text["reason"] is None
    assert text["runner"] == "llamacpp-text"
    assert text["runnerLabel"] == "llama.cpp (CPU)"
    # Both names travel, and the Discover heading uses the short one — which on
    # a hardware variant is the same string, because the accelerator is part of
    # the engine's identity rather than a platform note.
    assert text["runnerShortLabel"] == "llama.cpp (CPU)"
    assert not any(m["id"].startswith("mlx-community/") for m in text["models"])
    # …and the default a bare `fused.ai.image()`-style call would reach for is
    # the loadable one, not the first entry of some other machine's list.
    assert catalog.default_for(registry.TEXT_GENERATION) == text["models"][0]["id"]

    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    text = next(row for row in catalog.describe()
                if row["capability"] == registry.TEXT_GENERATION)
    assert text["runner"] == "mlx-text"
    # The namespace set is a PROXY for "these are MLX conversions", and it
    # grows: `prism-ml/` was added with the Bonsai row and `LiquidAI/` with the
    # 8B-A1B, both because no `mlx-community/` conversion of them exists. What
    # the assertion is really pinning is the Windows half above — that the two
    # lists are disjoint — so a new publisher belongs here rather than being a
    # reason to weaken it.
    assert all(m["id"].startswith(("mlx-community/", "prism-ml/", "LiquidAI/"))
               for m in text["models"])


def test_the_cpu_warning_reaches_the_page(monkeypatch):
    """The CPU runner says what using it is LIKE, and the catalog carries it.

    The CPU row is the default off Apple Silicon, so a model that works and
    answers at walking pace is the ORDINARY outcome now rather than a Windows
    quirk — which is exactly why the sentence has to be there. Nothing else on
    the page can say it: the device a model really got is a measurement that
    does not exist until one has loaded.

    The sentence is rendered under that engine's row on the Engines tab (D315),
    not over the Discover sections it used to head; this asserts the CATALOG
    still carries it, which is the contract that does not depend on where a
    page prints it.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    text = next(row for row in catalog.describe()
                if row["capability"] == registry.TEXT_GENERATION)
    assert text["runnerNote"] and "CPU" in text["runnerNote"]
    # A standing fact about the BACKEND, never a claim about this machine — the
    # note must not assert a device nothing has measured yet.
    assert "GPU" in text["runnerNote"]


def test_speech_recognition_is_a_capability_something_here_serves(monkeypatch):
    """The glossary move is the whole feature from the page's point of view.

    "speech recognition" is the label `ai_models` puts on a Whisper repo, and
    while it sat in `NO_RUNNER_YET` every one of those cards showed no Load
    button on every machine. It resolves to a capability now — and to a runner
    on ALL of them, unlike text generation, which is the reason the runner is
    CTranslate2 rather than MLX.
    """
    reading = ai_tasks.classify("automatic-speech-recognition")
    assert reading.capability == registry.SPEECH_TO_TEXT
    assert reading.supported and not reading.ruled_out
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    runner = registry.for_capability(registry.SPEECH_TO_TEXT)
    assert runner is not None and runner.code == "faster-whisper"


def test_the_registry_describes_the_transcription_runner():
    rows = {row["code"]: row for row in registry.describe()}
    assert rows["faster-whisper"]["capability"] == registry.SPEECH_TO_TEXT


def test_llamacpp_text_is_registered_directly_below_mlx_text(monkeypatch):
    """`llamacpp-text` is SECOND, so it is the `auto` answer everywhere MLX
    cannot run — and MLX still wins on the platform it was built for.

    **This test asserted the opposite until D416 and the inversion is the
    decision, not a fixture repair.** It was
    `test_llamacpp_text_is_registered_below_every_transformers_row`: this row sat
    fourth precisely so `auto` could never reach it, because
    `llamacpp_text/pyproject.toml` records that the maintainer's wheel index is
    a coin-flip per release on macOS arm64 and a capability that fragile to
    install is a poor default. Removing the three rows above it removed the
    thing that made "never a fallthrough" achievable; D416 weighed that against
    a measurement this engine won on every axis (4.2x transformers' throughput
    on a Radeon GPU, 2.4x on CPU, a third of the download, a third of the peak
    RSS) and moved the default. macOS arm64 — where the audit's failures were —
    still resolves to `mlx-text` first, which is asserted below and is half of
    why the trade was acceptable.

    Position is checked directly, rather than only inferred from behaviour,
    because the ordering is invisible in a diff of the table (the same argument
    `test_AUTO_STAYS_ON_THE_UNACCELERATED_ROW_EVEN_WITH_AN_ACCELERATOR`'s
    docstring makes about the Diffusers split) and nothing else fails when a row
    moves one line up.
    """
    codes = [r.code for r in registry.all_runners() if r.capability == registry.TEXT_GENERATION]
    assert codes.index("llamacpp-text") == codes.index("mlx-text") + 1

    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(registry, "preferred_code", lambda capability: registry.AUTO)
    assert registry.for_capability(registry.TEXT_GENERATION).code == "llamacpp-text"

    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    assert registry.for_capability(registry.TEXT_GENERATION).code == "mlx-text"


def test_llamacpp_text_is_reachable_only_by_an_explicit_preference(monkeypatch):
    """The other half of "opt-in": `_always` means it CAN run everywhere, and a
    preference naming it is honoured exactly like any other runner's — the
    registered position only keeps AUTO off it, never a deliberate choice."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    _prefer(monkeypatch, registry.TEXT_GENERATION, "llamacpp-text")
    resolution = registry.resolve(registry.TEXT_GENERATION)
    assert resolution.runner.code == "llamacpp-text" and resolution.honoured


def test_llamacpp_text_suggestions_are_smallest_first_and_curated_by_filename():
    """Every id here is the GGUF's own filename, not a Hub repo id — see
    `runners/llama_text.py`'s module docstring for why there is no
    `repo:quant` grammar. `test_every_suggestion_list_is_ordered_smallest_first`
    already pins the ordering rule generically; this pins the SHAPE that is
    specific to this runner's list."""
    entries = catalog.SUGGESTIONS["llamacpp-text"]
    assert len(entries) >= 3
    for entry in entries:
        assert entry["id"].endswith(".gguf"), entry["id"]
        assert "/" not in entry["id"], (
            f"{entry['id']} looks like a Hub repo id — this list's ids are "
            f"curated filenames, never repo ids")
    codes = [r.code for r in registry.all_runners() if r.capability == registry.TEXT_GENERATION]
    assert "llamacpp-text" in codes


def test_llamacpp_text_default_is_reached_only_once_selected(monkeypatch):
    """`default_for` follows the RESOLVED runner (`catalog._runner_for`), so it
    only reaches this list once a preference actually selects the engine —
    the same mechanism `test_the_default_is_the_smallest_model_the_active_runner_offers`
    exercises for the other runners."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    _prefer(monkeypatch, registry.TEXT_GENERATION, "llamacpp-text")
    entries = catalog.SUGGESTIONS["llamacpp-text"]
    assert catalog.default_for(registry.TEXT_GENERATION) == entries[0]["id"]


def test_llamacpp_text_vulkan_is_registered_immediately_below_llamacpp_text(monkeypatch):
    """The Vulkan variant sits directly after the CPU/Metal row, and it is LAST —
    so `auto` never reaches it on any platform, and reaching it is always a
    choice made on the Engines tab.

    This is now the whole of the "never a fallthrough" property for text
    generation: D416 gave `llamacpp-text` the default and deliberately did not
    give it to this row, whose `_offload_schedule` backoff is known not to engage
    on AMD (see the row's own comment and PR #706 — radv satisfies an
    over-commit by evicting other clients, which took a desktop session down
    during testing). An over-large model on the row above costs a slow load; on
    this row it can cost a session, which is not a thing to hand a machine that
    did not ask for it.
    """
    codes = [r.code for r in registry.all_runners() if r.capability == registry.TEXT_GENERATION]
    assert codes.index("llamacpp-text-vulkan") == codes.index("llamacpp-text") + 1
    assert codes[-1] == "llamacpp-text-vulkan"

    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(registry, "preferred_code", lambda capability: registry.AUTO)
    assert registry.for_capability(registry.TEXT_GENERATION).code == "llamacpp-text"


def test_the_embeddings_capability_orders_mlx_then_onnx_then_the_accelerated_rows(monkeypatch):
    """Embeddings' five rows, pinned in full — the whole family shares one
    ordering rule with the image and text families, and it is invisible in a diff
    of the table.

    MLX takes the Macs (`_apple_silicon`), `onnx-embed` is the cross-platform
    default and the Apple-Silicon fallback, and `onnx-embed-directml`/`-cuda`/
    `-rocm` are opt-in accelerated siblings of that row — DirectML first because
    it is the only one Windows can take, then CUDA before ROCm, the same order
    `diffusers-image-cuda`/`-rocm` use. All three accelerated rows sit LAST so
    `auto` never reaches any of them on any platform, exactly as
    `test_llamacpp_text_vulkan_is_registered_immediately_below_llamacpp_text`
    pins for text generation's own accelerated tail.

    There were three `transformers-embed*` rows between MLX and these until the
    parity gate (`tests/test_ai_onnx_embed_real_weights.py`) showed both engines
    produce the same vectors; they went with the torch wheel they existed to
    install, and nothing moved to close the gap.
    """
    codes = [r.code for r in registry.all_runners() if r.capability == registry.EMBEDDINGS]
    assert codes == [
        "mlx-embed",
        "onnx-embed", "onnx-embed-directml", "onnx-embed-cuda", "onnx-embed-rocm",
    ]

    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(registry, "preferred_code", lambda capability: registry.AUTO)
    assert registry.for_capability(registry.EMBEDDINGS).code == "onnx-embed"


def test_llamacpp_text_vulkans_platform_gate_matches_its_published_wheel_tags(monkeypatch, tmp_path):
    """`_vulkan`, pinned directly — the maintainer's vulkan index publishes
    `manylinux2014_x86_64` and `win_amd64` for `0.3.29` and NOTHING else: no
    macOS build (Apple Silicon already gets acceleration through the CPU
    index's Metal-linked wheel), no Linux aarch64, no Windows ARM64.

    A working loader and ICD are faked via the same `_fake_vulkan` fixture
    `test_vulkan_needs_a_registered_icd_even_with_a_working_loader` and its
    neighbours use, so this only pins the ARCHITECTURE half of the gate.
    """
    for system, machine in (("Windows", "AMD64"), ("Linux", "x86_64")):
        monkeypatch.setattr(registry.platform, "system", lambda s=system: s)
        monkeypatch.setattr(registry.platform, "machine", lambda m=machine: m)
        _fake_vulkan(monkeypatch, tmp_path, system)
        status = registry.by_code("llamacpp-text-vulkan").available()
        assert status.ok is True, (system, machine, status.reason)

    for system, machine in (
        ("Darwin", "arm64"), ("Linux", "aarch64"), ("Windows", "ARM64"),
    ):
        monkeypatch.setattr(registry.platform, "system", lambda s=system: s)
        monkeypatch.setattr(registry.platform, "machine", lambda m=machine: m)
        status = registry.by_code("llamacpp-text-vulkan").available()
        assert status.ok is False, (system, machine)
        assert "x86_64" in status.reason


def _fake_vulkan(monkeypatch, tmp_path, system, *, loader=True, icd=True):
    """A machine with a working Vulkan loader and (optionally) a registered
    ICD — real files under `tmp_path`, repointed onto the module constants
    `_vulkan` reads, the same style `test_windows_gates_cuda_on_the_drivers_own_cuda_library`
    uses for `NVCUDA_DLL` rather than a global `os.path` patch.
    """
    if system == "Windows":
        dll = tmp_path / "vulkan-1.dll"
        if loader:
            dll.write_text("")
        monkeypatch.setattr(registry, "VULKAN_DLL", str(dll))
        return
    loader_path = tmp_path / "libvulkan.so.1"
    if loader:
        loader_path.write_text("")
    monkeypatch.setattr(registry, "VULKAN_LOADER_PATHS", (str(loader_path),))
    icd_dir = tmp_path / "icd.d"
    icd_dir.mkdir(exist_ok=True)
    if icd:
        (icd_dir / "fake_icd.json").write_text("")
    monkeypatch.setattr(registry, "VULKAN_ICD_DIRS", (str(icd_dir),))


def test_vulkan_needs_the_loader_even_before_a_gpu_is_asked_about(monkeypatch, tmp_path):
    """The hard-failure half: `libggml-vulkan.so`/`ggml-vulkan.dll` link the
    loader directly (`DT_NEEDED libvulkan.so.1` / a PE import on
    `vulkan-1.dll`, read off the actual `0.3.29` wheels on 2026-08-21), so a
    missing loader fails `import llama_cpp` itself — refused here rather than
    left to that import error, the same reasoning `_cuda`'s missing-device
    case documents for why ITS checks exist at all.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    _fake_vulkan(monkeypatch, tmp_path, "Linux", loader=False)
    status = registry.by_code("llamacpp-text-vulkan").available()
    assert status.ok is False
    assert "loader" in status.reason

    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    _fake_vulkan(monkeypatch, tmp_path, "Windows", loader=False)
    status = registry.by_code("llamacpp-text-vulkan").available()
    assert status.ok is False
    assert "Vulkan driver" in status.reason


def test_vulkan_needs_a_registered_icd_even_with_a_working_loader(monkeypatch, tmp_path):
    """The advisory half, on Linux only — Windows has no equivalent manifest
    directory this module checks, per `_vulkan`'s own docstring, so the DLL
    check stands in for both halves there.

    A loader with no registered driver is not a load failure (ggml's own
    bundled CPU backend answers instead), but it IS the "advertising a claim
    that buys nothing" case `_cuda`/`_rocm`'s device checks already refuse —
    an 8x larger download for the exact CPU outcome `llamacpp-text` already
    gives more cheaply.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    _fake_vulkan(monkeypatch, tmp_path, "Linux", loader=True, icd=False)
    status = registry.by_code("llamacpp-text-vulkan").available()
    assert status.ok is False
    assert "driver" in status.reason


def test_llamacpp_text_vulkan_is_reachable_only_by_an_explicit_preference(monkeypatch, tmp_path):
    """Opt-in, UNLIKE its neighbour since D416: a working loader and ICD make
    this row AVAILABLE, and `auto` still resolves to `llamacpp-text` above it —
    reaching this one is a choice."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    _fake_vulkan(monkeypatch, tmp_path, "Linux")
    _prefer(monkeypatch, registry.TEXT_GENERATION, "llamacpp-text-vulkan")
    resolution = registry.resolve(registry.TEXT_GENERATION)
    assert resolution.runner.code == "llamacpp-text-vulkan" and resolution.honoured


def test_llamacpp_text_vulkan_shares_its_neighbours_suggestion_list():
    """`_SHARED_SUGGESTIONS` aliases this row to `llamacpp-text`'s curated
    list, the same way the CUDA/ROCm Diffusers rows share theirs — one
    GGUF is one GGUF whichever wheel loads it, so a second copy of the list
    would be the exact drift `_SHARED_SUGGESTIONS`'s own docstring warns
    about."""
    assert catalog.for_runner("llamacpp-text-vulkan") == catalog.SUGGESTIONS["llamacpp-text"]


def test_no_runner_declares_a_dependency_that_has_to_be_BUILT():
    """Wheels only — a VCS or URL dependency is a source build, and a source
    build is a build backend running in an interpreter uv creates for it.

    `diffusers` tracked git while FLUX.2 lived only on main, and inside the
    packaged macOS app every one of those builds died: the build interpreter
    inherited the bundle's `PYTHONHOME`, imported the app's frozen
    `_distutils_hack` in place of the shipped one, and failed with
    `No module named 'jaraco.text'` (D266). `_env_install_worker` scrubs the env
    now, so this is not the only thing standing between a user and that bug —
    but a runner environment is built on someone's laptop the first time they
    press Download, and compiling from source there is a cost with no upside
    when a wheel exists. If a runner ever genuinely needs one, this test is the
    conversation.

    Read through `projectenv.dependencies_of` — the app's own manifest reader,
    which is markers-and-all verbatim (what a packaging invariant wants) and
    which still carries a `tomli` fallback, now unreachable: `requires-python`
    is >=3.11, so `tomllib` is always there.
    """
    from fused_render import projectenv

    for runner in registry.all_runners():
        assert os.path.isfile(runner.pyproject), runner.code
        declared = projectenv.dependencies_of(runner.folder)
        assert declared, f"{runner.code} declares nothing — did the manifest move?"
        for dependency in declared:
            assert " @ " not in dependency, (
                f"{runner.code} declares a source build: {dependency}")


def test_the_mflux_runner_BOUNDS_mflux():
    """`mflux` was declared with no version at all, and the `uv.lock` beside it
    is gitignored — so every environment provisioned after mflux 0.19.0 shipped
    re-resolved 0.18.1 -> 0.19.0, whose own `mlx>=0.32,<0.33` bound pulled in the
    mlx release that made default streams per-thread. Local image generation
    started aborting on its first denoising step with no commit behind it
    (`mflux_image/worker.py::_pin_stream`).

    The EXACT string, not merely "has a ceiling" like the test below: this is the
    one bound with a reproduced abort behind it, and the pair of numbers is the
    fact itself. `test_every_runner_BOUNDS_its_model_runtimes` is the
    general rule this incident produced; this stays as its worked example.
    """
    from fused_render import projectenv

    runner = next(r for r in registry.all_runners() if r.code == "mflux-image")
    declared = projectenv.dependencies_of(runner.folder)
    bounds = [d for d in declared if d.replace(" ", "").startswith("mflux")]
    assert bounds, f"mflux is not declared at all: {declared}"
    assert bounds[0].replace(" ", "") == "mflux>=0.19,<0.20", bounds


#: Runner dependencies allowed to stay open-ended, and what each one buys by it.
#:
#: **An ALLOW-LIST, and the direction is the whole design.** A table of packages
#: that MUST be bounded could only ever protect the libraries somebody already
#: thought about; the next `mflux` would be added to a manifest, resolve freely on
#: every new venv key, and the suite would stay green. Inverting it means an
#: unrecognised dependency is a FAILING one until a person either bounds it or
#: writes down here why it does not need bounding — which is the review that was
#: missing when `mflux` went in unbounded.
#:
#: What earns a place: a package whose next major cannot change what a model
#: computes or which runtime it computes on. Downloaders, codecs, tokenizer
#: formats and instrumentation qualify. Inference engines and model runtimes do
#: not — those are the ones whose transitive pins moved under us.
UNBOUNDED_RUNNER_DEPENDENCIES = {
    "huggingface-hub": "a download client; it fetches weights, it does not run them",
    "psutil": "reads RSS for the memory column and nothing else",
    "pillow": "writes the PNG",
    "sentencepiece": "a tokenizer file format, fixed by the checkpoints that use it",
    "protobuf": "sentencepiece's on-disk format, same argument",
    "jinja2": (
        "renders the chat template around a prompt; it is a general templating "
        "library with no notion of a model or a tensor, and its next major "
        "cannot change what a checkpoint computes"
    ),
    "av": "the ffmpeg libraries, for decoding to a waveform — not inference",
    "gguf": "a quantized-weight FILE reader; the tensors it returns are diffusers'",
    "triton-rocm": (
        "ROCm torch pins the version itself — `torch 2.13.0+rocm7.1` carries "
        "`Requires-Dist: triton-rocm==3.7.1` — so a ceiling here could only "
        "contradict the wheel that chose it. The ROCm manifests declare it at "
        "all only because `[tool.uv.sources]` cannot route a TRANSITIVE "
        "requirement to the ROCm index, and there is no `triton-rocm` on PyPI "
        "for uv to fall back to; the bound on `torch` therefore already governs "
        "it, exactly as `sherpa-onnx`'s does for the entry below"
    ),
    "sherpa-onnx-core": (
        "carries libonnxruntime for sherpa-onnx and is version-locked to it by "
        "sherpa's own `Requires-Dist: sherpa-onnx-core==<same version>`, so the "
        "bound on `sherpa-onnx` already governs both"
    ),
}


_FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _uv_source_for(project_dir: str, name: str) -> dict | None:
    """The `[tool.uv.sources]` entry routing `name`, or None.

    A version specifier means nothing for a dependency `uv` resolves off a git
    checkout instead of PyPI — `ltx-core-mlx`/`ltx-pipelines-mlx` carry no
    operator at all in `[project].dependencies` (there is no PyPI release to
    version against), so the BOUNDS test has to look here instead to decide
    whether the requirement is pinned.
    """
    from fused_render import projectenv

    meta = projectenv._load_manifest(project_dir)
    if not isinstance(meta, dict):
        return None
    sources = meta.get("tool", {}).get("uv", {}).get("sources", {})
    entry = sources.get(name)
    return entry if isinstance(entry, dict) else None


def _git_requirement_is_pinned(project_dir: str, name: str) -> bool:
    """A git-sourced dependency counts as BOUNDED only pinned to a full commit.

    A branch (`rev = "main"`), a tag that can be force-moved, or a bare git
    URL with no `rev` at all are each exactly the unbounded-requirement risk
    this suite exists to catch — `uv sync` re-resolves HEAD every time a venv
    key changes, with no diff anywhere to explain what changed. A 40-hex `rev`
    is the one form that cannot move under a user without a new commit to
    this file naming it.
    """
    source = _uv_source_for(project_dir, name)
    if source is None or "git" not in source:
        return False
    rev = source.get("rev")
    return isinstance(rev, str) and bool(_FULL_GIT_SHA.match(rev))


def test_a_git_dependency_pinned_to_a_full_commit_counts_as_bounded(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["ltx-core-mlx"]\n\n'
        '[tool.uv.sources]\n'
        'ltx-core-mlx = { git = "https://example.com/repo", '
        'rev = "8ebae0a7cb08312fbf884790b91b4d155e714cdc" }\n')
    assert _git_requirement_is_pinned(str(tmp_path), "ltx-core-mlx")


@pytest.mark.parametrize("bad_source", [
    '{ git = "https://example.com/repo", rev = "main" }',
    '{ git = "https://example.com/repo", branch = "main" }',
    '{ git = "https://example.com/repo" }',
], ids=["branch-as-rev", "explicit-branch", "no-rev-at-all"])
def test_a_git_dependency_on_a_branch_or_bare_url_still_fails_the_pin_check(
        tmp_path, bad_source):
    """The half that makes the positive case above safe: a rev that is not a
    40-hex commit — a branch name, or nothing at all — must still read as
    UNbounded, or the check above would rubber-stamp any `[tool.uv.sources]`
    entry rather than the one shape that is actually pinned."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\ndependencies = ["ltx-core-mlx"]\n\n'
        f'[tool.uv.sources]\n'
        f'ltx-core-mlx = {bad_source}\n')
    assert not _git_requirement_is_pinned(str(tmp_path), "ltx-core-mlx")


@pytest.mark.parametrize("runner", registry.all_runners(), ids=lambda r: r.code)
def test_every_runner_BOUNDS_its_model_runtimes(runner):
    """The generalisation of the mflux abort, applied to every manifest.

    None of these folders has a committed `uv.lock` (`.gitignore` ignores them
    globally and negates only `templates/`), and `_env_install_worker` runs a
    BARE `uv sync` on purpose — so an unbounded requirement is not "latest at
    install time", it is *re-resolved from scratch every time a venv key changes*,
    on a user's machine, with no diff anywhere to explain the new behaviour. That
    is how mflux 0.18.1 -> 0.19.0 -> mlx 0.32 arrived and aborted every render.

    A ceiling is asserted rather than an exact string because the numbers are
    supposed to move: bumping one is a normal change with a run behind it. What
    must not move silently is the major/minor, so the failure a new unbounded
    dependency produces is a prompt to decide, not a chore.

    **A git-sourced requirement never carries an operator at all** (there is
    no PyPI release to write `<` against), so it is checked through
    `_git_requirement_is_pinned` instead — a full commit `rev` counts as
    bounded, a branch or a bare URL does not (see the two tests above this
    one, which exercise that half directly against a synthetic manifest).
    """
    from fused_render import projectenv

    for requirement in projectenv.dependencies_of(runner.folder):
        name = requirement.split("[")[0].split(";")[0]
        for operator in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            name = name.split(operator)[0]
        name = name.strip().replace("_", "-").lower()
        if name in UNBOUNDED_RUNNER_DEPENDENCIES:
            continue
        if "<" in requirement or "==" in requirement:
            continue
        if _git_requirement_is_pinned(runner.folder, requirement.strip()):
            continue
        assert False, (
            f"{os.path.relpath(runner.pyproject)} declares `{requirement}` with "
            f"no upper bound. These runners have no committed uv.lock and "
            f"`uv sync` runs bare, so every new venv key re-resolves this from "
            f"PyPI — a major bump lands on users with no commit behind it "
            f"(the mflux abort above). Add a ceiling, pin a git source to a full "
            f"commit `rev`, or add `{name}` to "
            f"UNBOUNDED_RUNNER_DEPENDENCIES in {os.path.basename(__file__)} "
            f"with the reason it cannot change what a model computes.")


def test_the_unbounded_allow_list_is_not_quietly_unused():
    """An entry nothing declares is an exemption that has stopped exempting —
    the dependency dropped, the reason left behind reading as a decision. Same
    rule `test_the_split_table_is_not_quietly_unused` enforces next door."""
    from fused_render import projectenv

    declared = set()
    for runner in registry.all_runners():
        for requirement in projectenv.dependencies_of(runner.folder):
            name = re.split(r"[\[;<>=!~ ]", requirement, 1)[0]
            declared.add(name.strip().replace("_", "-").lower())
    for name in UNBOUNDED_RUNNER_DEPENDENCIES:
        assert name in declared, (
            f"no runner declares {name} any more — delete its exemption rather "
            f"than leaving a rule that cannot fire")


# -- the supervisor -------------------------------------------------------------


def test_a_model_loads_and_reports_its_memory(fake_runner):
    supervisor.load("org/small", registry.TEXT_GENERATION)
    worker = _wait_ready("org/small")
    assert worker.resident_bytes == 1234
    described = supervisor.describe()
    assert described["loaded"][0]["model"] == "org/small"
    assert described["totalResidentBytes"] == 1234


def test_os_footprint_probe_returns_a_plausible_figure_or_none():
    """D597: the live figure's probe. Deliberately does NOT pin a byte count —
    it varies per machine and per moment — only that it answers with something
    usable and that it TRACKS real memory, which is the property RSS failed at
    (172 MB of RSS against 23 GB of Metal buffers on a live FLUX worker).

    Loaded by path because `worker_base` is a runner-side module that normally
    executes inside a runner venv, not as part of the server package's import
    graph.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "wb_probe", "fused_render/ai/runners/worker_base.py")
    wb = importlib.util.module_from_spec(spec)
    sys.modules["wb_probe"] = wb
    spec.loader.exec_module(wb)

    first = wb.os_footprint_bytes()
    # Either a real positive reading or None where no counter exists — never 0,
    # which a UI would render as "holding nothing".
    assert first is None or first > 0

    if first is None:
        return  # no counter on this platform; nothing further to assert

    # THE PROPERTY THAT MATTERS: it must move with genuinely dirtied memory.
    # A probe that reports a constant (or that misses a whole allocator pool,
    # which is exactly what RSS does here) would satisfy the check above and
    # still be useless.
    blob = bytearray(64 * 1024 * 1024)
    for i in range(0, len(blob), 4096):
        blob[i] = 1
    after = wb.os_footprint_bytes()
    assert after is not None
    assert after > first, f"probe did not track a 64 MiB allocation: {first} -> {after}"
    del blob


def test_os_footprint_is_never_below_the_resident_size():
    """CODE REVIEW 2026-08-28, FINDING 3 — the mmap-heavy shape.

    `phys_footprint` EXCLUDES clean file-backed pages, which `resident_size`
    counts, so the footprint alone is the SMALLER of the two for any runner that
    maps its weights read-only (GGUF/llama.cpp, torch with `mmap=True`).
    Measured in a plain interpreter with no framework loaded at all:
    `resident_size` 19.2 MB against `phys_footprint` 9.3 MB — so this is not an
    exotic case, it is the default one.

    Reporting the footprint alone rendered the model row as
    `8.2 GB now (1.1 GB held)` — a pair that reads as a contradiction — and
    painted the status bar's colour band off the smaller of the two numbers,
    the exact false-comfort signal the band exists to prevent. The probe now
    returns `max(footprint, resident)`, so "held" can never come back below
    "now". Asserted against the kernel's OWN `resident_size` where the counter
    exists, so the test cannot pass by measuring the same thing twice.
    """
    import importlib.util
    import struct

    spec = importlib.util.spec_from_file_location(
        "wb_probe_floor", "fused_render/ai/runners/worker_base.py")
    wb = importlib.util.module_from_spec(spec)
    sys.modules["wb_probe_floor"] = wb
    spec.loader.exec_module(wb)

    if sys.platform != "darwin":
        pytest.skip("task_vm_info's resident_size is the darwin path only")

    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.mach_task_self.restype = ctypes.c_uint32
    buf = (ctypes.c_uint8 * 1024)()
    count = ctypes.c_uint32(1024 // 4)
    rc = libc.task_info(
        ctypes.c_uint32(libc.mach_task_self()),
        ctypes.c_int(wb._TASK_VM_INFO),
        ctypes.byref(buf),
        ctypes.byref(count),
    )
    assert rc == 0, "the premise: this machine has task_vm_info"
    raw = bytes(buf)
    resident = struct.unpack_from("<Q", raw, wb._RESIDENT_SIZE_OFFSET)[0]
    footprint = struct.unpack_from("<Q", raw, wb._PHYS_FOOTPRINT_OFFSET)[0]
    assert resident > 0 and footprint > 0

    held = wb.os_footprint_bytes()
    assert held is not None
    # A tolerance rather than equality: the probe takes its own reading a moment
    # after this one, and an interpreter allocates between the two. The claim
    # under test is that it is not the SMALLER counter — the gap it must clear
    # is the size of a model file, so 2 MiB of slack cannot hide it.
    assert held >= min(resident, footprint), "the probe must never report below either counter"
    assert held + 2 * 1024 * 1024 >= resident, (
        f"os_footprint_bytes() {held} came back under resident_size {resident} — "
        "the mmap-heavy shape is back"
    )


def test_os_footprint_falls_back_cleanly_when_no_counter_exists(monkeypatch):
    """The non-macOS path, and any darwin failure: RSS, or None — never a raise
    and never a guess. Forced by claiming a platform with no `task_info`, which
    is what every non-Apple runner genuinely is."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "wb_probe_fallback", "fused_render/ai/runners/worker_base.py")
    wb = importlib.util.module_from_spec(spec)
    sys.modules["wb_probe_fallback"] = wb
    spec.loader.exec_module(wb)

    monkeypatch.setattr(wb.sys, "platform", "linux")
    value = wb.os_footprint_bytes()
    # psutil is absent from the server venv (AI-2), so this legitimately
    # resolves to None here; where psutil IS present it must be a real RSS.
    assert value is None or value > 0


def test_describe_carries_the_os_footprint_without_coercing_null(
        fake_runner, monkeypatch):
    """D597: the new field rides through `describe` (and therefore
    `/api/ai/runtime`) beside `residentBytes` rather than replacing it, and a
    missing reading stays NULL — zero would render as "holding nothing" in the
    one column whose job is to explain memory pressure the user can see.

    The fake runner reports no `os_footprint_bytes`, which is exactly the
    "runner too old / probe unavailable" case.
    """
    supervisor.load("org/small", registry.TEXT_GENERATION)
    _wait_ready("org/small")

    row = supervisor.describe()["loaded"][0]

    assert "osFootprintBytes" in row
    assert row["osFootprintBytes"] is None
    # ...and the field it sits BESIDE is untouched, which is the whole point of
    # this being additive: `residentBytes` still feeds `fit.py`'s measured rung.
    assert row["residentBytes"] == 1234


# The populated path is covered by
# `test_refresh_memory_propagates_a_growing_os_footprint` above, which asserts
# the same thing through the REAL path. The version that used to live here set
# `worker.os_footprint_bytes` directly and then called `describe()` — a premise
# D599 invalidated: `describe()` calls `refresh_memory()`, which now re-reads
# the field from `/health` on every request, so a directly-assigned value is
# correctly overwritten by what the worker actually reports. That the old test
# passed BEFORE the fix and fails after it is the point — it was asserting the
# frozen-value behaviour that was the bug.


def test_describe_carries_the_machine_ceiling_once_not_per_row(fake_runner):
    """D594: the denominator the status bar colours against is a per-machine
    constant, so it rides at the TOP LEVEL rather than being repeated on every
    loaded row."""
    supervisor.load("org/small", registry.TEXT_GENERATION)
    _wait_ready("org/small")
    described = supervisor.describe()

    assert "memoryCeilingBytes" in described
    ceiling = described["memoryCeilingBytes"]
    # Either a real positive reading or None where the machine cannot be read —
    # never 0, which would be a denominator that silently divides wrong.
    assert ceiling is None or ceiling > 0
    assert "memoryCeilingBytes" not in described["loaded"][0]


def test_describe_reports_a_footprint_and_its_basis_per_loaded_model(
        fake_runner, tmp_path, monkeypatch):
    """D594: each row carries what the model COSTS plus which rung of
    `fit.footprint_bytes`' ladder answered — the same vocabulary
    `AiFitVerdict` established, so the status bar and the fit badge cannot
    disagree about the same model.

    Driven through the real measured path (`refresh_memory` is the one writer
    for the footprint store, SPEC AI-16a) rather than by stubbing
    `footprint_bytes`, so this pins the WIRING and not a mock.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    supervisor.load("org/small", registry.TEXT_GENERATION)
    _wait_ready("org/small")
    supervisor.refresh_memory()

    row = supervisor.describe()["loaded"][0]

    # The PEAK (9999), not the instantaneous RSS (1234) — those are different
    # numbers on purpose, and the row now reports both in their own fields:
    # peak as the cost, RSS as the live reading.
    assert row["footprintBytes"] == 9999
    assert row["footprintBasis"] == "measured"
    assert row["residentBytes"] == 1234


def test_an_unmeasured_model_reports_null_not_zero(fake_runner, monkeypatch):
    """THE NULL-NOT-ZERO RULE, which the whole payload follows and which this
    field needs most: a model with nothing measured and nothing declared has NO
    cost figure. Zero would be a lie the page would happily colour green, so
    the row must say null and let it fall back to RSS alone, uncoloured.

    `footprint_bytes` is stubbed to its own documented "nothing to report"
    answer rather than the store being left empty: the ladder has THREE rungs
    and a catalog entry can satisfy the lower two, so an empty store does not
    reliably produce a null (it did not — the fixture's peak came back through
    the measured rung). Stubbing the ladder's ANSWER is what isolates the
    contract this test is about, which is that `describe` passes a null
    through as null instead of coercing it.
    """
    from fused_render.ai import fit

    monkeypatch.setattr(fit, "footprint_bytes", lambda *a, **kw: (None, None))
    supervisor.load("org/small", registry.TEXT_GENERATION)
    _wait_ready("org/small")

    row = supervisor.describe()["loaded"][0]

    assert row["footprintBytes"] is None
    assert row["footprintBasis"] is None
    # ...and the live RSS is still there, which is what the row falls back to.
    assert row["residentBytes"] == 1234


def _ready_worker():
    """A ready worker in the table. Its `_health` is then put under the test's
    control by the callers below.

    `_health` is the seam because the defect these tests exist for lived in
    `refresh_memory`'s own body, not in any runner: the field was read in the
    LOAD loop and nowhere else, so nothing that only exercised loading could
    see it. Driving `_health` directly is what lets a test assert what happens
    on the polls that come AFTER ready.
    """
    supervisor.load("org/small", registry.TEXT_GENERATION)
    return _wait_ready("org/small")


def test_refresh_memory_propagates_a_growing_os_footprint(fake_runner, tmp_path, monkeypatch):
    """D599, THE DEFECT: `os_footprint_bytes` was assigned only in the load
    loop, which exits the moment the worker reaches `ready`. So the value froze
    at whatever the last load-time poll saw — before MLX had faulted in its
    Metal buffers — and every later poll left it untouched. Live, that showed
    436 MB against a real `phys_footprint` of 24 GB.

    The number MUST be able to grow after load, which is the whole reason the
    field exists, so this asserts a post-ready INCREASE rather than merely that
    the field is populated — the load path already populated it, and that is
    exactly why the omission was invisible.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    worker = _ready_worker()
    worker.os_footprint_bytes = 456_967_248  # the frozen load-time reading

    monkeypatch.setattr(supervisor, "_health", lambda w: {
        "state": "ready",
        "resident_bytes": 1_918_320_888,
        "peak_resident_bytes": 12_912_055_206,
        "os_footprint_bytes": 24_000_000_000,
    })
    supervisor.refresh_memory()

    assert worker.os_footprint_bytes == 24_000_000_000
    assert supervisor.describe()["loaded"][0]["osFootprintBytes"] == 24_000_000_000

    # ADDITIVE ONLY: the durable "measured" store still gets the PEAK, never
    # the OS footprint — that store feeds `fit.py`'s measured rung, and a 24 GB
    # figure landing there would re-verdict every model the user has run.
    from fused_render.ai import footprints
    assert footprints.read(registry.TEXT_GENERATION, "org/small") == 12_912_055_206


def test_refresh_memory_clears_the_os_footprint_when_the_worker_has_no_counter(
        fake_runner, tmp_path, monkeypatch):
    """A worker that ANSWERS and reports no counter (the non-Darwin fallback)
    must clear the cell, not leave a stale number beside a live one. Prefer
    showing nothing over showing something old — this field claims to describe
    RIGHT NOW."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    worker = _ready_worker()
    worker.os_footprint_bytes = 24_000_000_000

    monkeypatch.setattr(supervisor, "_health", lambda w: {
        "state": "ready", "resident_bytes": 1234, "os_footprint_bytes": None,
    })
    supervisor.refresh_memory()

    assert worker.os_footprint_bytes is None
    # ...and the fields that legitimately persist are untouched by that rule.
    assert worker.resident_bytes == 1234


def test_a_failed_poll_leaves_the_last_os_footprint_standing(
        fake_runner, tmp_path, monkeypatch):
    """The OTHER half, and why this is not simply "always assign": a poll that
    failed outright tells us nothing about the worker, so the last known figure
    stands rather than the cell blinking empty on one dropped request. `health`
    falsy is the transient case; `health` present with a null field is the
    definite one above."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    worker = _ready_worker()
    worker.os_footprint_bytes = 24_000_000_000

    monkeypatch.setattr(supervisor, "_health", lambda w: None)
    supervisor.refresh_memory()

    assert worker.os_footprint_bytes == 24_000_000_000


def test_refresh_memory_writes_the_peak_into_footprints(fake_runner, tmp_path, monkeypatch):
    """SPEC AI-16a, D497: the ONE writer for the measured-footprint store is
    `supervisor.refresh_memory`, re-reading `/health` on the same cadence the
    rest of the app already relies on — this is the wiring `fit.py`'s
    "measured" basis depends on existing at all."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    from fused_render.ai import footprints

    supervisor.load("org/small", registry.TEXT_GENERATION)
    worker = _wait_ready("org/small")
    # `_wait_ready` only waits on the BRING-UP loop's own polling, which sets
    # `resident_bytes` directly but not the peak — `refresh_memory` (called by
    # `describe()`, the status endpoint's own path) is the one place that
    # writes the peak at all, exactly as it is in production.
    supervisor.refresh_memory()
    assert worker.peak_resident_bytes == 9999
    assert footprints.read(registry.TEXT_GENERATION, "org/small") == 9999


def test_a_footprint_write_failure_does_not_break_refresh_memory_or_describe(
        fake_runner, monkeypatch):
    """Code review: `footprints.record` calls `storage.write_json`, which can
    raise `OSError` (a full disk, a permissions problem, a home directory that
    went away mid-session). `describe()` calls `refresh_memory()`
    unconditionally, and `describe()` backs `GET /api/ai/runtime` — a status
    route with no business 500ing because a NICE-TO-HAVE observation (a peak
    footprint, remembered for next time's fit verdict) failed to write. The
    resident-bytes reading itself, which the route exists to report, must
    still land."""
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(footprints, "record", _boom)
    supervisor.load("org/small", registry.TEXT_GENERATION)
    worker = _wait_ready("org/small")
    supervisor.refresh_memory()
    assert worker.peak_resident_bytes == 9999
    described = supervisor.describe()
    assert described["loaded"][0]["model"] == "org/small"


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


def test_evicting_a_model_does_not_block_other_calls_while_it_terminates(fake_runner, monkeypatch):
    """B1: `_terminate` can block for ~9s (a `/quit` POST, SIGTERM+wait,
    SIGKILL+wait, `proc.wait`). If the eviction branch in `_start_resident`
    holds `_lock` across that call, every other supervisor call — `describe`,
    `ready_worker`, a health poll, another model's load — queues behind it for
    the whole teardown. This is exactly why a benchmark "Run all" looked frozen
    specifically on its first model: only the first load evicts a previously
    resident worker, so 2..N never hit this path and the freeze looked like it
    was about the first model rather than about eviction."""
    supervisor.load("org/first", registry.TEXT_GENERATION)
    _wait_ready("org/first")

    real_terminate = supervisor._terminate
    started = threading.Event()
    release = threading.Event()

    def slow_terminate(worker):
        started.set()
        release.wait(timeout=5)
        real_terminate(worker)

    monkeypatch.setattr(supervisor, "_terminate", slow_terminate)

    # What a benchmark's second model load does: request a different model for
    # the same capability, which evicts the first.
    t = threading.Thread(
        target=supervisor.load, args=("org/second", registry.TEXT_GENERATION)
    )
    t.start()
    try:
        assert started.wait(timeout=5), "eviction never reached _terminate"

        # While the old worker is (slowly) tearing down, an ordinary read must
        # not queue behind it — that queuing is the bug.
        t0 = time.monotonic()
        supervisor.describe()
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"describe() blocked {elapsed:.1f}s behind _terminate"
    finally:
        release.set()
        t.join(timeout=5)


def test_eviction_still_starts_the_new_worker_when_terminating_the_old_one_raises(
        fake_runner, monkeypatch):
    """The new worker is published into `_workers[capability]` BEFORE
    `_terminate(current)` runs on the evicted one (see the comment in
    `_start_resident`). If that call raises — `_terminate` is best-effort
    internally but not blanket-guarded — the new worker must still get its
    `_bring_up` thread started, or it sits in the table forever in a
    non-`error` state: every later `load()` for this capability takes the
    join branch and hands back that dead record, and `_wait_ready` blocks for
    `LOAD_WAIT_TIMEOUT_S` (an hour)."""
    supervisor.load("org/first", registry.TEXT_GENERATION)
    _wait_ready("org/first")

    real_terminate = supervisor._terminate

    def raising_terminate(worker):
        real_terminate(worker)  # still actually tear the old one down
        raise RuntimeError("boom from a _terminate callee")

    monkeypatch.setattr(supervisor, "_terminate", raising_terminate)

    try:
        supervisor.load("org/second", registry.TEXT_GENERATION)
        worker = _wait_ready("org/second")
        assert worker.model == "org/second"
    finally:
        # `fake_runner`'s own teardown calls `unload()`, which would hit this
        # same raising stub again (and, this time unhandled, break fixture
        # teardown) if it were still installed.
        monkeypatch.setattr(supervisor, "_terminate", real_terminate)


def test_unload_all_waits_for_an_in_progress_eviction_to_finish_draining(
        fake_runner, monkeypatch):
    """The lock-release fix (B1) pops the evicted worker from `_workers`
    before its ~9s `_terminate` runs, so for that window it exists nowhere
    `unload_all` (walking `_workers` at shutdown) would find it — quitting
    the app in that window used to leave the old process running with
    nothing left tracking it, the same orphan-holding-gigabytes failure
    `unload_all`'s own docstring exists to prevent for weights-only fetches.
    `_draining` closes that: `unload_all` must wait for it to clear rather
    than declaring shutdown complete while a worker is still going down."""
    supervisor.load("org/first", registry.TEXT_GENERATION)
    first = _wait_ready("org/first")

    real_terminate = supervisor._terminate
    started = threading.Event()
    release = threading.Event()

    def slow_terminate(worker):
        started.set()
        release.wait(timeout=5)
        real_terminate(worker)

    monkeypatch.setattr(supervisor, "_terminate", slow_terminate)

    t = threading.Thread(
        target=supervisor.load, args=("org/second", registry.TEXT_GENERATION)
    )
    t.start()
    try:
        assert started.wait(timeout=5), "eviction never reached _terminate"
        assert supervisor._draining, "the evicted worker must be visible while it drains"

        done = threading.Event()

        def run_unload_all():
            supervisor.unload_all()
            done.set()

        u = threading.Thread(target=run_unload_all)
        u.start()
        try:
            assert not done.wait(timeout=0.3), (
                "unload_all() returned while an eviction was still draining"
            )
            release.set()
            assert done.wait(timeout=5), "unload_all() never returned once draining finished"
        finally:
            u.join(timeout=5)

        assert not supervisor._draining
        assert not supervisor._alive(first)
    finally:
        release.set()
        t.join(timeout=5)


def test_cancel_check_is_not_tied_to_the_tightened_health_poll_cadence(
        fake_runner, monkeypatch):
    """C1 tightened `_bring_up`'s health-poll sleep from 0.5s to 0.1s for load
    latency — a local loopback GET, cheap to do 5x more often. The cancel
    check sitting beside it is a different call, `_cancel_requested` ->
    `jobs.list_jobs()`, which takes the GLOBAL jobs lock, runs a sweep, and
    `asdict()`s up to `MAX_JOBS` records — contending with every `_report`
    call from every other loading worker. It must stay on its own ~0.5s
    cadence rather than scale 5x alongside the health poll."""
    calls = {"n": 0}
    real_cancel_requested = supervisor._cancel_requested

    def counting(job):
        calls["n"] += 1
        return real_cancel_requested(job)

    monkeypatch.setattr(supervisor, "_cancel_requested", counting)
    monkeypatch.setenv("FAKE_LOAD_SECONDS", "1.0")

    supervisor.load("org/slow-count", registry.TEXT_GENERATION)
    _wait_ready("org/slow-count")

    # ~2-3 calls at the intended 0.5s cadence over a ~1s load; tying it to the
    # tightened 0.1s health-poll cadence would have made it ~10.
    assert calls["n"] <= 5, f"_cancel_requested called {calls['n']} times over a ~1s load"


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
    # The registry is narrowed to the Metal-only runner because since D293 no
    # real capability is unservable on Linux — which is the feature, and which
    # took away the situation this test was written to describe. The BEHAVIOUR
    # under test is unchanged: a load nothing can serve answers with the
    # machine's reason rather than with a job row that dies.
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(registry, "_RUNNERS", (registry.by_code("mlx-text"),))
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


# -- per-worker activity, for the idle reaper (AI-13) ------------------------


def test_a_streamed_chunk_re_stamps_last_activity(fake_runner):
    supervisor.load("org/chat", registry.TEXT_GENERATION)
    worker = _wait_ready("org/chat")
    worker.last_activity = time.monotonic() - 999
    list(supervisor.generate_text("org/chat", {"messages": []}))
    # Every yielded chunk re-stamps, not just entry — a stream running longer
    # than the idle window must never look idle partway through it.
    assert time.monotonic() - worker.last_activity < 5


def test_closing_a_partially_read_stream_frees_in_flight(fake_runner):
    # `generate_text` is a generator: a page that stops iterating without
    # calling `close()` is exactly the leak the `finally` in `_in_use` exists
    # to prevent (see Key decisions).
    supervisor.load("org/chat", registry.TEXT_GENERATION)
    worker = _wait_ready("org/chat")
    stream = supervisor.generate_text("org/chat", {"messages": []})
    next(stream)
    assert worker.in_flight == 1
    stream.close()
    assert worker.in_flight == 0


def test_in_use_nests_without_losing_track(fake_runner):
    supervisor.load("org/chat", registry.TEXT_GENERATION)
    worker = _wait_ready("org/chat")
    with supervisor._in_use(worker):
        with supervisor._in_use(worker):
            assert worker.in_flight == 2
        assert worker.in_flight == 1
    assert worker.in_flight == 0


# -- the idle reaper (AI-13) ------------------------------------------------


def _idle_worker(monkeypatch, *, state="ready", last_activity, in_flight=0,
                 capability=registry.TEXT_GENERATION, model="org/idle"):
    """A `ready` worker planted directly in `_workers`, the same shortcut
    `test_a_resident_worker_of_the_WRONG_ENGINE_is_not_served` uses — no
    process, so `_terminate` is stubbed the same way."""
    monkeypatch.setattr(supervisor, "_terminate", lambda worker: None)
    worker = supervisor.Worker(model=model, capability=capability,
                               runner_code="fake-text", token="t", state=state,
                               last_activity=last_activity, in_flight=in_flight)
    monkeypatch.setitem(supervisor._workers, capability, worker)
    return worker


class _ExitedProc:
    """A Popen whose process has already died — `poll()` answers its exit code."""

    def __init__(self, returncode):
        self.returncode = returncode

    def poll(self):
        return self.returncode


def test_a_ready_worker_whose_process_died_is_dropped_on_the_next_request(monkeypatch):
    """The 502-forever bug. A worker that died while idle — a native SIGSEGV, a
    memory kill — stayed in the table as `ready`, so every request was proxied
    to a dead port and answered `the model process did not answer`, and
    nothing ever respawned it. `ready_worker` now polls the process it is
    about to hand out: gone means dropped, so this request gets
    `ModelNotReady` (a fresh load) instead of a 502."""
    worker = _idle_worker(monkeypatch, last_activity=time.monotonic())
    worker.proc = _ExitedProc(-11)
    assert supervisor.ready_worker(registry.TEXT_GENERATION, "org/idle") is None
    assert registry.TEXT_GENERATION not in supervisor._workers
    assert worker.state == "error"
    assert "gone" in worker.error


def test_a_ready_worker_with_a_live_process_is_still_served(monkeypatch):
    """The other half: `poll()` returning None is alive, and the worker is
    handed out exactly as before. The engine-match check below it is untouched,
    so a fake runner code must resolve to nothing rather than to a mismatch."""
    monkeypatch.setattr(registry, "for_capability", lambda capability: None)
    worker = _idle_worker(monkeypatch, last_activity=time.monotonic())
    worker.proc = _ExitedProc(None)
    assert supervisor.ready_worker(registry.TEXT_GENERATION, "org/idle") is worker


def test_a_ready_worker_with_no_process_handle_is_not_judged(monkeypatch):
    """A worker planted without a Popen (every fixture in this file, an adopted
    process) cannot be polled, and "cannot tell" must not read as "gone" on
    the request path — the reaper's poll keeps its own stricter rule."""
    monkeypatch.setattr(registry, "for_capability", lambda capability: None)
    worker = _idle_worker(monkeypatch, last_activity=time.monotonic())
    assert worker.proc is None
    assert supervisor.ready_worker(registry.TEXT_GENERATION, "org/idle") is worker


def test_a_non_ready_worker_is_exempt_from_the_reaper(monkeypatch):
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 10)
    now = time.monotonic()
    worker = _idle_worker(monkeypatch, state="loading", last_activity=now - 10_000)
    # A 40-minute `uv sync` or an 8GB pull IS activity, and killing it mid-build
    # is hostile rather than a memory win — only `ready` is eligible.
    assert supervisor.idle_workers(now) == []
    assert supervisor.reap_idle(now) == []
    assert supervisor._workers[registry.TEXT_GENERATION] is worker


def test_a_fresh_stamp_is_spared(monkeypatch):
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 10)
    now = time.monotonic()
    _idle_worker(monkeypatch, last_activity=now - 5)
    assert supervisor.idle_workers(now) == []


def test_in_flight_with_a_fresh_stamp_is_spared(monkeypatch):
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 10)
    now = time.monotonic()
    _idle_worker(monkeypatch, last_activity=now - 5, in_flight=1)
    assert supervisor.idle_workers(now) == []


def test_a_mid_transcription_worker_is_spared_well_past_the_idle_window(monkeypatch):
    """The bug a collapsed predicate would ship: `generate_transcript` is a
    single blocking call, not a stream — nothing re-stamps `last_activity`
    between the request going out and the reply coming back, which can be up
    to `TRANSCRIBE_TIMEOUT_S` (4h) later. A 30-minute-old stamp on a
    `SPEECH_TO_TEXT` worker with `in_flight == 1` is exactly what a real
    90-minute transcription looks like at the 30-minute mark under a
    10-minute window, and reaping it here is `_terminate` killing the
    process the request is still waiting on."""
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 10)
    now = time.monotonic()
    _idle_worker(monkeypatch, last_activity=now - 30 * 60, in_flight=1,
                 capability=registry.SPEECH_TO_TEXT)
    assert supervisor.idle_workers(now) == []


def test_a_transcription_worker_past_its_leak_ceiling_is_reaped(monkeypatch):
    """The other half: `in_flight` is not a permanent exemption. Past a
    ceiling derived from `TRANSCRIBE_TIMEOUT_S` itself, the request that set
    `in_flight` would already have raised — `_worker_request` has its own
    timeout — so a counter still reading positive is a leaked stream
    (an abandoned `generate_text` iterator on another capability, say),
    never a legitimately slow answer."""
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 10)
    now = time.monotonic()
    ceiling = supervisor._leak_ceiling(registry.SPEECH_TO_TEXT, 600)
    _idle_worker(monkeypatch, last_activity=now - ceiling - 60, in_flight=1,
                 capability=registry.SPEECH_TO_TEXT)
    assert supervisor.idle_workers(now) != []


def test_a_slow_first_load_is_not_reaped_the_instant_it_becomes_ready(fake_runner, monkeypatch):
    """Regression: `last_activity` used to be seeded once, in the `Worker`
    dataclass, at CONSTRUCTION — before `_bring_up`'s venv build, pull and
    load even start. A first-ever download that takes longer than the idle
    window would become `ready` already past it, and the reaper's very next
    tick would unload it before anything had used it.

    Simulated without actually waiting minutes: `Worker.last_activity`'s
    `field(default_factory=time.monotonic)` captured the REAL function at
    class-definition time, so patching the `time` module's `monotonic`
    attribute afterwards does not touch the construction stamp — only calls
    made through the module attribute at RUN time are affected, which is
    every `time.monotonic()` call `_bring_up` makes once the fake worker
    (a ~0.1s bring-up) actually reaches `ready`. A constant offset rather
    than a frozen value, so every deadline/elapsed computation along the way
    still advances normally; only the ABSOLUTE reading moves, by more than
    the idle window.
    """
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 10)

    real_monotonic = time.monotonic
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: real_monotonic() + 800)

    supervisor.load("org/slow", registry.TEXT_GENERATION)
    worker = _wait_ready("org/slow")

    # If the ready transition still left `last_activity` at its construction
    # stamp (real time, unaffected by the patch), `now` here would read it as
    # ~800s stale — past a 10-minute window — and this would fail.
    assert supervisor.idle_workers(real_monotonic() + 800) == []
    assert worker.state == "ready"


def test_reap_decision_and_removal_share_one_lock_hold(fake_runner, monkeypatch):
    """Regression: `reap_idle` used to call `idle_workers()` (one `_lock`
    acquisition) and then `unload()` (a SECOND, later one) to remove what it
    found. In the gap between those two acquisitions the table was briefly
    unlocked, and a request racing in could call `ready_worker()`, enter
    `_in_use()` and start a call on the very worker the reaper had already
    condemned — `unload()` matches by model+capability alone and never
    re-checks `in_flight`, so it would terminate the process that request is
    now waiting on.

    Driven deterministically rather than left to scheduler luck, with no
    `sleep()` anywhere: `reap_idle` runs on its OWN thread, and `_is_idle` —
    called from INSIDE `_claim_for_removal`'s lock hold, unconditionally, for
    every candidate worker — is patched to pause there on an `Event`, still
    holding `_lock`. Only once that pause is observed does a second ("racer")
    thread start and immediately try to claim the same worker via
    `ready_worker()`, which itself needs `_lock`. Whatever happens next, the
    racer's call cannot complete until the reaper thread releases the lock —
    and by then, if the fix holds, decision AND pop have both already
    happened, so the racer can only ever observe "already gone", never a live
    worker the reaper is about to terminate out from under it.
    """
    supervisor.load("org/racer", registry.TEXT_GENERATION)
    worker = _wait_ready("org/racer")
    worker.last_activity = time.monotonic() - 700  # idle past a 10-min window

    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 10)

    real_is_idle = supervisor._is_idle
    paused = threading.Event()
    release = threading.Event()

    def slow_is_idle(w, now, window):
        result = real_is_idle(w, now, window)
        if w is worker:
            paused.set()
            assert release.wait(5), "the main thread never released us"
        return result

    monkeypatch.setattr(supervisor, "_is_idle", slow_is_idle)

    result = {}

    def run_reap():
        result["stopped"] = supervisor.reap_idle(time.monotonic())

    reaper = threading.Thread(target=run_reap)
    reaper.start()
    assert paused.wait(5), "reap_idle never reached its decision"
    # `reap_idle` is now blocked INSIDE `_claim_for_removal`'s lock hold,
    # mid-decision for this exact worker. Only now does the racer start.

    def racer():
        result["racer_saw"] = supervisor.ready_worker(registry.TEXT_GENERATION, "org/racer")

    t = threading.Thread(target=racer)
    t.start()
    release.set()
    reaper.join(5)
    t.join(5)

    assert result["stopped"] == ["org/racer"]
    # The racer's lookup could only run AFTER the whole reap finished
    # (decided AND removed) — it needed the same `_lock`, and `reap_idle`
    # never let go of it mid-decision — so it can only ever find the worker
    # already gone.
    assert result["racer_saw"] is None


def test_zero_minutes_disables_the_reaper_entirely(monkeypatch):
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 0)
    now = time.monotonic()
    _idle_worker(monkeypatch, last_activity=now - 100_000)
    assert supervisor.idle_workers(now) == []
    assert supervisor.reap_idle(now) == []


def test_a_weights_only_fetch_is_untouched_by_the_reaper(monkeypatch):
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 10)
    now = time.monotonic()
    stub = supervisor.Worker(model="org/fetching", capability=registry.TEXT_GENERATION,
                             runner_code="fake-text", token="t", state="downloading",
                             last_activity=now - 100_000)
    monkeypatch.setitem(supervisor._fetch_workers, "org/fetching", stub)
    assert supervisor.idle_workers(now) == []
    assert supervisor.reap_idle(now) == []
    assert supervisor._fetch_workers["org/fetching"] is stub


def test_the_pref_is_re_read_on_every_call(monkeypatch):
    """No caching between calls: a preference edited mid-session, or an env
    override that comes and goes, moves the answer on the very next tick —
    same discipline as `ready_worker`'s live re-read of the engine choice."""
    from fused_render.shell import prefs
    now = time.monotonic()
    _idle_worker(monkeypatch, last_activity=now - 400)
    minutes = [10]
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: minutes[0])
    assert supervisor.idle_workers(now) == [], "400s has not reached a 10-minute window"
    minutes[0] = 5
    assert supervisor.idle_workers(now) != [], "the SAME worker, a shorter window"


def test_the_reaped_job_row_names_the_idle_reason(monkeypatch):
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 10)
    now = time.monotonic()
    worker = _idle_worker(monkeypatch, last_activity=now - 700)
    job_id = supervisor.job_id_for(worker.model)
    supervisor._report(job_id, title=worker.model, state="running", kind="task")

    assert supervisor.reap_idle(now) == [worker.model]

    row = next(j for j in jobs.list_jobs() if j["id"] == job_id)
    assert row["detail"] == "Unloaded after 10 min idle"


def test_describe_reports_idle_seconds_and_the_countdown(monkeypatch, fake_runner):
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 10)
    supervisor.load("org/chat", registry.TEXT_GENERATION)
    worker = _wait_ready("org/chat")
    worker.last_activity = time.monotonic() - 60

    row = supervisor.describe()["loaded"][0]

    assert row["idleSeconds"] == pytest.approx(60, abs=3)
    assert row["unloadsInSeconds"] == pytest.approx(540, abs=3)


def test_describe_reports_no_countdown_when_the_window_is_disabled(monkeypatch, fake_runner):
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 0)
    supervisor.load("org/chat", registry.TEXT_GENERATION)
    _wait_ready("org/chat")

    row = supervisor.describe()["loaded"][0]

    assert row["unloadsInSeconds"] is None


def test_describe_countdowns_a_busy_worker_against_the_leak_ceiling(monkeypatch, fake_runner):
    """Regression: `unloadsInSeconds` used to be `window - idle age` with no
    reference to `in_flight`, so a busy worker's card would count down to
    "unloads in under a minute" and then sit there — the reaper's own
    predicate (`_is_idle`) spares a busy worker until `_leak_ceiling`, well
    past the bare window, so the card was asserting an unload that would not
    happen for a long time yet.
    """
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "effective_ai_idle_unload_minutes", lambda: 10)
    supervisor.load("org/chat", registry.TEXT_GENERATION)
    worker = _wait_ready("org/chat")
    worker.last_activity = time.monotonic() - 60
    worker.in_flight = 1

    row = supervisor.describe()["loaded"][0]

    ceiling = supervisor._leak_ceiling(registry.TEXT_GENERATION, 600)
    assert row["unloadsInSeconds"] == pytest.approx(ceiling - 60, abs=3)
    # Sanity: the leak ceiling is well past the bare 10-minute window here,
    # so this genuinely distinguishes the fix from the old `window`-only math
    # rather than coincidentally landing on the same number.
    assert ceiling > 600


# -- a worker's environment carries no Hub token of our making (D402) ------------


def test_an_inherited_hub_token_is_passed_through_untouched(monkeypatch, tmp_path):
    """A worker finds the machine's token by calling `huggingface_hub` itself, so
    nothing here manufactures one — but an `HF_TOKEN` this process inherited is
    not stripped either: hf reads that variable ahead of its own store, and a
    machine that exports one expects its workers to use it."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HF_TOKEN", "hf_from_the_environment")
    assert supervisor._child_env("worker-token")["HF_TOKEN"] == "hf_from_the_environment"


# -- the download-manager join --------------------------------------------------


def test_a_load_opens_a_server_owned_job_row(fake_runner):
    started = supervisor.load("org/rowed", registry.TEXT_GENERATION)
    _wait_ready("org/rowed")
    row = next(j for j in jobs.list_jobs() if j["id"] == started["jobId"])
    assert row["owner"] == jobs.OWNER_SERVER
    assert row["title"] == "org/rowed"
    assert row["id"].startswith(jobs.SERVER_ID_PREFIX)


def test_a_successful_load_reports_a_visible_done_state(fake_runner):
    """Regression pin for PR #785: `supervisor._finish` used to `_report(job,
    state="done")` and then `jobs.dismiss(job)` back to back on the same
    thread, with no `/api/jobs` read in between — so the "done" state existed
    for zero elapsed time and no poller could ever observe it (including
    `fused_ai.py`'s `_wait_job`, which then mistook a real success for a
    vanished row and raised). A successful job's terminal state must be
    observable by a reader, exactly like an error or a cancellation is."""
    started = supervisor.load("org/visible", registry.TEXT_GENERATION)
    row = _row(started["jobId"])
    assert row is not None, "the row disappeared before its done state could be read"
    assert row["state"] == "done"


def _row(job_id, timeout=10.0):
    """The job row for `job_id`, once it reaches a terminal state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = next((j for j in jobs.list_jobs() if j["id"] == job_id), None)
        if row and row["state"] != "running":
            return row
        time.sleep(0.02)
    return next((j for j in jobs.list_jobs() if j["id"] == job_id), None)


def test_a_load_that_throws_reports_the_failure_instead_of_freezing(
        fake_runner, monkeypatch):
    """The bug behind "No longer reporting" on a card still saying "Preparing".

    A bring-up runs on its own thread, so an exception that is not a
    `SupervisorError` is raised to NOBODY: it kills the only thing reporting,
    and the row sits at its last detail until the manager gives up and blames
    the process. The server was fine; the load simply stopped existing, silently.
    """
    def boom(runner, worker, job):
        raise RuntimeError("uv is on fire")

    monkeypatch.setattr(supervisor, "_ensure_venv", boom)
    started = supervisor.load("org/explodes", registry.TEXT_GENERATION)

    row = _row(started["jobId"])
    assert row["state"] == "error"
    # The class name is the only part a user can act on or paste into a report,
    # so it is named rather than flattened to "failed".
    assert "RuntimeError" in row["message"]
    assert "uv is on fire" in row["message"]
    # And the capability is free again, rather than held by a dead bring-up.
    assert supervisor.describe()["loaded"] == []


def test_a_download_that_throws_reports_the_failure_too(fake_runner, monkeypatch):
    """Same rule on the weights-only path, which is the one the user hit."""
    def boom(runner, worker, job):
        raise RuntimeError("the installer never started")

    monkeypatch.setattr(supervisor, "_ensure_venv", boom)
    started = supervisor.load("org/explodes", registry.TEXT_GENERATION,
                              weights_only=True)

    row = _row(started["jobId"])
    assert row["state"] == "error"
    assert "RuntimeError" in row["message"]
    # The in-flight table is cleared too, so a retry is not refused as a join.
    assert supervisor.describe()["downloading"] == []


def test_a_download_the_WORKER_stopped_reports_cancelled_not_error(
        fake_runner, monkeypatch):
    """Both sides of a download watch for the ✕, and the worker can win.

    This loop polls every 0.5s; the worker's fetch ticks every 1s and learns
    about the ✕ from the reply, raises `Cancelled` and exits non-zero. When that
    lands first — a quarter of cancels, on the two loops' periods alone — the
    `proc.poll() is None` guard drops straight through to the exit code without
    ever asking about the ✕, and the user who pressed Cancel was told their
    download had FAILED, with the worker's traceback as the reason.

    Driven through a process that is already gone, because that is exactly the
    state the race leaves behind and the only one that reproduces it every time.
    """
    job = supervisor.JOB_PREFIX + "org-stopped"
    jobs.upsert({"id": job, "title": "org/stopped", "kind": "download",
                 "state": "running", "cancellable": True}, server=True)
    jobs.request_cancel(job)

    class AlreadyExited:
        """The worker noticed the ✕ and was gone before our first poll."""

        pid = -1
        returncode = 1

        def poll(self):
            return 1

    monkeypatch.setattr(supervisor.subprocess, "Popen",
                        lambda *args, **kwargs: AlreadyExited())

    supervisor._fetch_only(fake_runner, "org/stopped", job)

    row = next(j for j in jobs.list_jobs() if j["id"] == job)
    assert row["state"] == "cancelled"
    # And no traceback dressed up as an explanation on a row nobody needs one for.
    assert not row.get("message")


def test_the_venv_wait_polls_the_key_the_installer_reports(monkeypatch, tmp_path):
    """`envinstall.start()` names its own key, and it is not always ours.

    With no pinned interpreter yet (D214) the first round installs the PYTHON,
    under `PYTHON_BOOTSTRAP_KEY` rather than under the project's venv key. A
    caller that re-derives the key polls a record nobody is writing — which is
    an infinite "Preparing…" over an install that is running fine, and is what
    `envinstall._reported` exists to prevent.
    """
    from fused_render import envinstall

    folder = tmp_path / "runner"
    folder.mkdir()
    runner = registry.Runner(code="r", capability=registry.TEXT_GENERATION,
                             folder=str(folder), label="R")
    rounds = []
    installed = {"yes": False}
    report_jobs = []

    def fake_start(project_dir, report_job=True):
        rounds.append(project_dir)
        report_jobs.append(report_job)
        # Round one is the interpreter, under a key that is NOT the venv key.
        return {"key": "bootstrap-key" if len(rounds) == 1 else "venv-key",
                "done": False}

    def fake_progress(key):
        # Only ever answers for the key `start()` actually reported.
        assert key in ("bootstrap-key", "venv-key"), f"polled a stale key: {key}"
        if key == "venv-key":
            installed["yes"] = True
        return {"done": True, "error": None, "stage": "done"}

    monkeypatch.setattr(envinstall, "start", fake_start)
    monkeypatch.setattr(envinstall, "progress", fake_progress)
    monkeypatch.setattr(envinstall, "is_installed", lambda d: installed["yes"])
    monkeypatch.setattr(envinstall, "venv_python_for", lambda d: "/venv/bin/python")
    monkeypatch.setattr(envinstall, "venv_key_for", lambda d: "venv-key")

    worker = supervisor.Worker(model="m", capability=registry.TEXT_GENERATION,
                               runner_code="r", token="t")
    assert supervisor._ensure_venv(runner, worker, "sys:ai-model:m") == "/venv/bin/python"
    # TWO rounds: the interpreter, then the packages. One would have installed a
    # python and then waited for packages nobody had asked for.
    assert len(rounds) == 2
    # `_ensure_venv` mirrors this same install into its own job row — `start()`
    # must not open a second, generic one alongside it.
    assert report_jobs == [False, False]


def test_a_venv_build_reports_more_than_a_stage_word(monkeypatch, tmp_path):
    """The bug this whole feature shipped to fix: `_ensure_venv` used to read
    only `record["stage"]` (the coarse "installing") and threw away the
    `activity`/`bytes_done`/`bytes_total` `_env_install_worker._UvProgress`
    computes from uv's own stderr — so a ROCm torch install sat on "Preparing
    Transformers Embeddings (ROCm) — installing…" with no bytes, no elapsed
    time the user could see move, for however many minutes the download took.

    This is the test that would have failed before those keys were read: the
    row's detail must carry uv's compact phrase, and the job's `done`/`total`/
    `unit` must be set from the same record so `ModelProgress` draws a real
    bar instead of a dot.
    """
    from fused_render import envinstall

    folder = tmp_path / "runner"
    folder.mkdir()
    runner = registry.Runner(code="r", capability=registry.TEXT_GENERATION,
                             folder=str(folder), label="ROCm")
    ticks = [
        {"done": False, "stage": "install", "activity": None,
         "bytes_done": None, "bytes_total": None},
        {"done": False, "stage": "install",
         "activity": "downloading torch (2m14s)",
         "bytes_done": 1_200_000_000, "bytes_total": 3_600_000_000},
        {"done": True, "error": None, "stage": "done"},
    ]

    monkeypatch.setattr(envinstall, "start", lambda d, report_job=True: {"key": "venv-key", "claimed": True})
    monkeypatch.setattr(envinstall, "progress", lambda key: ticks.pop(0) if ticks else ticks[-1])
    monkeypatch.setattr(envinstall, "is_installed", lambda d: not ticks)
    monkeypatch.setattr(envinstall, "venv_python_for", lambda d: "/venv/bin/python")
    monkeypatch.setattr(envinstall, "venv_key_for", lambda d: "venv-key")

    worker = supervisor.Worker(model="m", capability=registry.TEXT_GENERATION,
                               runner_code="r", token="t")
    job = "sys:ai-model:m"
    jobs.upsert({"id": job, "title": "m", "kind": "download", "state": "running",
                 "owner": "server"}, server=True)

    seen_bytes_row = []
    real_report = supervisor._report

    def _watch(job_id, **fields):
        real_report(job_id, **fields)
        if fields.get("total"):
            seen_bytes_row.append(dict(fields))

    monkeypatch.setattr(supervisor, "_report", _watch)

    assert supervisor._ensure_venv(runner, worker, job) == "/venv/bin/python"

    assert seen_bytes_row, "the byte-carrying tick never reached the job row"
    reported = seen_bytes_row[0]
    assert reported["done"] == 1_200_000_000
    assert reported["total"] == 3_600_000_000
    assert reported["unit"] == "bytes"
    assert "downloading torch" in reported["detail"]
    assert "Preparing ROCm" in reported["detail"]
    assert "GB" not in reported["detail"] and "MB" not in reported["detail"]


def test_a_finished_venv_build_clears_its_own_byte_counters(monkeypatch, tmp_path):
    """Review issue #3: the install loop breaks on `record.get("done")`
    BEFORE reporting anything, so without an explicit reset the job row keeps
    whatever `done`/`total`/`unit="bytes"` its last "still downloading" tick
    left behind. `_bring_up` then reports "Starting the model process…" with
    no reset of its own, which would draw a full "3.4 GB / 3.4 GB" bar under
    that sentence until the runner's first weight tick overwrote it — a
    finished download that still looks like it is running.
    """
    from fused_render import envinstall

    folder = tmp_path / "runner"
    folder.mkdir()
    runner = registry.Runner(code="r", capability=registry.TEXT_GENERATION,
                             folder=str(folder), label="ROCm")
    ticks = [
        {"done": False, "stage": "install",
         "activity": "downloading torch (2m14s)",
         "bytes_done": 3_400_000_000, "bytes_total": 3_400_000_000},
        {"done": True, "error": None, "stage": "done"},
    ]

    monkeypatch.setattr(envinstall, "start", lambda d, report_job=True: {"key": "venv-key", "claimed": True})
    monkeypatch.setattr(envinstall, "progress", lambda key: ticks.pop(0) if ticks else ticks[-1])
    monkeypatch.setattr(envinstall, "is_installed", lambda d: not ticks)
    monkeypatch.setattr(envinstall, "venv_python_for", lambda d: "/venv/bin/python")
    monkeypatch.setattr(envinstall, "venv_key_for", lambda d: "venv-key")

    worker = supervisor.Worker(model="m", capability=registry.TEXT_GENERATION,
                               runner_code="r", token="t")
    job = "sys:ai-model:m"
    jobs.upsert({"id": job, "title": "m", "kind": "download", "state": "running",
                 "owner": "server"}, server=True)

    assert supervisor._ensure_venv(runner, worker, job) == "/venv/bin/python"

    row = _row_now(job)
    assert row["done"] is None, row
    assert row["total"] is None, row
    assert row["unit"] == "", row


def test_the_cross_pressed_during_the_download_stops_the_load(fake_runner, monkeypatch):
    """The ✕ has to reach the phase that actually takes the time.

    `stopping` is set by an eviction or an explicit unload — things the SERVER
    decided. What a user presses is the ✕ on the row, which sets
    `cancel_requested` on the JOB. The env-build loop honoured it and the
    post-spawn loop did not, so a cancel during the multi-GB fetch (i.e. every
    cancel anyone would ever press) did nothing and the download finished.
    """
    monkeypatch.setenv("FAKE_LOAD_SECONDS", "30")  # never becomes ready on its own
    started = supervisor.load("org/slow", registry.TEXT_GENERATION)
    # Wait until it is past the spawn and into the loop this test is about.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(w["state"] in ("starting", "loading")
               for w in supervisor.describe()["loaded"]):
            break
        time.sleep(0.02)

    jobs.request_cancel(started["jobId"])

    row = _row(started["jobId"])
    assert row["state"] == "cancelled"
    assert supervisor.describe()["loaded"] == []


def test_shutdown_stops_a_download_that_is_still_running(fake_runner, monkeypatch):
    """A fetch holds no memory, so it was in no table shutdown walked.

    The consequence was not harmless: quitting the app left a detached
    `snapshot_download` pulling gigabytes with nothing left that could stop it.
    """
    monkeypatch.setenv("FAKE_DOWNLOAD_SECONDS", "30")
    supervisor.load("org/bigpull", registry.TEXT_GENERATION, weights_only=True)
    # Wait for the PROCESS, not merely for the row. The stub is registered
    # before its environment build (so shutdown can see it during that phase
    # too), so its presence in the table no longer means there is anything
    # spawned yet — which is what this test is about stopping.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pending = supervisor._fetch_workers.get("org/bigpull")
        if pending is not None and pending.proc is not None:
            break
        time.sleep(0.02)
    stub = supervisor._fetch_workers.get("org/bigpull")
    assert stub is not None and stub.proc is not None, "the download never spawned"

    supervisor.unload_all()

    # `poll()`, never `wait()`: `_terminate` reaps the child itself, and a second
    # `wait()` on an already-reaped Popen trips its own internal assertion.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and stub.proc.poll() is None:
        time.sleep(0.02)
    assert stub.proc.poll() is not None, "the downloader outlived the app"


def test_shutdown_cancels_an_environment_build_too(fake_runner, monkeypatch):
    """The venv phase is a detached multi-GB `uv sync` with no process of ours.

    Registering the fetch only once there was a DOWNLOAD process to kill left it
    invisible to shutdown for exactly the minutes the install runs — the same
    hole one layer up, since a first-ever runner build is the longest part of a
    first download.
    """
    from fused_render import envinstall

    cancelled = []
    monkeypatch.setattr(envinstall, "is_installed", lambda d: False)
    monkeypatch.setattr(envinstall, "start",
                        lambda d, report_job=True: {"key": "abc123", "done": False, "claimed": True})
    monkeypatch.setattr(envinstall, "progress", lambda key: {"done": False, "stage": "sync"})
    monkeypatch.setattr(envinstall, "cancel", lambda key: cancelled.append(key) or True)
    # The real one — the fixture stubs it, and this test is about what it does.
    monkeypatch.setattr(supervisor, "_ensure_venv", _REAL_ENSURE_VENV)

    supervisor.load("org/building", registry.TEXT_GENERATION, weights_only=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        stub = supervisor._fetch_workers.get("org/building")
        if stub is not None and stub.install_key:
            break
        time.sleep(0.02)
    stub = supervisor._fetch_workers.get("org/building")
    assert stub is not None, "the fetch was invisible while its environment built"
    assert stub.install_key == "abc123"

    supervisor.unload_all()
    assert cancelled == ["abc123"], "the detached installer was left running"


# -- one runner, one environment, several downloads waiting on it ----------------
#
# `envinstall` is single-flight per key: the first caller spawns `uv sync` and
# every later caller joins and polls. `_ensure_venv` cancelled on the KEY, which
# names the shared install rather than the caller's share of it — so cancelling
# a download that was merely WAITING killed the build every other download of
# that runner was joined to, and each of those then failed with a cancellation
# nobody had asked for. Worse with no pinned Python yet, where the key is the
# machine-global `PYTHON_BOOTSTRAP_KEY` and the blast radius is every runner.
#
# These tests need the REAL `_ensure_venv` (the `fake_runner` fixture stubs it
# out, which is exactly why the path had no coverage) over a faked `envinstall`.


@pytest.fixture()
def shared_install(fake_runner, monkeypatch):
    """A fake `envinstall` whose install is claimed once and joined thereafter.

    `state["done"]` is the release: until the test sets it, every poll reports an
    install still running, so a download parked in the venv phase stays parked
    and is there to be cancelled. `state["cancelled"]` is every key `cancel` was
    asked for — the assertion these tests are about.

    **`cancel` does what the real one does, and that is not decoration.** The
    real `envinstall.cancel` kills the pid AND writes
    `done=True, error="the install was cancelled"` into the SHARED record every
    joiner is polling (`envinstall.py`'s `cancel`), which is exactly how a cancel
    reaches workers that never asked for one. A fake that only recorded the call
    certified a behaviour production did not have: it let a joiner sail past a
    cancellation that would really have killed it, and the test asserting the
    joiner survives an owner's ✕ passed over a defect (see
    `test_cancelling_the_OWNER_leaves_a_joiner_alone`).
    """
    from fused_render import envinstall

    state = {"claims": 0, "done": False, "cancelled": [], "error": None}

    def start(project_dir, report_job=True):
        state["claims"] += 1
        return {"key": "shared-key", "done": False, "claimed": state["claims"] == 1}

    def cancel(key):
        state["cancelled"].append(key)
        # The real one refuses a record that is already done — there is no live
        # pid to signal — so a cancel on the way past a FINISHED install must not
        # invent an error for it either.
        if state["done"]:
            return False
        state["done"] = True
        state["error"] = "the install was cancelled"
        return True

    monkeypatch.setattr(envinstall, "is_installed",
                        lambda d: state["done"] and not state["error"])
    monkeypatch.setattr(envinstall, "start", start)
    monkeypatch.setattr(envinstall, "progress",
                        lambda key: {"done": state["done"], "error": state["error"],
                                     "stage": "sync"})
    monkeypatch.setattr(envinstall, "cancel", cancel)
    monkeypatch.setattr(envinstall, "venv_python_for", lambda d: sys.executable)
    monkeypatch.setattr(envinstall, "venv_key_for", lambda d: "shared-key")
    # The real one — the fixture stubs it, and these tests are about what it does.
    monkeypatch.setattr(supervisor, "_ensure_venv", _REAL_ENSURE_VENV)
    return state


def _waiting_on_the_install(model, timeout=10.0):
    """The download for `model`, once it is parked in the venv phase.

    Started one at a time and awaited, because "who owns the install" is
    whoever called `start()` first and two threads racing for that would make
    every assertion below a coin toss.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stub = supervisor._fetch_workers.get(model)
        if stub is not None and stub.install_key:
            return stub
        time.sleep(0.02)
    raise AssertionError(f"{model} never reached the environment build")


def test_cancelling_a_JOINER_leaves_the_shared_install_running(shared_install):
    """The defect, from the user's side: two downloads on one runner, ✕ on the
    one that is only waiting, and the other one dies too."""
    owner = supervisor.load("org/owner", registry.TEXT_GENERATION, weights_only=True)
    _waiting_on_the_install("org/owner")
    joiner = supervisor.load("org/joiner", registry.TEXT_GENERATION, weights_only=True)
    _waiting_on_the_install("org/joiner")

    jobs.request_cancel(joiner["jobId"])

    assert _row(joiner["jobId"])["state"] == "cancelled"
    assert shared_install["cancelled"] == [], \
        "a joiner's ✕ tore down the install the owner was building"

    # And the owner's download really does carry on to the end.
    shared_install["done"] = True
    assert _row(owner["jobId"])["state"] == "done"
    _drain_downloads()


def test_cancelling_the_OWNER_leaves_a_joiner_alone(shared_install):
    """The SYMMETRIC case, and the one ownership got wrong in the other
    direction.

    `envinstall.cancel` does not just kill a pid — it writes
    `error: "the install was cancelled"` into the SHARED record every joiner is
    polling. So an owner's ✕ reached the joiners just as surely as a joiner's ✕
    used to reach the owner: their next poll read the error and raised past the
    `_VENV_ROUNDS` loop, and a download nobody had touched died saying "the
    install was cancelled". Ownership cannot be the condition; "is anybody still
    waiting on this" can.
    """
    owner = supervisor.load("org/owner", registry.TEXT_GENERATION, weights_only=True)
    _waiting_on_the_install("org/owner")
    joiner = supervisor.load("org/joiner", registry.TEXT_GENERATION, weights_only=True)
    _waiting_on_the_install("org/joiner")

    jobs.request_cancel(owner["jobId"])

    assert _row(owner["jobId"])["state"] == "cancelled"
    assert shared_install["cancelled"] == [], \
        "the owner's ✕ cancelled an install the joiner was still waiting on"

    # And the joiner really does get its environment and finish.
    shared_install["done"] = True
    assert _row(joiner["jobId"])["state"] == "done"
    _drain_downloads()


def test_the_LAST_download_waiting_on_an_install_does_stop_it(shared_install):
    """The property the cancellation exists for, kept: an install nothing is
    waiting on any more must not go on pulling gigabytes.

    Both downloads cancelled, so the second ✕ is the last waiter leaving — and
    THAT is the one that reaches the detached `uv sync`, which outlives the
    threads and the app unless it is cancelled by name.
    """
    owner = supervisor.load("org/owner", registry.TEXT_GENERATION, weights_only=True)
    _waiting_on_the_install("org/owner")
    joiner = supervisor.load("org/joiner", registry.TEXT_GENERATION, weights_only=True)
    _waiting_on_the_install("org/joiner")

    jobs.request_cancel(owner["jobId"])
    assert _row(owner["jobId"])["state"] == "cancelled"
    jobs.request_cancel(joiner["jobId"])
    assert _row(joiner["jobId"])["state"] == "cancelled"

    assert shared_install["cancelled"] == ["shared-key"]
    _drain_downloads()


def test_a_GENUINE_build_failure_still_reaches_every_row_verbatim(shared_install):
    """The distinction the retry must preserve.

    A cancellation is somebody's decision about their own download and must not
    be inherited; a resolver failure is a fact about the environment and every
    row waiting on it has to say so, in uv's own words, without being retried
    (PY-18). Both arrive as `error` on the same shared record, so the difference
    has to come from somewhere else — which is why cancellation is now decided
    before anything is written, rather than read back out of the record.
    """
    started = supervisor.load("org/broken", registry.TEXT_GENERATION, weights_only=True)
    _waiting_on_the_install("org/broken")

    shared_install["done"] = True
    shared_install["error"] = "No solution found: imagecodecs has no wheels"

    row = _row(started["jobId"])
    assert row["state"] == "error"
    assert "imagecodecs" in row["message"]
    # Nothing was cancelled: the install had already finished (badly), so there
    # was no detached installer to stop.
    assert shared_install["cancelled"] == []
    _drain_downloads()


def _await_detail(job_id, needle, timeout=10.0):
    """The row's detail once it contains `needle` — the loop reports one tick
    after it starts waiting, so this is a wait and not a read."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = _row_now(job_id)
        if row and needle in (row.get("detail") or ""):
            return row["detail"]
        time.sleep(0.02)
    raise AssertionError(f"the row never said {needle!r}: "
                         f"{(_row_now(job_id) or {}).get('detail')!r}")


def test_a_download_waiting_on_someone_elses_env_build_says_so(shared_install):
    """"Preparing MLX — sync…" on a download that is not preparing anything is
    a true sentence read as a lie: two rows said the same thing while one was
    doing the work and the other was parked behind it, and a download that had
    died looked no different either. Same shape the transcription queue solved
    with `_QUEUED_DETAIL`."""
    owner = supervisor.load("org/owner", registry.TEXT_GENERATION, weights_only=True)
    _waiting_on_the_install("org/owner")
    joiner = supervisor.load("org/joiner", registry.TEXT_GENERATION, weights_only=True)
    _waiting_on_the_install("org/joiner")

    detail = _await_detail(joiner["jobId"], "another download")
    assert "Waiting for" in detail and "Fake" in detail

    # The owner IS the one building it, and its row is unchanged.
    owner_detail = _await_detail(owner["jobId"], "Preparing")
    assert "another download" not in owner_detail

    # And nothing keeps saying it once the install lands.
    shared_install["done"] = True
    for started in (owner, joiner):
        row = _row(started["jobId"])
        assert row["state"] == "done", row
        assert "another download" not in (row.get("detail") or "")
    _drain_downloads()


def test_terminating_a_worker_while_another_waits_leaves_the_install(monkeypatch):
    """`_terminate` is the other door out of the venv phase — an eviction, or
    `unload_all` at shutdown — and it cancelled `install_key` unconditionally,
    so shutting one worker down killed a build another one was joined to.

    Both directions in one test, because they are one rule read twice. The
    worker terminated first is the OWNER, deliberately: ownership is not the
    question and cancelling on it took the joiners down with the record (see
    `Worker.install_owned`). What decides is whether anybody is still waiting —
    and the last one out still ends the install, which is what keeps a detached
    multi-GB `uv sync` from outliving the app.
    """
    from fused_render import envinstall

    cancelled = []
    monkeypatch.setattr(envinstall, "cancel", lambda key: cancelled.append(key) or True)
    owner = supervisor.Worker(model="a", capability=registry.TEXT_GENERATION,
                              runner_code="r", token="t1")
    joiner = supervisor.Worker(model="b", capability=registry.TEXT_GENERATION,
                               runner_code="r", token="t2")
    supervisor._hold_install(owner, "shared-key", True)
    supervisor._hold_install(joiner, "shared-key", False)

    supervisor._terminate(owner)

    assert cancelled == [], "one worker's teardown cancelled an install another was waiting on"
    assert owner.install_key == "" and owner.install_owned is False

    supervisor._terminate(joiner)

    assert cancelled == ["shared-key"], "the last waiter left the installer running"
    assert joiner.install_key == ""


def test_releasing_a_share_twice_gives_back_only_one(monkeypatch):
    """Two threads legitimately release the same worker — its own bring-up
    thread on the way out of `_ensure_venv`, and `_terminate` from an eviction
    that raced it. A second release that decremented again would hand the count
    below zero and cancel an install the OTHER worker was still waiting on."""
    from fused_render import envinstall

    cancelled = []
    monkeypatch.setattr(envinstall, "cancel", lambda key: cancelled.append(key) or True)
    one = supervisor.Worker(model="a", capability=registry.TEXT_GENERATION,
                            runner_code="r", token="t1")
    two = supervisor.Worker(model="b", capability=registry.TEXT_GENERATION,
                            runner_code="r", token="t2")
    supervisor._hold_install(one, "shared-key", True)
    supervisor._hold_install(two, "shared-key", False)

    supervisor._release_install(one)
    supervisor._release_install(one)

    assert cancelled == []
    supervisor._release_install(two)
    assert cancelled == ["shared-key"]


def test_a_rehold_landing_before_the_cancel_call_stops_it(monkeypatch):
    """The window `_release_install` used to leave open: worker A is the only
    waiter on a key, pops it under the lock — and, before it ever reaches
    `envinstall.cancel`, an entirely fresh `load()` runs `envinstall.start()`,
    finds the install still alive, and joins it via `_hold_install`. Cancel
    then fires anyway, because it has no way to know a joiner just arrived —
    and that joiner's next poll reads the "cancelled" error nobody asked for.

    Reproduced deterministically, not by hoping a thread scheduler lands two
    threads in the right order: `_install_waiters.pop` is the last thing
    `_release_install` does before giving up `_lock`, so hooking it lets a
    SEPARATE thread run the entire rejoin (`_hold_install`) to completion
    right there, using the real lock to force the ordering rather than a
    sleep. That models "A got preempted for the length of a whole
    `envinstall.start` round trip", which is what the bug narrative describes
    — a `dict.pop` and a couple of statements later, not a same-thread
    reentrant call.
    """
    from fused_render import envinstall

    cancelled = []
    monkeypatch.setattr(envinstall, "cancel", lambda key: cancelled.append(key) or True)

    owner = supervisor.Worker(model="a", capability=registry.TEXT_GENERATION,
                              runner_code="r", token="t1")
    rejoiner = supervisor.Worker(model="b", capability=registry.TEXT_GENERATION,
                                 runner_code="r", token="t2")
    key = "shared-key"
    supervisor._hold_install(owner, key, True)

    class _PopHook(dict):
        """Stands in for `_install_waiters`, whose real `pop` is the only
        thing both the buggy and fixed `_release_install` call on their way
        out of the lock — the one seam that exists in either version."""

        fired = False

        def pop(self, k, *default):
            result = super().pop(k, *default)
            if k == key and not _PopHook.fired:
                _PopHook.fired = True
                # `_release_install` is still inside `with _lock:` here (this
                # IS that block's last statement) — drop it just long enough
                # for a genuinely different thread to run the whole rejoin,
                # then take it back, exactly as `with _lock:`'s `__exit__`
                # expects to find it.
                supervisor._lock.release()
                try:
                    t = threading.Thread(
                        target=supervisor._hold_install, args=(rejoiner, key, False))
                    t.start()
                    t.join(timeout=5)
                    assert not t.is_alive(), "the rejoin never completed"
                finally:
                    supervisor._lock.acquire()
            return result

    monkeypatch.setattr(supervisor, "_install_waiters", _PopHook(supervisor._install_waiters))

    supervisor._release_install(owner, cancel=True)

    assert cancelled == [], \
        "cancelled an install a fresh load() had already rejoined"
    assert supervisor._install_waiters.get(key) == 1, \
        "the rejoiner's own share went missing"
    assert rejoiner.install_key == key


def test_a_worker_past_the_venv_phase_cancels_nothing(monkeypatch, tmp_path):
    """Ownership is cleared with the key, so a worker that is now downloading
    cannot cancel an unrelated install later under a stale flag — the same
    reason the key itself is cleared."""
    from fused_render import envinstall

    folder = tmp_path / "runner"
    folder.mkdir()
    runner = registry.Runner(code="r", capability=registry.TEXT_GENERATION,
                             folder=str(folder), label="R")
    installed = {"yes": False}
    cancelled = []
    monkeypatch.setattr(envinstall, "is_installed", lambda d: installed["yes"])
    monkeypatch.setattr(envinstall, "start",
                        lambda d, report_job=True: {"key": "shared-key", "done": False, "claimed": True})
    monkeypatch.setattr(envinstall, "progress", lambda key: (
        installed.update(yes=True) or {"done": True, "error": None, "stage": "done"}))
    monkeypatch.setattr(envinstall, "venv_python_for", lambda d: "/venv/bin/python")
    monkeypatch.setattr(envinstall, "cancel", lambda key: cancelled.append(key) or True)
    worker = supervisor.Worker(model="m", capability=registry.TEXT_GENERATION,
                               runner_code="r", token="t")

    assert supervisor._ensure_venv(runner, worker, "sys:ai-model:m") == "/venv/bin/python"

    assert worker.install_key == "" and worker.install_owned is False
    supervisor._terminate(worker)
    assert cancelled == []


def test_a_prerequisite_this_machine_lacks_is_said_before_a_row_opens(monkeypatch):
    """`uv` and the `fused` package, refused at the REQUEST.

    A missing prerequisite used to surface as somebody's import error inside a
    thread, on a card that had already said "Preparing…" — "ModuleNotFoundError:
    No module named 'fused'" on a model download tells a user nothing about what
    to install. `envinstall` is the loader for the fused ENGINE and reads the
    base interpreter off that engine's backend, so a machine without the package
    cannot build a runner venv at all, and that is knowable up front.
    """
    from fused_render import engine

    monkeypatch.setattr(supervisor.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(engine, "available", lambda: False)
    with pytest.raises(supervisor.SupervisorError) as caught:
        supervisor._require_build_tools()
    assert "fused-render[fused]" in str(caught.value)

    monkeypatch.setattr(supervisor.shutil, "which", lambda name: None)
    with pytest.raises(supervisor.SupervisorError) as caught:
        supervisor._require_build_tools()
    assert "uv" in str(caught.value)


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
    assert {r["code"] for r in body["runners"]} == {
        "mlx-text", "llamacpp-text", "llamacpp-text-vulkan",
        "diffusers-image", "diffusers-image-cuda",
        "diffusers-image-rocm", "mflux-image",
        "faster-whisper", "mlx-whisper",
        "mlx-embed",
        "onnx-embed", "onnx-embed-directml", "onnx-embed-cuda",
        "onnx-embed-rocm", "ltx-video"}
    assert body["loaded"] == []
    # Exactly one runner per capability is ACTIVE — the distinction D302 needed,
    # since with a preference in the middle "available" stopped meaning "this is
    # what serves me". A capability nothing can serve here has no active runner,
    # which is why this counts rather than requiring one.
    for capability in {r["capability"] for r in body["runners"]}:
        active = [r for r in body["runners"]
                  if r["capability"] == capability and r["active"]]
        assert len(active) <= 1, active
        assert all(r["available"] for r in active)


def test_every_mutating_route_carries_the_guard(client):
    for path in ("/api/ai/runtime/load", "/api/ai/runtime/unload",
                 "/api/ai/runtime/download", "/api/ai/image", "/api/ai/transcribe",
                 "/api/ai/video"):
        assert client.post(path, json={"model": "org/x", "prompt": "x"}).status_code == 403


def test_the_catalog_explains_a_capability_this_machine_cannot_serve(client, monkeypatch):
    # Narrowed to the Metal-only runner, because text generation on Linux is
    # servable since D293 and no longer demonstrates an unavailable capability.
    # The behaviour under test is the same one: a capability this machine cannot
    # serve is SHOWN with its reason and keeps its shortlist, rather than being
    # hidden and leaving a user wondering where the feature went.
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(registry, "_RUNNERS", (registry.by_code("mlx-text"),))
    rows = {row["capability"]: row for row in client.get("/api/ai/catalog").json()["capabilities"]}
    text = rows[registry.TEXT_GENERATION]
    assert text["available"] is False and "Apple Silicon" in text["reason"]
    assert text["models"], "the suggestions are still listed"
    # The note is about a backend that CAN run; an unavailable one has a reason
    # instead, and saying both would be telling someone what it will be like to
    # use something they cannot use.
    assert text["runnerNote"] is None


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
    """…and the same call with a repo id does NOT reach for the CLI at all.

    On Linux this now takes the LOAD path rather than failing — text generation
    has a runner everywhere since D293 — so what proves the model went local is
    a `model_loading` answer carrying a job to watch, which is AI-5's contract
    for a generation whose model is not resident yet. The `_claude_bin` trap is
    the assertion that matters either way: reaching it fails the test outright.
    """
    from fused_render.server import ai as ai_mod

    monkeypatch.setattr(ai_mod, "_claude_bin",
                        lambda: pytest.fail("a local model reached the Claude CLI"))
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    # `_require_build_tools` would otherwise refuse before any of this, on a CI
    # box with no `uv` — and the question here is which DESTINATION the model id
    # picked, not whether this machine could build a venv for it.
    monkeypatch.setattr(supervisor, "_require_build_tools", lambda: None)
    response = client.post("/api/ai", json={"prompt": "hi", "model": "org/x"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["type"] == "model_loading"
    assert body["error"]["jobId"] == supervisor.job_id_for("org/x")
    supervisor.unload(model="org/x")


def test_a_slash_bearing_model_no_runner_can_serve_says_why(client, monkeypatch):
    """The failure this used to describe, kept where it can still be reached.

    A machine whose only text runner is Metal-only answers `ai_unavailable` with
    the platform's reason, rather than a `model_loading` job that nothing will
    ever finish.
    """
    from fused_render.server import ai as ai_mod

    monkeypatch.setattr(ai_mod, "_claude_bin",
                        lambda: pytest.fail("a local model reached the Claude CLI"))
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(registry, "_RUNNERS", (registry.by_code("mlx-text"),))
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


def test_the_reply_says_where_the_LIVE_PREVIEW_will_be(client, fake_image_runner):
    """The third path this API hands out, and it is decided here for the same
    reason the other two are: the server owns where user files go. Derived
    through `preview.preview_path`, never string-munged out of `path` — the
    worker that writes this file and the reply that advertises it have to name
    the same one, and a second spelling of the suffix is how they disagree."""
    from fused_render.ai.runners import preview

    started = client.post("/api/ai/image", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    assert started["previewPath"] == preview.preview_path(started["path"])
    assert os.path.dirname(started["previewPath"]) == os.path.dirname(started["path"])
    _wait_job(started["jobId"])


def test_the_worker_is_told_where_to_write_the_preview(client, fake_image_runner,
                                                       monkeypatch):
    """The advertised path and the requested one are the same string, because
    they come from the same call rather than from two agreeing spellings."""
    captured = {}
    real_start = supervisor.start_image

    def spy(model, request, job):
        captured.update(request)
        return real_start(model, request, job)

    monkeypatch.setattr(supervisor, "start_image", spy)
    started = client.post("/api/ai/image", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    # `captured` is the RAW request the worker gets (a native path,
    # backslashed on Windows); `previewPath` is the reply's canonical
    # (forward-slash) form of the same path — see `ai_runtime.canonical_fs_path`.
    assert ai_runtime.canonical_fs_path(captured["outPreview"]) == started["previewPath"]
    _wait_job(started["jobId"])


def _skill_section(title):
    """The body of one `## ` section of `skills/fused-render-ai/SKILL.md`.

    That file is what a page author actually reads — the bridge's own comments
    are for whoever maintains the bridge — so it is the copy of this API that
    can be wrong without anything noticing.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "skills", "fused-render-ai", "SKILL.md")
    source = open(path, encoding="utf-8").read()
    start = source.index("## " + title)
    end = source.find("\n## ", start + 1)
    return source[start:end if end != -1 else len(source)]


def test_the_SKILL_names_every_field_an_image_resolves_with(client, fake_image_runner):
    """Read off the ENDPOINT rather than listed here, because a hand-written
    list in a test is a third copy that can drift with the other two. This is
    the check that would have caught `previewPath` and `previewUrl` shipping
    with the skill still describing the API without them: the route grew two
    fields and the document a page author reads did not.

    `url` and `previewUrl` are added because they are the bridge's own, built
    on top of the reply rather than returned by it.
    """
    started = client.post("/api/ai/image", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    fields = set(started) | {"url", "previewUrl"}
    section = _skill_section("Images: `fused.ai.image({prompt, ...})`")
    assert sorted(field for field in fields if field not in section) == []
    _wait_job(started["jobId"])


def test_the_SKILL_names_the_image_FIELD_TOO_when_an_edit_is_asked_for(
        client, fake_image_runner, base_photo):
    """The same drift guard, over the reply an EDIT resolves with — `image`
    only ever appears on that reply, so a plain-render POST would never
    catch the skill going stale about it."""
    page, _photo = base_photo
    started = client.post(
        "/api/ai/image", json={"prompt": "x", "image": "photo.png", "base": page},
        headers={"X-Fused": "1"}).json()
    assert "image" in started
    section = _skill_section("Images: `fused.ai.image({prompt, ...})`")
    assert "image" in section
    _wait_job(started["jobId"])


def test_the_SKILL_names_every_field_a_video_resolves_with(client, fake_video_runner):
    """Same drift guard as the image route's own version of this test, over
    `/api/ai/video`'s reply."""
    started = client.post("/api/ai/video", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    fields = set(started) | {"url"}
    section = _skill_section("Video: `fused.ai.video({prompt, ...})`")
    assert sorted(field for field in fields if field not in section) == []
    _wait_job(started["jobId"])


def test_the_SKILL_names_the_video_image_FIELD_TOO_when_a_reference_is_given(
        client, fake_video_runner, base_photo):
    """Same drift guard as the image route's own version, over the reply a
    conditioned render resolves with — `image` only ever appears there, so a
    plain text-to-video POST would never catch the skill going stale about
    it."""
    page, _photo = base_photo
    started = client.post(
        "/api/ai/video", json={"prompt": "x", "image": "photo.png", "base": page},
        headers={"X-Fused": "1"}).json()
    assert "image" in started
    section = _skill_section("Video: `fused.ai.video({prompt, ...})`")
    assert "image" in section
    _wait_job(started["jobId"])


def test_a_runner_that_writes_no_preview_leaves_NOTHING_behind(client,
                                                               fake_image_runner):
    """The preview is a promise about a path, not about a file. A model with no
    fitted projection — and every worker built before this existed — renders
    exactly as it did, and the advertised path simply never appears."""
    started = client.post("/api/ai/image", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    row = _wait_job(started["jobId"])
    assert row["state"] == "done", row
    assert os.path.isfile(started["path"])
    assert not os.path.exists(started["previewPath"])


def _age(path, seconds):
    """Backdate a file, so a sweep sees it as something nobody is writing."""
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_a_preview_ORPHANED_BY_A_KILL_is_swept_up(tmp_path):
    """`Sink.discard` runs on a normal unwind, and a worker does not always get
    one: `supervisor._terminate` / `_kill_tree` end it outright on an unload, an
    app shutdown or a wedge. What is left is `<stem>.preview.png` — and possibly
    a `.tmp` beside it — in `~/ai/images`, a directory the user browses, with no
    job row to explain it and nothing that would ever remove it.

    So the directory is swept when the next render asks for it: the one moment
    this is free, since the caller is about to wait minutes anyway.
    """
    from fused_render.server.routers import ai_runtime

    room = tmp_path / "images"
    room.mkdir()
    orphan = room / "20260101-120000-abc.preview.png"
    temp = room / "20260101-120000-abc.preview.png.9999.tmp"
    kept = room / "20260101-120000-abc.png"
    for path in (orphan, temp, kept):
        path.write_bytes(b"x")
        _age(path, ai_runtime._PREVIEW_TTL + 60)

    ai_runtime._sweep_previews(str(room))
    # The render itself is the artefact and is never touched, however old.
    assert sorted(p.name for p in room.iterdir()) == ["20260101-120000-abc.png"]


def test_a_preview_a_RENDER_IS_STILL_WRITING_survives_the_sweep(tmp_path):
    """The one thing this must not do. A live preview is rewritten every
    denoising step, so its mtime is always seconds old — the age threshold is
    what separates "nobody is writing this" from "somebody is", and it is why
    the sweep does not need to know which renders are in flight."""
    from fused_render.server.routers import ai_runtime

    room = tmp_path / "images"
    room.mkdir()
    live = room / "20260101-120000-def.preview.png"
    live.write_bytes(b"x")

    ai_runtime._sweep_previews(str(room))
    assert live.exists()


def test_the_sweep_never_breaks_a_render(tmp_path):
    """It runs on the way in to a request that is about to work. A directory
    that cannot be listed, or a file that cannot be removed, is worth an untidy
    folder and never a refused render."""
    from fused_render.server.routers import ai_runtime

    ai_runtime._sweep_previews(str(tmp_path / "nothing-here"))


def test_a_render_SWEEPS_before_it_starts(client, fake_image_runner, monkeypatch,
                                          tmp_path):
    """Wired to the request rather than to a timer: there is no background
    sweeper to own, and the directory only grows when renders happen."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path))
    from fused_render.server.routers import ai_runtime

    room = tmp_path / "ai" / "images"
    room.mkdir(parents=True)
    orphan = room / "20250101-000000-old.preview.png"
    orphan.write_bytes(b"x")
    _age(orphan, ai_runtime._PREVIEW_TTL + 60)

    started = client.post("/api/ai/image", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    assert not orphan.exists()
    _wait_job(started["jobId"])


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


def test_an_explicit_ZERO_steps_or_guidance_is_CLAMPED_not_REPLACED(
        client, fake_image_runner):
    """`body.get("steps") or default` reads an explicit `0` as "the caller
    said nothing" and silently substitutes the default — this predates the
    edit option (the base commit already had `body.get("steps") or 28`),
    but two different defaults depending on mode is what makes the
    substitution obvious rather than a one-in-a-million miss. `0` is a
    real, in-range value for `guidance` and a real (if extreme) request for
    `steps`, and either must be CLAMPED, never quietly swapped out."""
    reply = client.post(
        "/api/ai/image", json={"prompt": "x", "steps": 0, "guidance": 0},
        headers={"X-Fused": "1"}).json()
    assert reply["steps"] == 1          # clamped to the floor, not 28
    assert reply["guidance"] == 0.0     # honoured, not replaced with 4.0


def test_a_fresh_render_defaults_to_the_curated_models_own_size(
        client, fake_image_runner, monkeypatch):
    """`segmind/tiny-sd` is 512x512-native and, as position 0 of a
    smallest-first list, is also `catalog.default_for()` — what a
    model-less `fused.ai.image()` loads. Its curated `defaults` names that
    size, and a fresh (non-edit) render with no `width`/`height` of its own
    must land on it rather than the generic 1024² meant for a model the
    catalog says nothing about."""
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-image", [
        {"id": "org/fake-image-small", "label": "Fake image (small)",
         "size_gb": 0.1, "note": "", "defaults": {"width": 512, "height": 512}},
    ])
    started = client.post("/api/ai/image", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    assert (started["width"], started["height"]) == (512, 512)
    _wait_job(started["jobId"])


def test_an_explicit_size_still_wins_over_the_curated_default(
        client, fake_image_runner, monkeypatch):
    """A caller's own `width`/`height` overrides the curated hint exactly as
    it overrides the plain 1024² default — this only changes what a caller
    who said nothing gets."""
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-image", [
        {"id": "org/fake-image-small", "label": "Fake image (small)",
         "size_gb": 0.1, "note": "", "defaults": {"width": 512, "height": 512}},
    ])
    started = client.post(
        "/api/ai/image", json={"prompt": "x", "width": 768, "height": 768},
        headers={"X-Fused": "1"}).json()
    assert (started["width"], started["height"]) == (768, 768)
    _wait_job(started["jobId"])


def test_a_fresh_render_of_an_UNCURATED_model_still_gets_1024(
        client, fake_image_runner):
    """A cached repo the user downloaded themselves has no row in
    `catalog.SUGGESTIONS` at all — `catalog.entry_for` returns None for it,
    and the route must keep the plain 1024² default rather than erroring or
    guessing a size."""
    started = client.post(
        "/api/ai/image", json={"prompt": "x", "model": "some/uncurated-repo"},
        headers={"X-Fused": "1"}).json()
    assert (started["width"], started["height"]) == (1024, 1024)
    _wait_job(started["jobId"])


def test_an_edit_still_derives_its_size_from_the_base_image_not_the_curated_one(
        client, fake_image_runner, monkeypatch, tmp_path):
    """Decision 1 keeps winning for an edit even when the resolved model
    carries a curated size: `image_path is not None` short-circuits the
    curated-default branch entirely, exactly as it already short-circuits
    the plain 1024² one."""
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-image", [
        {"id": "org/fake-image-small", "label": "Fake image (small)",
         "size_gb": 0.1, "note": "", "defaults": {"width": 512, "height": 512}},
    ])
    page = tmp_path / "pages" / "editor.html"
    page.parent.mkdir(parents=True)
    page.write_text("<html></html>")
    photo = page.parent / "photo.png"
    photo.write_bytes(_png_bytes(2000, 1000))

    started = client.post(
        "/api/ai/image",
        json={"prompt": "x", "image": "photo.png", "base": str(page)},
        headers={"X-Fused": "1"}).json()
    assert (started["width"], started["height"]) == (1024, 512)
    _wait_job(started["jobId"])


def test_a_fresh_render_takes_the_curated_models_own_steps_and_guidance(
        client, fake_image_runner, monkeypatch):
    """`segmind/tiny-sd` declares `"steps": 16, "guidance": 7.5` because 16
    steps is the first point on its measured convergence curve that looks
    finished (MAD 3.2 against a converged 28-step render, vs. 12.3 at 8) and
    7.5 is real classifier-free guidance for its non-distilled SD1.5 UNet —
    a fresh render with no `steps`/`guidance` of its own must land on those
    curated numbers rather than the generic 28/4.0 meant for a model the
    catalog says nothing about."""
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-image", [
        {"id": "org/fake-image-small", "label": "Fake image (small)",
         "size_gb": 0.1, "note": "",
         "defaults": {"width": 512, "height": 512, "steps": 16, "guidance": 7.5}},
    ])
    started = client.post("/api/ai/image", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    assert started["steps"] == 16
    assert started["guidance"] == 7.5
    _wait_job(started["jobId"])


def test_an_explicit_steps_and_guidance_still_win_over_the_curated_defaults(
        client, fake_image_runner, monkeypatch):
    """A caller's own `steps`/`guidance` overrides the curated hint exactly
    as it overrides the plain 28/4.0 default, and still goes through the
    same clamp — this only changes what a caller who said nothing gets."""
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-image", [
        {"id": "org/fake-image-small", "label": "Fake image (small)",
         "size_gb": 0.1, "note": "",
         "defaults": {"width": 512, "height": 512, "steps": 16, "guidance": 7.5}},
    ])
    started = client.post(
        "/api/ai/image", json={"prompt": "x", "steps": 20, "guidance": 3.0},
        headers={"X-Fused": "1"}).json()
    assert started["steps"] == 20
    assert started["guidance"] == 3.0
    _wait_job(started["jobId"])


def test_a_fresh_render_of_an_UNCURATED_model_still_gets_28_and_4(
        client, fake_image_runner):
    """A cached repo the user downloaded themselves has no row in
    `catalog.SUGGESTIONS` at all — `catalog.entry_for` returns None for it,
    and the route must keep the plain 28/4.0 default rather than erroring or
    guessing a step count."""
    started = client.post(
        "/api/ai/image", json={"prompt": "x", "model": "some/uncurated-repo"},
        headers={"X-Fused": "1"}).json()
    assert started["steps"] == 28
    assert started["guidance"] == 4.0
    _wait_job(started["jobId"])


def test_a_curated_entry_with_only_a_size_still_gets_28_and_4(
        client, fake_image_runner, monkeypatch):
    """A curated entry can name size without naming steps or guidance — the
    two klein rows below tiny-sd in the real catalog do exactly this, since
    they take the route's own 4-step distilled-guidance defaults rather than
    a per-model override. Each field the entry leaves unnamed must fall back
    to the generic default independently of the ones it does name."""
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-image", [
        {"id": "org/fake-image-small", "label": "Fake image (small)",
         "size_gb": 0.1, "note": "", "defaults": {"width": 512, "height": 512}},
    ])
    started = client.post("/api/ai/image", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    assert started["steps"] == 28
    assert started["guidance"] == 4.0
    _wait_job(started["jobId"])


def test_an_edit_still_gets_4_steps_and_guidance_1_even_with_a_curated_model(
        client, fake_image_runner, monkeypatch, tmp_path):
    """The edit path's own defaults (4 steps, guidance 1.0 — the prototype's
    numbers, not a generate default) must keep winning even when the
    resolved model carries curated `steps`/`guidance`: `image_path is not
    None` short-circuits the curated-default branch entirely, exactly as it
    already short-circuits the curated-size lookup."""
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-image", [
        {"id": "org/fake-image-small", "label": "Fake image (small)",
         "size_gb": 0.1, "note": "",
         "defaults": {"width": 512, "height": 512, "steps": 16, "guidance": 7.5}},
    ])
    page = tmp_path / "pages" / "editor.html"
    page.parent.mkdir(parents=True)
    page.write_text("<html></html>")
    photo = page.parent / "photo.png"
    photo.write_bytes(_png_bytes(2000, 1000))

    started = client.post(
        "/api/ai/image",
        json={"prompt": "x", "image": "photo.png", "base": str(page)},
        headers={"X-Fused": "1"}).json()
    assert started["steps"] == 4
    assert started["guidance"] == 1.0
    _wait_job(started["jobId"])


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


def test_an_unrecognised_image_option_is_a_400_naming_it(client, fake_image_runner):
    """The bug report: `image`/`strength` render a text-to-image picture and say
    nothing about the option that was ignored. `image` is now a real option
    (SPEC AI-9f) — `strength` is the one Decision 2 keeps out on purpose,
    since the edit mechanism does not use it at all — so it is what still
    stands for "an option this endpoint does not have"."""
    response = client.post(
        "/api/ai/image", json={"prompt": "a fox", "strength": 0.6},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    message = response.json()["error"]
    assert "strength" in message
    assert "not an option" in message


def test_an_unrecognised_image_option_names_BOTH_unknown_keys(client, fake_image_runner):
    response = client.post(
        "/api/ai/image",
        json={"prompt": "a fox", "strength": 0.6, "bogus": 1},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    message = response.json()["error"]
    assert "bogus" in message and "strength" in message


def test_the_envelope_is_checked_BEFORE_any_field_validation(client, fake_image_runner):
    """An unknown key and a bad `steps` in the same request: the envelope error
    wins, so the caller learns about the option it does not have first."""
    response = client.post(
        "/api/ai/image", json={"prompt": "a fox", "strength": 0.6, "steps": "nonsense"},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    message = response.json()["error"]
    assert "'strength' is not an option" in message
    # The field error ("'steps' must be a number") never appears — the
    # envelope check short-circuits before `steps` is ever parsed.
    assert "must be a number" not in message


def test_the_server_accepts_base_the_bridge_would_have_injected(
        client, fake_image_runner):
    """The SERVER's accepted set is the wider one on purpose (`base` is
    bridge-injected, same asymmetry `/api/ai/transcribe` carries) — a raw
    `curl` against this endpoint, which is what the skill documents, must be
    able to pass it directly the way the bridge does on a caller's behalf."""
    response = client.post(
        "/api/ai/image", json={"prompt": "a fox", "base": "/pages/other.html"},
        headers={"X-Fused": "1"})
    assert response.status_code == 200, response.json()


def test_every_documented_image_option_is_still_accepted(client, fake_image_runner):
    """The regression this change could plausibly cause: a false rejection of a
    valid option. Assert the whole accepted set, not two of them."""
    body = {
        "prompt": "a fox", "model": "org/x", "width": 512, "height": 512,
        "steps": 10, "guidance": 3.0, "seed": 7,
    }
    response = client.post("/api/ai/image", json=body, headers={"X-Fused": "1"})
    assert response.status_code == 200, response.json()


# -- editing a base image (SPEC AI-9f) -------------------------------------------
#
# mflux-only: `fake_image_runner`'s code is not a diffusers one, so every test
# below that exercises the SUCCESS path runs as if mflux were resolved — the
# refusal path (a diffusers code) has its own fixture and its own tests.


def _png_bytes(width, height):
    """The 24 bytes `_image_pixel_size` actually reads, plus enough padding
    that a real PNG signature check would not choke on a short read. Not a
    valid PNG otherwise (no IDAT, no CRCs) — this app's reader never needs
    one, and a test that faked a full PNG would be testing Pillow's
    decoder, not this one."""
    import struct as _struct
    return (b"\x89PNG\r\n\x1a\n" + _struct.pack(">I", 13) + b"IHDR"
            + _struct.pack(">II", width, height) + b"\x00" * 5)


@pytest.fixture()
def base_photo(tmp_path):
    """A `photo.png` beside a fake page, both real files on disk — the shape
    `image`/`base` resolution actually reads. 2000x1000, deliberately not
    already a multiple of 16 after scaling, so the snap-down arithmetic has
    something to do."""
    page = tmp_path / "pages" / "editor.html"
    page.parent.mkdir(parents=True)
    page.write_text("<html></html>")
    photo = page.parent / "photo.png"
    photo.write_bytes(_png_bytes(2000, 1000))
    return str(page), str(photo)


# -- WebP: all three sub-formats, against REAL Pillow-written files -------------
#
# `cwebp`, Pillow and a browser's own "Save as WebP" all emit plain `VP8 `
# (lossy) or `VP8L` (lossless) — only an explicit request for alpha/EXIF/ICC
# or animation gets the extended `VP8X` container. A reader that understood
# only `VP8X` would silently fall back to the 1024x1024 default for every
# ORDINARY WebP, stretching the render — exactly the class of surprise this
# feature exists to avoid. These write real files through Pillow rather than
# hand-rolled bytes, because the hand-rolled PNG fixture above is honest about
# testing the READER and not the ENCODER, but a hand-rolled VP8/VP8L bitstream
# would itself be the thing under test if this file wrote the bytes.


def test_a_LOSSY_webp_VP8_is_read_correctly(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    photo = tmp_path / "lossy.webp"
    Image.new("RGB", (1183, 800), (255, 0, 0)).save(
        str(photo), "WEBP", lossless=False, quality=80)
    assert open(photo, "rb").read(16)[12:16] == b"VP8 ", "fixture drifted"
    assert ai_runtime._image_pixel_size(str(photo)) == (1183, 800)


def test_a_LOSSLESS_webp_VP8L_is_read_correctly(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    photo = tmp_path / "lossless.webp"
    Image.new("RGB", (777, 333), (0, 255, 0)).save(
        str(photo), "WEBP", lossless=True)
    assert open(photo, "rb").read(16)[12:16] == b"VP8L", "fixture drifted"
    assert ai_runtime._image_pixel_size(str(photo)) == (777, 333)


def test_an_EXTENDED_webp_VP8X_still_works(tmp_path):
    """The one form this reader always understood — pinned again here
    against a REAL file (alpha forces the extended container) rather than
    only the hand-rolled bytes further down, now that it has two real
    siblings to be consistent with."""
    pytest.importorskip("PIL")
    from PIL import Image

    photo = tmp_path / "extended.webp"
    Image.new("RGBA", (500, 900), (0, 0, 255, 128)).save(str(photo), "WEBP")
    assert open(photo, "rb").read(16)[12:16] == b"VP8X", "fixture drifted"
    assert ai_runtime._image_pixel_size(str(photo)) == (500, 900)


def test_a_plain_webp_edit_does_NOT_silently_come_back_square(
        client, fake_image_runner, tmp_path):
    """End to end, through the real endpoint: a caller who saved a WebP the
    ordinary way (`cwebp`, Pillow's default, a browser's "Save as WebP")
    must get the SIZE-FROM-BASE promise SPEC AI-9f and the SKILL both make,
    not a silent fallback to 1024x1024 that stretches whatever renders."""
    pytest.importorskip("PIL")
    from PIL import Image

    page = tmp_path / "pages" / "editor.html"
    page.parent.mkdir(parents=True)
    page.write_text("<html></html>")
    photo = page.parent / "photo.webp"
    Image.new("RGB", (1183, 800), (10, 20, 30)).save(str(photo), "WEBP")

    started = client.post(
        "/api/ai/image",
        json={"prompt": "a fox", "image": "photo.webp", "base": str(page)},
        headers={"X-Fused": "1"}).json()
    assert (started["width"], started["height"]) == (1024, 688)
    _wait_job(started["jobId"])


def test_a_REAL_jpeg_is_read_correctly(tmp_path):
    """The one format above with no synthetic-bytes test at all — Pillow's
    default encoder, against the marker walk rather than a hand-rolled
    SOF0 segment."""
    pytest.importorskip("PIL")
    from PIL import Image

    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (640, 427), (128, 64, 32)).save(str(photo), "JPEG")
    assert ai_runtime._image_pixel_size(str(photo)) == (640, 427)


def test_editing_needs_a_base_image_that_EXISTS(client, fake_image_runner):
    response = client.post(
        "/api/ai/image", json={"prompt": "a fox", "image": "/nope/nowhere.png"},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "no such file" in response.json()["error"]
    assert not [j for j in jobs.list_jobs()
                if j["id"].startswith(supervisor.IMAGE_JOB_PREFIX)]


def test_editing_refuses_a_relative_image_with_no_base(client, fake_image_runner):
    response = client.post(
        "/api/ai/image", json={"prompt": "a fox", "image": "photo.png"},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "'image' must be absolute" in response.json()["error"]


def test_editing_resolves_a_RELATIVE_image_against_base(
        client, fake_image_runner, base_photo):
    """RH-1, restated for `image` the way `/api/ai/transcribe`'s `path`
    already has it: a relative `image` resolves against the directory of
    `base`, exactly what the bridge injects from the page's own `?path=`."""
    page, photo = base_photo
    started = client.post(
        "/api/ai/image", json={"prompt": "a fox", "image": "photo.png", "base": page},
        headers={"X-Fused": "1"}).json()
    assert started["image"] == ai_runtime.canonical_fs_path(photo)
    _wait_job(started["jobId"])


def test_an_absolute_image_ignores_base(client, fake_image_runner, base_photo):
    _page, photo = base_photo
    started = client.post(
        "/api/ai/image", json={"prompt": "a fox", "image": photo},
        headers={"X-Fused": "1"}).json()
    assert started["image"] == ai_runtime.canonical_fs_path(photo)
    _wait_job(started["jobId"])


def test_an_ARRAY_image_is_a_400_not_a_guess(client, fake_image_runner):
    """Decision 4: one image, a single string. mflux's own argument is a
    list (`image_paths`), and reading that as license to accept a list HERE
    would ship untested multi-reference conditioning inside a freshly opened
    envelope."""
    response = client.post(
        "/api/ai/image", json={"prompt": "a fox", "image": ["a.png", "b.png"]},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "single string" in response.json()["error"]


def test_a_NON_STRING_image_is_a_400(client, fake_image_runner):
    response = client.post(
        "/api/ai/image", json={"prompt": "a fox", "image": 7},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "single string" in response.json()["error"]


def test_an_EMPTY_image_is_a_400_not_an_unedited_render(client, fake_image_runner):
    response = client.post(
        "/api/ai/image", json={"prompt": "a fox", "image": "   "},
        headers={"X-Fused": "1"})
    assert response.status_code == 400


def test_the_request_the_WORKER_gets_carries_image_ONLY_when_asked(
        client, fake_image_runner, base_photo, monkeypatch):
    """`mflux_image/worker.py` derives its MODE off whether `image` is a key
    in the request at all — this pins that the route only ever sends the key
    when a caller actually asked for an edit, never a `None` placeholder."""
    page, photo = base_photo
    captured = []
    real_start = supervisor.start_image

    def spy(model, request, job):
        captured.append(dict(request))
        return real_start(model, request, job)

    monkeypatch.setattr(supervisor, "start_image", spy)

    plain = client.post("/api/ai/image", json={"prompt": "a fox"},
                        headers={"X-Fused": "1"}).json()
    _wait_job(plain["jobId"])
    edit = client.post(
        "/api/ai/image", json={"prompt": "a fox", "image": "photo.png", "base": page},
        headers={"X-Fused": "1"}).json()
    _wait_job(edit["jobId"])

    assert "image" not in captured[0]
    assert os.path.isabs(captured[1]["image"])
    assert os.path.samefile(captured[1]["image"], photo)


def test_an_edits_DEFAULTS_are_the_PROTOTYPES_not_the_generate_defaults(
        client, fake_image_runner, base_photo, monkeypatch):
    """Decision 1's other half: an edit that omits `steps`/`guidance` must
    get 4/1.0 (the prototype's own numbers), not the 28/4.0 shared between
    both engines' plain generate path — silently applying the generate
    defaults to an edit is a real quality regression."""
    page, _photo = base_photo
    captured = []
    real_start = supervisor.start_image
    monkeypatch.setattr(supervisor, "start_image",
                        lambda model, request, job: (captured.append(dict(request)),
                                                      real_start(model, request, job))[1])

    edit = client.post(
        "/api/ai/image", json={"prompt": "a fox", "image": "photo.png", "base": page},
        headers={"X-Fused": "1"}).json()
    _wait_job(edit["jobId"])
    plain = client.post("/api/ai/image", json={"prompt": "a fox"},
                        headers={"X-Fused": "1"}).json()
    _wait_job(plain["jobId"])

    assert edit["steps"] == 4 and edit["guidance"] == 1.0
    assert plain["steps"] == 28 and plain["guidance"] == 4.0
    # An explicit value still wins over either default.
    explicit = client.post(
        "/api/ai/image",
        json={"prompt": "a fox", "image": "photo.png", "base": page, "steps": 9},
        headers={"X-Fused": "1"}).json()
    _wait_job(explicit["jobId"])
    assert explicit["steps"] == 9


def test_an_edits_default_SIZE_comes_from_the_base_image(
        client, fake_image_runner, base_photo):
    """Decision 1: fit the longest side to 1024 without upscaling, snap down
    to a multiple of 16, floor 256, aspect preserved — the prototype's own
    arithmetic, confirmed as written by the gate run. The fixture's 2000x1000
    scales by 1024/2000 = 0.512 -> 1024x512, both already multiples of 16."""
    page, _photo = base_photo
    started = client.post(
        "/api/ai/image", json={"prompt": "a fox", "image": "photo.png", "base": page},
        headers={"X-Fused": "1"}).json()
    assert (started["width"], started["height"]) == (1024, 512)
    _wait_job(started["jobId"])


def test_an_edits_default_size_is_pinned_against_the_size_ARITHMETIC_directly(tmp_path):
    """The arithmetic itself, off `_edit_default_size` — a fixture image
    whose scaled sides are NOT already multiples of 16, so the snap-DOWN
    (never up, never a round) is what the test actually exercises."""
    photo = tmp_path / "odd.png"
    photo.write_bytes(_png_bytes(1500, 900))
    # scale = 1024/1500 ~ 0.6827; 1500*scale ~ 1024.0 -> //16*16 == 1024;
    # 900*scale ~ 614.4 -> int() == 614 -> //16*16 == 608.
    assert ai_runtime._edit_default_size(str(photo)) == (1024, 608)


def test_an_edits_default_size_never_UPSCALES_a_small_base(tmp_path):
    photo = tmp_path / "small.png"
    photo.write_bytes(_png_bytes(300, 200))
    # scale = min(1.0, 1024/300) = 1.0 -> unchanged, then snapped down.
    assert ai_runtime._edit_default_size(str(photo)) == (288, 256)


def test_the_256_FLOOR_overrides_aspect_on_an_extreme_ratio(tmp_path):
    """Documented, not a bug to route around: a 4000x200 base (20:1) fits
    its long side to 1024 (200 -> 51, floored to 256) and comes back
    1024x256 (4:1) — the arithmetic as confirmed on hardware, which is why
    the docstring, SPEC AI-9f and the SKILL all now say the floor overrides
    "aspect preserved" on an extreme ratio rather than merely asserting the
    aspect claim unqualified."""
    photo = tmp_path / "banner.png"
    photo.write_bytes(_png_bytes(4000, 200))
    assert ai_runtime._edit_default_size(str(photo)) == (1024, 256)


def test_an_edits_default_size_HITS_1024_EXACTLY_not_1008(tmp_path):
    """A width `float` rounding regresses on: `1024.0 / 1122 * 1122`
    lands on `1023.9999999999999` rather than `1024.0`, and a truncating
    `int()` then snaps that DOWN a whole extra `_SIDE_STEP` — `1122x600`
    coming back `1008x544` instead of the `1024x544` the docstring
    promises. Integer division (`width * 1024 // longest`) cancels exactly
    and does not have this failure mode. `1183x800` is the same bug from
    the other axis (height the long side)."""
    photo = tmp_path / "long.png"
    photo.write_bytes(_png_bytes(1122, 600))
    assert ai_runtime._edit_default_size(str(photo)) == (1024, 544)

    tall = tmp_path / "tall.png"
    tall.write_bytes(_png_bytes(800, 1183))
    assert ai_runtime._edit_default_size(str(tall)) == (688, 1024)


def test_an_explicit_width_still_wins_over_the_edit_default(
        client, fake_image_runner, base_photo):
    page, _photo = base_photo
    started = client.post(
        "/api/ai/image",
        json={"prompt": "a fox", "image": "photo.png", "base": page, "width": 512},
        headers={"X-Fused": "1"}).json()
    assert started["width"] == 512
    _wait_job(started["jobId"])


def test_an_explicit_ZERO_guidance_on_an_EDIT_is_honoured_not_replaced(
        client, fake_image_runner, base_photo):
    """The same falsy-`or` bug, under the edit defaults (4/1.0) rather than
    the generate ones (28/4.0) — either default silently swallowing an
    explicit `0` is wrong, and the edit path is where the two different
    defaults made the bug worth fixing in the first place."""
    page, _photo = base_photo
    started = client.post(
        "/api/ai/image",
        json={"prompt": "a fox", "image": "photo.png", "base": page,
             "steps": 0, "guidance": 0},
        headers={"X-Fused": "1"}).json()
    assert started["steps"] == 1        # clamped to the floor, not 4
    assert started["guidance"] == 0.0   # honoured, not replaced with 1.0
    _wait_job(started["jobId"])


def _only_image_runner(tmp_path, monkeypatch, code):
    """A registry whose ONLY runner renders images, under `code`, with the
    fake worker — so the endpoint's `image` refusal can be exercised against
    a REAL diffusers code rather than only the mechanism in isolation
    (`test_ai_engine_options.py`)."""
    folder = tmp_path / ("fake_runner_" + code.replace("-", "_"))
    folder.mkdir()
    (folder / "worker.py").write_text(FAKE_IMAGE_WORKER)
    runner = registry.Runner(
        code=code, capability=registry.IMAGE_GENERATION,
        folder=str(folder), label="Fake diffusers image",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (runner,))
    monkeypatch.setitem(catalog.SUGGESTIONS, code, [
        {"id": "org/fake-diffusers", "label": "Fake diffusers image",
         "size_gb": None, "note": ""},
    ])
    monkeypatch.setattr(supervisor, "_ensure_venv", lambda r, w, j: sys.executable)
    monkeypatch.setattr(supervisor, "_require_build_tools", lambda: None)
    return runner


@pytest.fixture()
def fake_diffusers_image_runner(tmp_path, monkeypatch):
    yield _only_image_runner(tmp_path, monkeypatch, "diffusers-image")
    supervisor.unload()
    supervisor.reset()


def test_the_diffusers_engine_refuses_image_BEFORE_a_job_opens(
        client, fake_diffusers_image_runner, base_photo):
    """`engine_options.py`'s real table, not a fake entry: the diffusers
    image engine — resolved here as the ONLY registered runner — refuses
    `image` with the sentence naming the way out, and no job row opens for
    it (the same treatment `test_an_option_the_RESOLVED_engine_cannot_honour_
    is_refused_before_a_job_opens` gives the transcribe side)."""
    page, _photo = base_photo
    response = client.post(
        "/api/ai/image", json={"prompt": "a fox", "image": "photo.png", "base": page},
        headers={"X-Fused": "1"})
    assert response.status_code == 400, response.json()
    assert "Diffusers image engine" in response.json()["error"]
    assert not [j for j in jobs.list_jobs()
                if j["id"].startswith(supervisor.IMAGE_JOB_PREFIX)]


def test_the_diffusers_engine_still_renders_an_ORDINARY_prompt(
        client, fake_diffusers_image_runner):
    """The table is an exception list keyed on `image`'s presence — a plain
    render must not be caught in the same net."""
    started = client.post("/api/ai/image", json={"prompt": "a fox"},
                          headers={"X-Fused": "1"}).json()
    _wait_job(started["jobId"])


def test_the_image_endpoint_and_the_worker_refuse_it_by_the_SAME_rule(
        client, fake_diffusers_image_runner, base_photo):
    """One sentence, one place — the image side of the same claim
    `test_the_endpoint_and_the_worker_refuse_an_option_by_the_SAME_rule`
    already pins for transcribe."""
    from fused_render.ai.runners import engine_options

    page, _photo = base_photo
    response = client.post(
        "/api/ai/image", json={"prompt": "a fox", "image": "photo.png", "base": page},
        headers={"X-Fused": "1"})
    with pytest.raises(ValueError) as raised:
        engine_options.unsupported_or_raise("diffusers-image", image="photo.png")
    assert response.json()["error"] == str(raised.value)


@pytest.fixture()
def fake_mflux_image_runner(tmp_path, monkeypatch):
    """Same fake worker as `fake_diffusers_image_runner`, resolved under the
    REAL `mflux-image` code — needed because the edit-recipe check below is
    keyed on that exact string, and a fake code would never reach it."""
    yield _only_image_runner(tmp_path, monkeypatch, "mflux-image")
    supervisor.unload()
    supervisor.reset()


def test_editing_a_model_with_NO_edit_recipe_is_refused_before_a_job_opens(
        client, fake_mflux_image_runner, base_photo, monkeypatch):
    """The ENGINE can edit (mflux resolved as the only runner) but this
    specific MODEL has no row in `formats.MFLUX_EDIT_VARIANTS` — refused
    here, before a job row opens, for the identical reason the engine-level
    refusal a few lines up is: a venv build and a multi-GB download must
    not happen before the worker's own `_build_variant` raises the same
    fact."""
    from fused_render.ai.runners import formats

    known = next(iter(formats.MFLUX_VARIANTS))
    monkeypatch.delitem(formats.MFLUX_EDIT_VARIANTS, known)
    page, _photo = base_photo
    response = client.post(
        "/api/ai/image",
        json={"prompt": "a fox", "image": "photo.png", "base": page, "model": known},
        headers={"X-Fused": "1"})
    assert response.status_code == 400, response.json()
    assert "no edit variant" in response.json()["error"]
    assert not [j for j in jobs.list_jobs()
                if j["id"].startswith(supervisor.IMAGE_JOB_PREFIX)]


def test_editing_a_model_WITH_an_edit_recipe_still_opens_a_job(
        client, fake_mflux_image_runner, base_photo):
    """The check is an exception, not a blanket refusal of every mflux
    edit — a model that DOES have a row in both tables must still work."""
    from fused_render.ai.runners import formats

    known = next(iter(formats.MFLUX_VARIANTS))
    assert formats.mflux_edit_recipe(known) is not None
    page, _photo = base_photo
    started = client.post(
        "/api/ai/image",
        json={"prompt": "a fox", "image": "photo.png", "base": page, "model": known},
        headers={"X-Fused": "1"}).json()
    _wait_job(started["jobId"])


# -- the fit verdict: measured > declared > download (SPEC AI-16, AI-16c, D497) -


def _fit_text_row(client):
    rows = client.get("/api/ai/catalog").json()["capabilities"]
    return next(row for row in rows if row["capability"] == registry.TEXT_GENERATION)


@pytest.fixture()
def fixed_fit_machine(monkeypatch):
    """A pinned 32GB machine with no Apple-Silicon wired ceiling in play, so
    the threshold arithmetic these tests reach for is deterministic on any
    host the suite runs on. `fit.py`'s OWN threshold arithmetic is tested
    directly in `tests/test_ai_fit.py`; this fixture is only what lets the
    route-level tests below assert a STABLE verdict word."""
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: 32.0)
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: None)


def test_fit_is_null_when_nothing_is_known(client, fake_runner, fixed_fit_machine,
                                           monkeypatch):
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-text", [
        {"id": "org/unknown-size", "label": "Unknown", "size_gb": None, "note": ""},
    ])
    entry = _fit_text_row(client)["models"][0]
    assert entry["id"] == "org/unknown-size"
    assert entry["fit"] is None


def test_fit_basis_download_from_size_gb_alone(client, fake_runner, fixed_fit_machine,
                                               monkeypatch):
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-text", [
        {"id": "org/only-size", "label": "Only size", "size_gb": 4.0, "note": ""},
    ])
    entry = _fit_text_row(client)["models"][0]
    # SPEC AI-19 item 3: the flat runtime-overhead constant now lands on
    # every `download`-rung estimate, and `score`/`runMode` are new fields
    # on every verdict — `fit.py`'s own tests cover their arithmetic in
    # full; this route-level test only needs the shape to still be right.
    assert entry["fit"]["basis"] == "download"
    assert entry["fit"]["verdict"] == "easy"
    assert entry["fit"]["footprintBytes"] == 4.0 * 1e9 + fit.RUNTIME_OVERHEAD_BYTES


def test_fit_basis_declared_wins_over_download(client, fake_runner, fixed_fit_machine,
                                               monkeypatch):
    """A curator's optional `resident_gb` — the shape AI-11i/AI-11j already
    established for `recommended`/`acceptsImage` — outranks the download
    figure, the same way LTX-2.3's `low_memory=True` peak is smaller than its
    two-repo download total."""
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-text", [
        {"id": "org/declared", "label": "Declared", "size_gb": 28.5,
         "resident_gb": 12.0, "note": ""},
    ])
    entry = _fit_text_row(client)["models"][0]
    assert entry["fit"]["basis"] == "declared"
    assert entry["fit"]["footprintBytes"] == 12.0 * 1e9
    assert entry["fit"]["verdict"] == "easy"


def test_fit_basis_measured_wins_over_declared_and_download(
        client, fake_runner, fixed_fit_machine, monkeypatch):
    """SPEC AI-16a: a model that has actually RUN here beats a guess about it,
    whichever guess — the download's byte count or a curator's estimate."""
    footprints.record(registry.TEXT_GENERATION, "org/measured", 5_000_000_000)
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-text", [
        {"id": "org/measured", "label": "Measured", "size_gb": 28.5,
         "resident_gb": 12.0, "note": ""},
    ])
    entry = _fit_text_row(client)["models"][0]
    assert entry["fit"]["basis"] == "measured"
    assert entry["fit"]["footprintBytes"] == 5_000_000_000


def test_the_catalog_forwards_harvested_kv_geometry_into_the_fit_footprint(
        client, fake_runner, fixed_fit_machine, monkeypatch):
    """Code review finding: `describe_catalog` called `fit.verdict` without
    ANY of the `num_hidden_layers`/`num_key_value_heads`/`num_attention_
    heads`/`head_dim`/`hidden_size`/`layer_types` kwargs that feed
    `fit.footprint_bytes`'s KV-cache term (fit.py L640-662), even though
    `hub_metadata.cached()` — read on this same request for the vision/
    tool-use tags — already holds that geometry on disk. Without forwarding
    it, every download-rung footprint collapses to exactly
    `size_gb * 1e9 + RUNTIME_OVERHEAD_BYTES`, with a KV term of zero no
    matter how big the context window or how wide the model. This test pins
    a row WITH cached geometry getting a strictly bigger footprint than that
    flat figure, so the wiring cannot go inert again."""
    monkeypatch.setattr(ai_runtime.hub_metadata, "cached", lambda model_id: {
        "numHiddenLayers": 32,
        "numKeyValueHeads": 8,
        "numAttentionHeads": 32,
        "headDim": 128,
        "hiddenSize": 4096,
        "layerTypes": None,
    } if model_id == "org/geometry-known" else None)
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-text", [
        {"id": "org/geometry-known", "label": "Geometry known", "size_gb": 4.0,
         "note": ""},
    ])
    entry = _fit_text_row(client)["models"][0]
    assert entry["id"] == "org/geometry-known"
    flat_floor = 4.0 * 1e9 + fit.RUNTIME_OVERHEAD_BYTES
    assert entry["fit"]["basis"] == "download"
    assert entry["fit"]["footprintBytes"] > flat_floor, (
        "KV-cache term is 0 despite cached geometry being available — "
        "hub_metadata.cached() geometry was not forwarded to fit.verdict")


def test_kv_geometry_kwargs_supplies_a_q8_0_kv_dtype_for_the_llamacpp_runners(
        monkeypatch):
    """`_KV_DTYPE_RUNNERS` names both llama.cpp runner codes — the CPU build
    and the Vulkan build share the exact same loader module and both try a
    q8_0 cache first (`llama_text._kv_cache_kwargs`), so both must get
    `kv_dtype="q8_0"` here, not just one of them."""
    monkeypatch.setattr(ai_runtime.hub_metadata, "cached", lambda model_id: None)
    for code in ("llamacpp-text", "llamacpp-text-vulkan"):
        assert ai_runtime._kv_geometry_kwargs("org/m", code) == {"kv_dtype": "q8_0"}


def test_kv_geometry_kwargs_leaves_kv_dtype_unset_for_every_other_runner(
        monkeypatch):
    """No other runner in `registry.py` quantizes its KV cache — MLX,
    diffusers, CT2 and ONNX all still cache at fp16 or have no KV cache at
    all — so `fit.py`'s own fp16 default must apply for any runner code that
    is not one of the two llama.cpp ones, including `None` (no runner
    resolved for this machine at all)."""
    monkeypatch.setattr(ai_runtime.hub_metadata, "cached", lambda model_id: None)
    for code in ("mlx-text", "diffusers-image", "faster-whisper", "onnx-embed", None):
        assert "kv_dtype" not in ai_runtime._kv_geometry_kwargs("org/m", code)


def test_the_catalog_uses_a_q8_0_kv_dtype_for_the_llamacpp_runner(
        client, fixed_fit_machine, monkeypatch, tmp_path):
    """End-to-end version of the two unit tests above: a catalog row served
    by a runner whose CODE is `llamacpp-text` gets a strictly smaller
    download-tier footprint than the identical geometry would get at fp16 —
    proving `describe_catalog`'s own runner resolution (`row["runner"]`),
    not a special case in the test, is what selects the q8_0 KV term.
    Registers a runner under the real `llamacpp-text` code rather than the
    suite's usual `fake-text`, since that code is the whole trigger for
    `_KV_DTYPE_RUNNERS`."""
    folder = tmp_path / "llamacpp_fake"
    folder.mkdir()
    (folder / "worker.py").write_text(FAKE_WORKER, encoding="utf-8")
    runner = registry.Runner(
        code="llamacpp-text", capability=registry.TEXT_GENERATION,
        folder=str(folder), label="Fake llama.cpp",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (runner,))
    monkeypatch.setattr(supervisor, "_ensure_venv", lambda r, w, j: sys.executable)
    monkeypatch.setattr(supervisor, "_require_build_tools", lambda: None)
    monkeypatch.setattr(ai_runtime.hub_metadata, "cached", lambda model_id: {
        "numHiddenLayers": 32,
        "numKeyValueHeads": 8,
        "numAttentionHeads": 32,
        "headDim": 128,
        "hiddenSize": 4096,
        "layerTypes": None,
    } if model_id == "org/geometry-known" else None)
    monkeypatch.setitem(catalog.SUGGESTIONS, "llamacpp-text", [
        {"id": "org/geometry-known", "label": "Geometry known", "size_gb": 4.0,
         "note": ""},
    ])
    entry = _fit_text_row(client)["models"][0]
    assert entry["id"] == "org/geometry-known"

    fp16_kv_bytes = fit._kv_cache_bytes(
        num_hidden_layers=32, num_key_value_heads=8, head_dim=128, kv_dtype="fp16")
    q8_0_kv_bytes = fit._kv_cache_bytes(
        num_hidden_layers=32, num_key_value_heads=8, head_dim=128, kv_dtype="q8_0")
    assert q8_0_kv_bytes == pytest.approx(fp16_kv_bytes / 2)
    expected_footprint = 4.0 * 1e9 + q8_0_kv_bytes + fit.RUNTIME_OVERHEAD_BYTES
    assert entry["fit"]["basis"] == "download"
    assert entry["fit"]["footprintBytes"] == pytest.approx(expected_footprint)
    supervisor.unload()
    supervisor.reset()


def test_a_measurement_under_a_DIFFERENT_capability_does_not_leak_into_this_one(
        client, fake_runner, fixed_fit_machine, monkeypatch):
    footprints.record(registry.IMAGE_GENERATION, "org/cross-cap", 9_000_000_000)
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-text", [
        {"id": "org/cross-cap", "label": "Cross capability", "size_gb": 4.0,
         "note": ""},
    ])
    entry = _fit_text_row(client)["models"][0]
    assert entry["fit"]["basis"] == "download"


def test_speed_estimate_is_present_on_a_text_generation_entry_with_a_known_size(
        client, fake_runner, fixed_fit_machine, monkeypatch):
    """SPEC AI-21: `entry["speedEstimate"]` is wired for `text-generation`
    only, and it is `fit.weight_bytes` (not a bare `size_gb`) that feeds it —
    proven by giving a recognized quantization a real params count."""
    monkeypatch.setattr(fit.hw_detect, "cached_hardware", lambda: None)
    monkeypatch.setattr(fit, "is_apple_silicon", lambda: False)
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-text", [
        {"id": "org/speedy", "label": "Speedy", "size_gb": 4.0,
         "params": "4B", "quantization": "GGUF Q4_K_M", "note": ""},
    ])
    entry = _fit_text_row(client)["models"][0]
    speed_estimate = entry["speedEstimate"]
    assert speed_estimate is not None
    assert speed_estimate["method"] == "backend-constant"
    assert speed_estimate["tokensPerSecond"] > 0
    assert speed_estimate["contextTokens"] == fit.KV_CACHE_CONTEXT_TOKENS


def test_speed_estimate_is_null_on_a_non_text_generation_capability(
        client, fake_image_runner, fixed_fit_machine, monkeypatch):
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-image", [
        {"id": "org/img", "label": "Image", "size_gb": 4.0, "note": ""},
    ])
    rows = client.get("/api/ai/catalog").json()["capabilities"]
    row = next(r for r in rows if r["capability"] == registry.IMAGE_GENERATION)
    entry = row["models"][0]
    assert entry["speedEstimate"] is None


def test_the_footprint_store_is_loaded_ONCE_per_catalog_request(
        client, fake_runner, fixed_fit_machine, monkeypatch):
    """Code review on AI-16: the route used to call `fit.verdict` per catalog
    entry, and each of THOSE did its own fresh `footprints.read` — a
    `storage.read_json` open, a JSON parse and a `benchmark.machine()`
    identity check per entry, for a route (`GET /api/ai/catalog`) the picker
    polls. `footprints.load_store()` now happens ONCE per request and every
    entry's `fit.verdict` reads off that same in-memory store — a multi-entry
    catalog (several suggested models under one capability) must not cost
    more than a single `load_store()` call regardless of how many entries it
    answers for."""
    calls = []
    real_load_store = footprints.load_store

    def _counting_load_store():
        calls.append(1)
        return real_load_store()

    monkeypatch.setattr(footprints, "load_store", _counting_load_store)
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-text", [
        {"id": "org/one", "label": "One", "size_gb": 4.0, "note": ""},
        {"id": "org/two", "label": "Two", "size_gb": 8.0, "note": ""},
        {"id": "org/three", "label": "Three", "size_gb": 12.0, "note": ""},
    ])
    row = _fit_text_row(client)
    assert len(row["models"]) >= 3
    assert all(m["fit"] is not None for m in row["models"])
    assert len(calls) == 1


def test_the_hardware_reading_is_loaded_ONCE_per_catalog_request(
        client, fake_runner, fixed_fit_machine, monkeypatch):
    """Code review on AI-19: `fit._select_pool` used to call `hw_detect.
    cached_hardware()` itself on every `fit.verdict` invocation — a fresh
    `storage.read_json` open and JSON parse per catalog ROW, the identical
    cost the test above already fixed for `footprints.load_store()`.
    `hw_detect.cached_hardware()` now happens ONCE per request
    (`describe_catalog`'s own `hardware = hw_detect.cached_hardware()`) and
    every entry's `fit.verdict` reads off that same threaded-through value.

    A test that only checked the verdict/runMode came out right on each row
    would pass equally well against the N-reads-per-row bug this pins — the
    assertion has to be a COUNT, not just a correct answer.

    `speed.estimate_tok_s` is NOT stubbed here — SPEC AI-21 wired it to call
    `hw_detect.cached_hardware()` too (its own, separate call path, one per
    `text-generation` entry), which used to inflate this test's count for a
    reason it was not about, and was stubbed out for that reason. Code
    review caught that as half a fix: `speed.estimate_tok_s` now takes the
    identical `hardware=` parameter `fit.verdict` does and is handed the
    SAME per-request reading below, so this test now asserts the real,
    end-to-end count across BOTH call paths — the assertion that actually
    proves the fix, rather than one that would pass equally well against
    `speed.py`'s own copy of the bug."""
    calls = []
    real_cached_hardware = fit.hw_detect.cached_hardware

    def _counting_cached_hardware():
        calls.append(1)
        return real_cached_hardware()

    monkeypatch.setattr(fit.hw_detect, "cached_hardware", _counting_cached_hardware)
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-text", [
        {"id": "org/one", "label": "One", "size_gb": 4.0, "note": ""},
        {"id": "org/two", "label": "Two", "size_gb": 8.0, "note": ""},
        {"id": "org/three", "label": "Three", "size_gb": 12.0, "note": ""},
    ])
    row = _fit_text_row(client)
    assert len(row["models"]) >= 3
    assert all(m["fit"] is not None for m in row["models"])
    assert all(m["speedEstimate"] is not None for m in row["models"])
    assert len(calls) == 1, f"expected exactly one hw_detect read, got {len(calls)}"


# -- who may be HANDED an image: the catalog's own `acceptsImage` (D467) --------
#
# The Playground's image composer draws its attach affordance off this flag, so
# the flag has to be the endpoint's own answer rather than a second opinion
# about it. Every test below asserts the pair together — the payload's bool and
# what a POST carrying `image` actually does — because a flag that drifts from
# the route is a button that 400s, which is the one failure this field exists
# to prevent.


def _image_row(client):
    rows = client.get("/api/ai/catalog").json()["capabilities"]
    return next(row for row in rows if row["capability"] == registry.IMAGE_GENERATION)


def test_the_catalog_marks_the_EDITABLE_model_and_the_endpoint_agrees(
        client, fake_mflux_image_runner, base_photo, monkeypatch):
    """Two entries under the real mflux code — one with an edit recipe, one
    without — and the flag matches which of them the route will accept."""
    from fused_render.ai.runners import formats

    known = next(iter(formats.MFLUX_EDIT_VARIANTS))
    page, _photo = base_photo
    monkeypatch.setitem(catalog.SUGGESTIONS, "mflux-image", [
        {"id": known, "label": "Editable", "size_gb": None, "note": ""},
        {"id": "org/no-edit-variant", "label": "Render only", "size_gb": None,
         "note": ""},
    ])
    flags = {m["id"]: m["acceptsImage"] for m in _image_row(client)["models"]}
    assert flags[known] is True
    assert flags["org/no-edit-variant"] is False

    # …and the route says the same thing about both, which is the claim.
    refused = client.post(
        "/api/ai/image",
        json={"prompt": "a fox", "image": "photo.png", "base": page,
              "model": "org/no-edit-variant"},
        headers={"X-Fused": "1"})
    assert refused.status_code == 400
    assert "no edit variant" in refused.json()["error"]
    accepted = client.post(
        "/api/ai/image",
        json={"prompt": "a fox", "image": "photo.png", "base": page, "model": known},
        headers={"X-Fused": "1"})
    assert accepted.status_code == 200, accepted.json()
    _wait_job(accepted.json()["jobId"])


def test_the_diffusers_engine_marks_NO_image_model_as_editable(
        client, fake_diffusers_image_runner, base_photo):
    """The engine refusal is the first gate, so on a diffusers machine the
    flag is False for every entry however editable the MODEL may be
    elsewhere — the Playground draws no attach button there at all."""
    from fused_render.ai.runners import formats

    known = next(iter(formats.MFLUX_EDIT_VARIANTS))
    page, _photo = base_photo
    row = _image_row(client)
    assert row["models"], "the fixture's shortlist vanished"
    assert all(m["acceptsImage"] is False for m in row["models"])
    refused = client.post(
        "/api/ai/image",
        json={"prompt": "a fox", "image": "photo.png", "base": page, "model": known},
        headers={"X-Fused": "1"})
    assert refused.status_code == 400
    assert "Diffusers image engine" in refused.json()["error"]


def test_no_TEXT_or_SPEECH_model_claims_to_accept_an_image(client, hub, monkeypatch):
    """False on every non-image capability rather than True by vacancy.

    `engine_options` is an exception list, so a text runner "refuses nothing"
    — and a flag computed off that answer alone would have every chat model in
    the payload claiming it takes a photo.

    **`hub` (an empty, isolated cache) is load-bearing here since AI-11j grew
    a second way to earn True**: none of the curated suggestions in this test
    is actually downloaded, so every one should read False for having no
    cached `config.json` to answer from at all — but on a real dev machine
    that HAS `mlx-community/Qwen3.5-4B-OptiQ-4bit` on disk (a unified
    checkpoint with a real vision tower), the un-isolated cache made this
    assertion machine-dependent: true here, false on a fresh checkout. The
    claim this test makes is about VACANCY, not about a lucky empty disk.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    for row in client.get("/api/ai/catalog").json()["capabilities"]:
        if row["capability"] == registry.IMAGE_GENERATION:
            continue
        for model in row["models"]:
            assert model["acceptsImage"] is False, (
                f"{row['capability']}/{model['id']} claims to accept an image")


# -- `acceptsImage` on TEXT_GENERATION: mlx-vlm reads a tower on demand -------
#
# AI-11j's other half. mlx_text/worker.py loads every checkpoint through
# mlx-vlm now (`lazy=True`), which CAN read a vision tower — but only a
# checkpoint that actually has one, and only on the ONE runner that goes
# through mlx-vlm at all. Both halves are asserted directly against
# `ai_runtime._accepts_image` rather than through the whole catalog endpoint,
# because the fact under test is the FUNCTION's own gate, not the endpoint's
# plumbing (already covered above).


def test_accepts_image_is_true_for_an_mlx_text_model_with_a_vision_tower(hub):
    _cached_repo(hub, "org/vlm", files=("model.safetensors",),
                config={"model_type": "qwen3_5", "vision_config": {"depth": 4}})
    assert ai_runtime._accepts_image(registry.TEXT_GENERATION, "mlx-text", "org/vlm") is True


def test_accepts_image_is_false_for_an_mlx_text_model_with_no_vision_tower(hub):
    """The ordinary chat repo: a real cached checkpoint, `mlx-text` resolved
    it, and it still has nothing to attach a picture to."""
    _cached_repo(hub, "org/plain-chat", files=("model.safetensors",),
                config={"model_type": "llama"})
    assert ai_runtime._accepts_image(registry.TEXT_GENERATION, "mlx-text", "org/plain-chat") is False


def test_accepts_image_is_false_for_a_llamacpp_text_model_even_with_a_vision_config(hub):
    """The RUNNER gate, not only the checkpoint's own config: llama.cpp's GGUF
    loader has no path to a vision tower at all here, whatever a cached
    repo's `config.json` happens to say — a model that resolves to
    `llamacpp-text` must come back False exactly as it did before this
    build."""
    _cached_repo(hub, "org/vlm", files=("model.safetensors",),
                config={"model_type": "qwen3_5", "vision_config": {"depth": 4}})
    assert ai_runtime._accepts_image(
        registry.TEXT_GENERATION, "llamacpp-text", "org/vlm") is False


def test_accepts_image_is_false_with_no_runner_resolved(hub):
    """`runner_code=None` — a capability with nothing to serve it — must not
    be read as vacancy meaning yes."""
    assert ai_runtime._accepts_image(registry.TEXT_GENERATION, None, "org/whatever") is False


# -- `acceptsImage`'s pre-download fallback (SPEC AI-17 item 17) --------------
#
# `has_vision_tower` can only read a snapshot already on disk. For a curated
# or searched repo with NOTHING cached yet, `_accepts_image` falls back to
# `hub_metadata.get`'s Hub-harvested `hasVisionTower` — but only there: an
# ALREADY-cached snapshot's own on-disk reading stays higher precedence, so a
# stale or wrong Hub-metadata reading can never override a real measurement.


def test_ai_runtime_module_only_reads_hub_metadatas_cache_never_the_fetch(hub):
    """Source-level guard (code review finding 1), the same shape `test_ai_
    hw_detect.py::test_fit_module_only_reads_the_cache_never_the_probe`
    already pins for `hw_detect`/`fit.py`: this route must never call
    `hub_metadata.get` (a synchronous, network-backed, 8-second-timeout
    fetch) — only `hub_metadata.cached` (a plain disk read). A future edit
    that reintroduces `hub_metadata.get(...)` here is caught by reading the
    source, not only by a test that happens to monkeypatch it away."""
    import inspect

    source = inspect.getsource(ai_runtime)
    assert "hub_metadata.get(" not in source
    assert "hub_metadata.cached(" in source


def test_accepts_image_falls_back_to_hub_metadata_when_nothing_is_cached(hub, monkeypatch):
    monkeypatch.setattr(ai_runtime.hub_metadata, "cached",
                        lambda repo_id: {"hasVisionTower": True})
    assert ai_runtime._accepts_image(registry.TEXT_GENERATION, "mlx-text", "org/uncached-vlm") is True


def test_accepts_image_is_false_when_uncached_and_hub_metadata_says_no_tower(hub, monkeypatch):
    monkeypatch.setattr(ai_runtime.hub_metadata, "cached",
                        lambda repo_id: {"hasVisionTower": False})
    assert ai_runtime._accepts_image(registry.TEXT_GENERATION, "mlx-text", "org/uncached-chat") is False


def test_accepts_image_is_false_when_uncached_and_hub_metadata_has_nothing(hub, monkeypatch):
    monkeypatch.setattr(ai_runtime.hub_metadata, "cached", lambda repo_id: None)
    assert ai_runtime._accepts_image(registry.TEXT_GENERATION, "mlx-text", "org/never-seen") is False


# -- orthogonal capability tags: tool-use / vision (SPEC AI-28) --------------


def test_capability_tags_is_empty_for_a_non_text_generation_capability(hub):
    assert ai_runtime._capability_tags(registry.IMAGE_GENERATION, "org/whatever") == ()


def test_capability_tags_tags_tool_use_from_the_repo_id_alone(hub):
    assert "tool-use" in ai_runtime._capability_tags(
        registry.TEXT_GENERATION, "Qwen/Qwen3-8B-Instruct")


def test_capability_tags_tags_vision_from_a_cached_snapshot(hub):
    _cached_repo(hub, "org/vlm", files=("model.safetensors",),
                config={"model_type": "qwen3_5", "vision_config": {"depth": 4}})
    assert "vision" in ai_runtime._capability_tags(registry.TEXT_GENERATION, "org/vlm")


def test_capability_tags_uses_hub_metadata_family_evidence_when_uncached(hub, monkeypatch):
    """A repo id alone (`org/my-finetune`) is uninformative — the harvested
    `modelType` from `hub_metadata` is what actually names the family."""
    monkeypatch.setattr(ai_runtime.hub_metadata, "cached",
                        lambda repo_id: {"modelType": "qwen3", "hasVisionTower": False})
    tags = ai_runtime._capability_tags(registry.TEXT_GENERATION, "org/my-finetune")
    assert tags == ("tool-use",)


def test_accepts_image_prefers_the_cached_reading_over_hub_metadata(hub, monkeypatch):
    """The on-disk answer wins even when it disagrees with a stale/wrong Hub
    reading — a real cached snapshot with no vision tower must not be
    overridden by `hub_metadata` claiming otherwise."""
    _cached_repo(hub, "org/plain-chat", files=("model.safetensors",),
                config={"model_type": "llama"})
    monkeypatch.setattr(ai_runtime.hub_metadata, "cached",
                        lambda repo_id: {"hasVisionTower": True})
    assert ai_runtime._accepts_image(registry.TEXT_GENERATION, "mlx-text", "org/plain-chat") is False


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


def test_an_image_whose_model_cannot_load_reports_WHY(client, fake_image_runner,
                                                       monkeypatch):
    """The bug a user hit trying to render through FLUX (D266).

    Their runner environment could not build, and the `sys:ai-model:` row said
    exactly why — uv's own text, verbatim. The IMAGE row they were watching said
    "was unloaded before it could be used", which reads like a race worth
    retrying, so they retried five times and re-ran the doomed build five times.

    The cause was a waiter reading the wrong thing: `_bring_up` drops a failed
    worker from `_workers` inside the same locked block that records the error,
    so a waiter polling the TABLE can only ever find the model gone and never
    the reason. Both rows have to be able to say the same failure.
    """
    def unbuildable(runner, worker, job):
        raise RuntimeError(
            "Failed to build the environment: No module named 'jaraco.text'")

    monkeypatch.setattr(supervisor, "_ensure_venv", unbuildable)
    started = client.post("/api/ai/image", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()

    row = _wait_job(started["jobId"], timeout=40)
    assert row["state"] == "error", row
    assert "jaraco.text" in row["message"], row["message"]
    assert "unloaded" not in row["message"], row["message"]
    # And the model's own row carries it too — two rows, one truth (D261).
    model_row = _row(supervisor.job_id_for(catalog.default_for(registry.IMAGE_GENERATION)))
    assert model_row["state"] == "error"
    assert "jaraco.text" in model_row["message"]


def test_a_model_taken_away_mid_wait_still_says_it_was_unloaded(fake_image_runner,
                                                                monkeypatch):
    """The other half: "unloaded" is a real outcome and must survive the fix.

    A bring-up that never errored and is simply GONE from the table — evicted by
    another model, unloaded from the AI Models page — has no failure message to
    report, and saying it was taken away is the honest answer.

    Driven with `_bring_up` stubbed out, so the only thing that touches the
    table is this test: the worker is removed the moment its row is opened, and
    the wait's first look finds it missing.
    """
    monkeypatch.setattr(supervisor, "_bring_up", lambda runner, worker, job: None)
    real_report = supervisor._report
    taken = {"yet": False}

    def report_then_take(job, **fields):
        real_report(job, **fields)
        if not taken["yet"]:
            taken["yet"] = True
            with supervisor._lock:
                supervisor._workers.pop(registry.IMAGE_GENERATION, None)

    monkeypatch.setattr(supervisor, "_report", report_then_take)

    with pytest.raises(supervisor.SupervisorError) as caught:
        supervisor._wait_ready("org/paints", registry.IMAGE_GENERATION,
                               supervisor.IMAGE_JOB_PREFIX + "waiter")
    assert "unloaded before it could be used" in str(caught.value)


# -- video generation (SPEC §40) ------------------------------------------------
# `/api/ai/video`, `api_ai_image`'s twin — job-backed for the same reason, with
# an engine-shaped canvas/frame grid instead of an arbitrary
# width/height/guidance, and (uniquely among these routes) a 409 that is the
# ORDINARY case off Apple Silicon rather than an edge one.
#
# These tests run on `fake_video_runner`, whose `code="fake-video"` is not in
# `registry.VIDEO_TRAITS`, so they exercise `video_traits_for`'s FALLBACK.
# D468 repointed that fallback from the dropped `h3-video` row (5 + 17n at
# 864x480/20 steps) to `ltx-video`'s (1 + 8n at 704x480/8 steps), which is why
# every number below is LTX's.


def test_a_video_renders_to_disk_and_the_job_finishes(client, fake_video_runner):
    response = client.post("/api/ai/video", json={"prompt": "a fox running"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 200
    started = response.json()

    row = _wait_job(started["jobId"])
    assert row["state"] == "done", row
    assert os.path.isfile(started["path"])
    assert open(started["path"], "rb").read(4) == b"\x00\x00\x00\x18"


def test_a_video_row_is_server_owned_and_reserved(client, fake_video_runner):
    """Mirrors test_an_image_row_is_server_owned_and_reserved — video had no
    counterpart, so a broken video row (never opened, or opened under the
    wrong owner) had nothing here that would catch it."""
    started = client.post("/api/ai/video", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    assert started["jobId"].startswith(jobs.SERVER_ID_PREFIX)
    row = _wait_job(started["jobId"])
    assert row["owner"] == jobs.OWNER_SERVER
    refused = client.post("/api/jobs", json={"id": started["jobId"], "state": "done"},
                          headers={"X-Fused": "1"})
    assert refused.status_code == 400
    assert "reserved" in refused.json()["error"]


def test_the_video_reply_describes_the_render_that_will_actually_happen(
        client, fake_video_runner):
    """Clamped and snapped, not echoed — same rule the image route's own
    version of this test states."""
    body = {"prompt": "x", "width": 99999, "height": 1344, "frames": 100, "steps": 500}
    reply = client.post("/api/ai/video", json=body, headers={"X-Fused": "1"}).json()
    # width clamped to 1344 then shaved down to fit `w*h <= 768*1344` against a
    # height already at the ceiling.
    assert reply["width"] * reply["height"] <= 768 * 1344
    assert reply["width"] % 32 == 0 and reply["height"] % 32 == 0
    assert reply["frames"] == 105      # next 1+8n at or above 100 is n=13 -> 105
    assert reply["steps"] == 50        # clamped to _MAX_VIDEO_STEPS
    _wait_job(reply["jobId"])


def test_video_width_and_height_snap_down_to_a_multiple_of_32(client, fake_video_runner):
    reply = client.post("/api/ai/video", json={"prompt": "x", "width": 1000, "height": 700},
                        headers={"X-Fused": "1"}).json()
    assert reply["width"] == 992   # 1000 - (1000 % 32)
    assert reply["height"] == 672  # 700 - (700 % 32)
    _wait_job(reply["jobId"])


def test_video_frames_default_to_97(client, fake_video_runner):
    reply = client.post("/api/ai/video", json={"prompt": "x"},
                        headers={"X-Fused": "1"}).json()
    assert reply["frames"] == 97   # 1 + 8*12, `default_frames_n`
    _wait_job(reply["jobId"])


def test_video_frames_align_UP_to_the_nearest_valid_grid_value(client, fake_video_runner):
    """Round UP to the next grid point, never to the nearest one — the
    direction is the contract (`_snap_frames`'s own docstring): rounding to
    nearest would report fewer frames than the render actually produces."""
    # 1 + 8n grid, n=1..21: 9, 17, 25, ..., 97, ..., 169
    for asked, expected in ((5, 9), (2, 9), (30, 33), (40, 41), (95, 97), (97, 97)):
        reply = client.post("/api/ai/video", json={"prompt": "x", "frames": asked},
                            headers={"X-Fused": "1"}).json()
        assert reply["frames"] == expected, (asked, reply["frames"])
        _wait_job(reply["jobId"])


def test_video_steps_default_to_8(client, fake_video_runner):
    reply = client.post("/api/ai/video", json={"prompt": "x"},
                        headers={"X-Fused": "1"}).json()
    assert reply["steps"] == 8
    _wait_job(reply["jobId"])


def test_video_steps_floor_is_2_not_1(client, fake_video_runner):
    """The floor came from the dropped `h3-video` runner, which refused 1 step
    outright ("denoising steps must be in [2, 1000]"). Kept as the app's own
    on that runner's removal (D468): 1 step is not a meaningfully faster
    render on any engine, so the rail stays rather than being relaxed."""
    reply = client.post("/api/ai/video", json={"prompt": "x", "steps": 1},
                        headers={"X-Fused": "1"}).json()
    assert reply["steps"] == 2
    _wait_job(reply["jobId"])


def test_video_frames_floor_is_n_1_not_n_0(client, fake_video_runner):
    """`registry.MIN_VIDEO_FRAMES_N` is 1, so the grid this route offers
    starts at `base + step` (9 here) and never at the bare `base` — an
    app-chosen rail, held for every engine."""
    for asked in (1, 5, 9):
        reply = client.post("/api/ai/video", json={"prompt": "x", "frames": asked},
                            headers={"X-Fused": "1"}).json()
        assert reply["frames"] == 9, (asked, reply["frames"])
        _wait_job(reply["jobId"])


def test_video_default_canvas_matches_the_engines_own_default(client, fake_video_runner):
    """704x480 — LTX's own CLI `--width`/`--height` defaults, reached here
    through `video_traits_for`'s fallback: a bare call should render at the
    shape the engine itself is tuned for, not an arbitrary size."""
    reply = client.post("/api/ai/video", json={"prompt": "x"},
                        headers={"X-Fused": "1"}).json()
    assert (reply["width"], reply["height"]) == (704, 480)
    _wait_job(reply["jobId"])


# -- Task 5: request shaping follows the SERVING runner's own traits -------------


def test_ltx_video_request_shape_follows_its_own_traits(client, fake_ltx_video_runner):
    """`registry.VIDEO_TRAITS["ltx-video"]`: 704x480 canvas, 8 denoising
    steps, frames on the `1 + 8n` grid, read off the REAL row rather than
    `video_traits_for`'s fallback."""
    reply = client.post("/api/ai/video", json={"prompt": "x"},
                        headers={"X-Fused": "1"}).json()
    assert (reply["width"], reply["height"]) == (704, 480)
    assert reply["steps"] == 8
    assert reply["frames"] == 97  # 1 + 8*12, this runner's own default n
    _wait_job(reply["jobId"])

    # Rounds UP to the next `1 + 8n` point, never to the nearest one.
    reply = client.post("/api/ai/video", json={"prompt": "x", "frames": 90},
                        headers={"X-Fused": "1"}).json()
    assert reply["frames"] == 97  # 90 is between 89 (1+8*11) and 97; rounds up
    _wait_job(reply["jobId"])


def test_naming_the_OTHER_video_engines_cached_model_is_refused_not_started(
        client, monkeypatch, tmp_path):
    """Naming a model explicitly does NOT pick its runner — resolution is by
    CAPABILITY plus stored preference (`registry.py`'s own module docstring),
    and `start_video`'s `_runner_or_raise` never reads `model` either. So when
    the caller names a repo whose cached format evidence points at a DIFFERENT
    video runner, the route would otherwise build and start the resolved
    worker against it — raising deep inside `load()` after a Hub listing round
    trip, a confusing failure for someone who deliberately named a model they
    already have on disk. The route catches it itself, off the same
    `hub_cache.cached_capability` evidence the AI Models page's card reads,
    before any job opens.

    **The route's guard is unreachable through REAL evidence since D468**:
    dropping `h3-video` left one video runner, and `formats.loaders` no longer
    names any other, so no snapshot on disk can produce a mismatching
    `runner_code`. The guard is kept anyway — it is generic over runners, and
    it is what a second video engine's arrival would otherwise have to
    remember to add back — so this test drives it through the READING rather
    than through a staged snapshot, which is the only honest way left to
    exercise it. Two fake runners, because what is under test is the ROUTE's
    refusal and not either runner's own format check.
    """
    def fake_runner(code):
        folder = tmp_path / f"fake_{code.replace('-', '_')}"
        folder.mkdir()
        (folder / "worker.py").write_text(FAKE_VIDEO_WORKER, encoding="utf-8")
        return registry.Runner(
            code=code, capability=registry.VIDEO_GENERATION,
            folder=str(folder), label=f"Fake {code}", short_label=code,
        )

    serving = fake_runner("ltx-video")
    other = fake_runner("other-video")
    # `serving` FIRST, matching production ordering — the whole point is that
    # it is the one that WOULD resolve here.
    monkeypatch.setattr(registry, "_RUNNERS", (serving, other))
    monkeypatch.setitem(catalog.SUGGESTIONS, "ltx-video", [
        {"id": "org/ltx", "label": "Fake ltx", "size_gb": None, "note": ""}])
    monkeypatch.setattr(supervisor, "_ensure_venv", lambda r, w, j: sys.executable)
    monkeypatch.setattr(supervisor, "_require_build_tools", lambda: None)

    # The evidence the ROUTE reads, naming the runner that is NOT serving.
    monkeypatch.setattr(
        ai_runtime, "cached_capability",
        lambda repo_id: ai_models.CacheReading(
            cached=True, capability=registry.VIDEO_GENERATION,
            looks_like="a fake other-video checkpoint",
            runner_code="other-video"))

    response = client.post(
        "/api/ai/video", json={"prompt": "x", "model": "org/other-video-weights"},
        headers={"X-Fused": "1"})

    assert response.status_code == 409
    message = response.json()["error"]
    assert "org/other-video-weights" in message
    assert "other-video" in message
    assert "Engines tab" in message
    # No job opened for work that was never going to start — the same
    # invariant `test_a_video_on_a_machine_with_no_video_runner_says_why`
    # checks for the "nothing can serve this at all" 409.
    assert not [j for j in jobs.list_jobs()
               if j["id"].startswith(supervisor.VIDEO_JOB_PREFIX)]


def test_naming_an_UNCACHED_model_is_not_refused_by_the_route(
        client, fake_ltx_video_runner, monkeypatch):
    """The refusal above needs EVIDENCE — an uncached id has none without a
    network call this route has never made, so it reaches the runner's own
    `load()` refusal instead, exactly as every other capability's route
    already does for a model nothing here recognises. This is the negative
    case that keeps the guard from becoming "the route silently accepts
    only ids it has curated"."""
    response = client.post(
        "/api/ai/video",
        json={"prompt": "x", "model": "someone/unrelated-repo-not-cached"},
        headers={"X-Fused": "1"})
    assert response.status_code == 200
    _wait_job(response.json()["jobId"])


def test_video_seed_is_invented_when_not_given_and_echoed_when_it_is(client, fake_video_runner):
    one = client.post("/api/ai/video", json={"prompt": "a"}, headers={"X-Fused": "1"}).json()
    two = client.post("/api/ai/video", json={"prompt": "b", "seed": 1234},
                      headers={"X-Fused": "1"}).json()
    assert isinstance(one["seed"], int)
    assert two["seed"] == 1234
    _wait_job(one["jobId"]); _wait_job(two["jobId"])


def test_a_video_needs_a_prompt(client, fake_video_runner):
    for body in ({}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 7}):
        response = client.post("/api/ai/video", json=body, headers={"X-Fused": "1"})
        assert response.status_code == 400, body


def test_an_unrecognised_video_option_is_a_400_naming_it(client, fake_video_runner):
    response = client.post(
        "/api/ai/video", json={"prompt": "a fox", "strength": 0.6},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    message = response.json()["error"]
    assert "strength" in message
    assert "not an option" in message


def test_guidance_is_rejected_as_an_unknown_video_option(client, fake_video_runner):
    """The video engine is CFG-distilled and takes no such parameter — the
    plan is explicit
    that there is no separate check for it, because the unknown-option
    rejection already handles anyone passing it."""
    response = client.post(
        "/api/ai/video", json={"prompt": "a fox", "guidance": 4.0},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "guidance" in response.json()["error"]


def test_the_video_envelope_is_checked_before_any_field_validation(client, fake_video_runner):
    response = client.post(
        "/api/ai/video", json={"prompt": "a fox", "bogus": "x.png", "steps": "nonsense"},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    message = response.json()["error"]
    assert "'bogus' is not an option" in message
    assert "must be a number" not in message


def test_every_documented_video_option_is_still_accepted(client, fake_video_runner):
    body = {
        "prompt": "a fox", "model": "org/x", "width": 768, "height": 768,
        "frames": 90, "steps": 20, "seed": 7,
    }
    response = client.post("/api/ai/video", json=body, headers={"X-Fused": "1"})
    assert response.status_code == 200, response.json()


def test_a_failing_video_render_reports_the_reason_on_the_row(client, fake_video_runner,
                                                               monkeypatch):
    monkeypatch.setenv("FAKE_VIDEO_FAILS", "1")
    started = client.post("/api/ai/video", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    row = _wait_job(started["jobId"])
    assert row["state"] == "error"
    assert "the renderer exited" in row["message"]


def test_a_video_on_a_machine_with_no_video_runner_says_why(client, monkeypatch):
    """The ordinary case, not the edge one — video generation is the first
    capability with no "everywhere" row, so a machine that is not Apple
    Silicon always answers 409 here, never opening a row for work that was
    never going to start."""
    ghost = registry.Runner(
        code="ghost-video", capability=registry.VIDEO_GENERATION,
        folder="/nowhere", label="Ghost video",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (ghost,))
    response = client.post("/api/ai/video", json={"prompt": "x"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 409
    assert "not built yet" in response.json()["error"]
    assert not [j for j in jobs.list_jobs() if j["id"].startswith(supervisor.VIDEO_JOB_PREFIX)]


def test_a_video_off_apple_silicon_says_so(client, monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(registry, "_RUNNERS", (registry.by_code("ltx-video"),))
    response = client.post("/api/ai/video", json={"prompt": "x"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 409
    assert "Apple Silicon" in response.json()["error"]


def test_a_video_waits_for_its_model_rather_than_failing_fast(client, fake_video_runner,
                                                              monkeypatch):
    """Same reasoning as the image route's own version of this test: a video
    caller already has a job to watch, so a cold load happens INSIDE it."""
    monkeypatch.setenv("FAKE_LOAD_SECONDS", "1.5")
    assert supervisor.describe()["loaded"] == []
    started = client.post("/api/ai/video", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    row = _wait_job(started["jobId"], timeout=40)
    assert row["state"] == "done", row
    assert os.path.isfile(started["path"])


# -- a reference image (I2V) -----------------------------------------------------
# One image, a single string, conditioning at frame 0 with strength 1.0 — the
# same scope decision `/api/ai/image`'s own `image` option made for editing,
# restated for video. `_resolve_reference_image` is the shared helper both
# routes call; these tests exercise it through `/api/ai/video`, the same way
# the block above exercises `_edit_default_size` through `/api/ai/image`.


def test_video_rejects_an_image_array(client, fake_video_runner):
    response = client.post(
        "/api/ai/video", json={"prompt": "a fox", "image": ["a.png", "b.png"]},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "single string" in response.json()["error"]
    assert not [j for j in jobs.list_jobs()
                if j["id"].startswith(supervisor.VIDEO_JOB_PREFIX)]


def test_video_rejects_an_empty_image_string(client, fake_video_runner):
    response = client.post(
        "/api/ai/video", json={"prompt": "a fox", "image": ""},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "single string" in response.json()["error"]


def test_video_reference_image_needs_a_file_that_exists(client, fake_video_runner):
    response = client.post(
        "/api/ai/video", json={"prompt": "a fox", "image": "/nope/nowhere.png"},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "no such file" in response.json()["error"]
    assert not [j for j in jobs.list_jobs()
                if j["id"].startswith(supervisor.VIDEO_JOB_PREFIX)]


def test_video_refuses_a_relative_image_with_no_base(client, fake_video_runner):
    response = client.post(
        "/api/ai/video", json={"prompt": "a fox", "image": "photo.png"},
        headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "'image' must be absolute" in response.json()["error"]


def test_video_resolves_a_relative_image_against_base(
        client, fake_video_runner, base_photo):
    """RH-1, same as `/api/ai/image`'s own version of this test: a relative
    `image` resolves against the directory of `base`."""
    page, photo = base_photo
    started = client.post(
        "/api/ai/video", json={"prompt": "a fox", "image": "photo.png", "base": page},
        headers={"X-Fused": "1"}).json()
    assert started["image"] == ai_runtime.canonical_fs_path(photo)
    _wait_job(started["jobId"])


def test_video_absolute_image_ignores_base(client, fake_video_runner, base_photo):
    _page, photo = base_photo
    started = client.post(
        "/api/ai/video", json={"prompt": "a fox", "image": photo},
        headers={"X-Fused": "1"}).json()
    assert started["image"] == ai_runtime.canonical_fs_path(photo)
    _wait_job(started["jobId"])


def test_a_plain_text_to_video_request_has_no_image_key(client, fake_video_runner):
    """A caller that never mentioned `image` sees no trace of it in the
    reply — the byte-identical-to-today's-call promise, restated at the
    route's own boundary."""
    started = client.post("/api/ai/video", json={"prompt": "x"},
                          headers={"X-Fused": "1"}).json()
    assert "image" not in started
    _wait_job(started["jobId"])


def test_video_canvas_derives_from_the_reference_image(
        client, fake_video_runner, base_photo):
    """`base_photo` is 2000x1000 (2:1) — fitted (without upscaling) to the
    engine's own longer default side (`max(704, 480) == 704`), landing at
    704x352, then snapped DOWN to the 64-multiple grid `snap_output_
    dimensions(..., two_stage=True)` uses: 704 (already a multiple of 64)
    x 320 (352 snapped down from 352 to 320)."""
    page, photo = base_photo
    started = client.post(
        "/api/ai/video", json={"prompt": "a fox", "image": "photo.png", "base": page},
        headers={"X-Fused": "1"}).json()
    assert (started["width"], started["height"]) == (704, 320)
    assert started["width"] % 64 == 0 and started["height"] % 64 == 0
    _wait_job(started["jobId"])


def test_a_small_reference_can_come_back_square(client, fake_video_runner, tmp_path):
    """`_video_default_size`'s own docstring: the 64-multiple step pairs with
    the SAME 256 floor `_edit_default_size` uses at 16, so aspect collapses
    far more readily here — not only on an extreme ratio. A 300x200 (3:2)
    reference is smaller than the engine's own 704 target on both axes (no
    downscale happens), and each axis then floors independently: `max(256,
    300 // 64 * 64) == 256`, `max(256, 200 // 64 * 64) == 256`. This pins
    that as deliberate rather than incidental — see D621."""
    page = tmp_path / "pages" / "editor.html"
    page.parent.mkdir(parents=True)
    page.write_text("<html></html>")
    photo = page.parent / "small.png"
    photo.write_bytes(_png_bytes(300, 200))
    started = client.post(
        "/api/ai/video", json={"prompt": "a fox", "image": "small.png", "base": str(page)},
        headers={"X-Fused": "1"}).json()
    assert (started["width"], started["height"]) == (256, 256)
    _wait_job(started["jobId"])


def test_an_explicit_width_still_wins_over_the_derived_default(
        client, fake_video_runner, base_photo):
    page, photo = base_photo
    started = client.post(
        "/api/ai/video",
        json={"prompt": "a fox", "image": "photo.png", "base": page, "width": 512},
        headers={"X-Fused": "1"}).json()
    # Height still comes from the reference image; only width was named.
    assert (started["width"], started["height"]) == (512, 320)
    _wait_job(started["jobId"])


def test_an_unreadable_reference_falls_back_to_the_engines_own_default(
        client, fake_video_runner, tmp_path):
    """A file that exists, is a regular file, but is not one of the three
    formats `_image_pixel_size` understands — the derived-default lookup
    fails toward None, and the render still goes ahead at the ENGINE's own
    default canvas rather than refusing the request outright."""
    junk = tmp_path / "not-really-a-photo.png"
    junk.write_bytes(b"this is not image data")
    started = client.post(
        "/api/ai/video", json={"prompt": "a fox", "image": str(junk)},
        headers={"X-Fused": "1"}).json()
    assert (started["width"], started["height"]) == (704, 480)
    _wait_job(started["jobId"])


def test_the_video_bridge_base_option_reaches_the_route(
        client, fake_video_runner, base_photo):
    """`base` is bridge-injected, not caller-facing (mirrors `_IMAGE_SERVER_
    OPTIONS`'s own asymmetry) — this exercises it through the SERVER side,
    since `base` alone with no `image` is a legitimate call the bridge
    itself would make on every video render once `runtime.js` injects it."""
    page, _photo = base_photo
    response = client.post(
        "/api/ai/video", json={"prompt": "a fox", "base": page},
        headers={"X-Fused": "1"})
    assert response.status_code == 200, response.json()


# -- transcription (SPEC §40) ---------------------------------------------------
# Job-backed like an image and for the same reason — a 90-minute recording is
# not a chat turn — with one addition: the transcript is a FILE, so the work
# outlives the tab that asked for it.


def _post_transcribe(client, **body):
    return client.post("/api/ai/transcribe", json=body, headers={"X-Fused": "1"})


def test_a_transcript_is_written_to_disk_and_the_job_finishes(
        client, fake_transcribe_runner, recording):
    started = _post_transcribe(client, path=recording).json()

    row = _wait_job(started["jobId"])
    assert row["state"] == "done", row
    # The output path the POST promised is the path that exists — no second
    # lookup, exactly as the image route works.
    written = json.load(open(started["output"]))
    assert written["text"] == "hello world"
    assert written["segments"][0]["start"] == 0.0
    # …and the plain-text sibling beside it, for anything that wants words
    # without timestamps.
    assert open(started["outputText"]).read().strip() == "hello world"


def test_the_transcribe_reply_settles_the_request_before_anything_runs(
        client, fake_transcribe_runner, recording):
    """Everything the caller needs comes back from the POST: which model, which
    file, and where the transcript will land. Nothing waits on the work."""
    reply = _post_transcribe(client, path=recording, task="translate").json()
    # Canonical (forward-slash), like every path this API hands back — see
    # `ai_runtime.canonical_fs_path` — so it is `os.path.abspath` run through
    # the SAME transform, not the raw (backslashed, on Windows) form.
    assert reply["path"] == ai_runtime.canonical_fs_path(os.path.abspath(recording))
    assert reply["model"] == catalog.default_for(registry.SPEECH_TO_TEXT)
    assert reply["task"] == "translate"
    assert reply["output"].endswith(".json")
    # A literal forward slash rather than `os.path.join`: `output` is already
    # canonical, so the suffix it ends with is too, on every platform.
    assert os.path.dirname(reply["output"]).endswith("ai/transcripts")
    _wait_job(reply["jobId"])


def test_the_reply_names_the_PARTIAL_transcript_too(
        client, fake_transcribe_runner, recording):
    """A page tailing the transcript must not have to string-munge one path out
    of another — that is the rule `outputText` already follows, and it is the
    same rule for the same reason: the derivation lives in one place
    (`runners/partial.py`), and a page that reimplemented it would break
    silently the day the suffix changed."""
    reply = _post_transcribe(client, path=recording).json()

    assert reply["outputPartial"] == reply["output"][:-len(".json")] + ".partial.jsonl"
    assert reply["outputPartial"] == partial.partial_path(reply["output"])
    _wait_job(reply["jobId"])


def test_the_partial_path_reaches_the_WORKER_as_well_as_the_page(
        client, fake_transcribe_runner, recording, monkeypatch):
    """Advertising a path nothing writes would be worse than not advertising
    one: a page would tail a file that never appears and show an empty
    transcript for the whole run, with no error anywhere to explain it."""
    seen = {}
    real = supervisor.start_transcribe
    monkeypatch.setattr(supervisor, "start_transcribe",
                        lambda model, request, job: (seen.update(request),
                                                     real(model, request, job)))

    reply = _post_transcribe(client, path=recording).json()
    _wait_job(reply["jobId"])

    # `seen` is the RAW request the worker gets (a native path, backslashed on
    # Windows); `outputPartial` is the reply's canonical (forward-slash) form
    # of the same path — see `ai_runtime.canonical_fs_path`.
    assert ai_runtime.canonical_fs_path(seen["outPartial"]) == reply["outputPartial"]
    # A SIBLING of the two the request already named, not a third location:
    # `_transcripts_dir()` is where the server decided user files go.
    assert (os.path.dirname(seen["outPartial"])
            == os.path.dirname(seen["out"]) == os.path.dirname(seen["outText"]))


def test_transcribing_needs_a_file_that_actually_exists(
        client, fake_transcribe_runner, recording, tmp_path):
    """Refused with a 400 BEFORE a job row opens. A path typo should be an
    error the caller can show, not a progress bar that dies."""
    for body in ({}, {"path": ""}, {"path": 7},
                 {"path": str(tmp_path / "nothing.wav")},
                 {"path": str(tmp_path)}):
        assert _post_transcribe(client, **body).status_code == 400, body
    assert not [j for j in jobs.list_jobs()
                if j["id"].startswith(supervisor.TRANSCRIBE_JOB_PREFIX)]


def test_transcribe_rejects_a_caller_supplied_unknown_option(
        client, fake_transcribe_runner, recording):
    response = _post_transcribe(client, path=recording, image="x.png")
    assert response.status_code == 400
    assert "image" in response.json()["error"]


def test_transcribe_accepts_the_bridge_injected_base(
        client, fake_transcribe_runner, recording, tmp_path):
    """`base` is not a caller-facing option — the bridge adds it from the
    page's own `?path=` — but the server must still accept it, or every
    existing `fused.ai.transcribe` call with a relative path breaks."""
    started = _post_transcribe(client, path=recording, base=str(tmp_path / "page.html"))
    assert started.status_code == 200, started.json()
    _wait_job(started.json()["jobId"])


def test_every_documented_transcribe_option_is_still_accepted(
        client, fake_transcribe_runner, recording):
    """The regression this change could plausibly cause: a false rejection of
    a valid option. Assert the whole accepted set, not two of them."""
    body = dict(
        path=recording, model=catalog.default_for(registry.SPEECH_TO_TEXT),
        language="en", task="transcribe", initialPrompt="hello",
        vad=True, diarize=False, speakers=None, words=False)
    response = _post_transcribe(client, **body)
    assert response.status_code == 200, response.json()
    _wait_job(response.json()["jobId"])


def test_an_explicit_null_vad_reaches_the_worker_as_the_default(
        client, fake_transcribe_runner, recording, monkeypatch):
    """A page spreading an options object with an unset `vad` key must not
    silently turn the VAD off — `bool(x, True)` reads null as False."""
    seen = {}
    real = supervisor.start_transcribe
    monkeypatch.setattr(supervisor, "start_transcribe",
                        lambda model, request, job: (seen.update(request),
                                                     real(model, request, job)))
    for sent, expected in (({}, True), ({"vad": None}, True),
                           ({"vad": True}, True), ({"vad": False}, False)):
        started = _post_transcribe(client, path=recording, **sent).json()
        assert seen["vad"] is expected, sent
        _wait_job(started["jobId"])


def test_a_BAD_speaker_count_is_refused_before_a_job_opens(
        client, fake_transcribe_runner, recording):
    """A count that was meant and is unusable, and this is the server's copy of
    that rule — `runtime.js` refuses first, but the bridge is not the only door
    into this endpoint and a rule enforced only in JavaScript is not enforced.

    None of these is a request to estimate: `0` and `-1` are arithmetic gone
    wrong, `true` is a copy-paste of the `diarize` flag, `"2"` is an <input>
    read without a parseInt. Reading any of them as "work it out yourself"
    would turn a caller's mistake into a quietly different transcript.

    Before a job row opens, like the `path` check above — this one would
    otherwise open a row that survives a multi-second model load to die.
    """
    for sent in ({"diarize": True, "speakers": 0},
                 {"diarize": True, "speakers": -1},
                 {"diarize": True, "speakers": True},
                 {"diarize": True, "speakers": 2.5},
                 {"diarize": True, "speakers": "2"},
                 {"diarize": True, "speakers": 10_000}):
        response = _post_transcribe(client, path=recording, **sent)
        assert response.status_code == 400, sent
        assert "speakers" in response.json()["error"], sent
    assert not [j for j in jobs.list_jobs()
                if j["id"].startswith(supervisor.TRANSCRIBE_JOB_PREFIX)]


def test_diarizing_WITHOUT_a_count_is_accepted_and_estimates_it(
        client, fake_transcribe_runner, recording, monkeypatch):
    """D318: the count is a hint. Omitted — or sent as an explicit null, which
    is what a page spreading an options object with an unset key produces — the
    job starts and the worker is told to diarize with no `speakers` key at all,
    which its clustering reads as "estimate"."""
    seen = {}
    real = supervisor.start_transcribe
    monkeypatch.setattr(supervisor, "start_transcribe",
                        lambda model, request, job: (seen.update(request),
                                                     real(model, request, job)))
    for sent in ({"diarize": True}, {"diarize": True, "speakers": None},
                 {"diarize": True, "speakers": ""}):
        seen.clear()
        response = _post_transcribe(client, path=recording, **sent)
        assert response.status_code == 200, (sent, response.json())
        assert seen["diarize"] is True, sent
        # ABSENT rather than null: the worker never has to tell an unspecified
        # count from one that arrived as a null.
        assert "speakers" not in seen, sent
        _wait_job(response.json()["jobId"])


@pytest.mark.parametrize("sent,needle", [
    ({"task": "translate"}, "only transcribes"),
    ({"language": "en"}, "'language' option"),
    ({"initialPrompt": "Acme Corp"}, "'initialPrompt'"),
])
def test_an_option_the_RESOLVED_engine_cannot_honour_is_refused_before_a_job_opens(
        client, fake_refusing_runner, recording, sent, needle):
    """The worker refuses these too, but by then the user has paid for a job
    row, a venv build and a multi-gigabyte download to be told no — and the
    runner is resolved synchronously HERE, so the answer was available before
    any of it. Same treatment as a bad `task` or `speakers`: an instant 400
    with the sentence `runners/engine_options.py` holds.

    Exercised against `fake_refusing_runner`'s temporary table entry rather
    than a real engine's: D406 withdrew `parakeet-mlx`, the one engine that
    used to populate `UNSUPPORTED`, and no currently-registered engine refuses
    anything — but the endpoint's refusal PATH still has to work the day one
    does."""
    response = _post_transcribe(client, path=recording, **sent)

    assert response.status_code == 400, (sent, response.json())
    assert needle in response.json()["error"]
    assert not [j for j in jobs.list_jobs()
                if j["id"].startswith(supervisor.TRANSCRIBE_JOB_PREFIX)]


def test_an_ORDINARY_request_to_that_engine_still_runs(
        client, fake_refusing_runner, recording):
    """The check is on the value of `task` and the presence of the other two:
    every request carries `task: "transcribe"`, and a refusal keyed on presence
    would refuse every call the engine exists to serve."""
    started = _post_transcribe(client, path=recording, task="transcribe",
                               language=None, initialPrompt=None)

    assert started.status_code == 200, started.json()
    _wait_job(started.json()["jobId"])


def test_an_engine_with_NOTHING_to_refuse_is_asked_the_same_question(
        client, fake_transcribe_runner, recording):
    """The table is an exception list, so a runner absent from it accepts what
    it always did — checked because a refusal that fired for every engine would
    take `translate` away from both whisper runners and pass every fake
    refusing-engine test while doing it."""
    started = _post_transcribe(client, path=recording, task="translate",
                               language="en", initialPrompt="Acme Corp")

    assert started.status_code == 200, started.json()
    _wait_job(started.json()["jobId"])


def test_the_endpoint_and_the_worker_refuse_an_option_by_the_SAME_rule(
        client, fake_refusing_runner, recording):
    """One sentence, one place. The endpoint hands the caller whatever
    `runners/engine_options.py` raises, which is the module the worker imports
    out of its own venv — so a message reworded there is reworded here."""
    from fused_render.ai.runners import engine_options

    response = _post_transcribe(client, path=recording, task="translate")
    with pytest.raises(ValueError) as raised:
        engine_options.unsupported_or_raise("fake-refusing-engine", task="translate")
    assert response.json()["error"] == str(raised.value)


def test_the_server_and_the_workers_READ_a_speaker_count_by_the_SAME_rule(
        client, fake_transcribe_runner, recording):
    """One sentence, one place. The endpoint hands the caller whatever
    `runners/diarize.py` raises, so a rule that changes there changes here —
    which is the point of the server importing the module the workers import
    rather than restating it."""
    from fused_render.ai.runners import diarize

    response = _post_transcribe(client, path=recording, diarize=True, speakers=0)
    with pytest.raises(ValueError) as raised:
        diarize.speakers_or_raise(0)
    assert response.json()["error"] == str(raised.value)


def test_diarize_and_speakers_reach_the_worker_and_default_to_OFF(
        client, fake_transcribe_runner, recording, monkeypatch):
    """Additive: a call that does not ask for speakers must send `diarize`
    false and no `speakers` at all, so the worker writes exactly the transcript
    it always did."""
    seen = {}
    real = supervisor.start_transcribe
    monkeypatch.setattr(supervisor, "start_transcribe",
                        lambda model, request, job: (seen.update(request),
                                                     real(model, request, job)))
    for sent, expected in (({}, None), ({"diarize": False}, None),
                           ({"diarize": None}, None),
                           ({"diarize": True, "speakers": 3}, 3)):
        seen.clear()
        started = _post_transcribe(client, path=recording, **sent).json()
        assert seen["diarize"] is (expected is not None), sent
        assert seen.get("speakers") == expected, sent
        if expected is None:
            # Not a null, ABSENT — the worker's own `speakers_or_raise` never
            # sees an argument for a run that did not ask for one.
            assert "speakers" not in seen, sent
        _wait_job(started["jobId"])


def test_words_reaches_the_worker_and_defaults_to_OFF(
        client, fake_transcribe_runner, recording, monkeypatch):
    """`diarize`'s contract for `words` (D392): off unless asked, and a JSON
    null means the same as an absent key — the inversion `vad` once shipped,
    where a page spreading an options object with an unset key got the
    opposite of the documented default."""
    seen = {}
    real = supervisor.start_transcribe
    monkeypatch.setattr(supervisor, "start_transcribe",
                        lambda model, request, job: (seen.update(request),
                                                     real(model, request, job)))
    for sent, expected in (({}, False), ({"words": False}, False),
                           ({"words": None}, False), ({"words": True}, True)):
        seen.clear()
        started = _post_transcribe(client, path=recording, **sent).json()
        assert seen["words"] is expected, sent
        _wait_job(started["jobId"])


def test_words_on_an_engine_without_them_is_NOT_a_refusal(
        client, fake_transcribe_runner, recording):
    """The one option answered best-effort rather than refused. A page that asks
    for word timings on a machine whose engine has none must get its transcript,
    with the `words` key simply absent from the segments — not a `bad_request`
    that forces it to ask what hardware it is on before it asks for anything.

    This is the endpoint's half; `test_ai_engine_options.py` pins the rule and
    `test_ai_mlx_whisper_worker.py` the runner that does carry them.
    """
    started = _post_transcribe(client, path=recording, words=True)

    assert started.status_code == 200, started.text
    _wait_job(started.json()["jobId"])


def test_words_with_TRANSLATE_is_not_a_refusal_either(
        client, fake_transcribe_runner, recording):
    """A translation carries no words on any engine — there is nothing in the
    audio to align English ones to — but it is DECLINED, not refused, so the
    combination reads exactly like an engine that has none. Refusing here would
    make `words: true` unusable in a page that also offers translation."""
    started = _post_transcribe(client, path=recording, words=True,
                               task="translate")

    assert started.status_code == 200, started.text
    _wait_job(started.json()["jobId"])


def test_a_relative_path_resolves_against_the_PAGE_not_the_server(
        client, fake_transcribe_runner, recording):
    """The same page-relative rule every other path-taking call follows (RH-1).

    `fused.readFile("clip.m4a")` resolves against the calling page's directory,
    so `fused.ai.transcribe({path: "clip.m4a"})` reading it against the
    SERVER's cwd is a trap — a 400 about a path the author never wrote, or,
    if a same-named file happens to sit under the server's cwd, silently
    transcribing the wrong recording. `base` is the page's own absolute path,
    exactly as `/api/fs/raw` takes it.
    """
    reply = client.post("/api/ai/transcribe",
                        json={"path": os.path.basename(recording),
                              "base": os.path.join(os.path.dirname(recording),
                                                   "page.html")},
                        headers={"X-Fused": "1"})
    assert reply.status_code == 200, reply.json()
    # Canonical (forward-slash), like every path this API hands back — see
    # `ai_runtime.canonical_fs_path` — not the raw (backslashed, on Windows)
    # form `recording` is built in.
    assert reply.json()["path"] == ai_runtime.canonical_fs_path(recording)
    _wait_job(reply.json()["jobId"])


def test_a_relative_path_with_no_page_is_refused_rather_than_guessed(
        client, fake_transcribe_runner):
    """A caller reaching the API directly has no page to resolve against, and
    the server's cwd is never the right answer — it is wherever the app
    happened to be launched from."""
    response = client.post("/api/ai/transcribe", json={"path": "clip.m4a"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "absolute" in response.json()["error"]


def test_an_unknown_task_names_both_valid_ones(client, fake_transcribe_runner,
                                               recording):
    """Named rather than silently defaulted: "translation" instead of
    "translate" would otherwise transcribe in the original language and look
    like the model ignoring the request."""
    response = _post_transcribe(client, path=recording, task="summarise")
    assert response.status_code == 400
    message = response.json()["error"]
    assert "transcribe" in message and "translate" in message


def test_the_worker_is_given_the_row_identity_to_restate(
        client, fake_transcribe_runner, recording, monkeypatch):
    """The worker reports to this row for the whole decode, from another
    PROCESS — so it has to be told what the row is, or its ticks cannot rebuild
    one the manager evicted mid-run."""
    seen = {}
    real = supervisor.generate_transcript
    monkeypatch.setattr(supervisor, "generate_transcript",
                        lambda model, request, job: (seen.update(request),
                                                     real(model, request, job))[1])
    started = _post_transcribe(client, path=recording).json()
    _wait_job(started["jobId"])

    assert seen["row"] == {"title": os.path.basename(recording), "model": "org/fake-whisper",
                           "kind": "task", "cancellable": True, "unit": "s"}


def test_the_terminal_report_can_rebuild_an_evicted_row(
        client, fake_transcribe_runner, recording):
    """A decode can run for hours, so the row may well be gone by the time it
    finishes — and a bare `state="done"` is refused, leaving the page watching
    a row that never completes for a transcript already on disk."""
    started = _post_transcribe(client, path=recording).json()
    row = _wait_job(started["jobId"])
    assert row["state"] == "done"

    # Evict it exactly as the cap does, then replay the terminal report.
    with jobs._lock:
        jobs._jobs.pop(started["jobId"], None)
    supervisor._report(started["jobId"],
                       **supervisor.transcribe_row_fields(os.path.basename(recording)),
                       state="done", detail="Saved")
    rebuilt = _row_now(started["jobId"])
    assert rebuilt is not None and rebuilt["state"] == "done"
    assert rebuilt["title"] == os.path.basename(recording)


def test_a_transcription_row_is_server_owned_and_reserved(
        client, fake_transcribe_runner, recording):
    started = _post_transcribe(client, path=recording).json()
    assert started["jobId"].startswith(supervisor.TRANSCRIBE_JOB_PREFIX)
    row = _wait_job(started["jobId"])
    assert row["owner"] == jobs.OWNER_SERVER
    refused = client.post("/api/jobs", json={"id": started["jobId"], "state": "done"},
                          headers={"X-Fused": "1"})
    assert refused.status_code == 400


def test_a_failure_reaches_the_page_even_with_the_QUEUE_OVER_THE_CAP(
        client, fake_transcribe_runner, recording, monkeypatch):
    """The outcome a watcher sees, end to end, on the queue this feature is for.

    With more than `MAX_JOBS` live server rows, a row that had just reached a
    terminal state was the only thing the cap could still evict — so it went on
    the next `list_jobs()`, which is the same read `fused.watchJob` polls, and
    the page learned nothing. A success survives that because the transcript is
    on disk; a FAILURE has no artefact, so `fused.ai.transcribe()` rejected with
    "no longer being reported" instead of the reason.

    `_wait_job` polls exactly what the bridge polls, so this asserts what the
    page would actually observe rather than what the registry contains.
    """
    monkeypatch.setenv("FAKE_TRANSCRIBE_FAILS", "1")
    # A queue already over the cap, all of it live server work.
    for i in range(jobs.MAX_JOBS + 4):
        supervisor._report(f"{jobs.SERVER_ID_PREFIX}ai-transcribe:bulk{i}",
                           **supervisor._transcribe_row(f"rec{i}.m4a", "Queued…"))

    started = _post_transcribe(client, path=recording).json()
    row = _wait_job(started["jobId"])
    assert row["state"] == "error", row
    assert "exploded" in row["message"], row


def test_a_failing_transcription_reports_the_reason_on_the_row(
        client, fake_transcribe_runner, recording, monkeypatch):
    monkeypatch.setenv("FAKE_TRANSCRIBE_FAILS", "1")
    started = _post_transcribe(client, path=recording).json()
    row = _wait_job(started["jobId"])
    assert row["state"] == "error"
    assert "exploded" in row["message"]


def test_a_transcription_with_no_runner_says_why_before_a_row_opens(
        client, monkeypatch, recording):
    """The image route's property, kept: the runner check is synchronous, so an
    unservable request answers with the reason instead of opening a row."""
    ghost = registry.Runner(
        code="ghost", capability=registry.SPEECH_TO_TEXT,
        folder="/nowhere", label="Ghost",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (ghost,))
    response = _post_transcribe(client, path=recording)
    assert response.status_code == 409
    assert "not built yet" in response.json()["error"]
    assert not [j for j in jobs.list_jobs()
                if j["id"].startswith(supervisor.TRANSCRIBE_JOB_PREFIX)]


def test_start_transcribe_raises_rather_than_opening_a_row(monkeypatch, recording):
    """The same thing at the supervisor's own door, since that is where the
    property lives — the route only passes the error on."""
    monkeypatch.setattr(registry, "_RUNNERS", ())
    with pytest.raises(supervisor.SupervisorError):
        supervisor.start_transcribe(
            "org/whisper", {"path": recording, "out": "/tmp/x.json",
                            "outText": "/tmp/x.txt"},
            supervisor.transcribe_job_id("abc"))
    assert not jobs.list_jobs()


def test_a_transcription_is_not_capped_at_the_image_timeout(monkeypatch):
    """The socket timeout has to outlast the DECODE, because nothing is sent
    until it finishes.

    `_single` writes one JSON reply when `generate` returns, so `urlopen` blocks
    for the whole run — and at `GENERATE_TIMEOUT_S` (900s) the 90-minute
    recording this whole feature is designed around (~18 minutes at CPU int8)
    times out, errors the row, and rejects `fused.ai.transcribe()` while the
    worker carries on and writes a perfectly good transcript nobody is told
    about. Worse, that worker still holds `GENERATE_LOCK`, so every queued
    request repeats the failure in turn.
    """
    seen = {}

    class _Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"ok": True, "result": {"duration": 5400.0}}).encode()

    def fake_request(worker, path, body=None, timeout=None):
        seen["timeout"] = timeout
        return _Reply()

    # A bare `object()` stood in here before `_in_use` needed `last_activity`
    # / `in_flight` to bracket the call — these tests are about the request
    # timeout and the queueing, not about the worker, so a `SimpleNamespace`
    # with just the two new fields is the whole of the update.
    monkeypatch.setattr(supervisor, "ready_worker", lambda capability, model=None:
                        types.SimpleNamespace(last_activity=time.monotonic(), in_flight=0))
    monkeypatch.setattr(supervisor, "_worker_request", fake_request)
    supervisor.generate_transcript("org/whisper", {"path": "/x"},
                                   supervisor.TRANSCRIBE_JOB_PREFIX + "t")

    assert seen["timeout"] > supervisor.GENERATE_TIMEOUT_S
    # Comfortably past any recording somebody would actually hand it: four
    # hours of DECODING is ~20 hours of audio at the default model's CPU speed.
    assert seen["timeout"] >= 4 * 3600


def _row_now(job_id):
    """The row as it stands RIGHT NOW — `_row` waits for a terminal state, and
    everything below is about a row that is deliberately still running."""
    return next((j for j in jobs.list_jobs() if j["id"] == job_id), None)


def _wait_for_queued(job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = _row_now(job_id)
        if row and "ueued" in (row.get("detail") or ""):
            return row
        time.sleep(0.02)
    return _row_now(job_id)


def _open_transcribe_row(job, title="recording.m4a"):
    """Open the row the way `start_transcribe` does, since these tests drive
    `generate_transcript` directly — the first report for a job must carry a
    title, and in production that one has already happened."""
    supervisor._report(job, title=title, state="running", kind="task",
                       cancellable=True, unit="s", detail="Preparing…",
                       done=None, total=None)


def _blocking_worker_request(release, started=None):
    """A `_worker_request` that parks until `release` is set, like a worker
    holding its GENERATE_LOCK through a long decode."""
    class _Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"ok": True, "result": {"duration": 1.0}}).encode()

    def request(worker, path, body=None, timeout=None):
        if started is not None:
            started.set()
        release.wait(20)
        return _Reply()

    return request


def test_a_queued_transcription_says_QUEUED_rather_than_going_stale(monkeypatch):
    """The cost of a four-hour timeout, and the bug it turned from rare to normal.

    A second transcription blocks inside the worker's `_single` BEFORE it
    reaches `heartbeat()`, so its row gets no ticks at all while it waits.
    `jobs.STALE_AFTER_S` is 30s, so the manager labelled merely-queued work "no
    longer reporting", its ✕ did nothing, and `_sweep` dropped the row after
    ten minutes — at which point `fused.ai.transcribe()` rejects work that is
    still running and about to write a perfectly good transcript.
    """
    release = threading.Event()
    first_in_flight = threading.Event()
    # A bare `object()` stood in here before `_in_use` needed `last_activity`
    # / `in_flight` to bracket the call — these tests are about the request
    # timeout and the queueing, not about the worker, so a `SimpleNamespace`
    # with just the two new fields is the whole of the update.
    monkeypatch.setattr(supervisor, "ready_worker", lambda capability, model=None:
                        types.SimpleNamespace(last_activity=time.monotonic(), in_flight=0))
    monkeypatch.setattr(supervisor, "_worker_request",
                        _blocking_worker_request(release, first_in_flight))
    monkeypatch.setattr(supervisor, "_QUEUE_TICK_S", 0.05)
    monkeypatch.setattr(supervisor, "_QUEUE_POLL_S", 0.02)

    _open_transcribe_row(supervisor.TRANSCRIBE_JOB_PREFIX + "one")
    _open_transcribe_row(supervisor.TRANSCRIBE_JOB_PREFIX + "two")
    first = threading.Thread(
        target=supervisor.generate_transcript,
        args=("org/w", {"path": "/a"}, supervisor.TRANSCRIBE_JOB_PREFIX + "one"),
        daemon=True)
    first.start()
    assert first_in_flight.wait(5), "the first transcription never reached the worker"

    second = threading.Thread(
        target=supervisor.generate_transcript,
        args=("org/w", {"path": "/b"}, supervisor.TRANSCRIBE_JOB_PREFIX + "two"),
        daemon=True)
    second.start()
    try:
        row = _wait_for_queued(supervisor.TRANSCRIBE_JOB_PREFIX + "two")
        assert row and "ueued" in (row.get("detail") or ""), row
        assert row["state"] == "running", row
        # Ticking is the point: a row that keeps reporting is never stalled and
        # is never swept, however long the queue is.
        first_update = row["updated_at"]
        time.sleep(0.25)
        assert _row_now(supervisor.TRANSCRIBE_JOB_PREFIX + "two")["updated_at"] > first_update
    finally:
        release.set()
        first.join(10)
        second.join(10)


def test_an_EVICTED_queued_row_is_rebuilt_on_DETECTION_not_on_the_next_tick(monkeypatch):
    """A queued row is thrown away by the cap, and it has to come back FAST.

    `jobs._sweep` drops rows over `MAX_JOBS` sorted by `(running, updated_at)`,
    and queueing is exactly what produces more than 64 rows. Two ways this
    used to break: a rebuild carrying only a `title` came back as a different
    row (no ✕, no unit), and — once the write cadence was slowed so the cap
    sheds queued rows before the active decode's — a rebuild that waited for
    the next scheduled tick left the row absent for ten seconds against a
    watcher that gives up after ~3.5s, so the page was told a queued
    transcription had stopped reporting.

    So the write cadence is a heartbeat and the POLL is what guarantees the
    row: it has just read the list, so it knows the row is gone and restates it
    at once. `_QUEUE_TICK_S` is set far beyond this test's patience here —
    nothing but detection can make it pass.
    """
    release = threading.Event()
    first_in_flight = threading.Event()
    # A bare `object()` stood in here before `_in_use` needed `last_activity`
    # / `in_flight` to bracket the call — these tests are about the request
    # timeout and the queueing, not about the worker, so a `SimpleNamespace`
    # with just the two new fields is the whole of the update.
    monkeypatch.setattr(supervisor, "ready_worker", lambda capability, model=None:
                        types.SimpleNamespace(last_activity=time.monotonic(), in_flight=0))
    monkeypatch.setattr(supervisor, "_worker_request",
                        _blocking_worker_request(release, first_in_flight))
    monkeypatch.setattr(supervisor, "_QUEUE_TICK_S", 30.0)
    monkeypatch.setattr(supervisor, "_QUEUE_POLL_S", 0.02)

    job = supervisor.TRANSCRIBE_JOB_PREFIX + "two"
    _open_transcribe_row(supervisor.TRANSCRIBE_JOB_PREFIX + "one")
    _open_transcribe_row(job, title="recording.m4a")
    first = threading.Thread(
        target=supervisor.generate_transcript,
        args=("org/w", {"path": "/a"}, supervisor.TRANSCRIBE_JOB_PREFIX + "one"),
        daemon=True)
    first.start()
    assert first_in_flight.wait(5)

    def run_second():
        # Swallowed because the assertion below CANCELS this one on purpose,
        # and a SupervisorError raised at the top of a thread is raised at
        # nobody but pytest's unhandled-exception warning.
        try:
            supervisor.generate_transcript("org/w", {"path": "/recording.m4a"}, job)
        except supervisor.SupervisorError:
            pass

    second = threading.Thread(target=run_second, daemon=True)
    second.start()
    try:
        _wait_for_queued(job)
        # Exactly what the sweep does to it when the cap bites.
        with jobs._lock:
            jobs._jobs.pop(job, None)
        assert _row_now(job) is None

        evicted_at = time.monotonic()
        deadline = evicted_at + 2
        while time.monotonic() < deadline and _row_now(job) is None:
            time.sleep(0.01)
        reopened = _row_now(job)
        assert reopened is not None, "an evicted queue row never came back"
        # Within a poll, not within a tick — the tick is 30s here, so this
        # latency is the property under test and not incidental.
        assert time.monotonic() - evicted_at < 1.0
        # THE SAME row, not merely a row with the same id. A rebuild that
        # restated only the title came back with `cancellable` defaulted to
        # False, so the manager hid the ✕ and the user still could not stop a
        # queued transcription — the exact failure reopening exists to prevent.
        # `unit` matters for the same reason at one remove: without it the
        # seconds clock reverts to a bare pair of numbers.
        assert reopened["cancellable"] is True, reopened
        assert reopened["state"] == "running" and reopened["kind"] == "task"
        assert reopened["unit"] == "s"
        assert reopened["title"] == "recording.m4a"
        # …and the ✕ works again, which is the whole reason the row matters.
        assert jobs.request_cancel(job) is not None
    finally:
        release.set()
        first.join(10)
        second.join(10)


def test_the_WAIT_FOR_A_COLD_MODEL_can_rebuild_an_evicted_row(
        client, fake_transcribe_runner, recording, monkeypatch):
    """The last reporter on this path that could not rebuild its row — and the
    likeliest of all of them to have to.

    A cold model is a multi-GB pull, so `_wait_ready` is the longest-running
    reporter in the supervisor; with the cap biting, its row is evicted during
    the download. Its tick used to carry only a `detail`, so `upsert` refused
    it, `_report` swallowed the error, and the row stayed gone for the whole
    load: no progress, no ✕, and `watch()` resolving null a few seconds in
    while the transcription was still perfectly alive.
    """
    # Long enough that the whole assertion below happens INSIDE the wait: the
    # worker's own ticks would rebuild the row too, so a test that let the load
    # finish would pass without `_wait_ready` restating anything.
    monkeypatch.setenv("FAKE_LOAD_SECONDS", "6")
    started = _post_transcribe(client, path=recording).json()
    job = started["jobId"]

    # Evict it exactly as the cap does, mid-load. `waiting_for`, not a
    # "Waiting for" substring in `detail` any more — the merge (this change)
    # makes `detail` the LOAD row's own line verbatim, and `waiting_for` is
    # what actually names the wait now.
    deadline = time.monotonic() + 5
    row = None
    while time.monotonic() < deadline:
        row = _row_now(job)
        if row and row.get("waiting_for"):
            break
        time.sleep(0.02)
    assert row and row.get("waiting_for"), row
    with jobs._lock:
        jobs._jobs.pop(job, None)

    deadline = time.monotonic() + 3
    rebuilt = None
    while time.monotonic() < deadline:
        rebuilt = _row_now(job)
        if rebuilt is not None:
            break
        time.sleep(0.02)
    assert rebuilt is not None, "the wait could not rebuild its row"
    # STILL WAITING — so it was the wait's own tick that rebuilt it, not a
    # later reporter. That is what makes this test about `_wait_ready`.
    assert rebuilt.get("waiting_for"), rebuilt
    assert rebuilt["title"] == os.path.basename(recording)
    # `cancellable` survives the rebuild from `transcribe_row_fields`'s
    # payload; `unit` does NOT stay "s" here — the merge (this change) mirrors
    # the LOAD row's own unit ("", nothing byte-shaped reported yet at
    # "Starting the model process…") for as long as the wait holds, and only
    # restores the transcription's own "s" once the wait ends (below).
    assert rebuilt["cancellable"] is True
    final = _wait_job(job, timeout=40)
    assert final["unit"] == "s"


def _watcher_giveup_window_s():
    """How long `fused.watchJob` tolerates a missing row, read from the bridge.

    Two numbers in `runtime.js`: the poll interval and the number of
    consecutive misses that resolve the promise with null. Read rather than
    restated, for the same reason `HEARTBEAT_S` is — this is a relationship
    between the supervisor and the page, and a copy here would go stale in the
    direction that looks fine.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fused_render", "static", "runtime.js")
    source = open(path, encoding="utf-8").read()
    watch = source[source.index("async watch(onUpdate, intervalMs)"):]
    watch = watch[:watch.index("stop()")]
    interval_ms = int(re.search(r"intervalMs \|\| (\d+)", watch).group(1))
    misses = int(re.search(r"\+\+missing >= (\d+)", watch).group(1))
    return misses * interval_ms / 1000.0


def test_a_LIVE_transcription_row_is_never_absent_at_all():
    """The guarantee, and it is now structural rather than arithmetic.

    Nine rounds of this feature tried to make the row come BACK fast enough:
    restate it on every tick, split the cadences, rebuild on detection. All of
    it was compensation for the row being evictable in the first place, and
    none of it could reach the real consequence — `watchJob` resolves null
    after five consecutive misses (~3.5s) and a settled promise cannot be
    un-settled by a row that returns.

    Live server rows are exempt from the cap now
    (`test_live_SERVER_work_is_never_evicted_by_the_cap`), so for a live
    transcription the maximum absence is ZERO and the watcher's window does not
    constrain any cadence this module picks. What is asserted here is that the
    exemption actually covers the rows this feature opens — a `sys:` id and a
    running state — because that is the link between the two modules and the
    thing that would break silently if either end changed.
    """
    job = supervisor.TRANSCRIBE_JOB_PREFIX + "exempt"
    supervisor._report(job, **supervisor._transcribe_row("rec.m4a", "Preparing…"))
    row = _row_now(job)
    assert row is not None
    assert row["owner"] == jobs.OWNER_SERVER and row["state"] == "running"

    # Enough page-owned rows to blow the cap several times over: the
    # transcription row must still be there afterwards.
    for i in range(jobs.MAX_JOBS * 2):
        jobs.upsert({"id": f"noise{i}", "title": "x", "state": "running"})
    assert _row_now(job) is not None, "a live transcription row was evicted"

    # Every removal path was walked against a row of this exact shape and none
    # of them reaches it: cap eviction (exempt), the age sweep (`_QUEUE_TICK_S`
    # is far inside `STALE_DROP_S`), `clear_finished` (refuses every RUNNING
    # row unconditionally, stalled included — D558) and `dismiss` (refuses a
    # RUNNING row that is not stalled). ONE remote path survives — a tick
    # thread starved past `STALE_AFTER_S` makes the row dismissible one at a
    # time, and a user looking at "no longer reporting" may well dismiss it —
    # and the rebuild on detection heals that, because `_transcribe_row`
    # carries the `title` and `state: "running"` that reopen a forgotten id.
    # So the poll cadence is the backstop's latency, and it still has to beat
    # the watcher.
    window = _watcher_giveup_window_s()
    assert supervisor._QUEUE_POLL_S < window / 2, (
        f"a dismissed-while-stalled row takes {supervisor._QUEUE_POLL_S}s to come "
        f"back against a {window}s give-up window")


def test_a_waiting_row_never_reads_as_no_longer_reporting():
    """What the write cadence is FOR, now that it is not about eviction.

    It used to be sized against `worker_base.HEARTBEAT_S`, to decide which live
    row the cap would shed first. The cap does not shed live server rows any
    more, so that relationship is gone and the only remaining constraint is the
    honest one it always also had: a queued row must report often enough not to
    be displayed as "no longer reporting" while it is merely waiting its turn.

    Deliberately re-derived rather than left pointing at the heartbeat — a
    rationale that no longer describes the code is worse than none, because the
    next person trusts it (D288).
    """
    assert supervisor._QUEUE_TICK_S < jobs.STALE_AFTER_S, (
        "a queued transcription would be reported as stalled while waiting")
    # And the ✕ is read on its own, much faster cadence, so cancelling a queued
    # transcription does not wait on the display heartbeat.
    assert supervisor._QUEUE_POLL_S <= 1.0
    assert supervisor._QUEUE_POLL_S < supervisor._QUEUE_TICK_S


def test_an_exception_taking_the_turn_does_not_WEDGE_transcription_forever(monkeypatch):
    """`_TRANSCRIBE_LOCK` is a module global that is never re-created, so a
    single leak is permanent and process-wide.

    `_await_turn` returns HOLDING the lock, and the post-acquire cancel check
    ran before any caller's `finally` existed — it walks `jobs.list_jobs()`, so
    an exception there escaped with the lock held and every later transcription
    blocked forever showing "Queued behind another transcription…" with nothing
    running.
    """
    job = supervisor.TRANSCRIBE_JOB_PREFIX + "boom"
    _open_transcribe_row(job)

    def explode(_job):
        raise RuntimeError("the job registry blew up")

    monkeypatch.setattr(supervisor, "_cancel_state", explode)
    with pytest.raises(RuntimeError):
        supervisor._await_turn(job, "x.m4a")
    monkeypatch.undo()

    # The lock is free, so the NEXT transcription is not wedged.
    assert supervisor._TRANSCRIBE_LOCK.acquire(blocking=False)
    supervisor._TRANSCRIBE_LOCK.release()


def test_the_turn_is_released_even_when_the_body_raises(monkeypatch):
    """The pairing itself: acquisition and release are one construct, so a
    caller cannot take a turn and forget to give it back."""
    monkeypatch.setattr(supervisor, "_await_turn", lambda job, title, model="": None)
    supervisor._TRANSCRIBE_LOCK.acquire()
    with pytest.raises(ValueError):
        with supervisor._transcribe_turn("sys:ai-transcribe:x", "x.m4a"):
            raise ValueError("boom")
    assert supervisor._TRANSCRIBE_LOCK.acquire(blocking=False)
    supervisor._TRANSCRIBE_LOCK.release()


def test_a_missing_row_is_UNKNOWN_rather_than_not_cancelled():
    """The half that matters on its own. `cancel_requested` is server state no
    report can restore, so a poller reading a missing row as False is guessing
    — usually right, occasionally losing a ✕, and silent either way."""
    job = supervisor.TRANSCRIBE_JOB_PREFIX + "ghost"
    assert supervisor._cancel_state(job) is None
    assert supervisor._cancel_requested(job) is False  # unchanged for its callers

    supervisor._report(job, **supervisor._transcribe_row("x.m4a", "Preparing…"))
    assert supervisor._cancel_state(job) is False
    jobs.request_cancel(job)
    assert supervisor._cancel_state(job) is True


def test_the_cross_on_a_QUEUED_transcription_is_honoured(monkeypatch):
    """Its ✕ used to do nothing: cancellation reaches a worker through the
    reply to a tick, and a queued request is not ticking — nor has anything of
    it reached the worker to cancel."""
    release = threading.Event()
    first_in_flight = threading.Event()
    # A bare `object()` stood in here before `_in_use` needed `last_activity`
    # / `in_flight` to bracket the call — these tests are about the request
    # timeout and the queueing, not about the worker, so a `SimpleNamespace`
    # with just the two new fields is the whole of the update.
    monkeypatch.setattr(supervisor, "ready_worker", lambda capability, model=None:
                        types.SimpleNamespace(last_activity=time.monotonic(), in_flight=0))
    monkeypatch.setattr(supervisor, "_worker_request",
                        _blocking_worker_request(release, first_in_flight))
    monkeypatch.setattr(supervisor, "_QUEUE_TICK_S", 0.05)
    monkeypatch.setattr(supervisor, "_QUEUE_POLL_S", 0.02)

    _open_transcribe_row(supervisor.TRANSCRIBE_JOB_PREFIX + "one")
    first = threading.Thread(
        target=supervisor.generate_transcript,
        args=("org/w", {"path": "/a"}, supervisor.TRANSCRIBE_JOB_PREFIX + "one"),
        daemon=True)
    first.start()
    assert first_in_flight.wait(5)

    job = supervisor.TRANSCRIBE_JOB_PREFIX + "two"
    _open_transcribe_row(job)
    failed = {}

    def run():
        try:
            supervisor.generate_transcript("org/w", {"path": "/b"}, job)
        except supervisor.SupervisorError as e:
            failed["error"] = str(e)

    second = threading.Thread(target=run, daemon=True)
    second.start()
    try:
        _wait_for_queued(job)
        jobs.request_cancel(job)
        second.join(5)
        assert failed.get("error") == "cancelled", failed
    finally:
        release.set()
        first.join(10)


def test_the_cross_is_honoured_on_the_UNCONTENDED_path_too(monkeypatch):
    """The cancel check has to sit on every route into "we hold the lock", not
    only on the slow one.

    `_await_turn`'s fast path — one non-blocking acquire, added so a lone
    transcription's row is untouched — returned holding the lock without ever
    reading `cancel_requested`. So with a model already resident, a ✕ pressed
    on "Preparing…" still POSTed `/generate` and started faster-whisper's eager
    full-file decode; the cancel was noticed a tick later, by which point the
    abandoned decode thread was running and the NEXT transcription had to wait
    it out (`_await_orphan`). An optimisation that skips a guard is the guard
    being wrong, not the optimisation.
    """
    posted = []
    # A bare `object()` stood in here before `_in_use` needed `last_activity`
    # / `in_flight` to bracket the call — these tests are about the request
    # timeout and the queueing, not about the worker, so a `SimpleNamespace`
    # with just the two new fields is the whole of the update.
    monkeypatch.setattr(supervisor, "ready_worker", lambda capability, model=None:
                        types.SimpleNamespace(last_activity=time.monotonic(), in_flight=0))
    monkeypatch.setattr(supervisor, "_worker_request",
                        lambda *a, **k: posted.append(a) or pytest.fail(
                            "a cancelled transcription reached the worker"))

    job = supervisor.TRANSCRIBE_JOB_PREFIX + "solo"
    _open_transcribe_row(job)
    jobs.request_cancel(job)

    # Nothing else holds the lock: this is the uncontended path, start to finish.
    with pytest.raises(supervisor.SupervisorError) as caught:
        supervisor.generate_transcript("org/w", {"path": "/a"}, job)
    assert str(caught.value) == "cancelled"
    assert not posted
    # And the lock is not left held behind the raise, or the next transcription
    # queues forever behind a request that never ran.
    assert supervisor._TRANSCRIBE_LOCK.acquire(blocking=False)
    supervisor._TRANSCRIBE_LOCK.release()


def test_a_queued_transcription_resolves_its_MODEL_only_once_it_has_the_lock(monkeypatch):
    """Resolving the worker outside the lock lets a queued request kill a
    running one.

    `_wait_ready` -> `_start_resident` evicts whatever holds the capability when
    the model differs: it sets `stopping` and terminates the worker. Done before
    taking `_TRANSCRIBE_LOCK`, that is the ONE destructive step in this path
    happening outside the lock that exists to serialize it — so a page asking
    for `faster-whisper-small` while a 90-minute run is mid-decode on the
    catalog default kills that worker, loses the transcript, fails the first row
    with "the transcription process did not answer", and then queues behind a
    lock nobody is holding.

    The same ordering breaks the identical-model case more quietly: a `worker`
    captured before a wait that can last hours is a handle to a process an
    unload may since have killed, so the request goes to a dead port instead of
    re-resolving.
    """
    release = threading.Event()
    first_in_flight = threading.Event()
    resolved = []

    def spy_ready_worker(capability, model=None):
        resolved.append(model)
        return types.SimpleNamespace(last_activity=time.monotonic(), in_flight=0)

    monkeypatch.setattr(supervisor, "ready_worker", spy_ready_worker)
    monkeypatch.setattr(
        supervisor, "_wait_ready",
        lambda *a, **k: pytest.fail("a queued request evicted the running model"))
    monkeypatch.setattr(supervisor, "_worker_request",
                        _blocking_worker_request(release, first_in_flight))
    monkeypatch.setattr(supervisor, "_QUEUE_TICK_S", 0.05)
    monkeypatch.setattr(supervisor, "_QUEUE_POLL_S", 0.02)

    _open_transcribe_row(supervisor.TRANSCRIBE_JOB_PREFIX + "one")
    _open_transcribe_row(supervisor.TRANSCRIBE_JOB_PREFIX + "two")
    first = threading.Thread(
        target=supervisor.generate_transcript,
        args=("org/default", {"path": "/a"}, supervisor.TRANSCRIBE_JOB_PREFIX + "one"),
        daemon=True)
    first.start()
    assert first_in_flight.wait(5)

    second = threading.Thread(
        target=supervisor.generate_transcript,
        args=("org/other", {"path": "/b"}, supervisor.TRANSCRIBE_JOB_PREFIX + "two"),
        daemon=True)
    second.start()
    try:
        _wait_for_queued(supervisor.TRANSCRIBE_JOB_PREFIX + "two")
        # The whole point: while the first is still decoding, the second has
        # touched NOTHING about which model is resident.
        assert resolved == ["org/default"], resolved
    finally:
        release.set()
        first.join(10)
        second.join(10)
    # …and once it had its turn, it resolved for itself rather than reusing a
    # handle taken before the wait.
    assert resolved == ["org/default", "org/other"], resolved


def _lift_js_fn(source, marker):
    """One named `function` lifted out of `runtime.js` by its declaration
    text, body included. Shared by every harness below that drives a real
    bridge function under node."""
    start = source.index(marker)
    return source[start:source.index("\n  }\n", start) + 4]


def _js_fn_with_helper(source, marker):
    """`_lift_js_fn`, plus `rejectUnknownOptions` ahead of it — `aiImage` and
    `aiTranscribe` both call it now (D413), and it is not itself part of
    either function's own source, so a harness that lifts only the target
    function leaves the call unresolved."""
    return (_lift_js_fn(source, "  function rejectUnknownOptions(")
            + _lift_js_fn(source, marker))


def _run_ai_transcribe(readfile, record, node_required=True, opts='{path: "a.m4a"}',
                       extra=None):
    """Run `aiTranscribe` out of runtime.js under node, against stubs.

    The same extraction the claude suites use (`tests/test_claude_narrow.py`):
    a named function is lifted out and driven with its closure stubbed, because
    what matters is the decision it reaches rather than the DOM it reached it
    in. This bridge had only source assertions until now, which cannot tell a
    typed rejection from an untyped one.

    `readfile` is JS for the body of the stub `readFile`; `record` is the job
    row `watch` resolves with; `opts` is the argument object as JS, so a caller
    can drive the argument checks that reject before any of the stubs are ever
    reached. `extra` is one more JS `key: expression` reported alongside the
    rejection, for a test that cares about a field beyond the usual three.
    Returns the settled outcome as a dict.
    """
    import shutil
    import subprocess

    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own transcription glue")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fused_render", "static", "runtime.js")
    source = open(path, encoding="utf-8").read()
    fn = _js_fn_with_helper(source, "  function aiTranscribe(opts)")

    prelude = f"""
      const started = {{jobId: "sys:ai-transcribe:x", output: "/t/out.json",
                        outputText: "/t/out.txt",
                        outputPartial: "/t/out.partial.jsonl", path: "/t/a.m4a",
                        model: "m", task: "transcribe"}};
      const window = {{location: {{search: "?path=/pages/p.html"}}}};
      const aiPost = () => Promise.resolve(started);
      const rawUrl = (p) => "/api/fs/raw?path=" + p;
      const stat = () => Promise.reject(new Error("no stat"));
      const readFile = () => {readfile};
      // A caller with no `onSegment` must make no request of its own. The
      // bridge swallows tail failures on purpose, so an unstubbed `fetch`
      // would let a regression here pass silently as a rejected promise
      // nobody reads — this one is LOUD instead.
      globalThis.fetch = (url) => {{
        console.log(JSON.stringify({{ok: false, unexpectedFetch: String(url)}}));
        process.exit(0);
      }};
      const watchJob = () => ({{
        watch: () => Promise.resolve({record}),
        get: () => Promise.resolve({record}),
        stop() {{}}, cancel: () => Promise.resolve(true),
      }});
    """
    # Not an f-string: the body below is JS object literals, and doubling every
    # brace to smuggle one substitution through would make it unreadable.
    call = """
      aiTranscribe(OPTS).then(
        (value) => console.log(JSON.stringify({ok: true, value})),
        (err) => console.log(JSON.stringify(
          {ok: false, message: err.message, type: err.type, jobId: err.jobId,
           EXTRA})),
      );
    """.replace("OPTS", opts).replace("EXTRA", extra or '"_": null')
    # Node writes UTF-8 to stdout regardless of platform; without an explicit
    # `encoding` here, Windows decodes with `locale.getpreferredencoding()`
    # (often cp1252), which mangles or crashes on the multibyte transcript
    # text some of these harnesses drive through (see e.g.
    # test_a_MULTIBYTE_transcript_does_not_split_a_character_or_lose_its_place).
    out = subprocess.run(["node", "-e", prelude + fn + call],
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_an_unreadable_transcript_rejects_TYPED_like_every_other_failure():
    """`done()` does I/O, so it can fail on its own — a transcript deleted
    between the row going `done` and this fetch, an unreadable path, a
    truncated file that fails `JSON.parse`.

    Untyped, those arrived as a bare `SyntaxError` or "failed to read … HTTP
    404" with no `.type` and no `.jobId`, so a caller switching on `err.type`
    fell through to its unknown-error path on the one failure it could most
    easily explain — while the sibling branch three lines below was typed.
    `aiImage` has no equivalent exposure: its `done()` does no I/O.
    """
    row = '{state: "done"}'
    missing = _run_ai_transcribe('Promise.reject(new Error("failed to read (HTTP 404)"))', row)
    assert missing["ok"] is False
    assert missing["type"] == "ai_error"
    assert missing["jobId"] == "sys:ai-transcribe:x"
    assert "transcript could not be read" in missing["message"]

    truncated = _run_ai_transcribe('Promise.resolve("{\\"text\\": ")', row)
    assert truncated["ok"] is False and truncated["type"] == "ai_error"
    assert truncated["jobId"] == "sys:ai-transcribe:x"


def test_the_transcription_bridge_resolves_with_the_words_and_the_url():
    """The success path, end to end through the real function."""
    good = ('Promise.resolve(JSON.stringify({text: "hello world", '
            'segments: [{start: 0, end: 1.5, text: "hello world"}], '
            'language: "en", duration: 1.5}))')
    settled = _run_ai_transcribe(good, '{state: "done"}')
    assert settled["ok"] is True, settled
    value = settled["value"]
    assert value["text"] == "hello world"
    assert value["segments"][0]["end"] == 1.5
    assert value["language"] == "en" and value["duration"] == 1.5
    assert value["url"] == "/api/fs/raw?path=/t/out.json"


def _run_ai_image(record='{state: "done"}', ticks="[]", preview='"/t/a.preview.png"',
                  opts='{prompt: "a fox", onProgress: (job) => progress.push(job)}'):
    """Run `aiImage` out of runtime.js under node, against stubs.

    `_run_ai_transcribe`'s harness for the other half of the same API: the
    function is lifted out and driven with its closure stubbed, because what
    matters is the object it hands the page rather than the DOM it built it in.

    `ticks` is a JS array of job records the fake watcher replays through
    `onProgress`, and they are echoed back untouched so a test can prove the
    bridge annotated a COPY rather than the row the manager is drawing.
    """
    import shutil
    import subprocess

    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own image glue")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fused_render", "static", "runtime.js")
    source = open(path, encoding="utf-8").read()
    fn = _js_fn_with_helper(source, "  function aiImage(opts)")

    prelude = """
      const started = {jobId: "sys:ai-image:x", path: "/t/a.png", seed: 7,
                       steps: 4, previewPath: PREVIEW};
      const window = {location: {search: "?path=/pages/p.html"}};
      const aiPost = () => Promise.resolve(started);
      const rawUrl = (p) => "/api/fs/raw?path=" + p;
      const stat = () => Promise.reject(new Error("no stat"));
      const rows = TICKS;
      const watchJob = () => ({
        watch: (cb) => {
          for (const row of rows) if (cb) cb(row);
          return Promise.resolve(RECORD);
        },
        get: () => Promise.resolve(RECORD),
        stop() {}, cancel: () => Promise.resolve(true),
      });
      const progress = [];
    """.replace("PREVIEW", preview).replace("TICKS", ticks).replace("RECORD", record)
    call = """
      aiImage(OPTS).then(
        (value) => console.log(JSON.stringify({ok: true, value, progress, rows})),
        (err) => console.log(JSON.stringify(
          {ok: false, message: err.message, type: err.type, progress, rows})),
      );
    """.replace("OPTS", opts)
    # Node writes UTF-8 to stdout regardless of platform; without an explicit
    # `encoding` here, Windows decodes with `locale.getpreferredencoding()`
    # (often cp1252), which mangles or crashes on the multibyte transcript
    # text some of these harnesses drive through (see e.g.
    # test_a_MULTIBYTE_transcript_does_not_split_a_character_or_lose_its_place).
    out = subprocess.run(["node", "-e", prelude + fn + call],
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


RUNNING = '{state: "running", done: %d, total: 4}'


def test_every_progress_tick_carries_a_READY_TO_USE_preview_url():
    """A page sets this as an `<img>` src and gets a picture emerging out of
    noise. Built here rather than by every caller, for the same reason `url`
    is — and CACHE-BUSTED BY STEP, because the preview is one path overwritten
    in place and a browser handed the same URL twice shows frame 2 forever."""
    settled = _run_ai_image(ticks="[%s, %s]" % (RUNNING % 1, RUNNING % 2))
    assert settled["ok"] is True, settled
    assert [tick["previewUrl"] for tick in settled["progress"]] == [
        "/api/fs/raw?path=/t/a.preview.png&step=1",
        "/api/fs/raw?path=/t/a.preview.png&step=2",
    ]


def test_the_tick_a_page_sees_is_a_COPY_of_the_row_the_manager_is_drawing():
    """The record belongs to the job manager and every other watcher of that
    row sees the same object — a field written onto it here would travel."""
    settled = _run_ai_image(ticks="[%s]" % (RUNNING % 1))
    assert settled["rows"] == [{"state": "running", "done": 1, "total": 4}]
    assert settled["progress"][0]["done"] == 1


@pytest.mark.parametrize("state", ["done", "error", "cancelled"])
def test_the_LAST_tick_has_no_preview_because_the_file_is_already_gone(state):
    """`watch` calls back with the TERMINAL record too, and by the time a row
    reaches one the worker's sink has discarded the preview — `Sink.__exit__`
    runs before `generate()` returns, which is before the row can be marked.

    So a page that keeps its `<img>` pointed at the latest `previewUrl` would
    end every render on a guaranteed 404: a blank flash exactly where the
    finished picture should appear. Null is the honest answer, and it is the
    same answer on a cancel and an error — there is no frame there either."""
    settled = _run_ai_image(record='{state: "%s"}' % state,
                            ticks="[%s, {state: '%s', done: 4, total: 4}]"
                                  % (RUNNING % 3, state))
    ticks = settled["progress"]
    assert ticks[0]["previewUrl"] == "/api/fs/raw?path=/t/a.preview.png&step=3"
    assert ticks[1]["previewUrl"] is None, ticks[1]


def test_the_resolved_image_carries_the_url_and_a_NULL_preview():
    """Same fact from the other side: the resolved object names the real PNG,
    and `previewUrl` is null because the file it would name has been deleted.
    A URL to a file that is gone is worse than no URL — a page can test null."""
    settled = _run_ai_image()
    assert settled["ok"] is True, settled
    assert settled["value"]["url"] == "/api/fs/raw?path=/t/a.png"
    assert settled["value"]["previewUrl"] is None


def test_a_render_with_no_preview_hands_the_page_NULL_rather_than_a_dead_url():
    """A model whose latent space has no fitted projection writes no frames, and
    a URL pointing at a file that will never exist is worse than nothing: a page
    can test `previewUrl` but cannot test an `<img>` that 404s."""
    settled = _run_ai_image(ticks="[%s]" % (RUNNING % 1), preview="undefined")
    assert settled["ok"] is True, settled
    assert settled["progress"][0]["previewUrl"] is None
    assert settled["value"]["previewUrl"] is None


def test_the_bridge_rejects_an_unrecognised_image_option_before_the_POST():
    """The bug report itself, at the bridge layer: an option this API does
    not have must not reach `aiPost` at all. `image` is now a real option
    (SPEC AI-9f) — `strength` is the one Decision 2 keeps out on purpose,
    since the edit mechanism never uses it — so it is what still stands for
    "the caller learns about the drop instead of getting a picture back"."""
    settled = _run_ai_image(opts='{prompt: "a fox", strength: 0.6}')
    assert settled["ok"] is False
    assert settled["type"] == "bad_request"
    assert "strength" in settled["message"]


def test_the_bridge_names_BOTH_unknown_image_options():
    settled = _run_ai_image(
        opts='{prompt: "a fox", strength: 0.6, bogus: 1}')
    assert settled["ok"] is False and settled["type"] == "bad_request"
    assert "bogus" in settled["message"] and "strength" in settled["message"]


def test_onProgress_is_exempt_from_the_image_unknown_key_check():
    """`onProgress` is a callback consumed above the whitelist loop, not a body
    field — it must stay accepted or every existing caller that passes one
    breaks."""
    settled = _run_ai_image(opts='{prompt: "a fox", onProgress: () => {}}')
    assert settled["ok"] is True, settled


def test_the_bridge_checks_the_envelope_BEFORE_the_prompt_field():
    """Ordering, matching the server: a call with an unknown option AND no
    `prompt` must learn about the option, not about the missing prompt —
    "add a prompt" would "fix" the error and land the caller right back in
    the silent-drop illusion this change exists to end."""
    settled = _run_ai_image(opts='{strength: 0.6}')
    assert settled["ok"] is False and settled["type"] == "bad_request"
    assert "'strength' is not an option" in settled["message"]
    # The field error's specific text never appears — asserting the bare
    # word "prompt" would pass by accident, since it is in the accepted-set
    # listing too.
    assert "must be a non-empty string" not in settled["message"]


def test_the_bridge_names_unknown_image_options_SORTED():
    """The server's `_reject_unknown` sorts; the bridge must match, or the
    same two-key mistake reads in a different order depending on how the
    caller happened to write the object literal — the two layers' messages
    stop being comparable."""
    settled = _run_ai_image(
        opts='{prompt: "a fox", strength: 0.6, bogus: 1}')
    assert settled["ok"] is False and settled["type"] == "bad_request"
    assert "'bogus', 'strength'" in settled["message"]


def test_the_bridge_refuses_a_caller_supplied_base_as_an_unknown_option_TOO():
    """`aiImage` gained the identical asymmetry `aiTranscribe` already has the
    moment `image` became an option: `base` is injected from the page's own
    `?path=`, never accepted from the caller's own options object, so a
    caller passing it directly is passing an option that does not exist from
    the page's point of view even though the server accepts it once the
    bridge adds it."""
    settled = _run_ai_image(
        opts='{prompt: "a fox", base: "/pages/other.html"}')
    assert settled["ok"] is False and settled["type"] == "bad_request"
    assert "base" in settled["message"]


def test_the_bridge_rejects_an_image_that_is_NOT_A_STRING():
    """Decision 4, at the bridge — reachable only when a caller managed to
    hand the bridge a non-string despite JS having no static typing to stop
    it. The definitive check is the server's; this pins that the bridge does
    not itself mangle an array into something that looks like a plain
    string by the time it reaches `aiPost`."""
    settled = _run_ai_image(opts='{prompt: "a fox", image: ["a.png", "b.png"]}')
    # The bridge's own whitelist loop forwards `image` verbatim — Decision 4's
    # rejection is the SERVER's job, not restated here as a second copy of the
    # rule; this call reaches the (stubbed) POST rather than failing early.
    assert settled["ok"] is True, settled


def _run_ai_transcribe_opts_only(opts):
    return _run_ai_transcribe('Promise.resolve(JSON.stringify({text: "hi", segments: []}))',
                              '{state: "done"}', opts=opts)


def test_the_bridge_rejects_a_caller_supplied_unknown_transcribe_option():
    settled = _run_ai_transcribe_opts_only('{path: "a.m4a", image: "photo.png"}')
    assert settled["ok"] is False and settled["type"] == "bad_request"
    assert "image" in settled["message"]


def test_the_bridge_refuses_a_caller_supplied_base_as_an_unknown_option():
    """`base` is bridge-injected from the page's own `?path=` — a CALLER
    passing it directly is passing an option that does not exist from the
    page's point of view, even though the server accepts it once the bridge
    adds it itself."""
    settled = _run_ai_transcribe_opts_only('{path: "a.m4a", base: "/pages/other.html"}')
    assert settled["ok"] is False and settled["type"] == "bad_request"
    assert "base" in settled["message"]


def test_onProgress_and_onSegment_are_exempt_from_the_transcribe_unknown_key_check():
    settled = _run_ai_transcribe_opts_only(
        '{path: "a.m4a", onProgress: () => {}, onSegment: () => {}}')
    assert settled["ok"] is True, settled


def test_the_bridge_checks_the_transcribe_envelope_BEFORE_the_path_field():
    """Same ordering fix as `aiImage`: an unknown option and a missing
    `path` must report the option, not the missing field."""
    settled = _run_ai_transcribe_opts_only('{image: "photo.png"}')
    assert settled["ok"] is False and settled["type"] == "bad_request"
    assert "'image' is not an option" in settled["message"]
    assert "must be a non-empty string" not in settled["message"]


def test_the_bridges_accepted_image_keys_match_the_servers_constant():
    """The drift guard: the bridge's whitelist and the server's accepted set
    are the same fact written in two languages, which is exactly how they
    come to disagree. Extract the JS array literal and compare it, sorted,
    to the Python constant driving `_reject_unknown` on the server."""
    from fused_render.server.routers import ai_runtime

    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "fused_render", "static", "runtime.js"),
                  encoding="utf-8").read()
    start = source.index("  function aiImage(opts)")
    body = source[start:source.index("\n  }\n", start)]
    match = re.search(r'const imageKeys = \[(.*?)\];', body)
    assert match, "could not find aiImage's whitelist array in runtime.js"
    js_keys = sorted(re.findall(r'"([^"]+)"', match.group(1)))
    assert js_keys == sorted(ai_runtime._IMAGE_OPTIONS)
    # Same asymmetry as transcribe's own drift guard (D413): `base` must NOT
    # be in the caller-facing set the bridge validates against, and must be
    # in the wider server set — collapsing the two would silently stop
    # enforcing that a caller cannot pass `base` itself.
    assert "base" not in ai_runtime._IMAGE_OPTIONS
    assert "base" in ai_runtime._IMAGE_SERVER_OPTIONS


def _run_ai_video(record='{state: "done"}', ticks="[]",
                  opts='{prompt: "a fox running", onProgress: (job) => progress.push(job)}'):
    """`_run_ai_image`'s harness for `aiVideo` — no preview, since this build
    has none."""
    import shutil
    import subprocess

    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own video glue")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fused_render", "static", "runtime.js")
    source = open(path, encoding="utf-8").read()
    fn = _js_fn_with_helper(source, "  function aiVideo(opts)")

    prelude = """
      const started = {jobId: "sys:ai-video:x", path: "/t/a.mp4", seed: 7,
                       frames: 90, steps: 20};
      const window = {location: {search: "?path=/pages/p.html"}};
      const aiPost = () => Promise.resolve(started);
      const rawUrl = (p) => "/api/fs/raw?path=" + p;
      const stat = () => Promise.reject(new Error("no stat"));
      const rows = TICKS;
      const watchJob = () => ({
        watch: (cb) => {
          for (const row of rows) if (cb) cb(row);
          return Promise.resolve(RECORD);
        },
        get: () => Promise.resolve(RECORD),
        stop() {}, cancel: () => Promise.resolve(true),
      });
      const progress = [];
    """.replace("TICKS", ticks).replace("RECORD", record)
    call = """
      aiVideo(OPTS).then(
        (value) => console.log(JSON.stringify({ok: true, value, progress, rows})),
        (err) => console.log(JSON.stringify(
          {ok: false, message: err.message, type: err.type, progress, rows})),
      );
    """.replace("OPTS", opts)
    out = subprocess.run(["node", "-e", prelude + fn + call],
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_resolved_video_carries_the_url():
    settled = _run_ai_video()
    assert settled["ok"] is True, settled
    assert settled["value"]["url"] == "/api/fs/raw?path=/t/a.mp4"
    assert "previewUrl" not in settled["value"]


def test_a_video_tick_carries_no_preview_field(state="running"):
    """No live preview in this build — the tick is the record, copied, and
    nothing more."""
    settled = _run_ai_video(ticks="[%s]" % (RUNNING % 1))
    assert settled["ok"] is True, settled
    assert "previewUrl" not in settled["progress"][0]
    assert settled["progress"][0]["done"] == 1


def test_the_video_tick_a_page_sees_is_a_COPY_of_the_row(state="running"):
    settled = _run_ai_video(ticks="[%s]" % (RUNNING % 1))
    assert settled["rows"] == [{"state": "running", "done": 1, "total": 4}]
    assert settled["progress"][0]["done"] == 1


def test_the_bridge_rejects_an_unrecognised_video_option_before_the_POST():
    settled = _run_ai_video(opts='{prompt: "a fox", strength: 0.6}')
    assert settled["ok"] is False
    assert settled["type"] == "bad_request"
    assert "strength" in settled["message"]


def test_guidance_is_rejected_by_the_video_bridge_too():
    """The video engine takes no such parameter — the bridge's whitelist must
    agree with the
    server's, or a caller gets a 400 from the network instead of a same-tick
    rejection."""
    settled = _run_ai_video(opts='{prompt: "a fox", guidance: 4.0}')
    assert settled["ok"] is False and settled["type"] == "bad_request"
    assert "guidance" in settled["message"]


def test_onProgress_is_exempt_from_the_video_unknown_key_check():
    settled = _run_ai_video(opts='{prompt: "a fox", onProgress: () => {}}')
    assert settled["ok"] is True, settled


def test_the_video_bridge_checks_the_envelope_BEFORE_the_prompt_field():
    settled = _run_ai_video(opts='{bogus: "x"}')
    assert settled["ok"] is False and settled["type"] == "bad_request"
    assert "'bogus' is not an option" in settled["message"]
    assert "must be a non-empty string" not in settled["message"]


def test_the_bridges_accepted_video_keys_match_the_servers_constant():
    """The drift guard, exactly like the image one above: the bridge's
    whitelist and the server's accepted set are the same fact in two
    languages."""
    from fused_render.server.routers import ai_runtime

    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "fused_render", "static", "runtime.js"),
                  encoding="utf-8").read()
    start = source.index("  function aiVideo(opts)")
    body = source[start:source.index("\n  }\n", start)]
    match = re.search(r'const videoKeys = \[(.*?)\];', body)
    assert match, "could not find aiVideo's whitelist array in runtime.js"
    js_keys = sorted(re.findall(r'"([^"]+)"', match.group(1)))
    assert js_keys == sorted(ai_runtime._VIDEO_OPTIONS)
    # Same asymmetry as `_IMAGE_SERVER_OPTIONS` (D413): `base` is
    # bridge-injected, so it must NOT be in the caller-facing set the bridge
    # validates against, and must be in the wider server set.
    assert "base" not in ai_runtime._VIDEO_OPTIONS
    assert "base" in ai_runtime._VIDEO_SERVER_OPTIONS


def test_the_bridges_accepted_transcribe_keys_match_the_servers_CALLER_FACING_constant():
    """Same drift guard for transcribe — compared against the CALLER-FACING
    set, which must NOT include `base`: the server's set is wider because
    the bridge injects `base` itself, and the two sets must not collapse into
    one or a caller passing `base` stops being an error."""
    from fused_render.server.routers import ai_runtime

    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "fused_render", "static", "runtime.js"),
                  encoding="utf-8").read()
    start = source.index("  function aiTranscribe(opts)")
    body = source[start:source.index("\n  }\n", start)]
    match = re.search(r'const transcribeKeys = \[(.*?)\];', body, re.S)
    assert match, "could not find aiTranscribe's whitelist array in runtime.js"
    js_keys = sorted(re.findall(r'"([^"]+)"', match.group(1)))
    assert js_keys == sorted(ai_runtime._TRANSCRIBE_OPTIONS)
    assert "base" not in ai_runtime._TRANSCRIBE_OPTIONS
    assert "base" in ai_runtime._TRANSCRIBE_SERVER_OPTIONS


@pytest.mark.parametrize("opts", [
    '{path: "a.m4a", diarize: true, speakers: 0}',
    '{path: "a.m4a", diarize: true, speakers: -2}',
    '{path: "a.m4a", diarize: true, speakers: true}',
    '{path: "a.m4a", diarize: true, speakers: 2.5}',
    '{path: "a.m4a", diarize: true, speakers: "2"}',
    '{path: "a.m4a", diarize: true, speakers: NaN}',
    '{path: "a.m4a", diarize: true, speakers: 101}',
])
def test_diarizing_with_an_UNUSABLE_speaker_count_rejects_BEFORE_a_job_opens(opts):
    """The bridge's half of the count check, beside the `path` check and for
    the same reason: the caller fails synchronously with an actionable sentence
    instead of watching a row open and die.

    Every value here is a count that was MEANT — omitting it is the estimating
    path (D318) and is tested below, but a wrong number is a typo and reading
    it as "estimate" would hide it.

    `speakers: true` is in the list deliberately — `{diarize: true, speakers:
    true}` is a plausible copy-paste, and a language where `true` is not a
    number is the only thing between it and a transcript labelled entirely
    "Speaker 1". So is `"2"`: a count read out of an <input> without a parseInt
    is the common way this argument arrives wrong.
    """
    settled = _run_ai_transcribe('Promise.resolve("{}")', '{state: "done"}', opts=opts)
    assert settled["ok"] is False, settled
    assert settled["type"] == "bad_request", settled
    assert "speakers" in settled["message"]


def test_the_bridges_speaker_bound_is_the_SAME_NUMBER_python_enforces():
    """Four copies of one rule — `runtime.js`, the endpoint, and each worker —
    and this is the copy no other test can reach: JS cannot import
    `diarize.MAX_SPEAKERS`, so the bound is restated in the bridge and nothing
    but this comparison stops the two drifting.

    Read out of the SOURCE and then driven through the real function, because
    either half alone is weak: a number that matches but is never applied, or a
    rejection at some bound that is not Python's. `diarize.py:speakers_or_raise`
    and D309 both claim the four enforcement points are identical, and this is
    what makes that claim true rather than aspirational.
    """
    from fused_render.ai.runners import diarize

    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "fused_render", "static", "runtime.js"),
                  encoding="utf-8").read()
    transcribe = source[source.index("  function aiTranscribe(opts)"):]
    transcribe = transcribe[:transcribe.index("\n  }\n")]
    assert f"const MAX_SPEAKERS = {diarize.MAX_SPEAKERS};" in transcribe

    # …and the boundary is inclusive on both sides, in both languages.
    assert diarize.speakers_or_raise(diarize.MAX_SPEAKERS) == diarize.MAX_SPEAKERS
    at_the_bound = _run_ai_transcribe(
        'Promise.resolve(JSON.stringify({text: "x", segments: []}))', '{state: "done"}',
        opts='{path: "a.m4a", diarize: true, speakers: %d}' % diarize.MAX_SPEAKERS)
    assert at_the_bound["ok"] is True, at_the_bound
    over = _run_ai_transcribe(
        'Promise.resolve("{}")', '{state: "done"}',
        opts='{path: "a.m4a", diarize: true, speakers: %d}' % (diarize.MAX_SPEAKERS + 1))
    assert over["ok"] is False and over["type"] == "bad_request"


def test_asking_for_speakers_properly_gets_the_labels_and_the_legend_back():
    """The success path with `diarize`, through the real function: the reply
    carries the transcript's legend so a page can build a colour map without
    walking thousands of segments, and each segment carries its own label."""
    written = ('Promise.resolve(JSON.stringify({text: "hello hi", '
               'segments: [{start: 0, end: 1, text: "hello", speaker: "Speaker 1"}, '
               '{start: 1, end: 2, text: "hi", speaker: "Speaker 2"}], '
               'speakers: ["Speaker 1", "Speaker 2"], language: "en", duration: 2}))')
    settled = _run_ai_transcribe(written, '{state: "done"}',
                                 opts='{path: "a.m4a", diarize: true, speakers: 2}')
    assert settled["ok"] is True, settled
    assert settled["value"]["speakers"] == ["Speaker 1", "Speaker 2"]
    assert settled["value"]["segments"][1]["speaker"] == "Speaker 2"


@pytest.mark.parametrize("opts", [
    '{path: "a.m4a", diarize: true}',
    '{path: "a.m4a", diarize: true, speakers: null}',
    '{path: "a.m4a", diarize: true, speakers: ""}',
])
def test_diarizing_WITHOUT_a_count_is_the_estimating_path_not_a_rejection(opts):
    """D318. All three spellings of "I did not say" reach the server, and the
    reply carries `estimatedSpeakers` — the count the clustering settled on,
    which a caller who passed one would already know and does not get."""
    written = ('Promise.resolve(JSON.stringify({text: "hello hi", '
               'segments: [{start: 0, end: 1, text: "hello", speaker: "Speaker 1"}], '
               'speakers: ["Speaker 1"], estimatedSpeakers: 2, '
               'language: "en", duration: 2}))')
    settled = _run_ai_transcribe(written, '{state: "done"}', opts=opts)
    assert settled["ok"] is True, settled
    assert settled["value"]["estimatedSpeakers"] == 2
    assert settled["value"]["speakers"] == ["Speaker 1"]


def test_a_run_that_was_GIVEN_the_count_reports_no_estimate():
    """The field means "estimated", not "resolved": a caller who supplied the
    number gets no key back, and the transcript on disk has none either."""
    written = ('Promise.resolve(JSON.stringify({text: "hi", '
               'segments: [{start: 0, end: 1, text: "hi", speaker: "Speaker 1"}], '
               'speakers: ["Speaker 1"], language: "en", duration: 1}))')
    settled = _run_ai_transcribe(written, '{state: "done"}',
                                 opts='{path: "a.m4a", diarize: true, speakers: 1}')
    assert settled["ok"] is True, settled
    assert "estimatedSpeakers" not in settled["value"]


def test_a_call_that_does_not_ask_for_speakers_is_unchanged():
    """`diarize` defaults false, so the count is not required and nothing about
    the reply gains a value — the whole feature is additive or it is a breaking
    change to every page already calling this."""
    good = ('Promise.resolve(JSON.stringify({text: "hello", '
            'segments: [{start: 0, end: 1, text: "hello"}], language: "en"}))')
    settled = _run_ai_transcribe(good, '{state: "done"}')
    assert settled["ok"] is True, settled
    assert "speakers" not in settled["value"]
    assert "speaker" not in settled["value"]["segments"][0]


def test_a_FAILED_transcription_still_says_where_the_salvage_is():
    """A run that dies at minute 80 of 90 writes no `.json` at all, and the
    `.partial.jsonl` the worker deliberately LEAVES behind is the only place
    those 80 minutes survive. The POST reply named that path — but the caller
    of `fused.ai.transcribe` never sees the reply, only the rejection, so
    without this the file is documented, written, and unreachable from the one
    situation it exists for."""
    settled = _run_ai_transcribe('Promise.reject(new Error("no file"))',
                                 '{state: "error", message: "the decoder exploded"}',
                                 extra="outputPartial: err.outputPartial")
    assert settled["ok"] is False and settled["type"] == "ai_error"
    assert settled["outputPartial"] == "/t/out.partial.jsonl"


def test_a_cancelled_row_rejects_as_cancelled_not_as_an_error():
    settled = _run_ai_transcribe('Promise.reject(new Error("no file"))',
                                 '{state: "cancelled"}')
    assert settled["ok"] is False and settled["type"] == "cancelled"
    assert "cancelled" in settled["message"]


# -- the progressive transcript, as a page sees it ---------------------------------
#
# `_run_ai_transcribe` above stubs `watch` into a single resolved promise, so
# it cannot see the poll loop at all. This second harness drives the real one:
# the file GROWS between ticks, exactly as a worker appending to it makes it,
# and `fetch` is a real ranged reader over those bytes. What is proved is the
# part a page must not have to write itself — that `onSegment` fires in order,
# once each, while the run is still going.


def _run_ai_transcribe_tailing(lines, final, opts='{path: "a.m4a"}',
                               partial_path='"/t/out.partial.jsonl"',
                               ranged=True, on_segment=True, slow_ms=0,
                               terminal='{state: "done"}', readfile=None,
                               fetch_after=None):
    """Drive `aiTranscribe` through a real poll loop over a growing file.

    `lines` is a list of JS strings, one per tick: the WHOLE partial file as it
    stands when that tick fires, so a caller writes the file's history rather
    than a diff. `final` is the transcript JSON `readFile` resolves with.
    `ranged=False` makes the stub server ignore `Range` and answer 200 with the
    whole file, which is the behaviour a proxy in front of `/api/fs/raw` could
    impose and which must not duplicate a single segment.

    `terminal` is the record `watch` finally resolves with — `null` for a row
    that aged out, an `error`/`cancelled` row for the failure paths — and
    `readfile` overrides what `readFile` does, so a failure can be driven all
    the way through with the poll loop and the tail both real. `fetch_after` is
    JS for a replacement `fetch` installed the moment `watch` returns, which is
    how a read attempted AFTER the run is over — the terminal drain — can be
    made to fail on its own without disturbing the tail that ran before it.

    Returns `{settled, segments, segmentsAtSettle, fetches}` — the outcome,
    every `onSegment` argument in the order it arrived, how many of them had
    arrived at the moment the promise settled, and every request the bridge
    made.
    """
    import shutil
    import subprocess

    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own transcription glue")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fused_render", "static", "runtime.js")
    source = open(path, encoding="utf-8").read()
    fn = _js_fn_with_helper(source, "  function aiTranscribe(opts)")

    prelude = """
      const started = {jobId: "sys:ai-transcribe:x", output: "/t/out.json",
                       outputText: "/t/out.txt", outputPartial: PARTIAL,
                       path: "/t/a.m4a", model: "m", task: "transcribe"};
      const window = {location: {search: "?path=/pages/p.html"}};
      const aiPost = () => Promise.resolve(started);
      const rawUrl = (p) => "/api/fs/raw?path=" + p;
      const callHeaders = (extra) => Object.assign({"X-Fused-Call": "1"}, extra);
      const readFile = () => READFILE;
      const RANGED = IS_RANGED;
      const HISTORY = LINES;
      const FETCH_AFTER = AFTER_FETCH;
      const fetches = [];
      let onDisk = "";
      globalThis.fetch = async (url, init) => {
        const headers = (init && init.headers) || {};
        fetches.push({url, range: headers.Range || null});
        // The bytes are taken WHEN THE REQUEST IS SERVED, before the latency
        // below, because that is what `/api/fs/raw` does: it opens the file
        // now and the delay is the answer travelling back. Snapshotting after
        // the sleep would hand every slow read the future contents of the
        // file, which quietly hides the bug this models — a segment appended
        // while a read is in flight is one that read CANNOT have seen.
        const body = Buffer.from(onDisk, "utf8");
        // A read still in flight when the row goes done, which is the ordinary
        // case on a real machine and the one where the drain could interleave
        // with the tail and deliver a segment twice.
        if (SLOW_MS) await new Promise((r) => setTimeout(r, SLOW_MS));
        const match = /bytes=(\\d+)-/.exec(headers.Range || "");
        const from = match ? Number(match[1]) : 0;
        if (!RANGED) {
          return {ok: true, status: 200,
                  arrayBuffer: async () => body.buffer.slice(
                    body.byteOffset, body.byteOffset + body.byteLength)};
        }
        // What /api/fs/raw really answers, verified against it: 206 with the
        // tail, 416 once the offset is at or past the end.
        if (from >= body.length) {
          return {ok: false, status: 416, arrayBuffer: async () => new ArrayBuffer(0)};
        }
        const slice = body.subarray(from);
        return {ok: true, status: 206,
                arrayBuffer: async () => slice.buffer.slice(
                  slice.byteOffset, slice.byteOffset + slice.byteLength)};
      };
      const watchJob = () => ({
        // The real loop's shape, down to the last callback: `watch` reports
        // EVERY record it sees and only then returns the terminal one (see
        // watchJob's `onUpdate(record)` above `if (record.state !== "running")`),
        // so the terminal tick starts a tail of its own. A row that aged out is
        // the one case with nothing to report — the record is null.
        watch: async (onUpdate) => {
          for (const state of HISTORY) {
            onDisk = state;
            if (typeof onUpdate === "function") onUpdate({state: "running", done: 1});
            await new Promise((r) => setTimeout(r, 5));
          }
          if (TERMINAL && typeof onUpdate === "function") onUpdate(TERMINAL);
          // Swapped in AFTER the terminal tick has started whatever tail it
          // was going to, so only reads belonging to the settled run see it.
          if (FETCH_AFTER) globalThis.fetch = FETCH_AFTER;
          return TERMINAL;
        },
        get: () => Promise.resolve(TERMINAL),
        stop() {}, cancel: () => Promise.resolve(true),
      });
      const heard = [];
    """
    prelude = (prelude.replace("PARTIAL", partial_path)
               .replace("SLOW_MS", str(int(slow_ms)))
               .replace("IS_RANGED", "true" if ranged else "false")
               .replace("LINES", json.dumps(lines))
               .replace("TERMINAL", terminal)
               .replace("AFTER_FETCH", fetch_after or "null")
               .replace("READFILE",
                        readfile or "Promise.resolve(%s)" % json.dumps(json.dumps(final))))
    # The report is taken AFTER a settle window, not at resolution. A read
    # still in flight when the promise resolves would otherwise deliver its
    # segments into `heard` after the snapshot was printed — so a bridge that
    # let the drain race the tail would look clean here, which is the one
    # failure this harness exists to see.
    call = """
      const report = (payload) => {
        // Counted the instant the promise settles, and printed after the
        // window below: the gap between the two is a segment that arrived
        // AFTER the caller was already told the run was over.
        const atSettle = heard.length;
        setTimeout(() => console.log(JSON.stringify(
          Object.assign(payload, {segments: heard, segmentsAtSettle: atSettle,
                                  fetches}))), 120);
      };
      const options = Object.assign(OPTS, LISTENER);
      aiTranscribe(options).then(
        (value) => report({settled: {ok: true, value}}),
        (err) => report({settled: {ok: false, message: err.message, type: err.type,
                                   output: err.output,
                                   outputPartial: err.outputPartial}}),
      );
    """.replace("OPTS", opts).replace(
        "LISTENER",
        "{onSegment: (s) => heard.push(s)}" if on_segment
        # …but still an `onProgress`, so the poll loop ticks exactly as it does
        # for the caller under test. Without one, `watch(null)` would never
        # reach the branch that decides whether to tail.
        else "{onProgress: () => {}}")
    # Node writes UTF-8 to stdout regardless of platform; without an explicit
    # `encoding` here, Windows decodes with `locale.getpreferredencoding()`
    # (often cp1252), which mangles or crashes on the multibyte transcript
    # text some of these harnesses drive through (see e.g.
    # test_a_MULTIBYTE_transcript_does_not_split_a_character_or_lose_its_place).
    out = subprocess.run(["node", "-e", prelude + fn + call],
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _jsonl(*segments):
    """The partial file's contents after those segments, as the worker writes
    it: `json.dumps(..., ensure_ascii=False)` per line, newline-terminated."""
    return "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in segments)


def test_a_page_gets_each_segment_WHILE_the_file_is_still_decoding():
    """The deliverable. A page must not have to implement file tailing to get a
    streaming transcript — it passes `onSegment` and the bridge does it, off
    the poll it was already running for `onProgress`."""
    one = {"start": 0.0, "end": 1.5, "text": "hello"}
    two = {"start": 1.5, "end": 3.0, "text": "world"}
    run = _run_ai_transcribe_tailing(
        ["", _jsonl(one), _jsonl(one, two)],
        {"text": "hello world", "segments": [one, two], "language": "en"})

    assert run["settled"]["ok"] is True, run["settled"]
    assert run["segments"] == [one, two]


def test_a_segment_the_TAIL_already_delivered_is_not_delivered_again():
    """The final transcript is the completeness backstop — it is read anyway,
    and it is the only source that is guaranteed whole — so it drains whatever
    the tail did not reach. Which makes double delivery the hazard: a page
    appending a cue per callback would end with the transcript twice."""
    one = {"start": 0.0, "end": 1.0, "text": "one"}
    two = {"start": 1.0, "end": 2.0, "text": "two"}
    three = {"start": 2.0, "end": 3.0, "text": "three"}
    run = _run_ai_transcribe_tailing(
        # The last two land between the final tick and the row going done — the
        # ordinary case, since the worker removes the partial file the moment
        # the transcript lands.
        [_jsonl(one)],
        {"text": "one two three", "segments": [one, two, three]})

    assert run["segments"] == [one, two, three]


def test_a_tail_still_IN_FLIGHT_when_the_row_finishes_cannot_double_deliver():
    """The ordinary case on a real machine, not an edge: the last read is
    started by the last tick and the row goes `done` under it. If the final
    drain ran without settling that read, it would see `delivered` from before
    the read landed, resend those segments, and the tail would then deliver
    them again — the transcript twice, out of order in the middle."""
    one = {"start": 0.0, "end": 1.0, "text": "one"}
    two = {"start": 1.0, "end": 2.0, "text": "two"}
    run = _run_ai_transcribe_tailing(
        [_jsonl(one), _jsonl(one, two)], {"text": "one two", "segments": [one, two]},
        slow_ms=30)

    assert run["segments"] == [one, two]


def test_a_run_whose_partial_file_never_appears_still_delivers_EVERY_segment():
    """404 for the whole run — an older server that advertises no partial path,
    a transcripts directory on a filesystem that lost it, a worker too fast to
    ever be caught mid-run. `onSegment` is a promise about segments, not about
    the file they arrived through."""
    one = {"start": 0.0, "end": 1.0, "text": "one"}
    run = _run_ai_transcribe_tailing(
        ["", "", ""], {"text": "one", "segments": [one]},
        partial_path="undefined")

    assert run["segments"] == [one]
    # Nothing was tailed, because there was nothing to tail — not a request per
    # tick against a path the reply never named.
    assert [f for f in run["fetches"] if f["range"]] == []


def test_the_tail_asks_for_the_BYTES_IT_HAS_NOT_SEEN(  # noqa: N802
):
    """Byte offsets, not "read the file and skip the lines I know" — which on a
    90-minute transcript re-downloads a megabyte every 700ms for the length of
    the run. `/api/fs/raw` answers Range for local files (206 + content-range,
    416 past the end), which was verified against the route rather than assumed
    from the fact that it uses FileResponse.

    **The first segment is deliberately multibyte**, because that is the only
    place the bytes-not-characters rule is observable. Get it wrong downstream
    and the reader lands mid-line, fails to parse, and falls back to the final
    drain — the right transcript by the slow path, with nothing to see. Here it
    is a number, and a wrong one is a wrong number.
    """
    one = {"start": 0.0, "end": 1.0, "text": "café 日本語"}
    two = {"start": 1.0, "end": 2.0, "text": "two"}
    line = _jsonl(one)
    assert len(line.encode()) > len(line), "the first line must be multibyte"
    run = _run_ai_transcribe_tailing(
        [line, _jsonl(one, two)], {"text": "x", "segments": [one, two]})

    ranges = [f["range"] for f in run["fetches"] if f["range"]]
    assert ranges[0] == "bytes=0-"
    # Exactly the end of the first line, in BYTES — so a `Range` that reset to
    # zero, or one counting characters, is a different number here.
    assert ranges[1] == "bytes=%d-" % len(line.encode())
    assert ranges[1] != "bytes=%d-" % len(line)
    assert run["segments"] == [one, two]


def test_a_MULTIBYTE_transcript_does_not_split_a_character_or_lose_its_place():
    """Offsets are bytes and the text is written unescaped, so "café" is five
    bytes of four characters. A tail that advanced by string LENGTH drifts by
    one byte per accent and three per CJK character, then starts reading from
    inside the previous line — delivering it a second time, in pieces.

    The first line here is deliberately many bytes wider than it is characters:
    a one-accent line drifts by exactly one byte, which lands on the newline
    and accidentally still works. This one lands well inside the JSON.
    """
    one = {"start": 0.0, "end": 1.0, "text": "café ünïcøde 日本語 ✓"}
    two = {"start": 1.0, "end": 2.0, "text": "naïve"}
    assert len(_jsonl(one).encode()) - len(_jsonl(one)) > 10, "not multibyte enough"
    run = _run_ai_transcribe_tailing(
        [_jsonl(one), _jsonl(one, two)],
        {"text": "x", "segments": [one, two]})

    assert run["segments"] == [one, two]


def test_a_server_that_IGNORES_the_range_still_delivers_each_segment_once():
    """`/api/fs/raw` honours Range today, but a 200 carrying the whole file is
    a legal answer to a Range request and anything in front of the route could
    give one. Re-reading from zero must not re-deliver what was already sent."""
    one = {"start": 0.0, "end": 1.0, "text": "one"}
    two = {"start": 1.0, "end": 2.0, "text": "two"}
    run = _run_ai_transcribe_tailing(
        [_jsonl(one), _jsonl(one, two)], {"text": "one two", "segments": [one, two]},
        ranged=False)

    assert run["segments"] == [one, two]


def test_a_caller_with_NO_onSegment_makes_exactly_the_requests_it_always_did():
    """The additive promise, and the one a page cannot see: a bridge that
    tailed unconditionally would put a second request on every poll of every
    existing transcription, for a file nobody is reading."""
    one = {"start": 0.0, "end": 1.0, "text": "one"}
    final = {"text": "one", "segments": [one]}
    history = [_jsonl(one), _jsonl(one)]

    # An `onProgress`-only caller: the loop ticks twice, and the partial file
    # is sitting right there with a segment in it. Zero requests is the claim.
    quiet = _run_ai_transcribe_tailing(history, final, on_segment=False)
    assert quiet["settled"]["ok"] is True, quiet["settled"]
    assert quiet["fetches"] == []

    # …and with `onSegment`, one tail per tick and no more — no second loop.
    # Three ticks, not two: `watch` reports the terminal record as well, and
    # that last tail is what carries the segments written between the final
    # running tick and the row finishing.
    loud = _run_ai_transcribe_tailing(history, final)
    assert len([f for f in loud["fetches"] if f["range"]]) == 3


def test_the_bridge_tails_the_path_the_ROUTE_advertised(client,
                                                        fake_transcribe_runner,
                                                        recording):
    """The two halves meeting. The route's `outputPartial` is what the bridge
    reads, so a suffix changed on one side and not the other is caught here
    rather than as an empty transcript on a page."""
    reply = _post_transcribe(client, path=recording).json()
    _wait_job(reply["jobId"])

    one = {"start": 0.0, "end": 1.0, "text": "one"}
    run = _run_ai_transcribe_tailing(
        [_jsonl(one)], {"text": "one", "segments": [one]},
        partial_path=json.dumps(reply["outputPartial"]))
    assert run["segments"] == [one]
    assert run["fetches"][0]["url"].endswith(reply["outputPartial"])


def test_a_FAILED_run_delivers_its_last_segments_BEFORE_it_rejects():
    """`onSegment` must stop when the promise settles, and the failure path is
    the one that did not honour that.

    `watch` reports the terminal record too, so an `error` or `cancelled` row
    starts one last tail — and a tail from the tick before it can still be in
    flight anyway. The success path settles `tailChain` before it drains, but
    the failure path threw straight out of the `.then`, so those reads landed
    afterwards and called `onSegment` on a promise the caller had already seen
    reject. A page that clears its transcript pane on the rejection then gets a
    cue painted into the cleared pane, from a run it was told was over.

    Waiting is the right answer rather than suppressing: those segments are
    real — they decoded before the run died — and delivering them BEFORE the
    rejection is exactly the salvage this feature is for.

    And waiting is only HALF of it, which is why the segment here lands where
    it does. The read from the tick before is still in flight when `one` is
    appended, so it cannot have seen it, and the terminal tick's `tail()`
    declines to start another while one is in flight. Settling the chain and
    rejecting therefore delivered nothing at all: `err.outputPartial` pointed
    at a file holding a segment `onSegment` never got. The success path drains
    the finished `.json` for exactly this reason; the failure path has no
    `.json`, so it re-reads the partial file one last time instead.
    """
    one = {"start": 0.0, "end": 1.0, "text": "one"}
    run = _run_ai_transcribe_tailing(
        # The segment lands on the last running tick, and the read over it is
        # still in flight when the row goes `error` under it.
        ["", _jsonl(one)], {"text": "", "segments": []},
        terminal='{state: "error", message: "the decoder exploded"}',
        readfile='Promise.reject(new Error("no transcript"))',
        slow_ms=30)

    assert run["settled"]["ok"] is False, run["settled"]
    assert run["settled"]["type"] == "ai_error"
    assert run["segments"] == [one], run["segments"]
    # The whole assertion: nothing arrived after the caller was told it failed.
    assert run["segmentsAtSettle"] == len(run["segments"]), run


def test_a_CANCELLED_run_also_stops_calling_onSegment_once_it_rejects():
    """Same rule on the other terminal state. A page cancels a transcription
    to make it stop; a cue arriving after `cancel()` resolved is the one thing
    a Stop button must not do."""
    one = {"start": 0.0, "end": 1.0, "text": "one"}
    run = _run_ai_transcribe_tailing(
        ["", _jsonl(one)], {"text": "", "segments": []},
        terminal='{state: "cancelled"}',
        readfile='Promise.reject(new Error("no transcript"))',
        slow_ms=30)

    assert run["settled"]["ok"] is False and run["settled"]["type"] == "cancelled"
    # Delivered, not dropped: a cancel removes the partial file, but whatever
    # decoded before the ✕ landed is still the honest answer to what was heard,
    # and the same final read carries it on both terminal states.
    assert run["segments"] == [one], run["segments"]
    assert run["segmentsAtSettle"] == len(run["segments"]), run


def test_a_run_whose_ROW_AGED_OUT_and_whose_TRANSCRIPT_IS_GONE_still_drains():
    """The third terminal path, and it had the same hole.

    `watch` resolving null means the row finished and aged out under a sleeping
    tab, so the transcript is read as the real witness — and when THAT fails
    too, this is a failed run whose only artefact is the `.partial.jsonl` the
    rejection names. `done()`'s drain never ran (it is inside the `.then` that
    the read failure skipped), and its `.catch` rejected without one, so the
    segments in the file the caller is being pointed at were never delivered.
    """
    one = {"start": 0.0, "end": 1.0, "text": "one"}
    run = _run_ai_transcribe_tailing(
        # Same shape as the live-failure test: appended under an in-flight read
        # that cannot have seen it, and there is no terminal tick at all here
        # to start another (a null record is reported to nobody).
        ["", _jsonl(one)], {"text": "", "segments": []},
        terminal="null",
        readfile='Promise.reject(new Error("no transcript"))',
        slow_ms=30)

    assert run["settled"]["ok"] is False, run["settled"]
    assert "no longer being reported" in run["settled"]["message"]
    assert run["settled"]["outputPartial"] == "/t/out.partial.jsonl"
    assert run["segments"] == [one], run["segments"]
    assert run["segmentsAtSettle"] == len(run["segments"]), run


def test_a_FINAL_DRAIN_THAT_FAILS_does_not_replace_the_runs_own_error():
    """Best-effort, like every other read of this file. The rejection a caller
    switches on is the run's — "the decoder exploded", typed `ai_error` — and a
    partial file that has already been removed, or a fetch that throws, must not
    turn that into a network error with a different `type`."""
    run = _run_ai_transcribe_tailing(
        [""], {"text": "", "segments": []},
        terminal='{state: "error", message: "the decoder exploded"}',
        readfile='Promise.reject(new Error("no transcript"))',
        fetch_after='() => { throw new Error("the socket died"); }')

    assert run["settled"]["ok"] is False, run["settled"]
    assert run["settled"]["type"] == "ai_error"
    assert "decoder exploded" in run["settled"]["message"], run["settled"]


def test_a_failure_that_AGED_OUT_still_says_where_the_salvage_is():
    """The gap the live `error` path already closed, on the path a long run is
    most likely to take.

    A transcription long enough to fail at minute 80 is long enough for its row
    to be dropped after retention while the tab sleeps, and then `watch`
    resolves null and `done()` fails because there is no `.json` — a failed
    run. That fallback rejection carried no paths at all, so the caller could
    not name the `.partial.jsonl` the worker deliberately left behind, in
    precisely the case the file exists for. The paths are not guessed here:
    they are the ones the POST reply named, held in `started` since the run
    opened.
    """
    one = {"start": 0.0, "end": 1.0, "text": "one"}
    run = _run_ai_transcribe_tailing(
        [_jsonl(one)], {"text": "", "segments": []},
        terminal="null",
        readfile='Promise.reject(new Error("no transcript"))')

    assert run["settled"]["ok"] is False, run["settled"]
    assert run["settled"]["type"] == "ai_error"
    assert "no longer being reported" in run["settled"]["message"]
    assert run["settled"]["output"] == "/t/out.json"
    assert run["settled"]["outputPartial"] == "/t/out.partial.jsonl"


def test_both_artefact_bridges_survive_a_row_that_aged_out():
    """The page half, pinned as an INVARIANT rather than an instance.

    Both `fused.ai.image` and `fused.ai.transcribe` wait on a job row that a
    backgrounded tab can sleep straight past its retention — and when it is
    gone, the FILE is the other witness and the one that matters. A new
    minutes-long call that only trusted the row would fail on work that had in
    fact finished, which is the failure this pins.

    **This asserts on SOURCE TEXT and cannot catch a runtime regression — do
    not read a pass here as the fallback working.** There is no JS harness for
    either bridge (the image one has never had a test of any kind), so what is
    checked is that the branch is still WRITTEN, not that it behaves: a
    `!record` arm that called the wrong function, read the wrong field, or threw
    on its own would pass this test. It is a tripwire against the branch being
    deleted or a third artefact call being added without one, and that is all.
    A real test needs node driving `runtime.js` against a stub `fetch`, which is
    worth doing the next time either bridge is touched.
    """
    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "fused_render", "static", "runtime.js"),
                  encoding="utf-8").read()
    assert "ai.transcribe = aiTranscribe" in source
    transcribe = source[source.index("function aiTranscribe("):]
    transcribe = transcribe[:transcribe.index("\n  const aiModels")]
    # The page-relative half is the bridge's job (RH-1): the server can only
    # resolve "clip.m4a" if the page says where it is standing.
    assert "body.base = ownPath" in transcribe
    # Both bridges answer an absent row the SAME way, which is the point:
    # read the artefact. Transcribe briefly grew a retry loop here to out-wait
    # mid-run evictions, and that loop could hang forever; live server rows are
    # cap-exempt now (`test_live_SERVER_work_is_never_evicted_by_the_cap`), so
    # the branch is a backstop for a row that finished and aged out under a
    # sleeping tab, not a state machine. Kept assertion-shaped so the loop
    # cannot creep back in.
    assert "regrace" not in transcribe and "GRACE_TRIES" not in transcribe
    for call in ("function aiImage(", "function aiTranscribe("):
        start = source.index(call)
        end = source.index("\n  }\n", start)
        assert "if (!record)" in source[start:end], call


def test_a_transcription_job_id_is_reserved_and_sanitised():
    """`sys:` is what makes a row unwritable by a page (BG-4a), so the id is
    minted in the supervisor rather than assembled by the router."""
    job = supervisor.transcribe_job_id("ab/cd 12")
    assert job.startswith(jobs.SERVER_ID_PREFIX)
    assert job == supervisor.TRANSCRIBE_JOB_PREFIX + "abcd12"


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


def test_raw_is_refused_for_claude_rather_than_dropped(client):
    """The same rule as history, and it had to be written twice because the
    validation is shared and only the local branch honoured the flag: a `raw`
    continuation answered as a chat turn is plausible text that is silently not
    what was asked for."""
    response = client.post("/api/ai", json={
        "prompt": "The capital of France is",
        "raw": True,
    }, headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "local model" in response.json()["error"]["message"]


def test_raw_and_history_together_are_refused(client):
    """`raw` means no chat template, so there is nowhere to put prior turns."""
    response = client.post("/api/ai", json={
        "prompt": "hi", "model": "org/chat", "raw": True,
        "history": [{"role": "user", "content": "hello"}],
    }, headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "one or the other" in response.json()["error"]["message"]


def test_sampling_reaches_the_worker(client, fake_runner, monkeypatch):
    """Displayed, persisted, and — until now — never sent anywhere.

    The chat app has had Temperature and Max tokens sliders since before the
    rewrite. The server forwarded both to the worker all along; the runtime
    never serialized them, so two controls sat on screen changing nothing.
    """
    seen = {}
    real = supervisor.generate_text
    monkeypatch.setattr(supervisor, "generate_text",
                        lambda model, request: (seen.update(request), real(model, request))[1])
    supervisor.load("org/chat", registry.TEXT_GENERATION)
    _wait_ready("org/chat")

    client.post("/api/ai", json={
        "prompt": "hi", "model": "org/chat",
        "temperature": 0.2, "max_tokens": 64, "top_p": 0.9,
    }, headers={"X-Fused": "1"})

    assert seen["temperature"] == 0.2
    assert seen["max_tokens"] == 64
    assert seen["top_p"] == 0.9


@pytest.mark.parametrize("body,expected", [
    ({"max_tokens": 10_000_000}, "between"),
    ({"max_tokens": 0}, "between"),
    ({"temperature": 9}, "between"),
    ({"top_p": 2}, "between"),
    ({"temperature": "warm"}, "must be a number"),
    # True is an int in Python and would pass a bare range check as max_tokens=1.
    ({"max_tokens": True}, "must be a number"),
])
def test_a_sampling_value_out_of_range_is_refused(client, body, expected):
    """One resident model serves every page, so an unbounded token budget is not
    one caller's slow request — it is everybody else blocked behind it."""
    response = client.post("/api/ai", json={
        "prompt": "hi", "model": "org/chat", **body,
    }, headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert expected in response.json()["error"]["message"]


def test_sampling_is_refused_for_claude_rather_than_dropped(client):
    """The CLI has `effort`, not a temperature — so accepting one would be a
    knob the caller can watch have no effect."""
    response = client.post("/api/ai", json={
        "prompt": "hi", "temperature": 0.2,
    }, headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "'temperature'" in response.json()["error"]["message"]


def test_an_out_of_range_value_on_the_claude_path_still_says_unsupported(client):
    """WHICH sentence a bad value earns, and it is not the range one.

    Range-checking before the fork answered `temperature: 5.0` sent to Claude
    with "must be between 0.0 and 2.0" — an error inviting the caller to correct
    a number and retry, on a path where no number would ever work. The check
    lives inside the local branch so the true refusal is never pre-empted by a
    message that implies support.
    """
    response = client.post("/api/ai", json={
        "prompt": "hi", "temperature": 5.0,
    }, headers={"X-Fused": "1"})
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "only applies to a local model" in message
    assert "between" not in message


# -- images: a current-turn attachment for a local VLM (D467's shape reused) --


def test_images_reach_the_worker_alongside_messages(client, fake_runner, monkeypatch):
    """A list of absolute paths, threaded straight through to the worker's
    request — the worker is the one that knows how to turn them into
    placeholder tokens (`mlx_text/worker.py`'s image path, commit 3)."""
    from fused_render.server import ai as ai_mod

    sent = {}

    def capture(model, request):
        sent["request"] = request
        yield {"type": "chunk", "text": "a cat"}
        yield {"type": "done", "ok": True, "tokens": 1}

    monkeypatch.setattr(supervisor, "generate_text", capture)
    # `fake_runner` registers `code="fake-text"`, which `_images_unsupported_
    # by_runner` correctly refuses (only `mlx-text` reads `images` at all) —
    # that refusal is its OWN test below; this one is about the plumbing once
    # a request has cleared it, so the runner-support gate is bypassed here
    # rather than standing up a whole mlx-text-shaped fixture for a threading
    # test that does not care which runner it is.
    monkeypatch.setattr(ai_mod, "_images_unsupported_by_runner", lambda model: None)
    response = client.post("/api/ai", json={
        "prompt": "what is this?",
        "model": "org/chat",
        "images": ["/Users/x/photo.png"],
    }, headers={"X-Fused": "1"})
    assert response.status_code == 200, response.text
    assert sent["request"]["images"] == ["/Users/x/photo.png"]


def test_no_images_key_at_all_is_not_sent_to_the_worker(client, fake_runner, monkeypatch):
    """Absent means absent — the worker's own contract is "empty/absent is
    today's text path, unchanged", and a bare empty list sent on every call
    would be a needless departure from that for every model that never uses
    it."""
    sent = {}

    def capture(model, request):
        sent["request"] = request
        yield {"type": "chunk", "text": "ok"}
        yield {"type": "done", "ok": True, "tokens": 1}

    monkeypatch.setattr(supervisor, "generate_text", capture)
    client.post("/api/ai", json={"prompt": "hi", "model": "org/chat"},
               headers={"X-Fused": "1"})
    assert "images" not in sent["request"]


@pytest.mark.parametrize("images,expected", [
    ("not a list", "must be a list"),
    ([""], "must be a non-empty string"),
    ([123], "must be a non-empty string"),
    ([f"/img/{i}.png" for i in range(20)], "may not carry more than"),
])
def test_a_malformed_images_list_says_why(client, images, expected):
    response = client.post("/api/ai", json={
        "prompt": "hi", "model": "org/chat", "images": images,
    }, headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert expected in response.json()["error"]["message"]


def test_images_are_refused_for_claude_rather_than_dropped(client):
    """The same rule `history` and `raw` are refused for: silently dropping a
    picture would answer as if it had never been attached, which reads as the
    model ignoring what was sent rather than the API declining to send it."""
    response = client.post("/api/ai", json={
        "prompt": "what is this?",
        "images": ["/Users/x/photo.png"],
    }, headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "local model" in response.json()["error"]["message"]


def test_raw_and_images_together_are_refused(client):
    """`raw` means no chat template at all, and the image placeholder tokens
    `images` needs are inserted BY that template — silently ignoring `raw`
    once a picture is attached (`mlx_text/worker.py`'s image branch reads
    `messages` unconditionally and never looks at `prompt`) is exactly the
    silent-drop `history` is refused for instead of dropped, so this pairing
    gets the same refusal rather than a request that watches `raw` do
    nothing."""
    response = client.post("/api/ai", json={
        "prompt": "what is this?", "model": "org/chat", "raw": True,
        "images": ["/Users/x/photo.png"],
    }, headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "one or the other" in response.json()["error"]["message"]


def test_images_are_refused_when_the_resolved_runner_cannot_read_them(
        client, fake_runner, monkeypatch):
    """The shape check (`_images_problem`) says nothing about whether the
    request MEANS anything: `fake_runner` registers `code="fake-text"`, which
    `_accepts_image` correctly refuses (only `mlx-text` reads `images` at
    all — `llamacpp_text`'s shared `generate` drops the field on the floor),
    and a caller must be told that up front rather than get back a confident
    answer about a picture the model never saw."""
    response = client.post("/api/ai", json={
        "prompt": "what is this?", "model": "org/chat",
        "images": ["/Users/x/photo.png"],
    }, headers={"X-Fused": "1"})
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "fake-text" in message
    assert "org/chat" in message


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


def test_unload_rejects_an_unrecognised_capability_like_cancel_does(client):
    """The addendum: `unload` never validated `capability` — a typo went
    straight to `supervisor.unload()`, which answers `{"stopped": false}` for
    an unrecognised capability, indistinguishable from a correct request
    against an idle machine. `cancel`, 45 lines below in the same file,
    already refuses this; `unload` gets the same guard."""
    bad = client.post("/api/ai/runtime/unload", json={"capability": "telepathy"},
                      headers={"X-Fused": "1"})
    assert bad.status_code == 400


def test_unload_by_MODEL_ALONE_still_works_with_no_capability(client, fake_runner):
    """The guard must stay compatible with the `model`-only form: `capability`
    is validated only when it is not None."""
    supervisor.load("org/bye", registry.TEXT_GENERATION)
    _wait_ready("org/bye")
    response = client.post("/api/ai/runtime/unload", json={"model": "org/bye"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 200, response.json()
    assert response.json()["stopped"] is True


def test_unload_by_a_REAL_capability_still_works(client, fake_runner):
    supervisor.load("org/bye2", registry.TEXT_GENERATION)
    _wait_ready("org/bye2")
    response = client.post(
        "/api/ai/runtime/unload", json={"capability": registry.TEXT_GENERATION},
        headers={"X-Fused": "1"})
    assert response.status_code == 200, response.json()
    assert response.json()["stopped"] is True


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
    assert set(done["result"]) == {"text", "model", "usage", "provider"}


# -- which capability a load without one gets (D321) ---------------------------
# The trap this closes: an omitted `capability` used to mean TEXT GENERATION
# unconditionally, so `fused.ai.models.load("mlx-community/FLUX.2-Klein-4B-4bit")`
# went to mlx-lm and surfaced as a FileNotFoundError about a config.json the repo
# has never had. It fired twice — a whisper repo did the same thing through
# Preload — because a silent default turns "you omitted an argument" into a wrong
# runner that reports itself as a corrupt model.


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    """An empty hub cache, pointed at by HF_HUB_CACHE — the same fixture
    `test_ai_models_api.py` uses, because the load route now reads the very
    listing that page reads. Every other HF var is cleared so a developer
    machine's real cache cannot answer for the test's."""
    for var in ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / "hub"
    d.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(d))
    return d


def _cached_repo(hub, repo_id, *, files=(), dirs=(), config=None):
    """One cached repo with a `main` revision holding `files` and `dirs`."""
    repo = hub / ("models--" + repo_id.replace("/", "--"))
    snapshot = repo / "snapshots" / "c0ffee"
    snapshot.mkdir(parents=True)
    for name in files:
        (snapshot / name).write_bytes(b"")
    for name in dirs:
        (snapshot / name).mkdir()
    if config is not None:
        (snapshot / "config.json").write_text(json.dumps(config))
    refs = repo / "refs"
    refs.mkdir(parents=True)
    (refs / "main").write_text("c0ffee")
    return repo


@pytest.fixture()
def dispatched(monkeypatch):
    """Records what the route asked the supervisor to load, without loading.

    The question every test below asks is WHICH CAPABILITY the route resolved,
    and that is decided before any process exists — so the supervisor is a
    recorder here rather than a fake worker.
    """
    calls = []

    def fake_load(model, capability, *, weights_only=False):
        calls.append({"model": model, "capability": capability,
                      "weightsOnly": weights_only})
        return {"jobId": "job", "model": model, "state": "loading"}

    monkeypatch.setattr(supervisor, "load", fake_load)
    return calls


def _load(client, body):
    return client.post("/api/ai/runtime/load", json=body, headers={"X-Fused": "1"})


def test_a_load_without_a_capability_reads_the_cached_repos_format(
        client, hub, dispatched, monkeypatch):
    """The reported bug. An mflux conversion has no config.json at all, so the
    old default sent it to mlx-lm; its component folders say image generation
    beyond doubt.

    **Platform PINNED to Apple Silicon, and that is not decoration.** An mflux
    conversion is an Apple-Silicon artefact: off that platform `mflux-image` is
    the only runner that curates or reads it, so nothing can serve it and the
    route now refuses the request outright with the engine named
    (`catalog.engine_gap`). What this test is about is capability INFERENCE, so
    it pins the machine where the model is real rather than asserting inference
    through a servability refusal. Unpinned it also answered differently on a Mac
    dev box than on Linux CI, which it should never have done.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    repo_id = next(iter(formats.MFLUX_VARIANTS))
    _cached_repo(hub, repo_id, dirs=formats.MFLUX_COMPONENTS)
    assert _load(client, {"model": repo_id}).status_code == 200
    assert dispatched == [{"model": repo_id, "capability": registry.IMAGE_GENERATION,
                           "weightsOnly": False}]


def test_a_cached_text_repo_without_a_capability_still_loads_as_text(
        client, hub, dispatched):
    """The back-compat half, and the reason this is inference rather than a
    required argument: every page that calls `load(id)` for a chat model today
    keeps working."""
    _cached_repo(hub, "org/chat", files=("model.safetensors",),
                 config={"architectures": ["LlamaForCausalLM"]})
    assert _load(client, {"model": "org/chat"}).status_code == 200
    assert dispatched[0]["capability"] == registry.TEXT_GENERATION


def test_a_cached_repo_with_no_task_but_readable_weights_is_text(
        client, hub, dispatched):
    """A directory of safetensors says nothing about the modality — but the only
    runner that reads one is the TEXT runner, so its capability is the answer
    rather than a guess.

    `config.json` is part of "readable weights" and not decoration: `mlx_lm.load`
    resolves a checkpoint through it. See the test below for the repo that has
    the extensions and not the config."""
    _cached_repo(hub, "org/mystery", files=("model.safetensors",),
                 config={"model_type": "qwen3"})
    assert _load(client, {"model": "org/mystery"}).status_code == 200
    assert dispatched[0]["capability"] == registry.TEXT_GENERATION


def test_the_catalog_lists_what_it_cannot_run_with_the_reason(client, hub):
    """D441: everything downloaded appears somewhere, and the unrunnable half
    says why.

    These repos are in no `capabilities[]` list by construction — that is what
    a null capability means — so before this key they were absent from every
    picker, which reads as a download that failed rather than as an answer.
    """
    _cached_repo(hub, "Intel/dpt-beit-base-384", files=("model.safetensors",),
                 config={"architectures": ["DPTForDepthEstimation"], "model_type": "dpt"})
    _cached_repo(hub, "SymphonyGen/SymphonyGen", files=("stage_one.pt",))
    body = client.get("/api/ai/catalog").json()
    rows = {row["id"]: row for row in body["unsupported"]}
    assert set(rows) == {"Intel/dpt-beit-base-384", "SymphonyGen/SymphonyGen"}

    dpt = rows["Intel/dpt-beit-base-384"]
    assert dpt["label"] == "dpt-beit-base-384"
    assert dpt["task"] == "depth estimation"
    assert dpt["support"] == "no-runner"
    assert dpt["reason"] == "Nothing on this machine runs depth estimation models."

    # A repo nothing could identify: no task, no sentence. The card then says
    # only "on this disk, unrunnable", which is the whole of what is known.
    policy = rows["SymphonyGen/SymphonyGen"]
    assert policy["support"] == "unknown"
    assert policy["task"] is None and policy["reason"] == ""

    # …and never as a loadable row: every app maps `capabilities[].models` and
    # offers what it finds.
    listed = {m["id"] for row in body["capabilities"] for m in row["models"]}
    assert not listed & set(rows)


def test_an_unmapped_architecture_is_not_a_chat_model(client, hub, dispatched):
    """`Intel/dpt-beit-base-384`, and the case that proved the card is not
    enough.

    A card downloaded from the Hub often carries NO `pipeline_tag` — the one
    that repo's API row reports is inferred server-side from its tags, and its
    actual front matter is `license: mit` and nothing else. So the architecture
    is the whole of the local evidence, `…ForDepthEstimation` was a suffix
    nothing mapped, and "no task" then let the file extensions decide: config +
    safetensors is `mlx-text`, one capability, chat model, Load button.

    A declared architecture we cannot map is EVIDENCE, not silence — transformers
    names the head in that string, and mlx-lm imports `mlx_lm.models.<type>` for
    a causal LM. Both halves are fixed here: the suffix is mapped now (so the
    card reads "depth estimation"), and the fallback refuses a config that
    declares a head this build does not recognise, which is what keeps the next
    unlisted suffix out of the text section.
    """
    _cached_repo(hub, "Intel/dpt-beit-base-384", files=("model.safetensors",),
                 config={"architectures": ["DPTForDepthEstimation"], "model_type": "dpt"})
    reading = ai_models.cached_capability("Intel/dpt-beit-base-384")
    assert reading.capability is None
    assert reading.support == "no-runner" and reading.tag == "depth-estimation"
    assert _load(client, {"model": "Intel/dpt-beit-base-384"}).status_code == 400
    assert dispatched == []


def test_an_unrecognised_head_stays_unloadable_even_unmapped(client, hub, dispatched):
    """The structural half on its own, with a head no table will ever name.

    The suffix list is a snapshot of transformers' vocabulary and will go stale
    again; this is what stops the next gap from being a Load button rather than
    a missing label.
    """
    _cached_repo(hub, "org/novel", files=("model.safetensors",),
                 config={"architectures": ["SomethingEntirelyNewForWidgets"],
                         "model_type": "widget"})
    assert ai_models.cached_capability("org/novel").capability is None
    assert _load(client, {"model": "org/novel"}).status_code == 400
    assert dispatched == []


def test_a_ruled_out_task_is_not_rescued_by_readable_weights(client, hub, dispatched):
    """The TTS-under-TEXT bug, which is a DIFFERENT path from SymphonyGen below.

    A real speech-synthesis repo has everything the text branch wants — a
    `config.json` mlx-lm could resolve, a directory of safetensors — so
    `formats.loaders` answers `('mlx-text',)` and the config guard never fires.
    What stops it is the other gate: the card SAID what this is
    (`text-to-speech`), that task is one we have ruled out, and a task we
    recognise and do not serve is never overruled by what the weight files look
    like. Without it the loaders-unanimity fallback files this under text
    generation, which is how a Qwen3-TTS repo came to sit in the Playground's
    chat section with a Load button.
    """
    repo = _cached_repo(hub, "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                        files=("model.safetensors",), config={"model_type": "qwen3"})
    (repo / "snapshots" / "c0ffee" / "README.md").write_text(
        "---\npipeline_tag: text-to-speech\n---\n")
    reading = ai_models.cached_capability("Qwen/Qwen3-TTS-12Hz-1.7B-Base")
    assert reading.cached and reading.capability is None
    assert reading.support == "no-runner" and reading.reason
    assert _load(client, {"model": "Qwen/Qwen3-TTS-12Hz-1.7B-Base"}).status_code == 400
    assert dispatched == []


def test_weights_with_no_config_are_not_a_chat_model(client, hub, dispatched):
    """`SymphonyGen/SymphonyGen`'s shape: four bare `.pt` checkpoints of a
    symbolic-music policy, no `config.json`, no card task this app serves.

    mlx-lm cannot resolve a checkpoint without a config, so claiming the format
    was a promise the load could not keep — and because the claim was the ONLY
    evidence left by then, the repo was filed under text generation and drawn in
    the Playground's chat section."""
    _cached_repo(hub, "SymphonyGen/SymphonyGen",
                 files=("stage_one_pretrained.pt", "grpo_clamp_epoch_10.pt"))
    assert _load(client, {"model": "SymphonyGen/SymphonyGen"}).status_code == 400
    assert dispatched == []


def test_an_uncached_suggested_repo_takes_the_catalogs_capability(
        client, hub, dispatched):
    """Nothing on disk to read, and no network call added to this path: the
    catalog already knows what every repo this app RECOMMENDS is for, which is
    the whole of the whisper-Preload case."""
    assert _load(client, {"model": "Systran/faster-whisper-small"}).status_code == 200
    assert dispatched[0]["capability"] == registry.SPEECH_TO_TEXT


def test_an_uncached_unknown_repo_still_defaults_to_text_generation(
        client, hub, dispatched):
    """The one case inference cannot answer without bytes. It keeps the old
    default rather than refusing a cold load, and the runner's own format check
    is what names the mismatch if the guess was wrong."""
    assert _load(client, {"model": "org/never-seen"}).status_code == 200
    assert dispatched[0]["capability"] == registry.TEXT_GENERATION


def test_a_cached_repo_no_engine_reads_is_refused_by_name(client, hub, dispatched):
    """Not a FileNotFoundError from inside a library, and not a bare "unknown
    capability": the repo, what it looks like, and what to pass.

    A `.ckpt`-only repo, not a `.gguf` one: since SPEC AI-11 a root-level GGUF
    IS decisively loadable, by `llamacpp-text` — see
    `test_a_cached_gguf_repo_now_loads_as_text_via_llamacpp` below. `.ckpt` is
    a raw pickle checkpoint no format check here recognises (it is not in
    `formats.TORCH_WEIGHTS`), so it remains a case nothing reads.
    """
    _cached_repo(hub, "org/ckpt-only", files=("model.ckpt", "README.md"))
    response = _load(client, {"model": "org/ckpt-only"})
    assert response.status_code == 400
    message = response.json()["error"]
    assert "org/ckpt-only" in message
    assert "capability" in message
    assert registry.IMAGE_GENERATION in message and registry.SPEECH_TO_TEXT in message
    assert dispatched == []


def _gguf_bytes(architecture: str) -> bytes:
    """A minimal, real GGUF header declaring `general.architecture` — enough
    for `formats.gguf_architecture` to read, which is what `loaders()` now
    requires before calling a root `.gguf` decisively `llamacpp-text`
    (code review finding 4: presence of the extension alone is not enough,
    since GGUF is a container format shared with image and speech models)."""
    import struct

    pairs = [("general.architecture", architecture)]
    buf = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + \
        struct.pack("<Q", len(pairs))
    for key, value in pairs:
        buf += struct.pack("<Q", len(key.encode())) + key.encode()
        buf += struct.pack("<I", 8)  # GGUF string type
        buf += struct.pack("<Q", len(value.encode())) + value.encode()
    return buf


def test_a_cached_gguf_repo_now_loads_as_text_via_llamacpp(client, hub, dispatched):
    """The other half of the story the test above used to tell alone: since
    SPEC AI-11 a root-level `.gguf` whose OWN `general.architecture` is a
    recognised text one IS decisively `llamacpp-text`'s (`formats.DECISIVE`),
    so `cached_capability`'s `meta.loaders` fallback resolves it to text
    generation with no task label needed — the same mechanism
    `test_a_cached_repo_with_no_task_but_readable_weights_is_text` exercises
    for a bare directory of safetensors."""
    repo_dir = _cached_repo(hub, "org/gguf-only", files=("model.gguf", "README.md"))
    (repo_dir / "snapshots" / "c0ffee" / "model.gguf").write_bytes(_gguf_bytes("qwen35"))
    assert _load(client, {"model": "org/gguf-only"}).status_code == 200
    assert dispatched[0]["capability"] == registry.TEXT_GENERATION


def test_a_cached_gguf_repo_with_a_non_text_architecture_is_still_refused(
        client, hub, dispatched):
    """`city96/FLUX.1-dev-gguf`'s own shape, verified 2026-08-21
    (`general.architecture = "flux"`) — a root `.gguf` alone must not be
    enough, or this app would offer a Load button for an image model under
    the text-generation capability (code review finding 4)."""
    repo_dir = _cached_repo(hub, "org/flux-gguf", files=("model.gguf",))
    (repo_dir / "snapshots" / "c0ffee" / "model.gguf").write_bytes(_gguf_bytes("flux"))
    response = _load(client, {"model": "org/flux-gguf"})
    assert response.status_code == 400
    assert dispatched == []


def test_an_explicit_capability_still_wins_over_the_format(
        client, hub, dispatched, monkeypatch):
    """Inference governs the OMITTED case only. A caller who names a capability
    gets it, right or wrong — that is what makes this additive.

    Note the ORDER this pins: the explicit capability wins over the FORMAT, not
    over servability. A model no engine here can serve is refused whatever
    capability is named for it, because that refusal is about the model rather
    than about the dispatch — see the platform note below.

    **Platform PINNED to Apple Silicon, and that is not decoration.** An mflux
    conversion is an Apple-Silicon artefact: off that platform `mflux-image` is
    the only runner that curates or reads it, so nothing can serve it and the
    route now refuses the request outright with the engine named
    (`catalog.engine_gap`). What this test is about is capability INFERENCE, so
    it pins the machine where the model is real rather than asserting inference
    through a servability refusal. Unpinned it also answered differently on a Mac
    dev box than on Linux CI, which it should never have done.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    repo_id = next(iter(formats.MFLUX_VARIANTS))
    _cached_repo(hub, repo_id, dirs=formats.MFLUX_COMPONENTS)
    assert _load(client, {"model": repo_id,
                          "capability": registry.TEXT_GENERATION}).status_code == 200
    assert dispatched[0]["capability"] == registry.TEXT_GENERATION


def test_download_infers_the_capability_the_same_way(client, hub, dispatched,
                                                     monkeypatch):
    """`/download` takes the same default through the same helper, so it had the
    same bug: a Download on the AI Models page fetched an image model into the
    text runner's idea of what to fetch.

    **Platform PINNED to Apple Silicon, and that is not decoration.** An mflux
    conversion is an Apple-Silicon artefact: off that platform `mflux-image` is
    the only runner that curates or reads it, so nothing can serve it and the
    route now refuses the request outright with the engine named
    (`catalog.engine_gap`). What this test is about is capability INFERENCE, so
    it pins the machine where the model is real rather than asserting inference
    through a servability refusal. Unpinned it also answered differently on a Mac
    dev box than on Linux CI, which it should never have done.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    repo_id = next(iter(formats.MFLUX_VARIANTS))
    _cached_repo(hub, repo_id, dirs=formats.MFLUX_COMPONENTS)
    response = client.post("/api/ai/runtime/download", json={"model": repo_id},
                           headers={"X-Fused": "1"})
    assert response.status_code == 200
    assert dispatched == [{"model": repo_id, "capability": registry.IMAGE_GENERATION,
                           "weightsOnly": True}]


def test_download_refuses_an_unreadable_cached_repo_too(client, hub, dispatched):
    """`.ckpt`-only, not `.gguf` — see the docstring on
    `test_a_cached_repo_no_engine_reads_is_refused_by_name` for why a GGUF is
    no longer this test's example (SPEC AI-11)."""
    _cached_repo(hub, "org/ckpt-only", files=("model.ckpt",))
    response = client.post("/api/ai/runtime/download", json={"model": "org/ckpt-only"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "org/ckpt-only" in response.json()["error"]
    assert dispatched == []


# -- a downloaded model joins the catalog's picker (D323) -----------------------
# The reported bug: a user finds a model on the Discover tab's Hub search, presses
# Download, the bytes land in the hub cache — and the model then appears in NO
# page's picker, because every page reads `fused.ai.models.catalog()` and that was
# the curation and nothing else. The cache scan the AI Models page already does is
# now joined onto the catalog, so a downloaded repo shows up beside the suggested
# ones with the same `{id, label, size_gb, note}` keys the apps read.
#
# The rule the union must not break is `catalog.py`'s: SMALLEST FIRST, AND THE
# DEFAULT FOLLOWS POSITION 0, over the CURATED list only. `default_for()` is what
# a bare `fused.ai.image()` loads, so an arbitrary repo off the disk reaching
# position 0 would make a no-model call load unvetted weights.


def _catalog(client):
    response = client.get("/api/ai/catalog")
    assert response.status_code == 200
    return {row["capability"]: row for row in response.json()["capabilities"]}


def _entry(client, capability, repo_id):
    """The one entry for `repo_id` under `capability`, or None if it is not offered."""
    return next((m for m in _catalog(client)[capability]["models"] if m["id"] == repo_id), None)


def _offered(client, capability, repo_id):
    """…and the same, for a test whose point is what the entry SAYS rather than
    whether it exists — so a missing entry fails on the row that looked for it."""
    entry = _entry(client, capability, repo_id)
    assert entry is not None, f"{repo_id} is not offered under {capability}"
    return entry


@pytest.fixture()
def safetensors_text_engine(monkeypatch):
    """Pin the platform to Apple Silicon, so the engine SERVING text generation is
    one that reads `_text_repo`'s safetensors.

    Required by every test below that builds a `_text_repo`, and it is not
    boilerplate: the cached-repo union only offers a repo the ACTIVE engine can
    load, so which engine is active decides whether these fixtures are visible at
    all. That used to be true on every platform by accident — `transformers-text`
    read safetensors and served Windows and Linux — and D416 removed it, leaving
    `mlx-text` as the only safetensors text engine and `llamacpp-text` (GGUF) as
    what a non-Apple machine resolves to. Without this fixture these tests assert
    the union mechanism on a developer's Mac and assert nothing at all in CI.

    Pinning rather than parametrising over both platforms on purpose: the subject
    here is the UNION — measured sizes, ordering, the memo, the downloaded flag —
    which has no per-engine behaviour. The engine-dependent half is its own test
    (`test_a_text_model_the_CHOSEN_engine_cannot_open_is_not_offered`).
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")


def _text_repo(hub, repo_id, *, size=0):
    """A cached repo whose config says text generation beyond doubt, sized.

    Safetensors, so a test using this needs the `safetensors_text_engine`
    fixture — see its docstring for why the ambient platform is not enough.

    The weights file is sparse: `size` here exists to be read back as a
    `size_gb`, and the scan that reads it sums `st_size`. See `_big_files`.
    """
    repo = _cached_repo(hub, repo_id, files=("model.safetensors",),
                        config={"architectures": ["LlamaForCausalLM"]})
    sparse_file(repo / "snapshots" / "c0ffee" / "model.safetensors", size)
    return repo


def test_a_downloaded_repo_the_curation_never_heard_of_joins_its_capability(client, hub, safetensors_text_engine):
    """The bug, end to end: a repo the SUGGESTIONS dict has never contained is on
    the disk, and the payload three real apps read now offers it."""
    repo_id = "some-org/a-model-nobody-curated"
    # The fixture defends itself: the whole point is a repo the curation does not
    # know, so a future SUGGESTIONS edit that happened to add this id must fail
    # here rather than turn the test into a tautology about a curated entry.
    assert repo_id not in catalog.all_suggested_ids()
    _text_repo(hub, repo_id, size=2048)
    entry = _offered(client, registry.TEXT_GENERATION, repo_id)
    assert entry["downloaded"] is True
    assert entry["source"] == "cached"
    # Every key the three apps read, present and the right type on a cached entry
    # too — they render `m.label || m.id` and `m.size_gb`, so a missing label or a
    # string where a number belongs is a broken picker, not a cosmetic slip.
    assert isinstance(entry["label"], str) and entry["label"]
    assert isinstance(entry["size_gb"], float)
    assert entry["note"] is None


def test_a_cached_entrys_size_is_its_real_measured_footprint(client, hub, safetensors_text_engine):
    """Measured, not guessed: the field means "every byte on the disk", the same
    thing it means for a curated entry (see catalog.py's docstring)."""
    _text_repo(hub, "some-org/three-gb", size=3_000_000_000)
    entry = _offered(client, registry.TEXT_GENERATION, "some-org/three-gb")
    assert entry["size_gb"] == 3.0


def test_an_uncurated_repo_on_disk_cannot_take_position_0_or_the_default(client, hub, safetensors_text_engine):
    """The regression guard for catalog.py's ordering rule.

    A NEARLY EMPTY repo is the dangerous case, not a huge one: the list is sorted
    smallest first and position 0 is the default, so a naive merge would promote a
    3KB folder off the disk to "what a bare `fused.ai()` call loads".
    """
    before_default = catalog.default_for(registry.TEXT_GENERATION)
    before_curated = [m["id"] for m in catalog.for_capability(registry.TEXT_GENERATION)]
    assert before_default and before_curated
    _text_repo(hub, "some-org/tiny-but-uncurated", size=3_000)
    _text_repo(hub, "some-org/enormous-and-uncurated", size=9_000_000)
    row = _catalog(client)[registry.TEXT_GENERATION]
    # The three answers a bare call goes through, all untouched.
    assert row["default"] == before_default
    assert catalog.default_for(registry.TEXT_GENERATION) == before_default
    assert [m["id"] for m in catalog.for_capability(registry.TEXT_GENERATION)] == before_curated
    # …and the payload's own ordering: every curated entry, in its curated order,
    # before anything that came off the disk.
    ids = [m["id"] for m in row["models"]]
    assert ids[:len(before_curated)] == before_curated
    assert set(ids[len(before_curated):]) == {
        "some-org/tiny-but-uncurated", "some-org/enormous-and-uncurated"}


def test_the_cached_tail_is_smallest_first_with_unknown_sizes_last(client, hub, safetensors_text_engine):
    """catalog.py's ordering rule, applied to the tail as well as the head."""
    _text_repo(hub, "some-org/big", size=9_000_000)
    _text_repo(hub, "some-org/small", size=2_000)
    curated = len(catalog.for_capability(registry.TEXT_GENERATION))
    ids = [m["id"] for m in _catalog(client)[registry.TEXT_GENERATION]["models"]]
    assert ids[curated:] == ["some-org/small", "some-org/big"]


def test_an_unmeasured_cached_entry_sorts_last_rather_than_first():
    """The defensive half of the same rule, on the sort key itself: nothing
    measurable means "nobody knows how big this is", and catalog.py is explicit that
    an unknown download must never be promoted into the smallest slot. Not reachable
    from a real scan today — a cache repo always has at least its `refs/main` on
    disk — which is exactly why it is pinned here rather than left to be noticed
    once something makes it reachable."""
    models = [CachedModel("c", registry.TEXT_GENERATION, 0),
              CachedModel("b", registry.TEXT_GENERATION, 5_000),
              CachedModel("a", registry.TEXT_GENERATION, 1_000)]
    ordered = sorted(models, key=ai_runtime._cached_order)
    assert [m.repo_id for m in ordered] == ["a", "b", "c"]


def test_a_curated_repo_that_is_also_on_disk_appears_once_marked_downloaded(client, hub, safetensors_text_engine):
    """Deduplicated, and it is the CURATED entry that survives — the hand-written
    label and note are the point of curating it."""
    repo_id = catalog.default_for(registry.TEXT_GENERATION)
    curated = next(m for m in catalog.for_capability(registry.TEXT_GENERATION)
                   if m["id"] == repo_id)
    _text_repo(hub, repo_id, size=2048)
    matches = [m for m in _catalog(client)[registry.TEXT_GENERATION]["models"]
               if m["id"] == repo_id]
    assert len(matches) == 1
    assert matches[0]["downloaded"] is True
    assert matches[0]["source"] == "curated"
    assert matches[0]["label"] == curated["label"]
    assert matches[0]["note"] == curated["note"]
    assert matches[0]["size_gb"] == curated["size_gb"]


def test_a_curated_repo_that_is_not_on_disk_says_so(client, hub):
    """The other side of the flag, and what makes it worth a field: an empty cache
    means every curated entry reads `downloaded: false` rather than nothing."""
    row = _catalog(client)[registry.TEXT_GENERATION]
    assert row["models"]
    assert all(m["downloaded"] is False for m in row["models"])
    assert all(m["source"] == "curated" for m in row["models"])


def test_a_llamacpp_curated_id_is_marked_downloaded_and_not_duplicated(
        client, hub, monkeypatch):
    """The regression code review found (finding 3): `formats.GGUF_RECIPES`
    keys `llamacpp-text`'s catalog entries by FILENAME, and
    `_catalog_with_downloads`'s `on_disk` set holds REPO ids — so
    `entry["id"] in on_disk` could never be true for one of these entries,
    and the SAME downloaded bytes then reappeared a second time as an
    undifferentiated "cached" row under the bare repo id, whose Load button
    failed (finding 2). Both symptoms must be gone together: the curated
    entry reads `downloaded: true`, and the repo does not also appear as a
    plain cached row.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    _prefer(monkeypatch, registry.TEXT_GENERATION, "llamacpp-text")

    entry_id = "gemma-4-E4B-it-Q4_K_M.gguf"
    recipe = formats.GGUF_RECIPES[entry_id]
    repo = _cached_repo(hub, recipe["repo"], files=(recipe["file"],))
    (repo / "snapshots" / "c0ffee" / recipe["file"]).write_bytes(
        _gguf_bytes("gemma4"))

    row = _catalog(client)[registry.TEXT_GENERATION]
    curated_match = next(m for m in row["models"] if m["id"] == entry_id)
    assert curated_match["downloaded"] is True
    assert curated_match["source"] == "curated"
    # The repo id itself must NOT also appear as a second, "cached" row.
    assert not any(m["id"] == recipe["repo"] for m in row["models"])
    # And the translation that made both answers possible is ON THE WIRE, since
    # the Local tab has the same duplicate to avoid and cannot redo it: its
    # "already have a card for this" map is keyed by repo id, so without this
    # field the entry kept a Download button beside its own finished disk card.
    assert curated_match["repo"] == recipe["repo"]


def test_every_catalog_entry_carries_the_repo_that_addresses_it(client, hub, monkeypatch):
    """`repo` is on EVERY entry, not just the filename-keyed half.

    A consumer that had to know which half it was holding before reading the
    field would be back to making the distinction this field exists to remove —
    so a repo-keyed curated entry and a cached one both answer with their own
    id, and only a `GGUF_RECIPES` entry answers with something different.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    _prefer(monkeypatch, registry.TEXT_GENERATION, "llamacpp-text")

    # One cached repo the curation has never heard of, so the "cached" tail is
    # populated too.
    _cached_repo(hub, "somebody/found-on-disk", files=("config.json",))

    rows = _catalog(client)
    seen_filename_keyed = False
    for row in rows.values():
        for entry in row["models"]:
            recipe = formats.GGUF_RECIPES.get(entry["id"])
            expected = recipe["repo"] if recipe else entry["id"]
            assert entry["repo"] == expected, entry["id"]
            seen_filename_keyed = seen_filename_keyed or recipe is not None
    # Guards the assertion above against going vacuous if the llama.cpp
    # shortlist ever stops being filename-keyed.
    assert seen_filename_keyed


def test_a_llamacpp_curated_id_with_a_sibling_quant_not_downloaded_is_told_apart(
        client, hub, monkeypatch):
    """Two curated entries sharing ONE repo: downloading one must not mark the
    OTHER "downloaded" too, since `CachedModel.files` is checked per FILE, not
    per repo.

    The pair is SYNTHESISED — appended to the shortlist and the recipe table
    for the duration of this test — rather than taken from the shipped
    curation. It used to be the real Qwen 4B's Q5_K_M and Q8_0 rows; the
    2026-08-21 refresh left exactly one quantization per repo, which would
    have turned this guard vacuous rather than red. The per-file rule has to
    hold for the day a repo carries two entries again, so the test supplies
    that day itself.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    _prefer(monkeypatch, registry.TEXT_GENERATION, "llamacpp-text")

    downloaded_id = "Model-Q4_K_M.gguf"
    other_id = "Model-Q8_0.gguf"
    recipes = dict(formats.GGUF_RECIPES)
    for name in (downloaded_id, other_id):
        recipes[name] = {"repo": "org/Model-GGUF", "file": name}
    monkeypatch.setattr(formats, "GGUF_RECIPES", recipes)
    monkeypatch.setitem(
        catalog.SUGGESTIONS, "llamacpp-text",
        catalog.SUGGESTIONS["llamacpp-text"] + [
            {"id": downloaded_id, "label": "Model (Q4_K_M)", "size_gb": 1.0,
             "note": "synthetic"},
            {"id": other_id, "label": "Model (Q8_0)", "size_gb": 2.0,
             "note": "synthetic"},
        ])

    recipe = recipes[downloaded_id]
    repo = _cached_repo(hub, recipe["repo"], files=(recipe["file"],))
    (repo / "snapshots" / "c0ffee" / recipe["file"]).write_bytes(
        _gguf_bytes("qwen35"))

    row = _catalog(client)[registry.TEXT_GENERATION]
    by_id = {m["id"]: m for m in row["models"]}
    assert by_id[downloaded_id]["downloaded"] is True
    assert by_id[other_id]["downloaded"] is False
    # Still no duplicate cached row for the shared repo.
    assert not any(m["id"] == recipe["repo"] for m in row["models"])


def test_a_component_repo_an_engine_fetched_is_never_offered_as_a_model(client, hub):
    """The Local tab files these under "Fetched by engines" and disables their
    Load; a picker must not offer one either. `formats.COMPONENT_REPOS` is the one
    list of them, asked rather than restated."""
    repo_id = next(iter(formats.COMPONENT_REPOS))
    _text_repo(hub, repo_id, size=2048)
    rows = _catalog(client)
    assert not any(m["id"] == repo_id for row in rows.values() for m in row["models"])


def test_a_repo_no_engine_can_read_joins_no_capability(client, hub):
    """A GGUF-only folder has no inferable capability, and inventing one for it is
    how `load()` came to send a diffusion repo to mlx-lm (D321)."""
    _cached_repo(hub, "some-org/gguf-only", files=("model.gguf", "README.md"))
    rows = _catalog(client)
    assert not any(m["id"] == "some-org/gguf-only"
                   for row in rows.values() for m in row["models"])


def test_a_dataset_in_the_cache_joins_no_capability(client, hub):
    """Nothing loads a dataset into a runner, whatever its config.json says."""
    repo = hub / "datasets--some-org--corpus"
    snapshot = repo / "snapshots" / "c0ffee"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text(json.dumps({"architectures": ["LlamaForCausalLM"]}))
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text("c0ffee")
    rows = _catalog(client)
    assert not any(m["id"] == "some-org/corpus" for row in rows.values() for m in row["models"])


def test_a_model_downloaded_after_a_read_appears_on_the_very_next_one(client, hub, safetensors_text_engine):
    """The cache-staleness trap, pinned. The scan is memoised because a page polls
    this route, and a memo that outlived a completed download would reproduce
    exactly the bug this change fixes — the model the user just fetched missing
    from the picker."""
    repo_id = "some-org/arrived-late"
    assert _entry(client, registry.TEXT_GENERATION, repo_id) is None
    _text_repo(hub, repo_id, size=2048)
    assert _entry(client, registry.TEXT_GENERATION, repo_id) is not None


def test_a_resident_model_is_marked_loaded(client, hub, monkeypatch, safetensors_text_engine):
    """The third state a picker wants: on the disk is not the same as held in
    memory, and a page showing "loaded" beside one entry should not have to join
    two endpoints to know which.

    Read LIVE from the supervisor rather than through the memoised scan — residency
    changes on a second's notice and the disk inventory does not, so a stale
    "loaded" would be a worse lie than a stale size.
    """
    repo_id = "some-org/resident"
    _text_repo(hub, repo_id, size=2048)
    assert _offered(client, registry.TEXT_GENERATION, repo_id)["loaded"] is False
    monkeypatch.setattr(supervisor, "resident_models", lambda: {repo_id})
    assert _offered(client, registry.TEXT_GENERATION, repo_id)["loaded"] is True


def test_resident_models_reports_a_held_worker_without_probing_it(client, fake_runner):
    """The other half of that flag, against a real worker. `resident_models()` is a
    dict read under the lock — no health request per worker, which is what makes it
    affordable on a route a page polls, and what `describe()` cannot offer."""
    assert supervisor.resident_models() == set()
    client.post("/api/ai/runtime/load",
                json={"model": "org/chat", "capability": registry.TEXT_GENERATION},
                headers={"X-Fused": "1"})
    _wait_ready("org/chat")
    assert supervisor.resident_models() == {"org/chat"}


def test_the_cache_walk_is_not_repeated_on_every_poll(client, hub, monkeypatch):
    """`cached_models()` is a full tree walk on a route the chatbot page hits on
    every `refreshRuntime()`, so it is memoised — pinned here because a memo that
    nothing measures is a memo somebody deletes as an over-optimisation."""
    _text_repo(hub, "some-org/polled", size=2048)
    walks = []
    real = ai_models._scan_repo
    monkeypatch.setattr(ai_models, "_scan_repo",
                        lambda root: walks.append(root) or real(root))
    _catalog(client)
    after_first = len(walks)
    assert after_first > 0
    _catalog(client)
    assert len(walks) == after_first


def test_the_memo_is_dropped_when_a_repo_lands_rather_than_when_a_timer_says_so(
        client, hub, monkeypatch, safetensors_text_engine):
    """The TTL is a BACKSTOP, not the mechanism. A read is invalidated by the cache
    directory's own signature, so a finished download is visible immediately even
    with the clock frozen — a memo that could only expire on time would hide the
    model the user just fetched, which is the bug this whole change fixes.

    `ai_models._now`, never `ai_models.time.time`: `ai_models.time` IS the stdlib
    module, so patching an attribute on it freezes the clock for the whole PROCESS —
    and `jobs.py` stamps five fields off `time.time()`, the supervisor two more, from
    daemon threads that keep running right through this test.
    """
    frozen = time.time()
    monkeypatch.setattr(ai_models, "_now", lambda: frozen)
    assert _entry(client, registry.TEXT_GENERATION, "some-org/just-landed") is None
    _text_repo(hub, "some-org/just-landed", size=2048)
    assert _entry(client, registry.TEXT_GENERATION, "some-org/just-landed") is not None


# -- and only models the SERVING engine can load ---------------------------------
# The catalog's lists are per RUNNER, not per capability (AI-11a), because one
# capability's backends read mutually unloadable formats. A cached repo injected on
# its capability alone breaks that invariant inside the same array — and both
# examples below are repos a real user really has on a real disk.


#: The two speech resolutions that ship, forced for the same reason as
#: `_TEXT_PLATFORMS`: `mlx-whisper` serves an M-series Mac and `faster-whisper`
#: serves everywhere else, and they read completely different files.
_SPEECH_PLATFORMS = [
    pytest.param("Darwin", "arm64", "mlx-whisper", id="apple-silicon"),
    pytest.param("Linux", "x86_64", "faster-whisper", id="linux"),
]


@pytest.mark.parametrize("system, machine, expected_runner", _SPEECH_PLATFORMS)
def test_a_speech_model_NEITHER_speech_engine_reads_is_not_offered(
        client, hub, monkeypatch, system, machine, expected_runner):
    """`openai/whisper-large-v3` — the repo everyone reaches for, in transformers
    format, which no shipping speech runner opens. Its capability is beyond doubt
    (`pipeline_tag: automatic-speech-recognition`), so a capability-only union puts
    it straight into every page's speech picker and the load is then refused by
    name. This is the exact trap the SKILL.md pitfall warns page authors about; the
    payload must not be the thing that sets it.

    Run under BOTH speech resolutions, because "neither engine reads it" is a claim
    about two different engines and inheriting the dev machine's answer would only
    ever test one of them.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: system)
    monkeypatch.setattr(registry.platform, "machine", lambda: machine)
    repo_id = "openai/whisper-large-v3"
    assert repo_id not in catalog.all_suggested_ids()
    repo = _cached_repo(hub, repo_id, files=("model.safetensors",),
                        config={"architectures": ["WhisperForConditionalGeneration"]})
    (repo / "snapshots" / "c0ffee" / "README.md").write_text(
        "---\npipeline_tag: automatic-speech-recognition\n---\n")
    # The fixture defends itself twice over: if the capability stopped being
    # inferred, or if the serving engine ever learned to read this format, the
    # conclusion below would pass for a reason that is not the one under test.
    cached = next(m for m in ai_models.cached_models() if m.repo_id == repo_id)
    assert cached.capability == registry.SPEECH_TO_TEXT
    serving = catalog._runner_for(registry.SPEECH_TO_TEXT)
    assert serving is not None and serving.code == expected_runner
    assert serving.code not in cached.loaders
    assert _entry(client, registry.SPEECH_TO_TEXT, repo_id) is None


def test_a_text_model_the_CHOSEN_engine_cannot_open_is_not_offered(
        client, hub, monkeypatch):
    """The second half, and the one a preference can create out of nothing. An MLX
    conversion is a perfectly good text model that the llama.cpp runner cannot
    read — so on a Mac switched to llama.cpp on the Engines tab it is an unusable
    download, and D293's whole point is that the list moves when the preference
    does. It must move for the cached half too.

    The engine switched TO was `transformers-text` until D416; `llamacpp-text` is
    the same shape of counter-example and a sharper one — safetensors against
    GGUF is a difference of container, not merely of quantization layout."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    repo_id = "mlx-community/Qwen3-8B-MLX-4bit"
    assert repo_id not in catalog.all_suggested_ids()
    # A REAL MLX conversion: the `quantization` block with a `group_size` is what
    # `formats.py` reads as "mlx-lm packed this", and it is what makes the repo
    # unreadable to anything else rather than merely differently named.
    _cached_repo(hub, repo_id, files=("model.safetensors",),
                 config={"architectures": ["Qwen3ForCausalLM"],
                         "quantization": {"bits": 4, "group_size": 64}})
    assert next(m for m in ai_models.cached_models()
                if m.repo_id == repo_id).loaders == ("mlx-text",)
    # With MLX serving text generation this repo IS loadable, and is offered…
    assert registry.for_capability(registry.TEXT_GENERATION).code == "mlx-text"
    assert _entry(client, registry.TEXT_GENERATION, repo_id) is not None
    # …and the moment the user picks the engine that cannot read it, it is not.
    _prefer(monkeypatch, registry.TEXT_GENERATION, "llamacpp-text")
    ai_models._CACHED_MODELS.clear()
    assert registry.for_capability(registry.TEXT_GENERATION).code == "llamacpp-text"
    assert _entry(client, registry.TEXT_GENERATION, repo_id) is None


def test_a_repo_no_runner_reads_at_all_is_not_offered_either(client, hub):
    """The empty-`loaders` case, distinct from the wrong-engine one: a GGUF folder
    with a pipeline_tag has a capability and nothing that opens it."""
    repo = _cached_repo(hub, "some-org/gguf-with-a-tag", files=("model.gguf",))
    (repo / "snapshots" / "c0ffee" / "README.md").write_text(
        "---\npipeline_tag: text-generation\n---\n")
    cached = next(m for m in ai_models.cached_models()
                  if m.repo_id == "some-org/gguf-with-a-tag")
    assert cached.loaders == ()
    assert _entry(client, registry.TEXT_GENERATION, "some-org/gguf-with-a-tag") is None


# -- `loaded` means loaded ---------------------------------------------------------


def _park(model, state, alive=True):
    """A worker in `_workers` at `state`, with a process that is or is not running.

    The table is reached directly because the question is about a state the
    supervisor passes THROUGH: a real bring-up spends its `venv` phase in a
    multi-minute `uv sync`, which is not something a test can hold open.
    """
    worker = supervisor.Worker(model=model, capability=registry.TEXT_GENERATION,
                              runner_code="fake-text", state=state)
    worker.proc = types.SimpleNamespace(poll=lambda: None if alive else 1)
    with supervisor._lock:
        supervisor._workers[registry.TEXT_GENERATION] = worker
    return worker


@pytest.mark.parametrize("state", ["starting", "venv", "downloading", "loading", "error"])
def test_a_worker_that_is_not_READY_is_not_reported_as_loaded(state):
    """A Worker enters the table at `starting` and passes through a multi-minute
    `uv sync` and a multi-GB fetch before any weights exist. Reporting every row
    would light a picker's "loaded" mark the instant Load was pressed and hold it
    lit through the whole download — the opposite of what the mark promises."""
    _park("org/slow", state)
    assert supervisor.resident_models() == set()
    _park("org/slow", "ready")
    assert supervisor.resident_models() == {"org/slow"}
    supervisor.reset()


def test_a_worker_that_DIED_after_reaching_ready_is_not_reported_as_loaded():
    """`state` alone is a claim the worker made before it crashed. `refresh_memory`
    reaps on exactly this check, and skipping its health PROBE is not a licence to
    skip the liveness too — otherwise this one path reports a dead model as held
    forever."""
    _park("org/crashed", "ready", alive=False)
    assert supervisor.resident_models() == set()
    supervisor.reset()


# -- what holds when a runner has no curated list at all --------------------------


#: The two text-generation resolutions that ship, forced rather than inherited.
#: `_apple_silicon()` reads `platform` at CALL time precisely so a test can decide
#: this, and every assertion about a per-runner list has to: `mlx-text` serves text
#: generation on an M-series Mac and `llamacpp-text` serves it everywhere else,
#: their `SUGGESTIONS` lists are completely different (safetensors repo ids against
#: GGUF filenames), and a test that took whichever one the dev machine happened to
#: answer is a test that passes at home and fails in CI on the other platforms.
_TEXT_PLATFORMS = [
    pytest.param("Darwin", "arm64", "mlx-text", id="apple-silicon"),
    pytest.param("Linux", "x86_64", "llamacpp-text", id="linux"),
]


@pytest.mark.parametrize("system, machine, expected_runner", _TEXT_PLATFORMS)
def test_a_runner_with_no_suggestions_offers_the_disk_and_recommends_nothing(
        client, hub, monkeypatch, system, machine, expected_runner):
    """The one case a cached entry reaches index 0, pinned rather than left to be
    discovered. A runner with no `SUGGESTIONS` key has nothing curated to put in
    front of the disk — and `default` is then None, which is the honest answer
    rather than a promotion.

    This is why `source` is on every entry and why the documented contract is "read
    `default`, never `models[0]`": a consumer that invents a `models[0]` fallback can
    see that what it found is uncurated and refuse it.

    **The no-suggestions condition is CONSTRUCTED, and the construction is asserted.**
    The first version of this test emptied `SUGGESTIONS["mlx-text"]` by name, which
    is the runner a Mac resolves — so on Linux it emptied a list nobody was reading,
    the cross-platform row answered with its own position 0, and the test
    failed on its conclusion for a premise that was never true. Both resolutions now
    run, the list emptied is the one the row actually resolved, and the premise is
    checked before the conclusion so a future change to resolution fails loudly on
    the setup instead of mysteriously on the assertion.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: system)
    monkeypatch.setattr(registry.platform, "machine", lambda: machine)
    runner = catalog._runner_for(registry.TEXT_GENERATION)
    assert runner is not None and runner.code == expected_runner
    monkeypatch.setitem(catalog.SUGGESTIONS, runner.code, [])
    # The premise, before anything is concluded from it.
    assert catalog.for_capability(registry.TEXT_GENERATION) == []
    # …and the cached repo has to be in a format THIS engine reads, which since
    # D416 is a different format per platform: safetensors for `mlx-text`, a
    # root-level GGUF for `llamacpp-text`. Building one shape for both would have
    # made the Linux case assert "an unreadable repo is not offered" while
    # claiming to assert the opposite — the failure `safetensors_text_engine`'s
    # docstring describes, arriving here through the parametrisation instead.
    if expected_runner == "llamacpp-text":
        gguf = _cached_repo(hub, "some-org/only-thing-here", files=("model.gguf",))
        (gguf / "snapshots" / "c0ffee" / "model.gguf").write_bytes(_gguf_bytes("qwen35"))
    else:
        _text_repo(hub, "some-org/only-thing-here", size=2048)
    row = _catalog(client)[registry.TEXT_GENERATION]
    assert row["runner"] == expected_runner
    assert row["default"] is None
    assert [m["id"] for m in row["models"]] == ["some-org/only-thing-here"]
    assert row["models"][0]["source"] == "cached"


def test_a_cached_entry_never_leads_a_list_that_has_a_curated_one(client, hub, safetensors_text_engine):
    """The invariant that does hold unconditionally, stated as itself: wherever a
    curated entry exists AND the capability is servable here, index 0 is curated
    — so `models[0]` and `default` agree and a bare call cannot reach an unvetted
    repo. Video generation is the one capability that can be curated-but-
    unavailable on the machine running this test (its one engine is MLX, so
    a non-Apple-Silicon runner sees nothing) — `default` is None there by design
    (`catalog.describe`), which is a different claim from this one and is
    covered on the registry side instead."""
    _text_repo(hub, "some-org/aaa-alphabetically-first", size=1)
    for row in _catalog(client).values():
        if not row["available"]:
            continue
        if any(m["source"] == "curated" for m in row["models"]):
            assert row["models"][0]["source"] == "curated"
            assert row["models"][0]["id"] == row["default"]


def test_the_catalog_marks_the_curated_subset_and_never_a_cached_repo(
        client, hub, safetensors_text_engine):
    """`recommended` on the wire: a bool on every entry, True only where the
    curation put it, False on a repo the user found themselves (D425).

    The absent-vs-False distinction is the point of asserting the KEY rather
    than its truth — a consumer filtering on it (the Playground sidebar) reads a
    missing key as "not recommended", so the route must never let absence stand
    in for an answer.
    """
    _text_repo(hub, "some-org/mine-alone", size=1)
    rows = _catalog(client)
    for row in rows.values():
        for entry in row["models"]:
            assert isinstance(entry["recommended"], bool), entry["id"]
            if entry["source"] == "cached":
                assert entry["recommended"] is False, entry["id"]
    text = rows[registry.TEXT_GENERATION]
    assert any(m["recommended"] for m in text["models"]), \
        "text generation recommends nothing, so the Playground has nothing to offer"
    assert _offered(client, registry.TEXT_GENERATION,
                    "some-org/mine-alone")["recommended"] is False


def test_the_recommended_flag_does_not_move_the_default_or_the_order(client):
    """The two axes stay separate, end to end — asserted as a RELATIONSHIP and
    never against a model name.

    `default` is position 0 and position 0 is the smallest entry, whatever is
    marked (catalog.py's module docstring on why there is no `default: True`
    field). Naming the ids here would make re-curating a shortlist an edit to
    this file, which is the wrong thing to make expensive: the curation is data,
    and what a test may own is the mechanism it feeds.
    """
    for row in _catalog(client).values():
        curated = [m for m in row["models"] if m["source"] == "curated"]
        if not curated:
            continue
        # The head is still the default, and it is still the head whether or not
        # it is the marked one — EXCEPT when nothing here can serve the
        # capability at all (video generation, off Apple Silicon), where
        # `default` is None regardless of what the list's head
        # is. That is a claim about availability, not about this relationship.
        if row["available"]:
            assert curated[0]["id"] == row["default"]
        sizes = [(m["size_gb"] is None, m["size_gb"] or 0.0) for m in curated]
        assert sizes == sorted(sizes), row["capability"]


def test_a_second_revision_landing_in_an_EXISTING_repo_updates_its_size(
        client, hub, monkeypatch, safetensors_text_engine):
    """The staleness hole the first version of this memo had, and the reason the TTL
    is no longer what invalidates it.

    A repo folder's own mtime does not move when a blob renames into its `blobs/`,
    so a signature built from the cache directory's entries alone would have held a
    stale size for the whole TTL — and with the TTL lengthened to bound the cost of
    the walk, that meant five minutes of a download the user watched finish not
    changing the number beside it. The signature now includes each repo's own
    subdirectory mtimes, so this is caught with the clock frozen.
    """
    frozen = time.time()
    monkeypatch.setattr(ai_models, "_now", lambda: frozen)
    repo = _text_repo(hub, "some-org/grows", size=1_000_000_000)
    assert _offered(client, registry.TEXT_GENERATION, "some-org/grows")["size_gb"] == 1.0
    blobs = repo / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    sparse_file(blobs / "second-revision", 2_000_000_000)
    assert _offered(client, registry.TEXT_GENERATION, "some-org/grows")["size_gb"] == 3.0


def test_the_size_walk_is_not_repeated_while_a_repo_sits_still(client, hub, monkeypatch):
    """The other half of the same trade: the signature is four stats per repo, and
    the recursive walk it guards runs only when one of them moves. A poll of an
    unchanged cache must cost no walk at all — that is the whole point of paying for
    the stats."""
    _text_repo(hub, "some-org/settled", size=2048)
    walks = []
    real = ai_models._scan_repo
    monkeypatch.setattr(ai_models, "_scan_repo",
                        lambda root: walks.append(root) or real(root))
    _catalog(client)
    assert len(walks) == 1  # the first read pays for it once
    ai_models._CACHED_MODELS.clear()  # force the OUTER memo to miss…
    _catalog(client)
    # …and the size cache still answers, because nothing about the repo moved. This
    # is the assertion that would fail if the walk were keyed on time.
    assert len(walks) == 1


# -- the model mirror's permission is handed down per model (AI-5l) --------------
#
# `mirror.base_url()` now falls back to a real address (`render.fused.io`) when
# `FUSED_MODEL_MIRROR` is unset, so an unset env var no longer means "no
# mirror" — it means "the default mirror". None of the tests below currently
# call `mirror.manifest()` (the function that makes the HTTP request), which
# is why they have gotten away without a network guard so far, but that is
# safety by coincidence: the next mirror test added to this file, or a change
# to one of these that starts exercising `manifest()`, would reach the real
# `render.fused.io` with nothing stopping it. The `no_egress` import above is
# what closes that gap — do not remove it because "nothing here does
# networking".


def _suggested_id():
    """One id from the curated list, whatever it is today.

    Taken from the catalog rather than hardcoded: the shortlist is refreshed
    every few releases, and a test naming a model that has since been dropped
    would assert about a repo the app no longer offers.
    """
    return sorted(catalog.all_suggested_ids())[0]


def test_a_suggested_model_may_use_the_mirror(monkeypatch, tmp_path):
    """The permission is a REPO ID THE CLIENT ACCEPTS, not a bare flag.

    Carrying an id is what stops a value that arrived some other way from
    licensing a probe for whatever the next download happens to be — the worker
    checks it against the model it was sent to fetch. But the id it checks is
    the one it NAMES to the mirror, which for `llamacpp-text` is the recipe's
    repo and not the catalog's bare `.gguf` filename. So the assertion is the
    shape `mirror.allowed` accepts rather than equality with the catalog id:
    this test asserted equality and passed while the permission it described was
    a value the client refuses, for every model in that list (AI-5m).
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_MODEL_MIRROR", "https://mirror.example")
    granted = supervisor._child_env("t", _suggested_id())["FUSED_MODEL_MIRROR_OK"]

    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", granted)
    assert _mirror_client().allowed(granted) is True


def _mirror_client():
    """The runner-side mirror client, to check a permission against.

    Imported here rather than reasoned about: the whole failure this section
    guards is a permission that looks right in a dict and is refused by the one
    function that reads it.
    """
    from fused_render.ai.runners import mirror

    return mirror


@pytest.mark.parametrize("entry", catalog.SUGGESTIONS["llamacpp-text"],
                         ids=lambda entry: entry["id"])
def test_a_curated_gguf_gets_its_repo_as_the_permission(entry, monkeypatch,
                                                        tmp_path):
    """Without this translation the hook is dead code for EVERY real llama.cpp
    model (AI-5m).

    `llamacpp-text`'s catalog ids are bare `.gguf` FILENAMES — that is how the AI
    Models page keys them, since one repo publishes many quantizations — but
    `llama_text.download` names the recipe's REPO to `download_file`, and the
    repo id is therefore what `mirror.allowed` compares the permission against.
    Handed the filename, the client refuses it against `_REPO_ID` (`org/name`)
    and declines forever: no manifest request, no mirrored download, and nothing
    in the app's behaviour that would say so. Since D416 llama.cpp is the only
    local text engine on Windows and Linux, so that is every suggested text
    model on those platforms.

    Parametrized over the REAL curated list, because a synthetic id is exactly
    what would keep passing while this stayed broken.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_MODEL_MIRROR", "https://mirror.example")
    mirror = _mirror_client()

    env = supervisor._child_env("t", entry["id"])
    granted = env["FUSED_MODEL_MIRROR_OK"]

    # The id the WORKER will name — `llama_text.download` passes
    # `recipe["repo"]` to `worker_base.download_file` — and not the catalog id.
    assert granted == formats.GGUF_RECIPES[entry["id"]]["repo"]
    assert granted != entry["id"]
    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", granted)
    assert mirror.allowed(granted) is True, (
        f"the worker is handed {granted!r} and the client refuses it, so "
        f"{entry['id']} can never come off the mirror")
    # …and the untranslated id would have been refused, which is the whole point.
    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", entry["id"])
    assert mirror.allowed(entry["id"]) is False


def test_a_suggested_repo_id_is_still_handed_down_verbatim(monkeypatch, tmp_path):
    """Every OTHER runner's ids are already repo ids, and the translation must
    leave them exactly as they were — this is a lookup for one runner's id shape,
    not a rewrite of the permission."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    repo_id = next(model for model in sorted(catalog.all_suggested_ids())
                   if "/" in model)

    env = supervisor._child_env("t", repo_id)

    assert env["FUSED_MODEL_MIRROR_OK"] == repo_id


def test_a_gguf_the_user_found_themselves_is_never_translated(monkeypatch,
                                                              tmp_path):
    """The translation is a lookup in the CURATED table, so it cannot widen the
    privacy rule: an uncurated GGUF has no recipe row and no permission, and a
    repo id nobody suggested is refused whether or not it holds a GGUF."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))

    for model in ("some-model-Q4_K_M.gguf", "somebody/a-gguf-repo-we-never-suggested"):
        env = supervisor._child_env("t", model)
        assert "FUSED_MODEL_MIRROR_OK" not in env, model


def test_the_permission_still_names_ONE_repo_and_not_a_switch(monkeypatch,
                                                              tmp_path):
    """A worker fetching one curated GGUF learns the answer for that model's repo
    and for nothing else — the translation adds a lookup, not a second repo."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_MODEL_MIRROR", "https://mirror.example")
    mirror = _mirror_client()
    entry = catalog.SUGGESTIONS["llamacpp-text"][0]
    other = next(recipe["repo"] for key, recipe in formats.GGUF_RECIPES.items()
                 if recipe["repo"] != formats.GGUF_RECIPES[entry["id"]]["repo"])

    granted = supervisor._child_env("t", entry["id"])["FUSED_MODEL_MIRROR_OK"]

    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", granted)
    assert mirror.allowed(other) is False


def test_a_model_the_user_found_themselves_may_not(monkeypatch, tmp_path):
    """A Discover model is never NAMED to our distribution.

    This is the privacy choice the whole per-model design exists for, not an
    optimisation: the probe itself is what would tell us which models a user
    downloads.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    env = supervisor._child_env("t", "somebody/a-model-we-never-suggested")
    assert "FUSED_MODEL_MIRROR_OK" not in env


def test_a_worker_with_no_model_gets_no_permission(monkeypatch, tmp_path):
    """The default argument, which is what every other `_child_env` caller
    gets. No model, no permission — never a permission for everything."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    assert "FUSED_MODEL_MIRROR_OK" not in supervisor._child_env("t")


# -- FUSED_AI_MEMORY_BUDGET_BYTES: the item 14 wiring's budget seam ----------
#
# `fit.available_budget_bytes()` runs SERVER-side (it needs `hw_detect.
# cached_hardware()`, which lives in the `fused_render` package a worker's
# bare-module interpreter cannot import — see `formats.py`'s own top-of-file
# note). This env var is how the number crosses that process boundary, the
# identical shape `FUSED_MODEL_MIRROR_OK` already establishes for a
# per-model, computed-server-side fact a worker needs but cannot derive
# itself.


def test_child_env_carries_the_computed_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fit, "available_budget_bytes", lambda: 12_345_678_901.0)
    env = supervisor._child_env("t")
    assert env["FUSED_AI_MEMORY_BUDGET_BYTES"] == "12345678901"


def test_child_env_omits_the_budget_when_it_cannot_be_computed(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fit, "available_budget_bytes", lambda: None)
    env = supervisor._child_env("t")
    assert "FUSED_AI_MEMORY_BUDGET_BYTES" not in env


def test_an_inherited_budget_is_stripped_rather_than_passed_on(monkeypatch, tmp_path):
    """The same non-negotiable rule `FUSED_MODEL_MIRROR_OK` already keeps:
    this environment is a COPY of the server's, and a stale or operator-set
    value must not silently outlive the computation that is supposed to
    produce it fresh on every spawn."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_AI_MEMORY_BUDGET_BYTES", "999")
    monkeypatch.setattr(fit, "available_budget_bytes", lambda: None)
    env = supervisor._child_env("t")
    assert "FUSED_AI_MEMORY_BUDGET_BYTES" not in env


def test_an_inherited_permission_is_stripped_rather_than_passed_on(monkeypatch,
                                                                   tmp_path):
    """This environment is a COPY of the server's.

    So a variable an operator or a parent process exported would otherwise reach
    every worker for every model, which is exactly the global switch this design
    refused. Stripped, the only way it is ever set is the decision above.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", "somebody/anything")
    env = supervisor._child_env("t", "somebody/a-model-we-never-suggested")
    assert "FUSED_MODEL_MIRROR_OK" not in env


def test_an_operator_set_base_url_reaches_the_worker(monkeypatch, tmp_path):
    """`FUSED_MODEL_MIRROR` is left exactly as it was found — pointing it at a
    staging distribution is the supported way to use this. Unset now falls
    back to the shipped default (`mirror.DEFAULT_BASE`) rather than to no
    mirror at all; the per-model permission below is what still gates it."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_MODEL_MIRROR", "https://mirror.example/staging")
    env = supervisor._child_env("t", _suggested_id())
    assert env["FUSED_MODEL_MIRROR"] == "https://mirror.example/staging"

    monkeypatch.delenv("FUSED_MODEL_MIRROR")
    assert "FUSED_MODEL_MIRROR" not in supervisor._child_env("t", _suggested_id())


def test_on_macos_a_worker_is_spawned_in_the_posix_spawn_shape(monkeypatch):
    """The fork-crash fix. CPython uses `posix_spawn` only for a Popen with no
    `cwd`, `close_fds=False` and no `start_new_session`; anything else forks,
    and a forked child runs the server's atfork handlers — which is how a
    resident PROJ killed every worker with `code -11` and an empty stderr.
    The directory travels in the environment instead."""
    monkeypatch.setattr(sys, "platform", "darwin")
    env = {}
    kwargs = supervisor._spawn_kwargs("/runners/x", env)
    assert kwargs == {"close_fds": False}
    assert env[supervisor.WORKER_CWD_ENV] == "/runners/x"


def test_elsewhere_a_worker_keeps_its_own_session_and_cwd(monkeypatch):
    """Off macOS the shape is unchanged: `cwd` on the Popen, descriptors
    closed, and the platform's own new-session/new-group flag — which is
    `SPAWN_KWARGS` itself, so this reads the constant rather than naming
    `start_new_session`: on a Windows runner that key is `creationflags`."""
    monkeypatch.setattr(sys, "platform", "linux")
    env = {}
    kwargs = supervisor._spawn_kwargs("/runners/x", env)
    assert kwargs == {"cwd": "/runners/x", "close_fds": True, **supervisor.SPAWN_KWARGS}
    assert supervisor.WORKER_CWD_ENV not in env


def test_no_spawn_site_bypasses_the_spawn_shape_helper():
    """Both Popen sites go through `_spawn_kwargs`, and neither passes `cwd`,
    `close_fds` or the session flag directly — the arguments that silently
    turn `posix_spawn` back into `fork()`. Checked on the source, because a
    keyword added to one call is exactly the regression nobody would notice
    until a geo page had rendered on someone's Mac."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(supervisor))
    popens = [node for node in ast.walk(tree)
              if isinstance(node, ast.Call) and ast.unparse(node.func) == "subprocess.Popen"]
    assert len(popens) >= 2, f"{len(popens)} Popen sites, expected the two worker spawns"
    forbidden = {"cwd", "close_fds", "start_new_session", "preexec_fn"}
    for call in popens:
        names = {kw.arg for kw in call.keywords if kw.arg is not None}
        starred = [ast.unparse(kw.value) for kw in call.keywords if kw.arg is None]
        assert not names & forbidden, (
            f"Popen at line {call.lineno} sets {names & forbidden} directly — "
            f"route it through _spawn_kwargs")
        assert any(x.startswith("_spawn_kwargs(") for x in starred), (
            f"Popen at line {call.lineno} does not spread _spawn_kwargs(...)")


def test_neither_spawn_site_forgets_the_model(monkeypatch):
    """Both spawn sites, in lockstep.

    One of them forgetting it is a mirror that works for a Download button and
    not for a load-triggered fetch — the same model, downloaded two ways, taking
    two different paths — and nothing in the app's behaviour would say so.
    Checked on the SOURCE because that is the shape of the mistake: an argument
    with a default is exactly the kind a call site silently omits.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(supervisor))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and getattr(node.func, "id", None) == "_child_env"]
    assert len(calls) == 2, f"{len(calls)} `_child_env` call sites, expected 2"
    for call in calls:
        # >= 2, not == 2: the resident spawn site also passes `worker.capability`
        # (the capability-gated env injection's own gate) as a third positional
        # argument — a call this test must not silently accept with a MISSING
        # model, which is what the >= keeps checking below.
        assert len(call.args) >= 2, (
            f"_child_env at line {call.lineno} passes no model, so that worker "
            f"can never use the mirror")
        # …and passes the model being FETCHED, not some other string. A worker
        # handed the wrong id gets no permission at all, since the id is what
        # `mirror.allowed` compares against.
        assert ast.unparse(call.args[1]) in ("worker.model", "model"), (
            f"_child_env at line {call.lineno} passes "
            f"{ast.unparse(call.args[1])!r} as the model")
