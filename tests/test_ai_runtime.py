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
from fused_render.ai import catalog, registry, supervisor
from fused_render.ai.runners import formats, partial
from fused_render.server import create_app
from fused_render.server.routers import ai_models, ai_runtime
from fused_render.server.routers.ai_models import CachedModel

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
        STATE.update(state="ready", resident_bytes=1234, loaded_at=time.time())
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
    (folder / "worker.py").write_text(FAKE_WORKER)
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
    (folder / "worker.py").write_text(FAKE_IMAGE_WORKER)
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


def _only_transcribe_runner(tmp_path, monkeypatch, code):
    """A registry whose ONLY runner transcribes, under `code`, with the fake
    worker and this interpreter — so no CTranslate2, no weights, no audio.

    The code is a parameter because it is not decoration since D319: the
    endpoint asks `runners/engine_options.py` what the RESOLVED runner cannot
    do, so a test about that answer has to be able to say which runner resolved.
    """
    folder = tmp_path / ("fake_runner_" + code.replace("-", "_"))
    folder.mkdir()
    (folder / "worker.py").write_text(FAKE_TRANSCRIBE_WORKER)
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
def fake_parakeet_runner(tmp_path, monkeypatch):
    """The same fake worker, resolving under the PARAKEET code — which is what
    makes the endpoint's per-engine refusals reachable from a test."""
    yield _only_transcribe_runner(tmp_path, monkeypatch, "parakeet-mlx")
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
    assert resolved is not None and resolved.code == "transformers-text"
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
    assert [m["id"] for m in catalog.for_capability(registry.IMAGE_GENERATION)] == [
        "tonera/FLUX.2-klein-4B-int8-diffusers",
        "black-forest-labs/FLUX.2-klein-4B"]

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


def test_a_preference_for_the_WRONG_capabilitys_runner_is_ignored(monkeypatch):
    """Runner codes are global and capabilities are not, so a stale or
    hand-edited file can pair them wrongly. Loading a Whisper runner for text
    generation would fail at the first `/generate` with something unreadable."""
    _prefer(monkeypatch, registry.TEXT_GENERATION, "faster-whisper")
    resolution = registry.resolve(registry.TEXT_GENERATION)
    assert resolution.runner is not None
    assert resolution.runner.capability == registry.TEXT_GENERATION
    assert "does not do" in resolution.ignored_reason


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
    assert set(choices) == {"mlx-whisper", "parakeet-mlx", "faster-whisper"}
    assert choices["mlx-whisper"]["available"] is False
    assert "Apple Silicon" in choices["mlx-whisper"]["reason"]
    assert choices["faster-whisper"]["reason"] is None
    # Every capability is listed, servable here or not — a preference the user
    # cannot see is one they cannot fix.
    assert set(rows) == set(registry.capabilities())


