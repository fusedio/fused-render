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
import sys
import threading
import time
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


def test_every_runner_that_needs_a_memory_probe_supplies_one():
    """The hook exists BECAUSE of MLX (AI-8a), so it has to be wired there.

    `serve(memory=)` was added after a card read "379 MB in memory" for a 6.1 GB
    model — MLX memory-maps its weights and its arrays are lazy, so RSS right
    after a load is measuring the interpreter. A `memory=` nobody passes leaves
    that number exactly as wrong as it was, with a branch in the base that looks
    like the fix. Source again, because mlx does not import on Linux.
    """
    from fused_render.ai import registry

    for code, why in (
        ("mlx-text",
         "memory-mapped weights and lazy arrays: RSS right after a load is the "
         "interpreter, which read as 379 MB for a 6.1 GB model"),
        ("diffusers-image",
         "the weights live in a GPU allocator's pool, which on MPS is not in "
         "the process's resident set: an 11.9B pipeline read as 33 MB"),
    ):
        runner = registry.by_code(code)
        assert runner is not None, code
        source = open(runner.worker, encoding="utf-8").read()
        assert "def memory(" in source, f"{code} has no memory probe — {why}"
        assert "memory=memory" in source, (
            f"{code} does not pass its probe to serve(), so /health reports RSS: {why}"
        )


# -- the total is scoped to what is actually fetched ----------------------------


class _Sibling:
    def __init__(self, rfilename, size):
        self.rfilename = rfilename
        self.size = size


class _Info:
    def __init__(self, siblings):
        self.siblings = siblings
        #: What the requested revision resolved to. The download pins itself to
        #: this rather than to the name it asked for, so the list and the fetch
        #: cannot describe different commits.
        self.sha = "c0mm1t"


def _hub(monkeypatch, base, siblings):
    """Stand in for one Hub metadata call."""
    class _Api:
        def model_info(self, model_id, revision=None, files_metadata=False):
            return _Info(siblings)

    import types
    monkeypatch.setitem(
        __import__("sys").modules, "huggingface_hub",
        types.SimpleNamespace(HfApi=_Api))


def test_a_single_file_total_is_that_file_not_the_repo(base, monkeypatch):
    """A GGUF repo publishes a dozen quantizations of one model. Measuring a
    2.6GB pull against all of them is how a download reads as barely started for
    its whole life and then jumps to complete."""
    _hub(monkeypatch, base, [
        _Sibling("flux-2-klein-4b-Q4_K_M.gguf", 2_600_000_000),
        _Sibling("flux-2-klein-4b-Q8_0.gguf", 4_800_000_000),
        _Sibling("flux-2-klein-4b-F16.gguf", 8_100_000_000),
    ])
    assert base.repo_total_bytes("u/x", include="flux-2-klein-4b-Q4_K_M.gguf") == 2_600_000_000
    # …and the unscoped answer is still the whole repo, for a runner that wants it.
    assert base.repo_total_bytes("u/x") == 15_500_000_000


def test_an_ignored_subfolder_is_left_out_of_the_total(base, monkeypatch):
    """A pull that deliberately skips the weights it is replacing must not
    measure itself against them — the bar would stall partway and then jump."""
    _hub(monkeypatch, base, [
        _Sibling("model_index.json", 500),
        _Sibling("transformer/config.json", 1_000),
        _Sibling("transformer/diffusion_pytorch_model.safetensors", 8_000_000_000),
        _Sibling("text_encoder/model.safetensors", 300_000_000),
    ])
    scoped = base.repo_total_bytes("org/m", ignore=["transformer/*.safetensors"])
    assert scoped == 500 + 1_000 + 300_000_000
    # The config is deliberately still counted: it is still downloaded, because
    # `from_single_file` needs it and a cache without it cannot load offline.


def test_progress_never_exceeds_a_scoped_total(base, monkeypatch):
    """`bytes_on_disk` measures the whole repo folder. A machine already holding
    another quantization of the same model would otherwise report 8GB of a
    2.6GB download — a bar past 100%."""
    ticks = []
    monkeypatch.setattr(base, "repo_folder", lambda model_id, repo_type="model": "/repo")
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: 8_000_000_000)
    monkeypatch.setattr(base, "report",
                        lambda job=None, **fields: ticks.append(fields) or None)

    base.fetch_with_progress("u/x", lambda: "/f", total=2_600_000_000)

    assert all(t.get("done", 0) <= 2_600_000_000 for t in ticks if "done" in t), ticks


def test_the_image_recipe_keeps_the_config_it_needs_to_load():
    """The recipe skips WEIGHT files, never the subfolder. `from_single_file`
    reads `transformer/config.json`, so ignoring `transformer/*` would leave a
    "downloaded" model that still needs the network — the one promise Download
    makes."""
    import importlib.util

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fused_render", "ai", "runners", "diffusers_image", "worker.py",
    )
    source = open(path, encoding="utf-8").read()
    # Read as source: importing it would pull in torch.
    assert '"transformer/*"' not in source, "the whole subfolder is ignored again"
    assert '"transformer/*.safetensors"' in source
    assert '"skip"' in source


