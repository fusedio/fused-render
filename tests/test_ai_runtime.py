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

import pytest
from fastapi.testclient import TestClient

from fused_render import jobs
from fused_render.ai import catalog, registry, supervisor
from fused_render.ai.runners import formats
from fused_render.server import create_app

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


@pytest.fixture()
def fake_transcribe_runner(tmp_path, monkeypatch):
    """A registry whose ONLY runner transcribes, with the fake worker and this
    interpreter — so no CTranslate2, no weights, no audio."""
    folder = tmp_path / "fake_transcribe_runner"
    folder.mkdir()
    (folder / "worker.py").write_text(FAKE_TRANSCRIBE_WORKER)
    runner = registry.Runner(
        code="fake-whisper", capability=registry.SPEECH_TO_TEXT,
        folder=str(folder), label="Fake whisper",
    )
    monkeypatch.setattr(registry, "_RUNNERS", (runner,))
    # See `fake_image_runner`: the catalog is keyed by runner since D293, so the
    # fake backend brings its own default.
    monkeypatch.setitem(catalog.SUGGESTIONS, "fake-whisper", [
        {"id": "org/fake-whisper", "label": "Fake whisper", "size_gb": None, "note": ""},
    ])
    monkeypatch.setattr(supervisor, "_ensure_venv", lambda r, w, j: sys.executable)
    monkeypatch.setattr(supervisor, "_require_build_tools", lambda: None)
    yield runner
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
    """Image generation is arranged like the other two: MLX takes the Macs (D305).

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
    assert set(choices) == {"mlx-whisper", "faster-whisper"}
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
        folder="/nowhere", label="Transformers (PyTorch)",
        short_label="Transformers")
    monkeypatch.setattr(
        registry, "_RUNNERS", (registry.by_code("mlx-text"), ghost))

    reason = registry.unavailable_reason(registry.TEXT_GENERATION)
    assert "not built yet" in reason, reason
    # The SHORT name. This sentence is read wherever a capability has to
    # explain itself — a card, a job row, an API error — and none of those is
    # the engine picker, which is the one surface that keeps the qualifier.
    assert "Transformers" in reason, reason
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


def test_the_picker_keeps_the_qualifier_and_everything_else_drops_it():
    """The one surface that shows a platform qualifier is the engine picker.

    That is the whole point of the split: on the picker the reader is CHOOSING
    between backends and "(Apple Silicon)" is the difference between two
    options; everywhere else they are being told what is happening on a machine
    they are already sitting at.
    """
    engines = registry.describe_engines()
    choices = [c for row in engines for c in row["choices"]]
    assert any("(" in c["label"] for c in choices), choices
    # …and the summary line beside it, and the runner rows every other surface
    # reads, carry the short one.
    for row in engines:
        if row["effective"]:
            assert "(" not in (row["effectiveShortLabel"] or "")
    for row in registry.describe():
        assert "(" not in row["shortLabel"], row
        assert row["label"].startswith(row["shortLabel"])


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
    assert catalog.default_for(registry.SPEECH_TO_TEXT) == \
        "mlx-community/whisper-small-mlx"

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
    assert text["runnerLabel"] == "Transformers (PyTorch)"
    # Both names travel, and the Discover heading uses the short one.
    assert text["runnerShortLabel"] == "Transformers"
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


def test_the_cpu_warning_reaches_the_page_before_the_download(monkeypatch):
    """The PyTorch runner says what using it is LIKE, and the catalog carries it.

    torch from PyPI is CPU-only on Windows, so the ordinary outcome there is a
    model that works and answers at walking pace. That is worth knowing BEFORE
    an 8GB pull, and nothing else on the page can say it: the device a model
    really got is a measurement that does not exist until one has loaded.
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
        "mlx-text", "transformers-text", "diffusers-image", "mflux-image",
        "faster-whisper", "mlx-whisper"}
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