def test_the_unavailable_reason_names_EVERY_runner_not_just_the_first(monkeypatch):
    """A capability with two runners must not answer for only the first of them.

    `mlx-text` is registered first, so a Linux machine whose transformers worker
    was missing — a state `Runner.available` documents, since a runner is
    registered before its folder is written — was told text generation "needs
    Apple Silicon": the one backend that was never going to serve it, with the
    one that would have gone unmentioned. Reported by review on the PR that
    added the second runner, and the fix is that all three copies of this
    lookup (registry, `_runner_or_raise`, `start_image`) became one.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    # The transformers runner present but unbuilt, which is what makes the whole
    # capability unservable on a machine MLX has already turned down.
    ghost = registry.Runner(
        code="transformers-text", capability=registry.TEXT_GENERATION,
        folder="/nowhere", label="Transformers (CPU)",
        short_label="Transformers (CPU)")
    monkeypatch.setattr(
        registry, "_RUNNERS", (registry.by_code("mlx-text"), ghost))

    reason = registry.unavailable_reason(registry.TEXT_GENERATION)
    assert "not built yet" in reason, reason
    # The SHORT name. This sentence is read wherever a capability has to
    # explain itself — a card, a job row, an API error — and none of those is
    # the engine picker, which is the one surface that keeps a PLATFORM
    # qualifier. On a torch row the two names are now equal, because the
    # accelerator is part of the short name too (a hardware variant is not
    # identifiable without it), so what this pins is that the sentence names
    # the engine at all and names it the way the rest of the app does.
    assert "Transformers (CPU)" in reason, reason
    assert "(PyTorch)" not in reason, reason
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
#: A HARDWARE qualifier is kept, because it is the only thing that tells three
#: builds of one library apart, and the short name is what the Local card and
#: `servingLine` print — three engines all reading "Diffusers" would render as
#: one engine everywhere but the picker.
#:
#: An allow-list rather than a rule about brackets, so adding a row with a
#: qualifier in its short name is a decision somebody writes down here.
_QUALIFIED_SHORT_NAMES = {
    "transformers-text": "(CPU)",
    "transformers-text-cuda": "(CUDA)",
    "transformers-text-rocm": "(ROCm)",
    "diffusers-image": "(CPU)",
    "diffusers-image-cuda": "(CUDA)",
    "diffusers-image-rocm": "(ROCm)",
}


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


def test_text_generation_resolves_to_a_runner_on_every_supported_platform(monkeypatch):
    """The whole point of D293, stated as one assertion.

    Text generation was Apple-Silicon-only, which made the app's flagship local
    capability something a Windows or Linux user could read about and not use.
    """
    for system, machine, code in (
        ("Darwin", "arm64", "mlx-text"),
        ("Windows", "AMD64", "transformers-text"),
        ("Linux", "x86_64", "transformers-text"),
    ):
        monkeypatch.setattr(registry.platform, "system", lambda s=system: s)
        monkeypatch.setattr(registry.platform, "machine", lambda m=machine: m)
        runner = registry.for_capability(registry.TEXT_GENERATION)
        assert runner is not None and runner.code == code, (system, machine)


def test_intel_macos_is_not_advertised_as_a_supported_text_platform(monkeypatch):
    """Availability controls the catalog and Load button, so it is a support claim."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")

    assert registry.for_capability(registry.TEXT_GENERATION) is None
    status = registry.by_code("transformers-text").available()
    assert status.ok is False
    assert "Apple Silicon macOS" in status.reason


def test_apple_silicon_falls_back_to_transformers_when_mlx_is_unavailable(
        monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        registry, "_RUNNERS",
        tuple(runner for runner in registry.all_runners() if runner.code != "mlx-text"),
    )

    runner = registry.for_capability(registry.TEXT_GENERATION)
    assert runner is not None and runner.code == "transformers-text"


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
    for code in ("transformers-text-rocm", "diffusers-image-rocm"):
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
    status = registry.by_code("transformers-text-rocm").available()
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
    status = registry.by_code("transformers-text-rocm").available()
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
    status = registry.by_code("transformers-text-rocm").available()
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


@pytest.mark.skipif(os.geteuid() == 0, reason="os.access ignores mode bits for root")
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
    status = registry.by_code("transformers-text-rocm").available()
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
    status = registry.by_code("transformers-text-rocm").available()
    assert status.ok is False
    assert "renderD*" in status.reason
    assert "--device /dev/dri" in status.reason
    assert "render` group" not in status.reason


@pytest.mark.skipif(os.geteuid() == 0, reason="os.access ignores mode bits for root")
def test_a_render_node_that_is_CLOSED_asks_for_permission(monkeypatch, tmp_path):
    """…and the other state IS the group case, which keeps that advice.

    HIP opens `/dev/kfd` AND the card's render node, so a readable kfd is not
    enough — a user added to `video` but not `render` has exactly this
    half-working state, and here `usermod` is the whole fix.
    """
    _fake_amd(monkeypatch, tmp_path, render_mode=0o000)
    status = registry.by_code("transformers-text-rocm").available()
    assert status.ok is False
    assert "needs permission" in status.reason
    assert "renderD128" in status.reason
    assert "render` group" in status.reason


@pytest.mark.skipif(os.geteuid() == 0, reason="os.access ignores mode bits for root")
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
    status = registry.by_code("transformers-text-rocm").available()
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
        status = registry.by_code("transformers-text-rocm").available()
        assert status.ok is False
        assert "needs Linux" in status.reason
        assert system.lower() in status.reason


