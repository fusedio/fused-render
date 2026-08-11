"""The worker contract itself (SPEC AI-3, AI-9), driven directly.

`tests/test_ai_runtime.py` tests the SUPERVISOR against a fake worker — it
proves the parent's half: spawn, wait, evict, measure, kill. Nothing tested our
half, because neither real worker can run here: mlx_text needs Metal and
diffusers_image needs several GB of torch.

`worker_base` is why that changes. It is the contract both runners actually use,
it is stdlib-only, and so it imports and runs on CI with stub callables standing
in for the model. What is checked here is exactly the part a fake worker cannot
check: that OUR implementation of the four routes, the auth header, the state
machine and the disk-measured download behaves the way the supervisor assumes.
"""
import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

BASE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "worker_base.py",
)


def _fresh_base():
    """A fresh import of worker_base.

    By path and by hand, for the reason the module exists: runners load it off
    sys.path in their own interpreter, not as `fused_render.ai.runners...`, so
    importing it that way here would be testing a different import than the one
    that ships. A fresh module per test also keeps the module-level STATE from
    leaking between them.
    """
    spec = importlib.util.spec_from_file_location("worker_base_under_test", BASE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def base():
    return _fresh_base()


def _serve(module, generate, streaming=False):
    """Start the real HTTP half on an ephemeral port; stop it after the test."""
    server = module.build_server(generate, streaming=streaming)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _call(server, path, body=None, token="secret", method=None):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Fused-Worker"] = token
    request = urllib.request.Request(
        url, data=data, headers=headers,
        method=method or ("POST" if data is not None else "GET"))
    return urllib.request.urlopen(request, timeout=5)


# -- the auth header ------------------------------------------------------------


def test_every_route_refuses_a_wrong_token(base):
    """The ephemeral port is not the security boundary — the token is. A local
    process that guessed the port must still get nowhere."""
    base.TOKEN = "secret"
    base.set_state(state="ready")
    server = _serve(base, lambda body: {"ok": True})
    try:
        for path, body in (("/health", None), ("/generate", {}), ("/cancel", {}),
                           ("/quit", {})):
            with pytest.raises(urllib.error.HTTPError) as caught:
                _call(server, path, body, token="wrong")
            assert caught.value.code == 403, path
    finally:
        server.shutdown()


def test_a_missing_token_is_refused_too(base):
    base.TOKEN = "secret"
    server = _serve(base, lambda body: {"ok": True})
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            _call(server, "/health", token=None)
        assert caught.value.code == 403
    finally:
        server.shutdown()


# -- /health and the state machine ----------------------------------------------


def test_health_reports_the_state_the_supervisor_polls_for(base):
    base.TOKEN = "secret"
    base.set_state(state="loading", model="org/m", detail="Loading weights…")
    server = _serve(base, lambda body: {})
    try:
        with _call(server, "/health") as response:
            health = json.loads(response.read())
        assert health["state"] == "loading"
        assert health["model"] == "org/m"
        assert health["detail"] == "Loading weights…"
    finally:
        server.shutdown()


def test_generating_before_the_model_is_ready_is_a_409(base):
    """`ready` is the ONLY state that means the model can answer. A worker that
    served a request while still loading would answer with an empty model."""
    base.TOKEN = "secret"
    base.set_state(state="loading")
    server = _serve(base, lambda body: {"never": True})
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            _call(server, "/generate", {"prompt": "hi"})
        assert caught.value.code == 409
    finally:
        server.shutdown()


def test_bring_up_walks_downloading_then_loading_then_ready(base):
    seen = []

    def download(model_id):
        seen.append(("download", model_id, base.snapshot()["state"]))
        return "/tmp/snapshot"

    def load(model_id, fetched):
        seen.append(("load", fetched, base.snapshot()["state"]))

    base._bring_up("org/m", download, load)
    assert seen == [
        ("download", "org/m", "downloading"),
        # `load` is handed what `download` returned rather than resolving the
        # files again — the bug that re-ran the Hub metadata call on every load.
        ("load", "/tmp/snapshot", "loading"),
    ]
    assert base.snapshot()["state"] == "ready"
    assert base.snapshot()["loaded_at"] is not None


def test_a_failed_load_ends_in_error_never_loading_forever(base):
    """The bring-up thread is the only thing that can explain a failure, so its
    catch is deliberately broad: an unhandled exception there would leave
    /health saying "loading" until someone killed the process."""
    def load(model_id, fetched):
        raise RuntimeError("no metal for you")

    base._bring_up("org/m", lambda m: None, load)
    state = base.snapshot()
    assert state["state"] == "error"
    assert "no metal for you" in state["error"]


# -- /generate, both shapes -----------------------------------------------------


def test_a_json_generate_returns_one_object(base):
    """The image shape. An artefact is not a stream."""
    base.TOKEN = "secret"
    base.set_state(state="ready")
    server = _serve(base, lambda body: {"path": "/tmp/x.png", "seed": body["seed"]})
    try:
        with _call(server, "/generate", {"seed": 7}) as response:
            payload = json.loads(response.read())
        assert payload == {"ok": True, "result": {"path": "/tmp/x.png", "seed": 7}}
    finally:
        server.shutdown()


def test_a_streaming_generate_writes_ndjson(base):
    """The text shape — the frames `fused.ai`'s reader already speaks."""
    base.TOKEN = "secret"
    base.set_state(state="ready")

    def generate(body, write):
        write({"type": "chunk", "text": "he"})
        write({"type": "chunk", "text": "llo"})
        write({"type": "done", "ok": True, "tokens": 2})

    server = _serve(base, generate, streaming=True)
    try:
        with _call(server, "/generate", {"prompt": "hi"}) as response:
            frames = [json.loads(line) for line in response.read().splitlines() if line.strip()]
        assert [f.get("text") for f in frames if f["type"] == "chunk"] == ["he", "llo"]
        assert frames[-1] == {"type": "done", "ok": True, "tokens": 2}
    finally:
        server.shutdown()


def test_a_raising_generate_answers_instead_of_hanging(base):
    """A worker that died mid-request without answering would leave the
    supervisor blocked until its 15-minute timeout."""
    base.TOKEN = "secret"
    base.set_state(state="ready")

    def generate(body):
        raise ValueError("bad prompt")

    server = _serve(base, generate)
    try:
        with _call(server, "/generate", {}) as response:
            payload = json.loads(response.read())
        assert payload["ok"] is False
        assert "bad prompt" in payload["error"]
    finally:
        server.shutdown()


def test_a_cancelled_generate_says_so_rather_than_failing(base):
    base.TOKEN = "secret"
    base.set_state(state="ready")

    def generate(body):
        raise base.Cancelled()

    server = _serve(base, generate)
    try:
        with _call(server, "/generate", {}) as response:
            payload = json.loads(response.read())
        assert payload == {"ok": True, "cancelled": True}
    finally:
        server.shutdown()


def test_generate_clears_a_stale_cancel(base):
    """A cancel belongs to the request it interrupted. Left set, the NEXT
    generation would stop on its first step for a ✕ pressed minutes ago."""
    base.TOKEN = "secret"
    base.set_state(state="ready")
    base.CANCEL.set()
    seen = {}

    def generate(body):
        seen["cancelled_at_entry"] = base.CANCEL.is_set()
        return {}

    server = _serve(base, generate)
    try:
        _call(server, "/generate", {}).close()
        assert seen["cancelled_at_entry"] is False
    finally:
        server.shutdown()


def test_cancel_sets_the_flag_the_runners_read(base):
    base.TOKEN = "secret"
    server = _serve(base, lambda body: {})
    try:
        assert not base.CANCEL.is_set()
        _call(server, "/cancel", {}).close()
        assert base.CANCEL.is_set()
    finally:
        server.shutdown()


def test_one_generation_at_a_time(base):
    """A laptop has one GPU, and neither backend's model object is thread-safe.
    A second request waits rather than interleaving."""
    base.TOKEN = "secret"
    base.set_state(state="ready")
    overlapped = []
    inside = threading.Event()
    release = threading.Event()

    def generate(body):
        overlapped.append(inside.is_set())
        inside.set()
        release.wait(timeout=5)
        inside.clear()
        return {}

    server = _serve(base, generate)
    try:
        first = threading.Thread(target=lambda: _call(server, "/generate", {}).close())
        first.start()
        assert inside.wait(timeout=5)
        second = threading.Thread(target=lambda: _call(server, "/generate", {}).close())
        second.start()
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert overlapped == [False, False], "a second generation ran inside the first"
    finally:
        release.set()
        server.shutdown()


# -- reporting ------------------------------------------------------------------


def test_report_is_silent_when_there_is_nowhere_to_report(base):
    """Reporting is decoration. A worker started outside the app — no origin, no
    job — must still load its model rather than raising on every tick."""
    base.JOB_ID = ""
    base.JOB_URL = "/api/jobs"
    assert base.report(detail="x") is None


def test_report_or_cancel_raises_on_a_requested_cancel(base, monkeypatch):
    """The ✕ reaches a process inside an opaque C call ONLY through the reply to
    a tick it was sending anyway. That is why `report` returns the record."""
    monkeypatch.setattr(base, "report", lambda job=None, **f: {"cancel_requested": True})
    with pytest.raises(base.Cancelled):
        base.report_or_cancel(job="sys:ai-image:x", done=1, total=10)

    monkeypatch.setattr(base, "report", lambda job=None, **f: {"cancel_requested": False})
    assert base.report_or_cancel(job="sys:ai-image:x") == {"cancel_requested": False}


# -- download progress, measured from the disk (AI-5b) --------------------------


def test_bytes_on_disk_counts_incomplete_files_and_skips_symlinks(base, tmp_path):
    """`.incomplete` files ARE the progress, and a snapshot symlink must not be
    counted on top of the blob it points at — that would report a repo as twice
    its size, which is how a bar reaches 200%."""
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    (blobs / "abc").write_bytes(b"x" * 100)
    (blobs / "def.incomplete").write_bytes(b"y" * 50)
    snapshots = tmp_path / "snapshots" / "c1"
    snapshots.mkdir(parents=True)
    try:
        (snapshots / "model.safetensors").symlink_to(blobs / "abc")
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create symlinks unprivileged")
    assert base.bytes_on_disk(str(tmp_path)) == 150


def test_bytes_on_disk_is_none_when_the_folder_is_unknown(base):
    """A missing helper degrades to an indeterminate bar, never a wrong figure."""
    assert base.bytes_on_disk(None) is None


def test_fetch_with_progress_reports_throughout_and_returns_the_value(base, monkeypatch):
    """The one-second poll is the progress AND the heartbeat: without a tick
    during a long single-file download the manager calls the row abandoned."""
    ticks = []
    monkeypatch.setattr(base, "repo_folder", lambda model_id, repo_type="model": "/repo")
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: 512)
    monkeypatch.setattr(base, "report",
                        lambda job=None, **fields: ticks.append(fields) or None)

    result = base.fetch_with_progress("org/m", lambda: "/snap", total=1024)

    assert result == "/snap"
    assert ticks[0]["unit"] == "bytes" and ticks[0]["total"] == 1024
    # Lands ON the total: the snapshot symlinks are not counted, so a finished
    # repo measures slightly under its own size and a bar stopping at 98% reads
    # as a download that gave up.
    assert ticks[-1]["done"] == 1024