# -- the heartbeat --------------------------------------------------------------


def test_a_slow_generation_keeps_its_row_alive(base, monkeypatch):
    """A row is called stalled after 30s of silence, and a denoiser is slower.

    The image runner reports once per denoising step. A FLUX step on a laptop
    routinely takes longer than the whole stale window, so a render that was
    progressing perfectly announced, at step 1 of 3, that nobody was reporting
    it. AI-5b already made this rule for downloads ("the poll doubles as the
    heartbeat"); this is it reaching the other reporter.
    """
    sent = []
    monkeypatch.setattr(base, "HEARTBEAT_S", 0.02)
    monkeypatch.setattr(base, "_send", lambda job, fields: sent.append((job, fields)))

    # One real tick, then a long silence — exactly the shape of a slow step.
    base._last_report.update(job="sys:ai-image:abc",
                             fields={"done": 1, "total": 3, "detail": "step 1/3"})
    with base.heartbeat():
        time.sleep(0.2)

    assert sent, "the row went un-touched through a slow generation"
    job, fields = sent[0]
    assert job == "sys:ai-image:abc"
    # The SAME payload: a tick that learned nothing must not move the bar.
    assert fields == {"done": 1, "total": 3, "detail": "step 1/3"}


def test_the_heartbeat_sends_without_remembering(base):
    """The beat must repeat a payload, never re-record it.

    A heartbeat that called `report` re-wrote `_last_report` with the payload it
    had just read, so a real tick landing between that read and that write was
    overwritten by the older one and every later beat repeated stale numbers —
    the bar going BACKWARDS while the model made progress, which is a worse lie
    than the stall the heartbeat exists to prevent.

    Asserted on the SOURCE rather than by driving it. The window is two adjacent
    statements wide, so a timing test would pass with the bug in place far more
    often than not — and a test that only sometimes fails is not a guard. What is
    checkable is the invariant itself: `report` remembers and sends, `_send` only
    sends, and the beat is a caller of the second.
    """
    import inspect

    body = inspect.getsource(base.heartbeat)
    assert "_send(" in body, "the heartbeat does not send through `_send`"
    assert "report(" not in body.replace("_send(", ""), (
        "the heartbeat calls `report`, which re-records what it repeats and "
        "clobbers any real tick that landed while it was working"
    )


def test_the_heartbeat_stops_with_the_work(base, monkeypatch):
    """It must not keep touching a row after the generation returns — that would
    be the same lie in the other direction."""
    sent = []
    monkeypatch.setattr(base, "HEARTBEAT_S", 0.02)
    monkeypatch.setattr(base, "_send", lambda job, fields: sent.append((job, fields)))
    base._last_report.update(job="j", fields={"done": 1})

    with base.heartbeat():
        time.sleep(0.1)
    settled = len(sent)
    time.sleep(0.1)

    assert len(sent) == settled, "the heartbeat outlived the work it was for"


def test_a_tick_in_flight_cannot_outlive_the_work(base, monkeypatch):
    """`stop.set()` cannot reach a beat already inside its POST.

    That tick lands after the work finished, and the FIRST payload of a
    generation carries `state: "running"` — so it would flip a row the
    supervisor had just marked done back to running. The context manager JOINS
    the thread rather than only signalling it.
    """
    monkeypatch.setattr(base, "HEARTBEAT_S", 0.02)
    landed = []

    def slow_send(job, fields):
        time.sleep(0.15)          # still posting when the work finishes
        landed.append(time.monotonic())

    monkeypatch.setattr(base, "_send", slow_send)
    base._last_report.update(job="j", fields={"state": "running", "done": 0})

    with base.heartbeat():
        time.sleep(0.05)          # long enough for one beat to start its POST
    left = time.monotonic()

    assert landed, "the test never exercised an in-flight tick"
    assert max(landed) <= left, "a tick landed after the work was over"


def test_a_finished_row_is_never_re_reported(base, monkeypatch):
    """A terminal state is the end of the row's life. Repeating it would revive
    a record the manager had already retired."""
    sent = []
    monkeypatch.setattr(base, "HEARTBEAT_S", 0.02)
    monkeypatch.setattr(base, "_send", lambda job, fields: sent.append((job, fields)))
    base._last_report.update(job="j", fields={"state": "done", "detail": "Saved"})

    with base.heartbeat():
        time.sleep(0.1)

    assert sent == [], "a finished row was kept alive by its own heartbeat"


# -- what a failure SAYS ---------------------------------------------------------
#
# The load path is the one place a user meets a library's own error text, and
# the libraries a runner loads all re-raise. Reporting the top frame is how a
# missing stdlib module reached the AI Models page as a sentence about a model.