def test_an_nvidia_machine_is_offered_the_cuda_engines(monkeypatch, tmp_path):
    _fake_nvidia(monkeypatch, tmp_path)
    for code in ("transformers-text-cuda", "diffusers-image-cuda"):
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
    status = registry.by_code("transformers-text-cuda").available()
    assert status.ok is False
    assert "needs an NVIDIA GPU" in status.reason
    # …and the capability is untouched: the CPU row above it still serves.
    monkeypatch.setattr(registry, "preferred_code", lambda capability: registry.AUTO)
    assert registry.for_capability(registry.TEXT_GENERATION).code == "transformers-text"


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
    for code in ("transformers-text-cuda", "diffusers-image-cuda"):
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
    assert registry.by_code("transformers-text-cuda").available().ok is True
    # …and it takes BOTH: a `/dev/dxg` with no CUDA library is a WSL2 guest whose
    # host driver does not carry one, which is not a CUDA machine.
    os.remove(str(registry.WSL_CUDA_LIBRARY))
    status = registry.by_code("diffusers-image-cuda").available()
    assert status.ok is False
    assert "needs an NVIDIA GPU" in status.reason


@pytest.mark.skipif(os.geteuid() == 0, reason="os.access ignores mode bits for root")
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
    status = registry.by_code("transformers-text-cuda").available()
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
    status = registry.by_code("transformers-text-cuda").available()
    assert status.ok is False
    assert "nvcuda.dll" in status.reason

    (tmp_path / "nvcuda.dll").write_text("")
    assert registry.by_code("transformers-text-cuda").available().ok is True


def test_AUTO_STAYS_ON_THE_CPU_ROW_EVEN_WITH_AN_ACCELERATOR(monkeypatch, tmp_path):
    """The whole user-facing decision of the per-hardware split, in one test.

    A machine with a working NVIDIA GPU and a working AMD GPU has five text
    engines available and resolves to the CPU one, because CPU is the default and
    the accelerated rows are OPT-IN from the Engines tab. That is a choice, not
    an accident of ordering: the accelerated wheels are much larger downloads
    with a hardware requirement, and a default that silently required one would
    fail hardest on the machines least able to explain why. Anyone who wants the
    GPU says so once, and `prefs.json` remembers.

    Pinned because the ordering is invisible in a diff of the table and nothing
    else fails when a row moves — the same argument the mflux ordering test
    makes, applied to the decision it was written for.
    """
    _fake_amd(monkeypatch, tmp_path)
    _fake_nvidia(monkeypatch, tmp_path)
    monkeypatch.setattr(registry, "preferred_code", lambda capability: registry.AUTO)

    for code in ("transformers-text-cuda", "transformers-text-rocm",
                 "diffusers-image-cuda", "diffusers-image-rocm"):
        assert registry.by_code(code).available().ok is True, code
    assert registry.for_capability(registry.TEXT_GENERATION).code == "transformers-text"
    assert registry.for_capability(registry.IMAGE_GENERATION).code == "diffusers-image"

    # …and opting in is honoured, which is what makes the default a default
    # rather than a restriction.
    _prefer(monkeypatch, registry.TEXT_GENERATION, "transformers-text-cuda")
    resolution = registry.resolve(registry.TEXT_GENERATION)
    assert resolution.runner.code == "transformers-text-cuda" and resolution.honoured


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
        folder=os.path.join(registry.RUNNERS_DIR, "transformers_text"),
        label="Flapping", short_label="Flapping",
        _available=probe,
    )
    monkeypatch.setattr(registry, "_RUNNERS", (runner,))
    monkeypatch.setattr(registry, "preferred_code", lambda capability: registry.AUTO)

    for row in registry.describe_engines():
        for choice in row["choices"]:
            assert (choice["available"] is False) == (choice["reason"] is not None), choice


def test_the_cpu_torch_rows_name_the_apple_silicon_GPU_they_run_on(monkeypatch):
    """The Engines tab must not contradict the card beside it.

    `torch_text._placement()` returns `("mps", float16)` on a Mac and
    `torch_image._place()` moves the pipeline to `mps` — which is the whole point
    of the `whl/cpu` pin resolving darwin to the ordinary MPS-capable wheel: this
    row is what a Mac falls back to when MLX is unavailable (AI-2b). So a note
    reading "Runs on the CPU on any machine, at a few words a second" printed a
    CPU speed claim under the picker while the loaded card reported device `mps`,
    on the exact machine the fallback exists for.

    The `code`, the `label` and the `short_label` are deliberately NOT what
    changed: a stored engine preference keys on `code` (D381), and "(CPU)" names
    the BUILD — the install with no accelerator libraries in it — which is the
    identity AI-2c requires a hardware variant to carry in both names.
    """
    for code in ("transformers-text", "diffusers-image"):
        runner = registry.by_code(code)
        assert runner.label == runner.short_label
        assert "(CPU)" in runner.label
        note = runner.note
        assert "Apple Silicon" in note, (code, note)
        # ONE LINE is the constraint the field documents, so the Mac clause has
        # to be paid for rather than appended.
        assert len(note) <= 110, (code, len(note), note)