def test_fetch_with_progress_re_raises_on_the_calling_thread(base, monkeypatch):
    """The fetch runs on a thread; an exception swallowed there would look like
    a successful download of nothing."""
    monkeypatch.setattr(base, "repo_folder", lambda model_id, repo_type="model": None)
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    def boom():
        raise OSError("connection reset")

    with pytest.raises(OSError, match="connection reset"):
        base.fetch_with_progress("org/m", boom, total=None)


# -- the shipped runners actually use it ----------------------------------------


def test_every_registered_runner_ships_both_of_its_files():
    """A runner is a folder holding a declaration and a worker (AI-2), and
    `available()` reports a folder missing either as "not built yet" — which is
    a silent degradation if it ever happens by accident (a packaging exclusion,
    a half-finished backend). Pinned so it is a failing test instead."""
    from fused_render.ai import registry

    for runner in registry.all_runners():
        assert os.path.isfile(runner.worker), f"{runner.code}: no worker.py"
        assert os.path.isfile(runner.pyproject), f"{runner.code}: no pyproject.toml"


def test_no_runner_reimplements_the_contract():
    """The whole point of the extraction (AI-9a).

    A worker that grew its own HTTP server, its own auth check or its own
    reporter would put the SUPERVISOR's contract back in two places — the exact
    drift this module exists to prevent, and invisible until the two disagree.
    Checked as source, because the alternative is running mlx on Linux.
    """
    from fused_render.ai import registry

    for runner in registry.all_runners():
        source = open(runner.worker, encoding="utf-8").read()
        assert "import worker_base" in source, f"{runner.code} does not use the base"
        assert "worker_base.serve(" in source, f"{runner.code} does not serve through the base"
        for reimplemented in ("BaseHTTPRequestHandler", "X-Fused-Worker",
                              "socketserver", "argparse"):
            assert reimplemented not in source, (
                f"{runner.code} reimplements {reimplemented!r}, which belongs to "
                f"worker_base — see SPEC AI-9a"
            )