def test_a_wrapped_failure_reports_the_ROOT_cause_not_the_wrapper(base):
    """transformers' lazy-module machinery wraps every import failure, so what
    arrived on the page was `Could not import module 'AutoTokenizer'` — beside
    the name of a Qwen repo that was downloaded correctly — while the exception
    it was raised `from` said `No module named 'filecmp'`. One of those is
    actionable."""
    def load(model_id, fetched):
        try:
            raise ModuleNotFoundError("No module named 'filecmp'", name="filecmp")
        except ModuleNotFoundError as cause:
            raise RuntimeError("Could not import module 'AutoTokenizer'") from cause

    base._bring_up("mlx-community/Qwen3-8B-4bit", lambda m: None, load)
    error = base.snapshot()["error"]

    assert "Could not import module 'AutoTokenizer'" in error, "the wrapper still shows"
    assert "No module named 'filecmp'" in error, "and so does the cause"


def test_a_missing_STDLIB_module_is_named_as_an_interpreter_problem(base):
    """The distinction that decides what the user does next: a third-party
    package is fixed by rebuilding the environment, a stdlib module is baked
    into the interpreter the environment was built FROM — so rebuilding
    reproduces it exactly, forever."""
    def load(model_id, fetched):
        raise ModuleNotFoundError("No module named 'filecmp'", name="filecmp")

    base._bring_up("org/m", lambda m: None, load)
    error = base.snapshot()["error"]

    assert "STANDARD LIBRARY" in error
    assert "rebuilding the environment" in error
    assert sys.base_prefix in error, "name the interpreter, so it can be reported"


def test_a_missing_THIRD_PARTY_module_gets_no_stdlib_hint(base):
    """The hint must not fire for the ordinary case, or it is noise on every
    genuinely missing dependency."""
    def load(model_id, fetched):
        raise ModuleNotFoundError("No module named 'mlx_lm'", name="mlx_lm")

    base._bring_up("org/m", lambda m: None, load)
    error = base.snapshot()["error"]

    assert "mlx_lm" in error
    assert "STANDARD LIBRARY" not in error


def test_an_unchained_failure_reads_exactly_as_before(base):
    """No `from`, no context, nothing to add — the message must not grow a
    dangling "caused by"."""
    assert base.describe_failure(RuntimeError("no metal for you")) == (
        "RuntimeError: no metal for you")


def test_a_cycle_in_the_exception_chain_terminates(base):
    """`__context__` can point back into a chain already walked (an except block
    that re-raises something it caught earlier). The walk is bounded by identity
    rather than by trust."""
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__context__ = second
    second.__context__ = first

    assert "first" in base.describe_failure(first)


def test_a_generation_failure_is_described_the_same_way(base):
    """The load path is not special: a generate that dies inside a library gets
    the same treatment, since the same wrapping happens there."""
    base.TOKEN = "secret"
    base.set_state(state="ready")

    def generate(body):
        try:
            raise ModuleNotFoundError("No module named 'filecmp'", name="filecmp")
        except ModuleNotFoundError as cause:
            raise RuntimeError("Could not import module 'AutoTokenizer'") from cause

    server = _serve(base, generate)
    try:
        with _call(server, "/generate", {}) as response:
            payload = json.loads(response.read())
    finally:
        server.shutdown()

    assert payload["ok"] is False
    assert "filecmp" in payload["error"] and "STANDARD LIBRARY" in payload["error"]


def test_a_suppressed_context_is_not_walked_past(base):
    """`raise … from None` is a library saying "what I caught is not the
    explanation", and the commonest thing hidden that way is an optional
    dependency probe: `except ImportError: raise … from None`. Following it
    anyway would report a deliberately hidden error as the root cause — and if
    the probe happened to be for a stdlib module, would accuse an interpreter
    that is perfectly complete."""
    def load(model_id, fetched):
        try:
            raise ModuleNotFoundError("No module named 'filecmp'", name="filecmp")
        except ModuleNotFoundError:
            raise RuntimeError("this backend is unavailable") from None

    base._bring_up("org/m", lambda m: None, load)
    error = base.snapshot()["error"]

    assert error == "RuntimeError: this backend is unavailable"
    assert "filecmp" not in error
    assert "STANDARD LIBRARY" not in error


def test_a_name_error_inside_a_present_stdlib_package_is_not_blamed_on_the_interpreter(base):
    """`from email import nope` raises a plain ImportError whose `.name` is
    `email` — a package that is present and working. Keying the hint on
    ImportError would accuse a complete interpreter of missing part of its
    stdlib and tell the user that rebuilding cannot help: the exact class of
    confidently-wrong cause this whole change exists to stop."""
    def load(model_id, fetched):
        from email import definitely_not_a_real_name  # noqa: F401

    base._bring_up("org/m", lambda m: None, load)
    error = base.snapshot()["error"]

    assert "definitely_not_a_real_name" in error, "the real error still reaches the user"
    assert "STANDARD LIBRARY" not in error
    assert "rebuilding the environment" not in error


def test_a_missing_stdlib_SUBMODULE_is_still_named(base):
    """A partially-shipped stdlib fails as `No module named 'email.mime'`, and
    `sys.stdlib_module_names` holds only top-level names — so the top level is
    what decides, while the full name is what gets reported."""
    def load(model_id, fetched):
        raise ModuleNotFoundError("No module named 'email.mime'", name="email.mime")

    base._bring_up("org/m", lambda m: None, load)
    error = base.snapshot()["error"]

    assert "email.mime" in error
    assert "STANDARD LIBRARY" in error