def test_the_rocm_image_row_warns_that_a_render_can_stall_the_desktop():
    """The ROCm note names the desktop risk, and stays one line while doing it.

    Observed rather than theorised (D383): a sustained submission on an RX 9060
    XT (gfx1200) starved `gfx_0.0.0` until the driver reset the ring, and the
    process the kernel named was the COMPOSITOR — the desktop died while the GPU
    itself recovered without a reboot. Compute and display share that ring on a
    single-GPU machine, so "seconds per image" was true and incomplete: the row
    promised the speed and said nothing about what paying for it can cost.

    Pinned because it is the kind of clause a later tidy-up deletes as hedging.
    The length assertion is the same one-line budget the CPU rows are held to —
    the warning had to be paid for out of the sentence, not appended to it.
    """
    runner = registry.by_code("diffusers-image-rocm")
    assert "desktop" in runner.note, runner.note
    assert len(runner.note) <= 110, (len(runner.note), runner.note)


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
    assert catalog.for_runner("transformers-text-cuda") == catalog.SUGGESTIONS["transformers-text"]
    assert catalog.for_runner("diffusers-image-rocm") == catalog.SUGGESTIONS["diffusers-image"]
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


def test_transformers_and_whisper_suggestions_show_snapshot_size_estimates():
    expected = {
        "Qwen/Qwen3-4B-Instruct-2507": 8.1,
        "microsoft/Phi-4-mini-instruct": 7.7,
        "Qwen/Qwen3-1.7B": 4.1,
        "Qwen/Qwen3-8B": 16.4,
        "deepdml/faster-whisper-large-v3-turbo-ct2": 1.6,
        "Systran/faster-whisper-medium": 1.5,
        "Systran/faster-whisper-small": 0.5,
    }
    actual = {
        model["id"]: model["size_gb"]
        for runner in ("transformers-text", "faster-whisper")
        for model in catalog.SUGGESTIONS[runner]
    }
    assert actual == expected


def test_every_suggestion_list_is_ordered_smallest_first():
    """One ordering rule, and the default is whatever it puts at position 0.

    The user was shown the trade — a bare `fused.ai.transcribe()` now loads
    `Systran/faster-whisper-small` rather than the turbo model — and chose one
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


def test_the_default_is_the_smallest_model_the_active_runner_offers(monkeypatch):
    """`default_for` is position 0, and position 0 is the smallest — end to end.

    Named per capability because these are the ids a no-model call reaches for,
    and the whole point of the change is that they are now the SMALL ones.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    assert catalog.default_for(registry.TEXT_GENERATION) == \
        "mlx-community/Qwen3.5-2B-MLX-4bit"
    # tiny.en is English-only, and it still leads: the one-rule trade above was
    # chosen with its cost in view, and an entry is added to the list because
    # it is worth OFFERING, not because it should be default-proof.
    assert catalog.default_for(registry.SPEECH_TO_TEXT) == \
        "mlx-community/whisper-tiny.en-8bit"

    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    assert catalog.default_for(registry.TEXT_GENERATION) == "Qwen/Qwen3-1.7B"
    assert catalog.default_for(registry.SPEECH_TO_TEXT) == \
        "Systran/faster-whisper-small"