def test_diarizing_without_a_speaker_count_is_refused_before_a_job_opens(
        client, fake_transcribe_runner, recording):
    """`speakers` is REQUIRED with `diarize`, and this is the server's copy of
    that rule — `runtime.js` refuses first, but the bridge is not the only door
    into this endpoint and a rule enforced only in JavaScript is not enforced.

    Refused with a 400 rather than guessed, for the reason `diarize.py` states:
    the alternative to a cluster count is a cosine threshold nobody outside a
    lab can set, so a guess relabels the whole transcript with total confidence.
    Before a job row opens, like the `path` check above — this one would
    otherwise open a row that survives a multi-second model load to die.
    """
    for sent in ({"diarize": True},
                 {"diarize": True, "speakers": None},
                 {"diarize": True, "speakers": 0},
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


def test_the_server_and_the_workers_refuse_a_speaker_count_by_the_SAME_rule(
        client, fake_transcribe_runner, recording):
    """One sentence, one place. The endpoint hands the caller whatever
    `runners/diarize.py` raises, so a rule that changes there changes here —
    which is the point of the server importing the module the workers import
    rather than restating it."""
    from fused_render.ai.runners import diarize

    response = _post_transcribe(client, path=recording, diarize=True)
    with pytest.raises(ValueError) as raised:
        diarize.speakers_or_raise(None)
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


def _run_ai_transcribe(readfile, record, node_required=True, opts='{path: "a.m4a"}'):
    """Run `aiTranscribe` out of runtime.js under node, against stubs.

    The same extraction the claude suites use (`tests/test_claude_narrow.py`):
    a named function is lifted out and driven with its closure stubbed, because
    what matters is the decision it reaches rather than the DOM it reached it
    in. This bridge had only source assertions until now, which cannot tell a
    typed rejection from an untyped one.

    `readfile` is JS for the body of the stub `readFile`; `record` is the job
    row `watch` resolves with; `opts` is the argument object as JS, so a caller
    can drive the argument checks that reject before any of the stubs are ever
    reached. Returns the settled outcome as a dict.
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
                        outputText: "/t/out.txt", path: "/t/a.m4a",
                        model: "m", task: "transcribe"}};
      const window = {{location: {{search: "?path=/pages/p.html"}}}};
      const aiPost = () => Promise.resolve(started);
      const rawUrl = (p) => "/api/fs/raw?path=" + p;
      const stat = () => Promise.reject(new Error("no stat"));
      const readFile = () => {readfile};
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
          {ok: false, message: err.message, type: err.type, jobId: err.jobId})),
      );
    """.replace("OPTS", opts)
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


@pytest.mark.parametrize("opts", [
    '{path: "a.m4a", diarize: true}',
    '{path: "a.m4a", diarize: true, speakers: null}',
    '{path: "a.m4a", diarize: true, speakers: 0}',
    '{path: "a.m4a", diarize: true, speakers: -2}',
    '{path: "a.m4a", diarize: true, speakers: true}',
    '{path: "a.m4a", diarize: true, speakers: 2.5}',
    '{path: "a.m4a", diarize: true, speakers: "2"}',
    '{path: "a.m4a", diarize: true, speakers: NaN}',
])
def test_diarizing_without_a_usable_speaker_count_rejects_BEFORE_a_job_opens(opts):
    """The bridge's half of the required argument, beside the `path` check and
    for the same reason: the caller fails synchronously with an actionable
    sentence instead of watching a row open and die.

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


def test_a_cancelled_row_rejects_as_cancelled_not_as_an_error():
    settled = _run_ai_transcribe('Promise.reject(new Error("no file"))',
                                 '{state: "cancelled"}')
    assert settled["ok"] is False and settled["type"] == "cancelled"
    assert "cancelled" in settled["message"]


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


# -- which capability a load without one gets (D307) ---------------------------
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
    capability": the repo, what it looks like, and what to pass."""
    _cached_repo(hub, "org/gguf-only", files=("model.gguf", "README.md"))
    response = _load(client, {"model": "org/gguf-only"})
    assert response.status_code == 400
    message = response.json()["error"]
    assert "org/gguf-only" in message
    assert "gguf" in message
    assert "capability" in message
    assert registry.IMAGE_GENERATION in message and registry.SPEECH_TO_TEXT in message
    assert dispatched == []


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
    _cached_repo(hub, "org/gguf-only", files=("model.gguf",))
    response = client.post("/api/ai/runtime/download", json={"model": "org/gguf-only"},
                           headers={"X-Fused": "1"})
    assert response.status_code == 400
    assert "org/gguf-only" in response.json()["error"]
    assert dispatched == []