def test_the_catalog_follows_the_runner_that_would_actually_load(monkeypatch):
    """A Windows machine must not be shown MLX repos, or told it has no runner.

    Both halves were one bug: `describe()` took the FIRST runner registered for
    a capability regardless of whether it could run, so with MLX listed above
    transformers a Windows box would have read "needs Apple Silicon" under a
    heading whose four suggestions were all Metal-packed checkpoints it could
    not load — while a runner sat ready to serve it.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    text = next(row for row in catalog.describe()
                if row["capability"] == registry.TEXT_GENERATION)
    assert text["available"] is True and text["reason"] is None
    assert text["runner"] == "transformers-text"
    assert text["runnerLabel"] == "Transformers (CPU)"
    # Both names travel, and the Discover heading uses the short one — which on
    # a torch row is the same string, because the accelerator is part of the
    # engine's identity rather than a platform note.
    assert text["runnerShortLabel"] == "Transformers (CPU)"
    assert not any(m["id"].startswith("mlx-community/") for m in text["models"])
    # …and the default a bare `fused.ai.image()`-style call would reach for is
    # the loadable one, not the first entry of some other machine's list.
    assert catalog.default_for(registry.TEXT_GENERATION) == text["models"][0]["id"]

    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    text = next(row for row in catalog.describe()
                if row["capability"] == registry.TEXT_GENERATION)
    assert text["runner"] == "mlx-text"
    assert all(m["id"].startswith(("mlx-community/", "prism-ml/")) for m in text["models"])


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
    assert registry.capability_for_task("speech recognition") == registry.SPEECH_TO_TEXT
    assert "speech recognition" not in registry.NO_RUNNER_YET
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    runner = registry.for_capability(registry.SPEECH_TO_TEXT)
    assert runner is not None and runner.code == "faster-whisper"


def test_the_registry_describes_the_transcription_runner():
    rows = {row["code"]: row for row in registry.describe()}
    assert rows["faster-whisper"]["capability"] == registry.SPEECH_TO_TEXT


def test_llamacpp_text_is_registered_below_every_transformers_row(monkeypatch):
    """AI-11's precedent, restated for the fourth text runner: it sits BELOW
    all three `transformers-text` rows, so `auto` resolution never reaches it
    on ANY platform — reaching it is always a choice made on the Engines tab.

    Position is checked directly, rather than only inferred from behaviour,
    because the ordering is invisible in a diff of the table (the same
    argument `test_AUTO_STAYS_ON_THE_CPU_ROW_EVEN_WITH_AN_ACCELERATOR`'s
    docstring makes about the CPU/CUDA/ROCm split) and nothing else fails when
    a row moves one line up.
    """
    codes = [r.code for r in registry.all_runners() if r.capability == registry.TEXT_GENERATION]
    assert codes.index("llamacpp-text") > codes.index("transformers-text")
    assert codes.index("llamacpp-text") > codes.index("transformers-text-cuda")
    assert codes.index("llamacpp-text") > codes.index("transformers-text-rocm")

    # AUTO on every platform this app ships reaches MLX or a transformers row,
    # never this one — `_always` makes it AVAILABLE everywhere, which is
    # exactly why the ORDER is what keeps `auto` off it.
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(registry, "preferred_code", lambda capability: registry.AUTO)
    assert registry.for_capability(registry.TEXT_GENERATION).code == "transformers-text"

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
    `runners/llamacpp_text.py`'s module docstring for why there is no
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
    which already carries the `tomli` fallback for the 3.10 that
    `requires-python` still promises.
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
    """
    from fused_render import projectenv

    for requirement in projectenv.dependencies_of(runner.folder):
        name = requirement.split("[")[0].split(";")[0]
        for operator in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            name = name.split(operator)[0]
        name = name.strip().replace("_", "-").lower()
        if name in UNBOUNDED_RUNNER_DEPENDENCIES:
            continue
        assert "<" in requirement or "==" in requirement, (
            f"{os.path.relpath(runner.pyproject)} declares `{requirement}` with "
            f"no upper bound. These runners have no committed uv.lock and "
            f"`uv sync` runs bare, so every new venv key re-resolves this from "
            f"PyPI — a major bump lands on users with no commit behind it "
            f"(the mflux abort above). Add a ceiling, or add `{name}` to "
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


# -- the download-manager join --------------------------------------------------


def test_a_load_opens_a_server_owned_job_row(fake_runner):
    started = supervisor.load("org/rowed", registry.TEXT_GENERATION)
    _wait_ready("org/rowed")
    row = next(j for j in jobs.list_jobs() if j["id"] == started["jobId"])
    assert row["owner"] == jobs.OWNER_SERVER
    assert row["title"] == "org/rowed"
    assert row["id"].startswith(jobs.SERVER_ID_PREFIX)


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

    def fake_start(project_dir):
        rounds.append(project_dir)
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
    monkeypatch.setattr(envinstall, "start", lambda d: {"key": "abc123", "done": False})
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
        "mlx-text", "transformers-text", "transformers-text-cuda",
        "transformers-text-rocm", "diffusers-image", "diffusers-image-cuda",
        "diffusers-image-rocm", "mflux-image",
        "faster-whisper", "mlx-whisper", "parakeet-mlx"}
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
                 "/api/ai/runtime/download", "/api/ai/image", "/api/ai/transcribe"):
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
    assert captured["outPreview"] == started["previewPath"]
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
    assert reply["path"] == os.path.abspath(recording)
    assert reply["model"] == catalog.default_for(registry.SPEECH_TO_TEXT)
    assert reply["task"] == "translate"
    assert reply["output"].endswith(".json")
    assert os.path.dirname(reply["output"]).endswith(os.path.join("ai", "transcripts"))
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

    assert seen["outPartial"] == reply["outputPartial"]
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
        client, fake_parakeet_runner, recording, sent, needle):
    """D319/AI-10g. The worker refuses these too, but by then the user has paid
    for a job row, a venv build and a multi-gigabyte download to be told no —
    and the runner is resolved synchronously HERE, so the answer was available
    before any of it. Same treatment as a bad `task` or `speakers`: an instant
    400 with the sentence `runners/engine_options.py` holds."""
    response = _post_transcribe(client, path=recording, **sent)

    assert response.status_code == 400, (sent, response.json())
    assert needle in response.json()["error"]
    assert not [j for j in jobs.list_jobs()
                if j["id"].startswith(supervisor.TRANSCRIBE_JOB_PREFIX)]


def test_an_ORDINARY_request_to_that_engine_still_runs(
        client, fake_parakeet_runner, recording):
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
    take `translate` away from both whisper runners and pass every parakeet
    test while doing it."""
    started = _post_transcribe(client, path=recording, task="translate",
                               language="en", initialPrompt="Acme Corp")

    assert started.status_code == 200, started.json()
    _wait_job(started.json()["jobId"])


def test_the_endpoint_and_the_worker_refuse_an_option_by_the_SAME_rule(
        client, fake_parakeet_runner, recording):
    """One sentence, one place. The endpoint hands the caller whatever
    `runners/engine_options.py` raises, which is the module the worker imports
    out of its own venv — so a message reworded there is reworded here."""
    from fused_render.ai.runners import engine_options

    response = _post_transcribe(client, path=recording, task="translate")
    with pytest.raises(ValueError) as raised:
        engine_options.unsupported_or_raise("parakeet-mlx", task="translate")
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
    assert reply.json()["path"] == recording
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

    assert seen["row"] == {"title": os.path.basename(recording), "kind": "task",
                           "cancellable": True, "unit": "s"}


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

    monkeypatch.setattr(supervisor, "ready_worker", lambda capability, model=None: object())
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
    monkeypatch.setattr(supervisor, "ready_worker", lambda capability, model=None: object())
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
    monkeypatch.setattr(supervisor, "ready_worker", lambda capability, model=None: object())
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

    # Evict it exactly as the cap does, mid-load.
    deadline = time.monotonic() + 5
    row = None
    while time.monotonic() < deadline:
        row = _row_now(job)
        if row and "Waiting for" in (row.get("detail") or ""):
            break
        time.sleep(0.02)
    assert row and "Waiting for" in (row.get("detail") or ""), row
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
    assert "Waiting for" in (rebuilt.get("detail") or ""), rebuilt
    assert rebuilt["title"] == os.path.basename(recording)
    assert rebuilt["cancellable"] is True and rebuilt["unit"] == "s"
    _wait_job(job, timeout=40)


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
    # is far inside `STALE_DROP_S`), `dismiss` and `clear_finished` (both refuse
    # a RUNNING row that is not stalled). ONE remote path survives — a tick
    # thread starved past `STALE_AFTER_S` makes the row dismissible, and a user
    # looking at "no longer reporting" may well dismiss it — and the rebuild on
    # detection heals that, because `_transcribe_row` carries the `title` and
    # `state: "running"` that reopen a forgotten id. So the poll cadence is the
    # backstop's latency, and it still has to beat the watcher.
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
    monkeypatch.setattr(supervisor, "_await_turn", lambda job, title: None)
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
    monkeypatch.setattr(supervisor, "ready_worker", lambda capability, model=None: object())
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
    monkeypatch.setattr(supervisor, "ready_worker", lambda capability, model=None: object())
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
        return object()

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
    start = source.index("  function aiTranscribe(opts)")
    end = source.index("\n  }\n", start) + 4
    fn = source[start:end]

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
    out = subprocess.run(["node", "-e", prelude + fn + call],
                         capture_output=True, text=True)
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


def _run_ai_image(record='{state: "done"}', ticks="[]", preview='"/t/a.preview.png"'):
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
    start = source.index("  function aiImage(opts)")
    end = source.index("\n  }\n", start) + 4
    fn = source[start:end]

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
      aiImage({prompt: "a fox", onProgress: (job) => progress.push(job)}).then(
        (value) => console.log(JSON.stringify({ok: true, value, progress, rows})),
        (err) => console.log(JSON.stringify(
          {ok: false, message: err.message, type: err.type, progress, rows})),
      );
    """
    out = subprocess.run(["node", "-e", prelude + fn + call],
                         capture_output=True, text=True)
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
    start = source.index("  function aiTranscribe(opts)")
    fn = source[start:source.index("\n  }\n", start) + 4]

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
    out = subprocess.run(["node", "-e", prelude + fn + call],
                         capture_output=True, text=True)
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
        client, hub, dispatched):
    """The reported bug. An mflux conversion has no config.json at all, so the
    old default sent it to mlx-lm; its component folders say image generation
    beyond doubt."""
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
    runners that read one are the two TEXT runners, so their shared capability
    is the answer rather than a guess."""
    _cached_repo(hub, "org/mystery", files=("model.safetensors",))
    assert _load(client, {"model": "org/mystery"}).status_code == 200
    assert dispatched[0]["capability"] == registry.TEXT_GENERATION


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


def test_a_cached_gguf_repo_now_loads_as_text_via_llamacpp(client, hub, dispatched):
    """The other half of the story the test above used to tell alone: since
    SPEC AI-11 a root-level `.gguf` IS decisively `llamacpp-text`'s
    (`formats.DECISIVE`), so `cached_capability`'s `meta.loaders` fallback
    resolves it to text generation with no task label needed — the same
    mechanism `test_a_cached_repo_with_no_task_but_readable_weights_is_text`
    exercises for a bare directory of safetensors."""
    _cached_repo(hub, "org/gguf-only", files=("model.gguf", "README.md"))
    assert _load(client, {"model": "org/gguf-only"}).status_code == 200
    assert dispatched[0]["capability"] == registry.TEXT_GENERATION


def test_an_explicit_capability_still_wins_over_the_format(client, hub, dispatched):
    """Inference governs the OMITTED case only. A caller who names a capability
    gets it, right or wrong — that is what makes this additive."""
    repo_id = next(iter(formats.MFLUX_VARIANTS))
    _cached_repo(hub, repo_id, dirs=formats.MFLUX_COMPONENTS)
    assert _load(client, {"model": repo_id,
                          "capability": registry.TEXT_GENERATION}).status_code == 200
    assert dispatched[0]["capability"] == registry.TEXT_GENERATION


def test_download_infers_the_capability_the_same_way(client, hub, dispatched):
    """`/download` takes the same default through the same helper, so it had the
    same bug: a Download on the AI Models page fetched an image model into the
    text runner's idea of what to fetch."""
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


def _text_repo(hub, repo_id, *, size=0):
    """A cached repo whose config says text generation beyond doubt, sized."""
    repo = _cached_repo(hub, repo_id, files=("model.safetensors",),
                        config={"architectures": ["LlamaForCausalLM"]})
    (repo / "snapshots" / "c0ffee" / "model.safetensors").write_bytes(b"x" * size)
    return repo


def test_a_downloaded_repo_the_curation_never_heard_of_joins_its_capability(client, hub):
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


def test_a_cached_entrys_size_is_its_real_measured_footprint(client, hub):
    """Measured, not guessed: the field means "every byte on the disk", the same
    thing it means for a curated entry (see catalog.py's docstring)."""
    _text_repo(hub, "some-org/three-gb", size=3_000_000_000)
    entry = _offered(client, registry.TEXT_GENERATION, "some-org/three-gb")
    assert entry["size_gb"] == 3.0


def test_an_uncurated_repo_on_disk_cannot_take_position_0_or_the_default(client, hub):
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


def test_the_cached_tail_is_smallest_first_with_unknown_sizes_last(client, hub):
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


def test_a_curated_repo_that_is_also_on_disk_appears_once_marked_downloaded(client, hub):
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


def test_a_model_downloaded_after_a_read_appears_on_the_very_next_one(client, hub):
    """The cache-staleness trap, pinned. The scan is memoised because a page polls
    this route, and a memo that outlived a completed download would reproduce
    exactly the bug this change fixes — the model the user just fetched missing
    from the picker."""
    repo_id = "some-org/arrived-late"
    assert _entry(client, registry.TEXT_GENERATION, repo_id) is None
    _text_repo(hub, repo_id, size=2048)
    assert _entry(client, registry.TEXT_GENERATION, repo_id) is not None


def test_a_resident_model_is_marked_loaded(client, hub, monkeypatch):
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
        client, hub, monkeypatch):
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
    conversion is a perfectly good text model that the Transformers runner cannot
    read — so on a Mac switched to Transformers on the Engines tab it is an unusable
    download, and D293's whole point is that the list moves when the preference
    does. It must move for the cached half too."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    repo_id = "mlx-community/Qwen3-8B-MLX-4bit"
    assert repo_id not in catalog.all_suggested_ids()
    # A REAL MLX conversion: the `quantization` block with a `group_size` is what
    # `formats.py` reads as "mlx-lm packed this", and it is what makes the repo
    # unreadable to Transformers rather than merely differently named.
    _cached_repo(hub, repo_id, files=("model.safetensors",),
                 config={"architectures": ["Qwen3ForCausalLM"],
                         "quantization": {"bits": 4, "group_size": 64}})
    assert next(m for m in ai_models.cached_models()
                if m.repo_id == repo_id).loaders == ("mlx-text",)
    # With MLX serving text generation this repo IS loadable, and is offered…
    assert registry.for_capability(registry.TEXT_GENERATION).code == "mlx-text"
    assert _entry(client, registry.TEXT_GENERATION, repo_id) is not None
    # …and the moment the user picks the engine that cannot read it, it is not.
    _prefer(monkeypatch, registry.TEXT_GENERATION, "transformers-text")
    ai_models._CACHED_MODELS.clear()
    assert registry.for_capability(registry.TEXT_GENERATION).code == "transformers-text"
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
#: generation on an M-series Mac and `transformers-text` serves it everywhere else,
#: their `SUGGESTIONS` lists are completely different, and a test that took whichever
#: one the dev machine happened to answer is a test that passes at home and fails in
#: CI on the other three platforms.
_TEXT_PLATFORMS = [
    pytest.param("Darwin", "arm64", "mlx-text", id="apple-silicon"),
    pytest.param("Linux", "x86_64", "transformers-text", id="linux"),
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
    `transformers-text` answered with `Qwen/Qwen3-1.7B` at position 0, and the test
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
    _text_repo(hub, "some-org/only-thing-here", size=2048)
    row = _catalog(client)[registry.TEXT_GENERATION]
    assert row["runner"] == expected_runner
    assert row["default"] is None
    assert [m["id"] for m in row["models"]] == ["some-org/only-thing-here"]
    assert row["models"][0]["source"] == "cached"


def test_a_cached_entry_never_leads_a_list_that_has_a_curated_one(client, hub):
    """The invariant that does hold unconditionally, stated as itself: wherever a
    curated entry exists, index 0 is curated — so `models[0]` and `default` agree
    and a bare call cannot reach an unvetted repo."""
    _text_repo(hub, "some-org/aaa-alphabetically-first", size=1)
    for row in _catalog(client).values():
        if any(m["source"] == "curated" for m in row["models"]):
            assert row["models"][0]["source"] == "curated"
            assert row["models"][0]["id"] == row["default"]


def test_a_second_revision_landing_in_an_EXISTING_repo_updates_its_size(
        client, hub, monkeypatch):
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
    (blobs / "second-revision").write_bytes(b"x" * 2_000_000_000)
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
